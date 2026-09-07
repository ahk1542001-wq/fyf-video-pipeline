"""
FYF Video Pipeline - FastAPI Backend
Connects the Next.js frontend to the Gemini Writer/Producer Agent and Gemini-TTS Voice Generation.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from dotenv import load_dotenv

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.job_store import (
    create_job_dir,
    generate_job_id,
    initialize_job_status,
    is_valid_job_id,
    read_job_status,
    update_job_status,
    write_json_atomically,
)
from backend.budget_store import get_budget_status, release_reservation
from backend.lock_store import (
    GRANULAR_LOCK_SCOPES,
    create_script_lock,
    granular_lock_checker,
    locked_scopes,
    read_scope_locks,
    read_script_lock,
    set_scope_lock,
)
from backend.projects.commands import (
    ApprovalRequiredError,
    CommandValidationError,
    LockConflictError,
    StaleVersionError,
    apply_command,
    create_project_with_script,
    utc_now_iso,
)
from backend.projects.models import (
    PROJECT_ID_PATTERN,
    AssetReference,
    PinnedProductionConfig,
    ProjectCommand,
    ProjectVersion,
    RenderManifest,
    Selection,
    WorkflowEvent,
)
from backend.projects import chat
from backend.projects.store import (
    FileProjectStore,
    InvalidProjectIdError,
    ProjectNotFoundError,
    ProjectVersionNotFoundError,
    VersionAlreadyExistsError,
)
from backend.pipeline import run_pipeline
from backend.runtime_limits import (
    QUEUE_DEPTH_HEADER,
    QUEUE_POSITION_HEADER,
    REASON_CAPACITY_LIMIT,
    REJECTION_HEADER,
    acquire_guardrail_lease,
    enforce_generation_guardrails,
    register_active_job,
    release_active_job,
)
from backend.script_pipeline import run_script_pipeline, update_script_status
from backend.telemetry_store import get_all_telemetry_summary, get_job_telemetry
from backend.video_director import apply_director_pass
from backend.video_styles import apply_video_style, get_available_styles
from vertex_model_routing import model_for
from video_contract import ExactLockRequest, RenderControls, StoryModesResponse, VideoScript

# Stage B-III: durable idempotent queue (B7), cooperative cancellation (B9), and
# the single validated capacity-limit source (B11).
from backend import cancellation
from backend.capacity_config import load_capacity_config, validate_submission
from backend.job_queue import JobQueue, default_queue_root

REPO_ROOT = Path(__file__).resolve().parent.parent
JOBS_ROOT = REPO_ROOT / "output" / "jobs"
LOCKS_ROOT = REPO_ROOT / "output" / "locks"
SCRIPT_JOBS_ROOT = REPO_ROOT / "output" / "script-jobs"
# Versioned project spine root (Stage B-II). Honours FYF_PROJECTS_ROOT so tests
# can point it at a temp dir; the default is anchored under the gitignored
# output/ tree exactly like JOBS_ROOT.
PROJECTS_ROOT = REPO_ROOT / "output" / "projects"
# Durable idempotent job queue root (Stage B-III / B7). Honours FYF_QUEUE_ROOT so
# tests and deployments can relocate it; the default sits under the gitignored
# output/ tree exactly like JOBS_ROOT / PROJECTS_ROOT.
QUEUE_ROOT = REPO_ROOT / "output" / "queue"

app = FastAPI(title="FYF Video Pipeline API", version="0.1.0")

# Allow the Next.js frontend (dev server) to call this API
LOCAL_VIDEO_FRONTEND_ORIGINS = [
    "http://localhost:3001",
    "http://127.0.0.1:3001",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=LOCAL_VIDEO_FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ScriptRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=6000)
    duration_mode: Literal["short", "micro", "standard"] = "short"
    style: str | None = "fyf_explainer"
    use_adk_agent: bool = True
    studio_name: str = "FYF Studio"
    language: str = "my-MM"
    genre: str = "explainer"
    presenter_mode: str = "on_screen"
    voice_actor: str = "Sadaltager"

    @field_validator("topic")
    @classmethod
    def validate_topic(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("Topic cannot be empty or whitespace only")
        return trimmed


class ScriptResponse(BaseModel):
    success: bool
    data: dict | None = None
    error: str | None = None
    lock_id: str | None = None


class ScriptJobResponse(BaseModel):
    success: bool
    job_id: str
    status_url: str
    restart_resumable: bool = True


class StoryLockResponse(ScriptResponse):
    lock_id: str | None = None


class StoryPolishRequest(BaseModel):
    topic_or_draft: str = Field(min_length=1, max_length=6000)
    studio_name: str = "FYF Studio"
    language: str = "my-MM"
    genre: str = "explainer"
    presenter_mode: str = "on_screen"
    voice_actor: str = "Sadaltager"

    @field_validator("topic_or_draft")
    @classmethod
    def validate_topic_or_draft(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("Draft input cannot be empty or whitespace only")
        return trimmed


class StoryPolishResponse(BaseModel):
    success: bool
    variants: list[dict] | None = None
    model_used: str | None = None


class VideoRequest(RenderControls):
    model_config = ConfigDict(extra="ignore")

    lock_id: str = Field(pattern=r"^[0-9a-f]{8}$")
    voice_provider: Literal["gemini"] = "gemini"
    style: str | None = "fyf_explainer"
    studio_name: str = "FYF Studio"
    language: str = "my-MM"
    genre: str = "explainer"
    presenter_mode: str = "on_screen"
    voice_actor: str = "Sadaltager"

    @field_validator("style")
    @classmethod
    def validate_style(cls, v: str | None) -> str:
        if v is None or v == "":
            return "fyf_explainer"
        valid_ids = {s["id"] for s in get_available_styles()} | {
            "cinematic_documentary", "tech_explainer", "investigative", "narrative", "explainer"
        }
        if v not in valid_ids:
            raise ValueError(f"Unknown video style '{v}'. Valid styles: {sorted(valid_ids)}")
        return v


class VideoResponse(BaseModel):
    success: bool
    job_id: str | None = None
    status_url: str | None = None
    restart_resumable: bool = True
    error: str | None = None


class VideoJobItem(BaseModel):
    voice_provider: Literal["gemini"] = "gemini"
    job_id: str
    status_url: str


class RuntimeResponse(BaseModel):
    runtime_mode: Literal["hackathon", "product"]
    allowed_voice_providers: list[Literal["gemini"]]
    script_model: str
    fallback_model: str
    generation_available: bool
    generation_access_required: bool
    generation_status: Literal[
        "ready", "credential_required", "disabled", "access_token_required",
        "private_access_required",
    ]
    generation_message: str
    # B11: the complete, typed deployment capacity limits (single source:
    # backend.capacity_config). Task 13 renders this panel; the field names and
    # types returned here are that contract.
    limits: dict[str, Any]


class RecentApprovedVideo(BaseModel):
    job_id: str = Field(pattern=r"^[0-9a-f]{8}$")
    title: str = Field(min_length=1)
    voice_provider: Literal["gemini"]
    updated_at: str = Field(min_length=1)
    video_url: str


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _is_public_deployment() -> bool:
    """Require an explicit host flag before applying public-generation restrictions."""
    return _truthy_env("FYF_PUBLIC_DEPLOYMENT")


def _vertex_credentials_configured() -> bool:
    """Check configuration shape only; never expose or validate credential values."""
    try:
        load_dotenv(REPO_ROOT / ".env", override=False)
    except OSError:
        pass

    if os.getenv("FYF_VERTEX_API_KEY") or os.getenv("GOOGLE_API_KEY"):
        return True

    # Cloud Run / GCE: Application Default Credentials from the attached
    # service account (metadata server) are valid Vertex credentials.
    if _truthy_env("GOOGLE_GENAI_USE_VERTEXAI") and os.getenv("GOOGLE_CLOUD_PROJECT", "").strip():
        try:
            import urllib.request

            req = urllib.request.Request(
                "http://metadata.google.internal/computeMetadata/v1/project/project-id",
                headers={"Metadata-Flavor": "Google"},
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                return resp.status == 200
        except OSError:
            return False

    configured_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if configured_path and Path(configured_path).is_file():
        return True
    return (REPO_ROOT / "gcp-key.json").is_file()


def _generation_runtime_state() -> dict[str, bool | str]:
    """Return safe public runtime state without leaking any provider configuration."""
    if not _is_public_deployment():
        return {
            "generation_available": True,
            "generation_access_required": False,
            "generation_status": "ready",
            "generation_message": "Local generation controls are available.",
        }

    if not _vertex_credentials_configured():
        return {
            "generation_available": False,
            "generation_access_required": False,
            "generation_status": "credential_required",
            "generation_message": "Generation is unavailable until the operator configures Vertex in the host secret store.",
        }

    if not _truthy_env("FYF_PUBLIC_GENERATION_ENABLED"):
        return {
            "generation_available": False,
            "generation_access_required": False,
            "generation_status": "disabled",
            "generation_message": "Public generation is intentionally disabled by the operator.",
        }

    if os.getenv("FYF_GENERATION_ACCESS_TOKEN"):
        return {
            "generation_available": True,
            "generation_access_required": True,
            "generation_status": "private_access_required",
            "generation_message": "Private generation access is required before a provider request can be queued.",
        }

    # No access token configured: open demonstration mode protected by the
    # runtime budget caps and concurrency guard.
    return {
        "generation_available": True,
        "generation_access_required": False,
        "generation_status": "ready",
        "generation_message": "Public demonstration generation is available.",
    }


def _enforce_public_access_token(request: Request) -> None:
    """Require the operator token on public sensitive endpoints when configured."""
    if not _is_public_deployment():
        return

    expected_token = os.getenv("FYF_GENERATION_ACCESS_TOKEN", "")
    if not expected_token:
        # Preserve the documented open demonstration mode when no token is set.
        return

    submitted_token = request.headers.get("x-fyf-access-token", "")
    if not submitted_token or not hmac.compare_digest(submitted_token, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Private generation access is required.",
        )


def _enforce_public_generation_access(request: Request) -> None:
    """Fail closed before quota reservation or provider work on the public deployment."""
    if not _is_public_deployment():
        return

    _enforce_public_access_token(request)

    runtime = _generation_runtime_state()
    if not runtime["generation_available"]:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=runtime["generation_message"])


def _should_resume_script_job(data: dict) -> bool:
    """Resume only jobs interrupted before a terminal failure was recorded."""
    status_val = data.get("status")
    return status_val in {"queued", "writing"}


def _create_video_job(
    script_data: dict,
    voice_provider: Literal["gemini"] = "gemini",
    job_id: str | None = None,
) -> VideoJobItem:
    if job_id is None:
        job_id = create_job_dir(JOBS_ROOT)
    else:
        (JOBS_ROOT / job_id).mkdir(parents=True, exist_ok=True)
    output_dir = JOBS_ROOT / job_id
    initialize_job_status(output_dir, job_id, voice_provider)
    write_json_atomically(output_dir / "script.json", script_data)
    controls = script_data.get("render_controls")
    if isinstance(controls, dict):
        update_job_status(output_dir, {"render_controls": controls})
    return VideoJobItem(
        voice_provider=voice_provider,
        job_id=job_id,
        status_url=f"/api/jobs/{job_id}/status",
    )


async def _run_video_pipeline_tracked(
    job_id: str,
    script_data: dict,
    voice_provider: Literal["gemini"],
    jobs_root: Path,
):
    job_dir = Path(jobs_root) / job_id
    register_active_job(job_id)
    try:
        # B9: queued work halts BEFORE any provider dispatch when a cancel landed
        # while the job was still queued. Cheap no-op when nothing is cancelled.
        cancellation.checkpoint(job_id, job_dir=job_dir, boundary="pre_dispatch")
        await run_pipeline(job_id, script_data, voice_provider, jobs_root)
    except cancellation.JobCancelledError as exc:
        # In-flight work stopped at a safe boundary. Mark terminal ``cancelled``;
        # late costs already incurred are still reconciled by the ledger because
        # reconcile_budget always debits actual_usd > 0 (including cancelled).
        cancellation.mark_cancelled(job_id, job_dir=job_dir, boundary=exc.boundary)
        try:
            update_job_status(job_dir, {
                "status": "cancelled",
                "error": None,
                "cancellation": cancellation.cancellation_status(job_id, job_dir=job_dir).to_dict(),
            })
        except Exception:
            logger.exception("Could not persist cancelled status for job %s", job_id)
    finally:
        release_active_job(job_id)
        from backend.budget_store import release_reservation
        release_reservation(job_id)


def _queue_video_job(
    background_tasks: BackgroundTasks,
    script_data: dict,
    voice_provider: Literal["gemini"] = "gemini",
    request: Request | None = None,
) -> VideoJobItem:
    job = _create_video_job(script_data, voice_provider)
    client_key = _client_idempotency_key(request) if request is not None else None
    queue, message = _submit_to_queue(
        kind="video",
        target="_run_video_pipeline_tracked",
        args=[job.job_id, script_data, voice_provider, JOBS_ROOT],
        job_id=job.job_id,
        client_key=client_key,
    )
    if not message.is_duplicate:
        background_tasks.add_task(queue.dispatch, message.message_id)
    return job


def _with_render_controls(script_data: dict[str, Any], controls: RenderControls) -> dict[str, Any]:
    """Attach one immutable control snapshot to the job-local script."""
    control_fields = set(RenderControls.model_fields.keys())
    dumped = controls.model_dump(mode="json")
    snapshot = {k: v for k, v in dumped.items() if k in control_fields}
    result = dict(script_data)
    # Keep the explicit nested snapshot and the flat Remotion props in sync so
    # old readers can continue to consume script.json while new readers can
    # validate a single render contract.
    result["render_controls"] = snapshot
    result.update(snapshot)
    return result


# ---------------------------------------------------------------------------
# Stage B-III helpers: durable queue access (B7), idempotency (B7), and
# server-side capacity validation on submit (B11).
# ---------------------------------------------------------------------------
def _job_queue() -> JobQueue:
    """Durable queue rooted at ``FYF_QUEUE_ROOT`` or ``output/queue`` (B7)."""
    return JobQueue(default_queue_root())


def _client_idempotency_key(request: Request) -> str | None:
    """Return the client-supplied idempotency key, or None when not provided.

    Deduplication is OPT-IN via this header. Without it every submission mints a
    unique queue key (uuid-suffixed) so behaviour is identical to the previous
    in-process ``BackgroundTasks`` enqueue: one job, one dispatch, no dedup.
    """
    supplied = request.headers.get("x-fyf-idempotency-key")
    if supplied and supplied.strip():
        return supplied.strip()
    return None


def _submit_to_queue(
    *,
    kind: str,
    target: str,
    args: list,
    job_id: str,
    client_key: str | None,
    attempt_suffix: str = "",
) -> "object":
    """Durably enqueue work, deduplicating only on a client-supplied key (B7).

    * Client key present  -> stable key ``kind:key`` so a replayed submission
      returns the EXISTING message/job (``is_duplicate`` True), never a second job
      or a second budget reservation.
    * No client key       -> unique key ``kind:job_id:suffix:uuid`` so the message
      always dispatches exactly once (behaviour-compatible with BackgroundTasks).
    """
    queue = _job_queue()
    if client_key:
        idempotency_key = f"{kind}:{client_key}"
    else:
        idempotency_key = f"{kind}:{job_id}:{attempt_suffix}:{uuid.uuid4().hex}".replace("::", ":")
    message = queue.submit(
        idempotency_key=idempotency_key,
        target=target,
        args=args,
        job_id=job_id,
    )
    return queue, message


def _capacity_rejection(violations: list) -> HTTPException:
    """422 naming the SPECIFIC capacity limit a submission exceeded (B11)."""
    first = violations[0]
    return HTTPException(
        status_code=422,
        detail={
            "error": "capacity_limit_exceeded",
            "message": first.message(),
            "limits": [v.to_dict() for v in violations],
        },
        headers={REJECTION_HEADER: REASON_CAPACITY_LIMIT},
    )


def _validate_submit_capacity(payload_bytes: int) -> None:
    """Validate a submission against deployment capacity limits BEFORE reserving.

    ``upload_bytes`` (the serialized request payload) is always available and is
    the honest server-side dimension checked at submit; duration limits are
    enforced through the same ``validate_submission`` contract when declared.
    """
    violations = validate_submission(upload_bytes=payload_bytes)
    if violations:
        raise _capacity_rejection(violations)


@app.get("/health")
def health():
    return {"status": "ok", "service": "fyf-video-pipeline"}


@app.get("/api/health")
def api_health():
    """Same-origin alias of /health reachable through the frontend /api proxy."""
    return {"status": "ok", "service": "fyf-video-pipeline"}


@app.get("/api/runtime", response_model=RuntimeResponse)
def get_runtime():
    generation_state = _generation_runtime_state()
    return RuntimeResponse(
        runtime_mode="hackathon",
        allowed_voice_providers=["gemini"],
        script_model=model_for("script"),
        fallback_model=model_for("story_fallback"),
        limits=load_capacity_config().to_runtime_dict(),
        **generation_state,
    )


@app.get("/api/video-styles")
def list_video_styles():
    from backend.video_styles import get_available_genres
    return {"styles": get_available_styles() + get_available_genres()}



@app.post("/api/generate-script", status_code=status.HTTP_202_ACCEPTED, response_model=ScriptJobResponse)
async def generate_script(req: ScriptRequest, request: Request, background_tasks: BackgroundTasks):
    """Queue persisted Vertex script production and return immediately."""
    _enforce_public_generation_access(request)
    # B11: validate the submission against deployment capacity limits first.
    _validate_submit_capacity(len(req.model_dump_json()))
    # B7: a client-supplied idempotency key replays to the EXISTING job (no
    # second job, no second budget reservation). Without it, behaviour is
    # identical to the previous in-process enqueue.
    client_key = _client_idempotency_key(request)
    queue = _job_queue()
    if client_key:
        existing = queue.resolve(f"script:{client_key}")
        if existing is not None and existing.job_id:
            return ScriptJobResponse(
                success=True, job_id=existing.job_id,
                status_url=f"/api/script-jobs/{existing.job_id}/status",
            )
    job_id = uuid.uuid4().hex[:8]
    lease = acquire_guardrail_lease(
        operation_id=job_id,
        request=request,
        estimated_charge_usd=0.04,
        job_roots=(JOBS_ROOT, SCRIPT_JOBS_ROOT),
    )
    job_dir = SCRIPT_JOBS_ROOT / job_id
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomically(job_dir / "request.json", req.model_dump(mode="json"))
        write_json_atomically(job_dir / "status.json", {
            "job_id": job_id, "status": "queued", "stage": "queued", "progress": 0,
            "batch": None, "batch_count": None, "lock_id": None, "error": None,
            "retry_count": 0, "restart_resumable": True,
        })
        submit_queue, message = _submit_to_queue(
            kind="script",
            target="run_script_pipeline",
            args=[job_id, SCRIPT_JOBS_ROOT, LOCKS_ROOT],
            job_id=job_id,
            client_key=client_key,
        )
        if message.is_duplicate:
            lease.release()
            shutil.rmtree(job_dir, ignore_errors=True)
            existing_id = message.job_id or job_id
            return ScriptJobResponse(
                success=True, job_id=existing_id,
                status_url=f"/api/script-jobs/{existing_id}/status",
            )
        background_tasks.add_task(submit_queue.dispatch, message.message_id)
    except Exception:
        lease.release()
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
        logger.exception("Script generation initialization error:")
        raise HTTPException(status_code=500, detail="Failed to initialize script job")

    return ScriptJobResponse(
        success=True, job_id=job_id,
        status_url=f"/api/script-jobs/{job_id}/status",
    )


@app.get("/api/script-jobs/{job_id}/status")
def script_job_status(job_id: str):
    if not is_valid_job_id(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID format")
    job_dir = SCRIPT_JOBS_ROOT / job_id
    status_path = job_dir / "status.json"
    if not status_path.exists():
        raise HTTPException(status_code=404, detail="Script job not found")
    data = json.loads(status_path.read_text(encoding="utf-8"))
    if data.get("status") == "completed":
        result_path = job_dir / "result.json"
        if not result_path.exists():
            raise HTTPException(status_code=500, detail="Completed script result missing")
        data["data"] = json.loads(result_path.read_text(encoding="utf-8"))
    return data


@app.post("/api/script-jobs/{job_id}/resume", status_code=status.HTTP_202_ACCEPTED, response_model=ScriptJobResponse)
async def resume_script_job(job_id: str, request: Request, background_tasks: BackgroundTasks):
    """Resume a script job that is in needs_attention state."""
    _enforce_public_generation_access(request)
    if not is_valid_job_id(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID format")

    job_dir = SCRIPT_JOBS_ROOT / job_id
    if not job_dir.is_dir():
        raise HTTPException(status_code=404, detail="Script job not found")

    status_path = job_dir / "status.json"
    if not status_path.exists():
        raise HTTPException(status_code=404, detail="Script job status missing")

    try:
        status_data = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Script job status unreadable")

    if status_data.get("status") != "needs_attention" or not status_data.get("restart_resumable"):
        raise HTTPException(
            status_code=400,
            detail=f"Job with status '{status_data.get('status')}' is not in a resumable needs_attention state",
        )

    resume_count = int(status_data.get("resume_count", 0)) + 1
    if resume_count > 3:
        raise HTTPException(status_code=400, detail="Maximum script resume attempts (3) exceeded")

    lease = acquire_guardrail_lease(
        operation_id=job_id,
        request=request,
        estimated_charge_usd=0.04,
        job_roots=(JOBS_ROOT, SCRIPT_JOBS_ROOT),
    )

    try:
        update_script_status(
            job_dir,
            status="queued",
            stage="queued",
            error=None,
            resume_count=resume_count,
            retry_count=0,
        )
        submit_queue, message = _submit_to_queue(
            kind="script-resume",
            target="run_script_pipeline",
            args=[job_id, SCRIPT_JOBS_ROOT, LOCKS_ROOT],
            job_id=job_id,
            client_key=_client_idempotency_key(request),
            attempt_suffix=str(resume_count),
        )
        if not message.is_duplicate:
            background_tasks.add_task(submit_queue.dispatch, message.message_id)
    except Exception:
        lease.release()
        try:
            update_script_status(
                job_dir,
                status="needs_attention",
                restart_resumable=True,
                error="Failed to queue resume task",
            )
        except Exception:
            pass
        logger.exception("Failed to resume script job %s:", job_id)
        raise HTTPException(status_code=500, detail="Failed to queue script resume")

    return ScriptJobResponse(
        success=True,
        job_id=job_id,
        status_url=f"/api/script-jobs/{job_id}/status",
        restart_resumable=True,
    )


@app.on_event("startup")
async def resume_interrupted_script_jobs():
    """Resume persisted script and video jobs after a backend restart."""
    if _is_public_deployment():
        _quarantine_interrupted_public_jobs()
        logger.info(
            "Public deployment startup quarantined interrupted jobs instead of auto-resuming paid generation."
        )
        return

    if SCRIPT_JOBS_ROOT.exists():
        for job_dir in SCRIPT_JOBS_ROOT.iterdir():
            status_path = job_dir / "status.json"
            if not job_dir.is_dir() or not is_valid_job_id(job_dir.name) or not status_path.exists():
                continue
            try:
                data = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if _should_resume_script_job(data):
                lease = None
                try:
                    lease = acquire_guardrail_lease(
                        operation_id=job_dir.name,
                        client_ip="127.0.0.1",
                        estimated_charge_usd=0.04,
                        job_roots=(JOBS_ROOT, SCRIPT_JOBS_ROOT),
                    )
                    asyncio.create_task(asyncio.to_thread(
                        run_script_pipeline, job_dir.name, SCRIPT_JOBS_ROOT, LOCKS_ROOT
                    ))
                except Exception:
                    if lease is not None:
                        lease.release()
                    try:
                        update_script_status(
                            job_dir,
                            status="needs_attention",
                            restart_resumable=True,
                            error="Automatic restart delayed by guardrails or queue initialization. Manual resume available.",
                        )
                    except Exception:
                        logger.exception("Could not mark script job %s as needs_attention", job_dir.name)

    if not JOBS_ROOT.exists():
        return

    def needs_resume(data: dict) -> bool:
        return data.get("status") in {
            "queued", "visuals", "voice", "rendering", "qa", "creative_qa"
        } or (
            data.get("status") == "failed"
            and bool(data.get("restart_resumable"))
        )

    for job_dir in JOBS_ROOT.iterdir():
        status_path = job_dir / "status.json"
        script_path = job_dir / "script.json"
        if (
            not job_dir.is_dir()
            or not is_valid_job_id(job_dir.name)
            or not status_path.exists()
            or not script_path.exists()
        ):
            continue
        try:
            data = json.loads(status_path.read_text(encoding="utf-8"))
            script_data = json.loads(script_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        provider = data.get("voice_provider") or "gemini"
        resume_count = int(data.get("resume_count", 0))
        resumable = needs_resume(data)

        if resumable and resume_count >= 3:
            update_job_status(job_dir, {
                "status": "failed",
                "error": "Automatic resume limit reached; manual retry is required.",
                "restart_resumable": False,
            })
            continue

        if resumable and provider == "gemini":
            lease = None
            try:
                lease = acquire_guardrail_lease(
                    operation_id=job_dir.name,
                    client_ip="127.0.0.1",
                    estimated_charge_usd=0.06,
                    job_roots=(JOBS_ROOT, SCRIPT_JOBS_ROOT),
                )
                update_job_status(job_dir, {
                    "status": "queued",
                    "error": None,
                    "resume_count": resume_count + 1,
                    "restart_resumable": True,
                })
                asyncio.create_task(_run_video_pipeline_tracked(
                    job_dir.name, script_data, "gemini", JOBS_ROOT
                ))
            except Exception:
                if lease is not None:
                    lease.release()
                try:
                    update_job_status(job_dir, {
                        "status": "needs_attention",
                        "error": "Automatic restart delayed by guardrails or queue initialization. Manual resume available.",
                        "restart_resumable": True,
                    })
                except Exception:
                    logger.exception("Could not mark video job %s as needs_attention", job_dir.name)


def _quarantine_interrupted_public_jobs() -> None:
    """Free stale public concurrency slots without issuing provider requests.

    Replit preserves job files across deployments. Public startup intentionally
    does not auto-resume paid work, so persisted active statuses must become
    manually resumable or they block every new request forever.
    """
    active_by_root = (
        (SCRIPT_JOBS_ROOT, {"queued", "writing", "adk_producer", "retrying"}, True),
        (JOBS_ROOT, {"queued", "visuals", "voice", "rendering", "qa", "creative_qa"}, False),
    )
    for root, active_statuses, is_script_job in active_by_root:
        if not root.is_dir():
            continue
        for job_dir in root.iterdir():
            status_path = job_dir / "status.json"
            if (
                job_dir.is_symlink()
                or status_path.is_symlink()
                or not job_dir.is_dir()
                or not is_valid_job_id(job_dir.name)
                or not status_path.is_file()
            ):
                continue
            try:
                data = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            if data.get("status") not in active_statuses:
                continue
            try:
                updates = {
                    "status": "needs_attention",
                    "restart_resumable": True,
                    "error": "Public deployment restart paused this job before any automatic paid retry. Manual resume is available.",
                }
                release_reservation(job_dir.name)
                if is_script_job:
                    update_script_status(job_dir, **updates)
                else:
                    update_job_status(job_dir, updates)
            except Exception:
                logger.exception("Could not quarantine interrupted public job %s", job_dir.name)


@app.post("/api/story-polish", response_model=StoryPolishResponse)
async def story_polish(req: StoryPolishRequest, request: Request):
    """Vertex-only FYF story variants; never queues a video."""
    _enforce_public_generation_access(request)
    op_id = generate_job_id()
    lease = acquire_guardrail_lease(
        operation_id=op_id,
        request=request,
        estimated_charge_usd=0.02,
        job_roots=(JOBS_ROOT, SCRIPT_JOBS_ROOT),
    )
    try:
        from backend.vertex_telemetry import telemetry_scope
        with telemetry_scope(op_id, "story_polish", SCRIPT_JOBS_ROOT / op_id) as collector:
            from writer_agent_vertex import generate_story_modes
            generated = generate_story_modes(
                req.topic_or_draft,
                language=req.language,
                genre=req.genre,
                presenter_mode=req.presenter_mode,
                studio_name=req.studio_name,
                voice_actor=req.voice_actor,
            )
            result = StoryModesResponse.model_validate({"variants": generated["variants"]})
            summary = collector.summary()
            actual_cost = float(summary.get("estimated_cost_usd") or 0.0) if summary.get("cost_status") in ("exact", "partial") else 0.0
            lease.reconcile(actual_usd=actual_cost, outcome="completed")
            return StoryPolishResponse(
                success=True,
                variants=[v.model_dump(mode="json") for v in result.variants],
                model_used=generated.get("model_used"),
            )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Story polish failed")
        raise HTTPException(status_code=500, detail="Story polish failed")
    finally:
        lease.release()


@app.post("/api/story-lock", response_model=StoryLockResponse)
async def story_lock(req: ExactLockRequest, request: Request):
    """Use Vertex only for visuals; server verifies approved narration byte-for-byte."""
    _enforce_public_generation_access(request)
    op_id = generate_job_id()
    lease = acquire_guardrail_lease(
        operation_id=op_id,
        request=request,
        estimated_charge_usd=0.03,
        job_roots=(JOBS_ROOT, SCRIPT_JOBS_ROOT),
    )
    try:
        from backend.vertex_telemetry import telemetry_scope
        with telemetry_scope(op_id, "story_lock", SCRIPT_JOBS_ROOT / op_id) as collector:
            from writer_agent_vertex import generate_exact_lock
            data = VideoScript.model_validate(generate_exact_lock(req.model_dump(mode="json")))
            directed = apply_director_pass(data.model_dump(mode="json"))
            data = VideoScript.model_validate(directed)
            lock_id = create_script_lock(LOCKS_ROOT, directed)
            summary = collector.summary()
            actual_cost = float(summary.get("estimated_cost_usd") or 0.0) if summary.get("cost_status") in ("exact", "partial") else 0.0
            lease.reconcile(actual_usd=actual_cost, outcome="completed")
            return StoryLockResponse(success=True, data=data.model_dump(mode="json"), lock_id=lock_id)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Story lock failed")
        raise HTTPException(status_code=500, detail="Story lock failed")
    finally:
        lease.release()


@app.post("/api/generate-video", status_code=status.HTTP_202_ACCEPTED, response_model=VideoResponse)
async def generate_video(req: VideoRequest, request: Request, background_tasks: BackgroundTasks):
    """Takes the approved script lock, acquires guardrail lease, creates job, and queues pipeline."""
    _enforce_public_generation_access(request)
    try:
        script_data = read_script_lock(LOCKS_ROOT, req.lock_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=422, detail="Approved script lock not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    styled_script = apply_video_style(script_data, req.style)
    if isinstance(styled_script, dict):
        if req.studio_name:
            styled_script["studio_name"] = req.studio_name
        if req.language:
            styled_script["language"] = req.language
        if req.genre:
            styled_script["genre"] = req.genre
        if req.presenter_mode:
            styled_script["presenter_mode"] = req.presenter_mode
        if req.voice_actor:
            styled_script["voice_actor"] = req.voice_actor
        if req.presenter_mode == "voiceover_only":
            for seg in styled_script.get("segments", []):
                vis = seg.get("visual") or {}
                for shot in vis.get("evidence_shots", []):
                    shot["mascot_presence"] = "none"
        styled_script = _with_render_controls(styled_script, req)

    # B11: validate the queued payload against deployment capacity limits.
    _validate_submit_capacity(len(json.dumps(styled_script, default=str)))
    # B7: client-supplied idempotency key replays to the EXISTING video job.
    client_key = _client_idempotency_key(request)
    queue = _job_queue()
    if client_key:
        existing = queue.resolve(f"video:{client_key}")
        if existing is not None and existing.job_id:
            return VideoResponse(
                success=True,
                job_id=existing.job_id,
                status_url=f"/api/jobs/{existing.job_id}/status",
                restart_resumable=True,
            )

    job_id = uuid.uuid4().hex[:8]

    # Guardrail check happens BEFORE any disk creation!
    lease = acquire_guardrail_lease(
        operation_id=job_id,
        request=request,
        estimated_charge_usd=0.06,
        job_roots=(JOBS_ROOT, SCRIPT_JOBS_ROOT),
    )

    job_dir = JOBS_ROOT / job_id
    try:
        job = _create_video_job(styled_script, req.voice_provider, job_id=job_id)
        submit_queue, message = _submit_to_queue(
            kind="video",
            target="_run_video_pipeline_tracked",
            args=[job.job_id, styled_script, req.voice_provider, JOBS_ROOT],
            job_id=job.job_id,
            client_key=client_key,
        )
        if message.is_duplicate:
            lease.release()
            shutil.rmtree(job_dir, ignore_errors=True)
            existing_id = message.job_id or job.job_id
            return VideoResponse(
                success=True,
                job_id=existing_id,
                status_url=f"/api/jobs/{existing_id}/status",
                restart_resumable=True,
            )
        background_tasks.add_task(submit_queue.dispatch, message.message_id)
        return VideoResponse(
            success=True,
            job_id=job.job_id,
            status_url=job.status_url,
            restart_resumable=True
        )
    except Exception:
        lease.release()
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
        logger.exception("Video generation queueing error:")
        raise HTTPException(status_code=500, detail="Failed to initialize video job")


def _safe_job_path(path: Path) -> Path | None:
    try:
        resolved_root = JOBS_ROOT.resolve()
        resolved_path = path.resolve()
        resolved_path.relative_to(resolved_root)
    except (OSError, ValueError):
        return None
    return resolved_path


@app.get("/api/jobs/recent", response_model=list[RecentApprovedVideo])
def get_recent_approved_videos():
    if not JOBS_ROOT.is_dir():
        return []

    recent: list[tuple[float, RecentApprovedVideo]] = []
    try:
        job_entries = list(JOBS_ROOT.iterdir())
    except OSError:
        return []

    for job_entry in job_entries:
        if not job_entry.is_dir() or not is_valid_job_id(job_entry.name):
            continue
        job_dir = _safe_job_path(job_entry)
        if job_dir is None:
            continue

        try:
            status_data = read_job_status(job_dir)
            qa_report = status_data.get("qa_report")
            final_visual_qa = status_data.get("final_visual_qa")
            if (
                status_data.get("status") != "completed"
                or status_data.get("archived") is True
                or not isinstance(qa_report, dict)
                or qa_report.get("passed") is not True
                or not isinstance(final_visual_qa, dict)
                or final_visual_qa.get("passed") is not True
            ):
                continue

            provider = status_data.get("voice_provider") or "gemini"
            updated_at = status_data.get("updated_at")
            if provider != "gemini" or not isinstance(updated_at, str) or not updated_at.strip():
                continue
            try:
                sort_timestamp = datetime.fromisoformat(
                    updated_at.replace("Z", "+00:00")
                ).timestamp()
            except (ValueError, OverflowError):
                continue

            script_path = _safe_job_path(job_dir / "script.json")
            video_path = _safe_job_path(job_dir / "video.mp4")
            if script_path is None or video_path is None:
                continue
            if not script_path.is_file() or not video_path.is_file() or video_path.stat().st_size <= 0:
                continue

            script_data = json.loads(script_path.read_text(encoding="utf-8"))
            title = script_data.get("title") if isinstance(script_data, dict) else None
            if not isinstance(title, str) or not title.strip():
                continue

            item = RecentApprovedVideo(
                job_id=job_entry.name,
                title=title.strip(),
                voice_provider="gemini",
                updated_at=updated_at,
                video_url=f"/api/jobs/{job_entry.name}/video",
            )
            recent.append((sort_timestamp, item))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue

    recent.sort(key=lambda item: item[0], reverse=True)
    return [item for _, item in recent[:6]]


@app.get("/api/jobs/{job_id}/status")
def get_job_status(job_id: str):
    if not is_valid_job_id(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID")

    job_dir = JOBS_ROOT / job_id

    try:
        resolved_job_dir = job_dir.resolve()
        resolved_job_dir.relative_to(JOBS_ROOT.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Forbidden path")

    try:
        status_data = read_job_status(resolved_job_dir)
        return status_data
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    except ValueError:
        raise HTTPException(status_code=500, detail="Job status corrupted")


@app.delete("/api/jobs/{job_id}")
def delete_or_archive_job(job_id: str, request: Request):
    """Archive or delete a video job from the library."""
    _enforce_public_generation_access(request)
    if not is_valid_job_id(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID")

    job_dir = JOBS_ROOT / job_id
    try:
        resolved_job_dir = job_dir.resolve()
        resolved_job_dir.relative_to(JOBS_ROOT.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Forbidden path")

    if not resolved_job_dir.is_dir():
        raise HTTPException(status_code=404, detail="Job not found")

    status_file = resolved_job_dir / "status.json"
    if not status_file.is_file():
        raise HTTPException(status_code=500, detail="Job status missing")
    try:
        status_data = json.loads(status_file.read_text(encoding="utf-8"))
        status_data["archived"] = True
        status_data["status"] = "archived"
        write_json_atomically(status_file, status_data)
    except Exception as exc:
        logger.warning("Failed to update status.json for job %s: %s", job_id, exc)
        raise HTTPException(status_code=500, detail="Failed to archive job") from exc

    return {"success": True, "job_id": job_id, "archived": True}


@app.post("/api/jobs/{job_id}/resume", status_code=status.HTTP_202_ACCEPTED, response_model=VideoResponse)
async def resume_job(job_id: str, request: Request, background_tasks: BackgroundTasks):
    """Resume a failed or interrupted resumable video job."""
    _enforce_public_generation_access(request)
    if not is_valid_job_id(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID")

    job_dir = _safe_job_path(JOBS_ROOT / job_id)
    if job_dir is None:
        raise HTTPException(status_code=403, detail="Forbidden path")
    if not job_dir.is_dir():
        raise HTTPException(status_code=404, detail="Job not found")

    try:
        status_data = read_job_status(job_dir)
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=400, detail="Job status unreadable")

    if status_data.get("status") in {"completed", "queued", "writing", "rendering", "voice", "visuals"}:
        raise HTTPException(status_code=400, detail=f"Job is currently in active or completed state: '{status_data.get('status')}'")

    if not status_data.get("restart_resumable") or status_data.get("status") not in {"needs_attention", "failed"}:
        raise HTTPException(status_code=400, detail="Job is not in a resumable state")

    script_path = job_dir / "script.json"
    if not script_path.exists():
        raise HTTPException(status_code=400, detail="Job script missing")

    try:
        script_data = json.loads(script_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Job script corrupt")

    persisted_controls = status_data.get("render_controls")
    if persisted_controls is not None:
        try:
            controls = RenderControls.model_validate(persisted_controls)
            script_controls = script_data.get("render_controls")
            if script_controls is None:
                script_controls = {
                    name: script_data[name]
                    for name in RenderControls.model_fields
                    if name in script_data
                }
            if script_controls:
                script_snapshot = RenderControls.model_validate(script_controls)
                if script_snapshot != controls:
                    raise HTTPException(
                        status_code=400,
                        detail="Job render controls do not match persisted status",
                    )
            else:
                script_data = _with_render_controls(script_data, controls)
                write_json_atomically(script_path, script_data)
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Job render controls corrupt") from exc

    resume_count = int(status_data.get("resume_count", 0)) + 1
    if resume_count > 3:
        raise HTTPException(status_code=400, detail="Maximum resume attempts (3) exceeded")

    lease = acquire_guardrail_lease(
        operation_id=job_id,
        request=request,
        estimated_charge_usd=0.06,
        job_roots=(JOBS_ROOT, SCRIPT_JOBS_ROOT),
    )

    try:
        update_job_status(job_dir, {
            "status": "queued",
            "error": None,
            "resume_count": resume_count,
            "restart_resumable": True,
        })

        submit_queue, message = _submit_to_queue(
            kind="video-resume",
            target="_run_video_pipeline_tracked",
            args=[job_id, script_data, "gemini", JOBS_ROOT],
            job_id=job_id,
            client_key=_client_idempotency_key(request),
            attempt_suffix=str(resume_count),
        )
        if not message.is_duplicate:
            background_tasks.add_task(submit_queue.dispatch, message.message_id)
    except Exception:
        lease.release()
        try:
            update_job_status(job_dir, {
                "status": "needs_attention",
                "restart_resumable": True,
                "error": "Failed to queue video resume task",
            })
        except Exception:
            pass
        logger.exception("Failed to resume video job %s:", job_id)
        raise HTTPException(status_code=500, detail="Failed to queue video resume")

    return VideoResponse(
        success=True,
        job_id=job_id,
        status_url=f"/api/jobs/{job_id}/status",
        restart_resumable=True,
    )


# ---------------------------------------------------------------------------
# Stage B-III (B9): cooperative cancellation routes.
#
# Cancellation is COOPERATIVE. A request records a durable marker and flips an
# in-process flag; queued work halts before dispatch and in-flight work stops at
# the next safe boundary (between fully-written segments). Late-arriving results
# and costs are still reconciled into the ledger, and a terminal ``cancelled``
# state can never be overwritten by a stale result.
# ---------------------------------------------------------------------------
_CANCEL_TERMINAL_STATUSES = {"completed", "cancelled", "archived"}


def _apply_cancellation(job_id: str, job_dir: Path, status_data: dict, *, is_script: bool) -> dict:
    """Shared cancel semantics for video and script jobs (B9)."""
    current = status_data.get("status")
    if current in _CANCEL_TERMINAL_STATUSES:
        # Terminal work is not cancellable; report its state honestly (idempotent)
        # and never downgrade a newer terminal state.
        return {
            "success": True,
            "job_id": job_id,
            "status": current,
            "already_terminal": True,
            "cancellation": cancellation.cancellation_status(job_id, job_dir=job_dir).to_dict(),
        }

    cstat = cancellation.request_cancellation(job_id, job_dir=job_dir, reason="user_requested")
    provider_op_id = status_data.get("provider_operation_id")
    provider_ack = cancellation.best_effort_provider_cancel(provider_op_id, job_id=job_id)

    # Queued work has not dispatched yet: it halts BEFORE dispatch and is terminal
    # now. In-flight work seeks the next safe boundary -> 'cancelling' until then.
    if current == "queued":
        cancellation.mark_cancelled(job_id, job_dir=job_dir, boundary="pre_dispatch")
        new_status = "cancelled"
    else:
        new_status = "cancelling"

    snapshot = cancellation.cancellation_status(job_id, job_dir=job_dir).to_dict()
    try:
        if is_script:
            update_script_status(job_dir, status=new_status, cancellation=snapshot)
        else:
            update_job_status(job_dir, {"status": new_status, "cancellation": snapshot})
    except Exception:
        logger.exception("Could not persist cancellation status for job %s", job_id)

    return {
        "success": True,
        "job_id": job_id,
        "status": new_status,
        "already_terminal": False,
        "provider_cancel_acknowledged": provider_ack,
        "cancellation": snapshot,
    }


@app.post("/api/jobs/{job_id}/cancel")
def cancel_video_job(job_id: str, request: Request):
    """Cooperatively cancel a video job (queued halts now; in-flight at boundary)."""
    _enforce_public_generation_access(request)
    if not is_valid_job_id(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID")
    job_dir = _safe_job_path(JOBS_ROOT / job_id)
    if job_dir is None:
        raise HTTPException(status_code=403, detail="Forbidden path")
    if not job_dir.is_dir():
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        status_data = read_job_status(job_dir)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    except ValueError:
        raise HTTPException(status_code=500, detail="Job status corrupted")
    return _apply_cancellation(job_id, job_dir, status_data, is_script=False)


@app.post("/api/script-jobs/{job_id}/cancel")
def cancel_script_job(job_id: str, request: Request):
    """Cooperatively cancel a script job (queued halts now; in-flight at boundary)."""
    _enforce_public_generation_access(request)
    if not is_valid_job_id(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID format")
    job_dir = SCRIPT_JOBS_ROOT / job_id
    if not job_dir.is_dir():
        raise HTTPException(status_code=404, detail="Script job not found")
    status_path = job_dir / "status.json"
    if not status_path.exists():
        raise HTTPException(status_code=404, detail="Script job status missing")
    try:
        status_data = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Script job status unreadable")
    return _apply_cancellation(job_id, job_dir, status_data, is_script=True)


@app.get("/api/jobs/{job_id}/video")
def get_job_video(job_id: str):
    if not is_valid_job_id(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID")

    job_dir = _safe_job_path(JOBS_ROOT / job_id)
    if job_dir is None:
        raise HTTPException(status_code=403, detail="Forbidden path")
    video_path = job_dir / "video.mp4"
    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Video file not found")

    try:
        status_data = read_job_status(job_dir)
        if status_data.get("status") != "completed":
            raise HTTPException(status_code=404, detail="Video not completed or approved")
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=404, detail="Job status missing or unreadable")

    return FileResponse(
        path=str(video_path),
        media_type="video/mp4",
        filename=f"fyf_{job_id}.mp4"
    )


@app.get("/api/telemetry")
def get_telemetry_summary():
    return get_all_telemetry_summary(job_roots=(JOBS_ROOT, SCRIPT_JOBS_ROOT))


@app.get("/api/telemetry/{job_id}")
def get_job_telemetry_detail(job_id: str):
    if not is_valid_job_id(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID format")
    try:
        return get_job_telemetry(job_id, job_roots=(JOBS_ROOT, SCRIPT_JOBS_ROOT))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job telemetry not found")


@app.get("/api/jobs/{job_id}/telemetry")
def get_job_telemetry_alias(job_id: str):
    return get_job_telemetry_detail(job_id)


@app.post("/api/insights")
def ask_data_insights(request: Request, payload: dict = Body(...)):
    """Ask the FYF Data Officer (ADK agent + mcp-clickhouse) a question about
    production telemetry. Read-only; answers from ClickHouse Cloud."""
    _enforce_public_generation_access(request)
    question = str(payload.get("question", "")).strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    if len(question) > 500:
        raise HTTPException(status_code=400, detail="question too long")
    try:
        import asyncio

        from backend.agent.data_officer import ask_data_officer

        # Hard-bound the whole officer turn inside one event loop so slow
        # Vertex/MCP turns fail cleanly instead of hanging the client.
        result = None
        try:
            async def _officer_turn():
                return await asyncio.wait_for(
                    ask_data_officer(question), timeout=26.0
                )

            result = asyncio.run(_officer_turn())
        except asyncio.TimeoutError:
            logger.warning("Data Officer exceeded 26s budget for question")
            raise HTTPException(
                status_code=504,
                detail="Data Officer timed out; try a simpler question.",
            )
        except RuntimeError as exc:
            logger.warning("Data Officer failed: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc))
        except Exception:
            logger.exception("Data Officer failed unexpectedly")
            raise HTTPException(status_code=502, detail="Data Officer failed unexpectedly")
        if result is None:
            raise HTTPException(status_code=503, detail="Data Officer unavailable")
    except HTTPException:
        raise
    except Exception:
        logger.exception("Data Officer failed for question")
        raise HTTPException(status_code=502, detail="Data Officer failed unexpectedly")
    return {"success": True, "question": question, **result}


@app.post("/api/clickhouse/query")
def execute_clickhouse_query(request: Request, payload: dict = Body(...)):
    """Execute a server-owned telemetry query by identifier."""
    _enforce_public_access_token(request)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON object body is required")
    if "query" in payload:
        raise HTTPException(
            status_code=400,
            detail="Caller SQL is not accepted; provide one of the supported query_id values.",
        )
    query_id = payload.get("query_id")
    from backend.telemetry_queries import QUERY_SQL, execute_telemetry_query

    if not isinstance(query_id, str) or query_id not in QUERY_SQL:
        raise HTTPException(
            status_code=400,
            detail=f"query_id must be one of: {', '.join(sorted(QUERY_SQL.keys()))}",
        )
    try:
        return execute_telemetry_query(
            query_id,
            jobs_root=JOBS_ROOT,
            script_jobs_root=SCRIPT_JOBS_ROOT,
            repo_root=REPO_ROOT,
        )
    except Exception as exc:
        logger.warning("Telemetry query %s failed: %s", query_id, exc)
        raise HTTPException(status_code=503, detail="Telemetry query unavailable") from exc


# ---------------------------------------------------------------------------
# Project spine (Stage B-II / B12): versioned projects + atomic commands.
#
# These routes are a THIN HTTP seam over backend.projects. Every durability,
# concurrency (exactly-one-winner), idempotency, stale-version reject/rebase and
# validate-ALL / zero-partial-edit guarantee lives in backend.projects.commands
# and backend.projects.store, NOT here. Existing routes above are untouched.
# ---------------------------------------------------------------------------


def _project_store() -> FileProjectStore:
    """FileProjectStore rooted at ``FYF_PROJECTS_ROOT`` or ``output/projects``."""
    override = os.getenv("FYF_PROJECTS_ROOT")
    return FileProjectStore(Path(override) if override else PROJECTS_ROOT)


class CreateProjectRequest(BaseModel):
    """Body for ``POST /api/projects``: wrap a Vertex ``VideoScript`` as version 1."""

    model_config = ConfigDict(extra="forbid")

    script: VideoScript
    actor: str = Field(min_length=1)
    # Optional client-chosen id; when omitted the server generates a unique one.
    project_id: Optional[str] = Field(default=None, pattern=PROJECT_ID_PATTERN)
    variant_name: Optional[str] = None
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=128)
    pinned_production_config: Optional[PinnedProductionConfig] = None


@app.post("/api/projects", status_code=status.HTTP_201_CREATED)
def create_project(req: CreateProjectRequest, request: Request):
    """Create a project and commit immutable version 1 wrapping the VideoScript.

    Idempotent: replaying the same ``idempotency_key`` returns the existing
    version 1 rather than committing a duplicate.
    """
    _enforce_public_access_token(request)
    store = _project_store()
    # Server-generated ids reuse create_job_dir's bounded-uniqueness guarantee.
    project_id = req.project_id or create_job_dir(store.root)
    try:
        version = create_project_with_script(
            store,
            project_id,
            req.script,
            req.actor,
            variant_name=req.variant_name,
            idempotency_key=req.idempotency_key,
            pinned_production_config=req.pinned_production_config,
        )
    except InvalidProjectIdError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid project id: {exc}") from exc
    except VersionAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        logger.exception("Project creation failed")
        raise HTTPException(status_code=500, detail="Failed to create project")
    return {
        "success": True,
        "project_id": project_id,
        "version_no": version.version_no,
        "version": version.model_dump(mode="json"),
    }


@app.get("/api/projects/{project_id}/versions/{version_no}")
def get_project_version(project_id: str, version_no: int):
    """Return one immutable project version (404 when the project/version is absent)."""
    if not is_valid_job_id(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    if version_no < 1:
        raise HTTPException(status_code=400, detail="version_no must be >= 1")
    store = _project_store()
    try:
        version = store.load_version(project_id, version_no)
    except InvalidProjectIdError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid project id: {exc}") from exc
    except (ProjectNotFoundError, ProjectVersionNotFoundError):
        raise HTTPException(status_code=404, detail="Project version not found")
    return {
        "success": True,
        "project_id": project_id,
        "version_no": version.version_no,
        "version": version.model_dump(mode="json"),
    }


@app.post("/api/projects/{project_id}/commands")
def apply_project_command(
    project_id: str, command: ProjectCommand, request: Request, rebase: bool = False
):
    """Atomically validate + apply ONE command against the project spine.

    Concurrency / staleness: a ``base_version`` that no longer matches the
    committed head is rejected with 409 (never silently overwritten); the client
    may pass ``?rebase=true`` to rebase explicitly. Paid operations
    (``request_render``) are refused server-side (402) unless a persisted
    approval exists and budget is available - cost is never rendered as 0.
    Validation failures reject the WHOLE command (422) with zero partial edits.
    """
    _enforce_public_access_token(request)
    if not is_valid_job_id(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    if command.project_id != project_id:
        raise HTTPException(status_code=400, detail="Body project_id does not match the path")
    store = _project_store()
    try:
        changeset = apply_command(
            store, command, rebase=rebase, lock_checker=granular_lock_checker(LOCKS_ROOT)
        )
    except InvalidProjectIdError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid project id: {exc}") from exc
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc
    except StaleVersionError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "stale_version",
                "project_id": exc.project_id,
                "base_version": exc.base_version,
                "current_version": exc.current_version,
                "hint": "re-fetch head and retry, or pass ?rebase=true to rebase explicitly",
            },
        ) from exc
    except LockConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "lock_conflict", "project_id": exc.project_id, "conflicts": exc.conflicts},
        ) from exc
    except ApprovalRequiredError as exc:
        # 402 Payment Required: a paid op lacks an approval / available budget.
        # budget_status is surfaced verbatim so unknown cost stays None, never 0.
        raise HTTPException(
            status_code=402,
            detail={
                "error": "approval_required",
                "operation": exc.operation,
                "reason": exc.reason,
                "budget_status": exc.budget_status,
            },
        ) from exc
    except CommandValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "validation_failed", "changeset": exc.changeset.model_dump(mode="json")},
        ) from exc
    except Exception:
        logger.exception("Command application failed")
        raise HTTPException(status_code=500, detail="Failed to apply command")
    return {
        "success": True,
        "status": changeset.status,
        "changeset": changeset.model_dump(mode="json"),
    }


# ---------------------------------------------------------------------------
# Stage C-II (Task 9): shared chat/canvas Studio seam.
#
# Additive HTTP routes over the SAME versioned spine (backend.projects). Design
# rules honoured here:
#   * Chat is a COMMAND EMITTER: backend.projects.chat maps one message +
#     selection to ONE ProjectCommand and performs no I/O; the command is then
#     applied through apply_command (the single mutating seam) with the granular
#     lock_checker injected. Chat never writes state directly.
#   * Undo / named variants APPEND a new immutable version (no destructive
#     rollback: an existing version is never mutated or deleted). The spine
#     enforces a linear parent (parent_version == version_no - 1), so an undo to
#     version T appends head+1 whose CONTENT is restored from T and whose
#     semantic source is recorded in applied_operations / event artifact_refs.
#   * Generation results attach ONLY to the version they were dispatched
#     against; a stale result is isolated (no write) and never mutates a newer
#     draft (C7).
#   * Events expose progress_source verbatim so the UI can show an actual
#     percentage or an honestly-labelled estimate (C9).
#   * Every route reuses _enforce_public_access_token and the existing error map.
# ---------------------------------------------------------------------------


def _granular_checker():
    """The granular lock_checker hook injected into apply_command.

    Widens - never narrows - the default: with an empty registry it behaves
    exactly like default_lock_checker (the version's own LockState).
    """
    return granular_lock_checker(LOCKS_ROOT)


def _version_summary(version: ProjectVersion) -> dict:
    """A lightweight, honest version-history row (no full script payload)."""
    return {
        "version_no": version.version_no,
        "parent_version": version.parent_version,
        "variant_name": version.variant_name,
        "actor": version.actor,
        "created_at": version.created_at,
        "applied_operations": list(version.applied_operations),
        "source_command_operation": version.source_command_operation,
        "segment_ids": version.segment_ids(),
        "locks": version.locks.model_dump(mode="json"),
    }


def _load_head_or_404(store: "FileProjectStore", project_id: str) -> tuple[int, ProjectVersion]:
    head_no = store.current_version_no(project_id)
    if head_no < 1:
        raise HTTPException(status_code=404, detail="Project not found")
    return head_no, store.load_version(project_id, head_no)


def _append_derived_version(
    store: "FileProjectStore",
    project_id: str,
    source: ProjectVersion,
    head_no: int,
    *,
    actor: str,
    stage: str,
    idempotency_key: Optional[str],
    variant_name: Optional[str] = None,
    overrides: Optional[dict] = None,
    artifact_refs: Optional[list[str]] = None,
) -> ProjectVersion:
    """Append head+1 derived from ``source`` content (linear parent = head).

    Used by undo / named-variant / result-attach. Never mutates ``source``.
    """
    now = utc_now_iso()
    data = source.model_dump(mode="json")
    data["version_no"] = head_no + 1
    data["parent_version"] = head_no
    data["actor"] = actor
    data["created_at"] = now
    data["idempotency_key"] = idempotency_key
    data["applied_operations"] = [stage]
    data["source_command_operation"] = None
    if variant_name is not None:
        data["variant_name"] = variant_name
    if overrides:
        data.update(overrides)
    new_version = ProjectVersion.model_validate(data)
    store.append_version(new_version)
    store.append_event(
        WorkflowEvent(
            event_id=uuid.uuid4().hex,
            project_id=project_id,
            version_no=head_no + 1,
            event_type="version_appended",
            stage=stage,
            status="completed",
            sequence=store.next_sequence(project_id),
            progress_source="actual",
            artifact_refs=list(artifact_refs or []),
            actor=actor,
            idempotency_key=idempotency_key,
            timestamp=now,
        )
    )
    return new_version


class ChatRequest(BaseModel):
    """Body for ``POST /api/projects/{id}/chat``."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)
    selection: Optional[Selection] = None
    actor: str = "creative-director"
    base_version: Optional[int] = Field(default=None, ge=0)
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=128)


class UndoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_version: int = Field(ge=1)
    actor: str = "creative-director"
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=128)


class VariantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variant_name: str = Field(min_length=1, max_length=80)
    actor: str = "creative-director"
    base_version: Optional[int] = Field(default=None, ge=1)
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=128)


class ScopeLockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1)
    locked: bool
    locked_by: Optional[str] = None
    reason: Optional[str] = None


class ResultRequest(BaseModel):
    """A generation result dispatched against one specific version (C7)."""

    model_config = ConfigDict(extra="forbid")

    dispatched_version: int = Field(ge=1)
    actor: str = "generation"
    asset_references: list[AssetReference] = Field(default_factory=list)
    render_manifest: Optional[RenderManifest] = None
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=128)


@app.get("/api/projects/{project_id}/versions")
def list_project_versions(project_id: str):
    """Persistent version history: every committed version, newest last."""
    if not is_valid_job_id(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    store = _project_store()
    if not store.project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    numbers = store.list_versions(project_id)
    versions = [_version_summary(store.load_version(project_id, n)) for n in numbers]
    return {
        "success": True,
        "project_id": project_id,
        "head": numbers[-1] if numbers else 0,
        "versions": versions,
    }


@app.get("/api/projects/{project_id}/events")
def list_project_events(project_id: str, after_sequence: int = 0):
    """Append-only workflow events (honest progress_source for the UI)."""
    if not is_valid_job_id(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    store = _project_store()
    if not store.project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    events = store.read_events(project_id, after_sequence=after_sequence)
    return {
        "success": True,
        "project_id": project_id,
        "events": [event.model_dump(mode="json") for event in events],
    }


@app.post("/api/projects/{project_id}/chat")
def project_chat(project_id: str, req: ChatRequest, request: Request):
    """Map ONE chat message + selection to ONE command, then apply it.

    Chat never writes state directly: backend.projects.chat produces a
    ProjectCommand which is applied through apply_command - the exact seam the
    canvas uses - so chat and canvas edit the SAME canonical version.
    """
    _enforce_public_access_token(request)
    if not is_valid_job_id(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    store = _project_store()
    try:
        head_no, head = _load_head_or_404(store, project_id)
    except InvalidProjectIdError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid project id: {exc}") from exc
    base_version = req.base_version if req.base_version is not None else head_no
    try:
        mapping = chat.map_message_to_command(
            project_id=project_id,
            base_version=base_version,
            message=req.message,
            version=head,
            selection=req.selection,
            actor=req.actor,
            idempotency_key=req.idempotency_key,
        )
    except chat.ChatMappingError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "chat_mapping_failed", "reason": str(exc)},
        ) from exc
    try:
        changeset = apply_command(
            store, mapping.command, rebase=False, lock_checker=_granular_checker()
        )
    except StaleVersionError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "stale_version",
                "project_id": exc.project_id,
                "base_version": exc.base_version,
                "current_version": exc.current_version,
                "hint": "the canvas moved the head; re-send against the current version",
            },
        ) from exc
    except LockConflictError as exc:
        scopes = read_scope_locks(LOCKS_ROOT, project_id)
        reasons = {
            scope: (scopes.get(scope) or {}).get("reason")
            for scope in exc.conflicts
        }
        raise HTTPException(
            status_code=409,
            detail={
                "error": "lock_conflict",
                "project_id": exc.project_id,
                "conflicts": exc.conflicts,
                "reasons": reasons,
                "hint": "this scope is locked; unlock it or edit a different scope",
            },
        ) from exc
    except CommandValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_failed",
                "changeset": exc.changeset.model_dump(mode="json"),
            },
        ) from exc
    except Exception:
        logger.exception("Chat command application failed")
        raise HTTPException(status_code=500, detail="Failed to apply chat command")
    new_version = (
        store.load_version(project_id, changeset.target_version)
        if changeset.target_version is not None
        else head
    )
    return {
        "success": True,
        "operation": mapping.operation,
        "summary": mapping.summary,
        "affected_segment_ids": mapping.affected_segment_ids,
        "command": mapping.command.model_dump(mode="json"),
        "status": changeset.status,
        "changeset": changeset.model_dump(mode="json"),
        "version": new_version.model_dump(mode="json"),
    }


@app.post("/api/projects/{project_id}/undo")
def undo_project_version(project_id: str, req: UndoRequest, request: Request):
    """Undo = append a NEW version restoring ``target_version``'s content.

    No destructive rollback: the target version is never mutated or deleted. The
    spine's linear-parent rule means the new head's parent is the current head,
    while the restored content and its provenance (``undo_to_v{T}``) are recorded
    on the version and its event.
    """
    _enforce_public_access_token(request)
    if not is_valid_job_id(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    store = _project_store()
    try:
        with store.transaction(project_id):
            if not store.project_exists(project_id):
                raise HTTPException(status_code=404, detail="Project not found")
            head_no = store.current_version_no(project_id)
            if head_no < 1:
                raise HTTPException(status_code=404, detail="Project not found")
            if req.target_version > head_no:
                raise HTTPException(status_code=404, detail="Target version not found")
            if req.idempotency_key:
                existing = store.find_by_idempotency_key(project_id, req.idempotency_key)
                if existing is not None:
                    return {
                        "success": True,
                        "status": "replayed",
                        "restored_from": req.target_version,
                        "version": existing.model_dump(mode="json"),
                    }
            target = store.load_version(project_id, req.target_version)
            head = store.load_version(project_id, head_no)
            new_version = _append_derived_version(
                store,
                project_id,
                target,
                head_no,
                actor=req.actor,
                stage=f"undo_to_v{req.target_version}",
                idempotency_key=req.idempotency_key,
                artifact_refs=[f"undo:v{req.target_version}", f"from_head:v{head.version_no}"],
            )
    except HTTPException:
        raise
    except (ProjectNotFoundError, ProjectVersionNotFoundError):
        raise HTTPException(status_code=404, detail="Project version not found")
    except Exception:
        logger.exception("Undo failed")
        raise HTTPException(status_code=500, detail="Failed to undo")
    return {
        "success": True,
        "status": "applied",
        "restored_from": req.target_version,
        "previous_head": head_no,
        "version": new_version.model_dump(mode="json"),
    }


@app.post("/api/projects/{project_id}/variants")
def create_project_variant(project_id: str, req: VariantRequest, request: Request):
    """Persist a NAMED variant as a new server-side version (survives reload)."""
    _enforce_public_access_token(request)
    if not is_valid_job_id(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    store = _project_store()
    try:
        with store.transaction(project_id):
            if not store.project_exists(project_id):
                raise HTTPException(status_code=404, detail="Project not found")
            head_no = store.current_version_no(project_id)
            if head_no < 1:
                raise HTTPException(status_code=404, detail="Project not found")
            if req.idempotency_key:
                existing = store.find_by_idempotency_key(project_id, req.idempotency_key)
                if existing is not None:
                    return {
                        "success": True,
                        "status": "replayed",
                        "variant_name": existing.variant_name,
                        "version": existing.model_dump(mode="json"),
                    }
            base_no = req.base_version if req.base_version is not None else head_no
            if base_no > head_no:
                raise HTTPException(status_code=404, detail="Base version not found")
            base = store.load_version(project_id, base_no)
            new_version = _append_derived_version(
                store,
                project_id,
                base,
                head_no,
                actor=req.actor,
                stage=f"variant:{req.variant_name}",
                idempotency_key=req.idempotency_key,
                variant_name=req.variant_name,
                artifact_refs=[f"variant:{req.variant_name}", f"from:v{base_no}"],
            )
    except HTTPException:
        raise
    except (ProjectNotFoundError, ProjectVersionNotFoundError):
        raise HTTPException(status_code=404, detail="Project version not found")
    except Exception:
        logger.exception("Variant creation failed")
        raise HTTPException(status_code=500, detail="Failed to create variant")
    return {
        "success": True,
        "status": "applied",
        "variant_name": new_version.variant_name,
        "version": new_version.model_dump(mode="json"),
    }


@app.get("/api/projects/{project_id}/locks")
def get_project_locks(project_id: str):
    """Current granular scope locks (content / visual / timing) for a project."""
    if not is_valid_job_id(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    return {
        "success": True,
        "project_id": project_id,
        "scopes": read_scope_locks(LOCKS_ROOT, project_id),
        "locked": locked_scopes(LOCKS_ROOT, project_id),
        "available_scopes": list(GRANULAR_LOCK_SCOPES),
    }


@app.post("/api/projects/{project_id}/locks")
def set_project_lock(project_id: str, req: ScopeLockRequest, request: Request):
    """Set or clear ONE granular scope lock. The whole-script ``story`` lock
    (``/api/story-lock``) is untouched; this governs content/visual/timing."""
    _enforce_public_access_token(request)
    if not is_valid_job_id(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    if req.scope not in GRANULAR_LOCK_SCOPES:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unknown_lock_scope",
                "scope": req.scope,
                "available_scopes": list(GRANULAR_LOCK_SCOPES),
            },
        )
    record = set_scope_lock(
        LOCKS_ROOT,
        project_id,
        req.scope,
        locked=req.locked,
        locked_by=req.locked_by,
        reason=req.reason,
    )
    return {
        "success": True,
        "project_id": project_id,
        "scope": req.scope,
        "record": record,
        "locked": locked_scopes(LOCKS_ROOT, project_id),
    }


@app.post("/api/projects/{project_id}/results")
def attach_project_result(project_id: str, req: ResultRequest, request: Request):
    """Attach a generation result ONLY to the version it was dispatched against.

    C7 stale-result isolation: when the head has advanced past
    ``dispatched_version`` (an edit landed during the render), the result is
    isolated - NOTHING is written and the newer draft is untouched. Otherwise a
    new version is appended carrying the result's asset references / manifest.
    """
    _enforce_public_access_token(request)
    if not is_valid_job_id(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    store = _project_store()
    try:
        with store.transaction(project_id):
            if not store.project_exists(project_id):
                raise HTTPException(status_code=404, detail="Project not found")
            head_no = store.current_version_no(project_id)
            if head_no < 1:
                raise HTTPException(status_code=404, detail="Project not found")
            if req.dispatched_version != head_no:
                # Stale: isolate. No write, newer draft preserved exactly.
                return {
                    "success": True,
                    "status": "stale_isolated",
                    "applied": False,
                    "dispatched_version": req.dispatched_version,
                    "current_version": head_no,
                    "reason": "stale_result",
                    "hint": (
                        f"result was dispatched against v{req.dispatched_version} but the "
                        f"head is v{head_no}; nothing was written and v{head_no} is unchanged"
                    ),
                }
            if req.idempotency_key:
                existing = store.find_by_idempotency_key(project_id, req.idempotency_key)
                if existing is not None:
                    return {
                        "success": True,
                        "status": "replayed",
                        "applied": False,
                        "target_version": existing.version_no,
                        "version": existing.model_dump(mode="json"),
                    }
            head = store.load_version(project_id, head_no)
            merged = {a["asset_id"]: a for a in head.model_dump(mode="json")["asset_references"]}
            for asset in req.asset_references:
                merged[asset.asset_id] = asset.model_dump(mode="json")
            overrides: dict = {"asset_references": list(merged.values())}
            if req.render_manifest is not None:
                overrides["render_manifest"] = req.render_manifest.model_dump(mode="json")
            new_version = _append_derived_version(
                store,
                project_id,
                head,
                head_no,
                actor=req.actor,
                stage="attach_result",
                idempotency_key=req.idempotency_key,
                overrides=overrides,
                artifact_refs=[a.asset_id for a in req.asset_references],
            )
    except HTTPException:
        raise
    except (ProjectNotFoundError, ProjectVersionNotFoundError):
        raise HTTPException(status_code=404, detail="Project version not found")
    except Exception:
        logger.exception("Result attachment failed")
        raise HTTPException(status_code=500, detail="Failed to attach result")
    return {
        "success": True,
        "status": "attached",
        "applied": True,
        "dispatched_version": req.dispatched_version,
        "target_version": new_version.version_no,
        "version": new_version.model_dump(mode="json"),
    }


@app.get("/api/budget")
def get_budget():
    """Fail-closed budget status. Unknown cost stays ``None``, never ``0``."""
    return {"success": True, "budget": get_budget_status()}
