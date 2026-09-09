"""
FYF Video Pipeline - FastAPI Backend
Connects the Next.js frontend to the Gemini Writer/Producer Agent and Gemini-TTS Voice Generation.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
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
from backend.budget_store import (
    begin_approval,
    finalize_approval,
    get_budget_status,
    record_approval,
    release_reservation,
    revoke_approval,
    rollback_approval,
    set_project_budget,
)
from backend.lock_store import (
    GRANULAR_LOCK_SCOPES,
    create_script_lock,
    final_acceptance_readiness,
    granular_lock_checker,
    read_human_acceptance,
    locked_scopes,
    read_scope_locks,
    read_script_lock,
    set_human_acceptance,
    set_scope_lock,
)
from backend.projects.commands import (
    ApprovalRequiredError,
    CommandValidationError,
    IdempotencyConflictError,
    LockConflictError,
    StaleVersionError,
    apply_command,
    command_fingerprint,
    command_touched_scopes,
    create_project_with_script,
    preview_command,
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
    ProjectProposal,
    ProposalDiff,
    WorkflowEvent,
)
from backend.projects import chat
from backend.projects.proposals import (
    FileProposalStore,
    ProposalConflictError,
    ProposalCorruptError,
    ProposalNotFoundError,
    new_proposal_id,
)
from backend.projects.store import (
    FileProjectStore,
    InvalidProjectIdError,
    ProjectNotFoundError,
    ProjectVersionNotFoundError,
    VersionAlreadyExistsError,
)
from backend.projects.scene_locks import (
    SCENE_LOCK_SCOPES,
    read_scene_locks,
    scene_lock_conflicts,
    set_scene_locks,
)
from backend.projects.selection import UnknownSelectionTargetError, resolve_selection
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
from video_contract import ExactLockRequest, RenderControls, ScriptSegment, StoryModesResponse, VideoScript

# Stage B-III: durable idempotent queue (B7), cooperative cancellation (B9), and
# the single validated capacity-limit source (B11).
from backend import cancellation
from backend.capacity_config import load_capacity_config, validate_submission
from backend.job_queue import JobQueue, default_queue_root
from backend.exports import (
    ExportInputMissing,
    UnknownExportKindError,
    list_export_kinds,
    plan_exports,
    resolve_export_spec,
    run_export,
)
from backend.render_manifest import read_render_manifest, verify_render_manifest
from backend.pipeline_graph import plan_recompute_for_regeneration
from backend.clickhouse_telemetry import get_telemetry_delivery_status
from backend.telemetry_outbox import get_outbox
from backend.telemetry_reconcile import build_reconciliation_report

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
TELEMETRY_ROOT = REPO_ROOT / "output" / "telemetry"

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
    source_mode: Literal["brief", "full_script"] = "brief"
    title: str | None = Field(default=None, max_length=200)
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

    @model_validator(mode="after")
    def validate_full_script_title(self):
        if self.source_mode == "full_script":
            self.title = (self.title or "").strip()
            if not self.title:
                raise ValueError("Video title is required for a full script")
        return self


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
    # Accessibility is intentionally top-level: RenderControls is a stable
    # nested contract, while both Python and Remotion resolve this flag from the
    # root render input.
    reduced_motion: bool = Field(default=False, strict=True)

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
        attachment = _attach_completed_project_result(job_id, job_dir)
        if attachment and attachment.get("status") == "stale_isolated":
            # The media is still available from its job URL for diagnostics, but
            # it is not a current project preview. Surface a terminal attention
            # state so the Studio cannot mistake a completed stale job for an
            # attached/current success.
            update_job_status(
                job_dir,
                {
                    "status": "needs_attention",
                    "video_url": None,
                    "error": (
                        "Render completed for an earlier project version; the stale "
                        "output was isolated and not attached to the current preview."
                    ),
                    "restart_resumable": False,
                },
            )
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
    except Exception:
        logger.exception("Post-pipeline project result attachment failed for job %s", job_id)
        try:
            current = read_job_status(job_dir)
            if current.get("status") == "completed":
                update_job_status(
                    job_dir,
                    {
                        "status": "needs_attention",
                        "video_url": None,
                        "error": "Project result failed integrity verification; no version was attached.",
                        "restart_resumable": False,
                    },
                )
        except Exception:
            logger.exception("Could not persist attachment failure for job %s", job_id)
    finally:
        release_active_job(job_id)
        from backend.budget_store import release_reservation
        try:
            release_reservation(job_id)
        except Exception:
            # Budget cleanup must not turn an already-terminal pipeline result
            # into an uncaught worker failure.  The ledger remains fail-closed
            # and the exception is visible for operator reconciliation.
            logger.exception("Could not release budget reservation for job %s", job_id)


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
    reduced_motion = getattr(controls, "reduced_motion", False)
    if not isinstance(reduced_motion, bool):
        raise ValueError("reduced_motion must be a boolean")
    result["reduced_motion"] = reduced_motion
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
async def generate_video(
    req: VideoRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    project_id: str | None = None,
):
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
        project_id=project_id,
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

    project_context = _read_project_context(job_dir)
    project_id = (
        project_context.get("project_id")
        if isinstance(project_context, dict)
        and isinstance(project_context.get("project_id"), str)
        else None
    )

    lease = acquire_guardrail_lease(
        operation_id=job_id,
        request=request,
        estimated_charge_usd=0.06,
        job_roots=(JOBS_ROOT, SCRIPT_JOBS_ROOT),
        project_id=project_id,
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

    # Legacy, non-project jobs retain the historical completed-video endpoint.
    # Project jobs must prove the exact current project/version acceptance gate
    # before bytes can be downloaded.
    _require_job_download_ready(job_id, job_dir)

    return FileResponse(
        path=str(video_path),
        media_type="video/mp4",
        filename=f"fyf_{job_id}.mp4"
    )


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kinds: list[str] = Field(min_length=1, max_length=12)


@app.get("/api/export-kinds")
def get_export_kinds():
    return {"success": True, "exports": list_export_kinds()}


@app.post("/api/jobs/{job_id}/exports", status_code=status.HTTP_201_CREATED)
def create_job_exports(job_id: str, req: ExportRequest, request: Request):
    """Materialise exactly the selected deterministic export artifacts."""
    _enforce_public_access_token(request)
    if not is_valid_job_id(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID")
    job_dir = _safe_job_path(JOBS_ROOT / job_id)
    if job_dir is None:
        raise HTTPException(status_code=403, detail="Forbidden path")
    if not job_dir.is_dir():
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        status_data = read_job_status(job_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Job status missing or unreadable") from exc
    if status_data.get("status") != "completed":
        raise HTTPException(status_code=409, detail="Exports require a completed, approved render")

    project_readiness = _require_job_download_ready(job_id, job_dir)

    # Validate the whole selection before creating the exports directory. This
    # prevents a mixed valid/invalid request from leaving partial artifacts.
    try:
        specs = plan_exports(req.kinds)
    except UnknownExportKindError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "unknown_export_kind", "reason": str(exc)},
        ) from exc

    manifest_document = read_render_manifest(job_dir)
    project_version = None
    if project_readiness is not None:
        try:
            project_version = _project_store().load_version(
                str(project_readiness["project_id"]),
                int(project_readiness["version_no"]),
            ).model_dump(mode="json")
        except (OSError, ValueError, ProjectNotFoundError, ProjectVersionNotFoundError) as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "download_not_ready",
                    "status": "project_version_unavailable",
                    "reason": "current project version is unavailable for export provenance",
                },
            ) from exc

    results: list[dict[str, Any]] = []
    staging_dir = job_dir / f".exports-{uuid.uuid4().hex}"
    try:
        for spec in specs:
            result = run_export(
                spec.kind,
                job_dir=job_dir,
                output_dir=staging_dir,
                manifest_document=manifest_document,
                version=project_version,
                budget=get_budget_status(
                    project_id=(
                        str(project_readiness["project_id"])
                        if project_readiness is not None
                        else None
                    )
                ),
            )
            results.append(result)
    except ExportInputMissing as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise HTTPException(
            status_code=422,
            detail={"error": "export_input_missing", "reason": str(exc)},
        ) from exc
    export_dir = job_dir / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    try:
        for staged_path in staging_dir.iterdir():
            if staged_path.is_file():
                os.replace(staged_path, export_dir / staged_path.name)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
    for result in results:
        result["path"] = str(export_dir / result["filename"])
        result["url"] = f"/api/jobs/{job_id}/exports/{result['filename']}"
    return {"success": True, "job_id": job_id, "exports": results}


@app.get("/api/jobs/{job_id}/exports/{filename}")
def download_job_export(job_id: str, filename: str):
    if not is_valid_job_id(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID")
    if Path(filename).name != filename or filename in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid export filename")
    matching_spec = next(
        (
            spec
            for item in list_export_kinds()
            for spec in [resolve_export_spec(item["kind"])]
            if f"{spec.kind}{spec.extension}" == filename
        ),
        None,
    )
    if matching_spec is None:
        raise HTTPException(status_code=404, detail="Export not found")
    job_dir = _safe_job_path(JOBS_ROOT / job_id)
    if job_dir is None:
        raise HTTPException(status_code=403, detail="Forbidden path")
    export_path = _safe_job_path(job_dir / "exports" / filename)
    if export_path is None or not export_path.is_file():
        raise HTTPException(status_code=404, detail="Export not found")
    # Re-check at download time: an artifact may have been materialized for an
    # older head or before its human-acceptance record was revoked.
    _require_job_download_ready(job_id, job_dir)
    return FileResponse(
        path=str(export_path),
        media_type=matching_spec.media_type,
        filename=filename,
    )


@app.get("/api/jobs/{job_id}/manifest")
def get_job_manifest(job_id: str):
    if not is_valid_job_id(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID")
    job_dir = _safe_job_path(JOBS_ROOT / job_id)
    if job_dir is None:
        raise HTTPException(status_code=403, detail="Forbidden path")
    try:
        document = read_render_manifest(job_dir)
        if document is None:
            raise HTTPException(status_code=404, detail="Render manifest not found")
        return {
            "success": True,
            "job_id": job_id,
            "manifest": document,
            "verification": verify_render_manifest(job_dir, document),
        }
    except HTTPException:
        raise
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail="Render manifest is unreadable") from exc


@app.get("/api/telemetry")
def get_telemetry_summary():
    summary = get_all_telemetry_summary(job_roots=(JOBS_ROOT, SCRIPT_JOBS_ROOT))
    delivery = summary.get("delivery") if isinstance(summary.get("delivery"), dict) else {}
    cloud_connected = delivery.get("cloud_connected") is True
    # Keep the local mirror and optional remote sink as separate facts.  In
    # particular, ``local_mirror`` must never be presented as ClickHouse Cloud.
    summary.update(
        {
            "source": "clickhouse" if cloud_connected else "local_mirror",
            "cloud": {
                "connected": cloud_connected,
                "status": "connected" if cloud_connected else "unavailable",
                "label": "remote_clickhouse" if cloud_connected else "not_configured",
            },
        }
    )
    return summary


def _local_telemetry_events() -> list[dict[str, Any]]:
    """Read only durable outbox identities for reconciliation reporting."""

    try:
        return [event.to_log_record() for event in get_outbox(TELEMETRY_ROOT).events()]
    except (OSError, ValueError, RuntimeError):
        return []


@app.get("/api/telemetry/reconciliation")
def get_telemetry_reconciliation():
    """Expose local outbox completeness and optional remote sink truth."""

    events = _local_telemetry_events()
    try:
        delivery = get_telemetry_delivery_status(TELEMETRY_ROOT)
    except (OSError, ValueError, RuntimeError):
        delivery = {
            "pending": None,
            "failed": None,
            "delivered": None,
            "total": None,
            "schema_ready": None,
            "cloud_connected": False,
            "drain_blocked_reason": "telemetry_delivery_unavailable",
            "ingestion_lag_seconds": None,
            "corrupted": True,
        }
    report = build_reconciliation_report(
        events,
        job_records=(),
        provider_usage=(),
        clickhouse_aggregates=(),
        source_events={"canonical": events, "outbox": events},
    )
    cloud_connected = delivery.get("cloud_connected") is True
    return {
        "success": True,
        "source": "local_mirror",
        "local_mirror": {
            "available": True,
            "label": "local_mirror",
            "event_count": len(events),
        },
        "cloud": {
            "connected": cloud_connected,
            "schema_ready": delivery.get("schema_ready"),
            "status": "connected" if cloud_connected else "unavailable",
            "label": "remote_clickhouse" if cloud_connected else "not_configured",
        },
        "outbox": delivery,
        "reconciliation": report["reconciliation"],
        "completeness": report["completeness"],
        "integrity": report["integrity"],
        "freshness": report["freshness"],
    }


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


def _proposal_store(store: FileProjectStore | None = None) -> FileProposalStore:
    """Proposal records share the configured project root, not a second authority."""
    project_store = store or _project_store()
    return FileProposalStore(project_store.root)


def _proposal_diff(mapping: chat.ChatCommandMapping, head: ProjectVersion) -> ProposalDiff:
    """Build a JSON-safe, field-level diff for the review card."""
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    affected_fields: list[str] = []
    by_id = {segment.id: segment for segment in head.script.segments}
    payload = mapping.command.payload

    if payload is not None and payload.kind == "script_segments":
        for replacement in payload.segments:
            current = by_id.get(replacement.id)
            if current is None:
                continue
            changed_before: dict[str, Any] = {}
            changed_after: dict[str, Any] = {}
            for field in (
                "text",
                "caption",
                "visual_action",
                "voice",
                "duration_seconds",
            ):
                before_value = getattr(current, field, None)
                after_value = getattr(replacement, field, None)
                if before_value != after_value:
                    changed_before[field] = before_value
                    changed_after[field] = after_value
                    affected_fields.append(f"{replacement.id}.{field}")
            if changed_before:
                before[replacement.id] = changed_before
                after[replacement.id] = changed_after
    elif payload is not None and payload.kind == "segment_timings":
        existing = {timing.segment_id: timing for timing in head.segment_timings}
        for replacement in payload.timings:
            current = existing.get(replacement.segment_id)
            before[replacement.segment_id] = (
                current.model_dump(mode="json") if current is not None else None
            )
            after[replacement.segment_id] = replacement.model_dump(mode="json")
            affected_fields.append(f"{replacement.segment_id}.timing")
    elif payload is not None and payload.kind == "surface":
        before["surface"] = head.surface.model_dump(mode="json") if head.surface else None
        after["surface"] = payload.surface.model_dump(mode="json")
        affected_fields.append("surface")
    elif payload is not None and payload.kind == "render_request":
        before["render_manifest"] = (
            head.render_manifest.model_dump(mode="json") if head.render_manifest else None
        )
        after["render_manifest"] = (
            payload.manifest.model_dump(mode="json") if payload.manifest else None
        )
        affected_fields.append("render_manifest")

    if not affected_fields:
        affected_fields.append(mapping.operation)
    return ProposalDiff(
        operation=mapping.command.operation,
        summary=mapping.summary,
        affected_fields=affected_fields,
        before=before,
        after=after,
    )


def _proposal_response(
    proposal: ProjectProposal,
    *,
    status_value: str | None = None,
    changeset: Any = None,
) -> dict[str, Any]:
    """Return one stable response shape for fresh decisions and replays."""
    payload = proposal.model_dump(mode="json")
    result: dict[str, Any] = {
        "success": True,
        "status": status_value or proposal.status,
        "proposal_id": proposal.proposal_id,
        "project_id": proposal.project_id,
        "base_version": proposal.base_version,
        "affected_segment_ids": list(proposal.affected_segment_ids),
        "affected_scopes": list(proposal.affected_scopes),
        "diff": payload["diff"],
        "command": payload["command"],
        "proposal": payload,
    }
    if proposal.target_version is not None:
        result["target_version"] = proposal.target_version
    if proposal.reason:
        result["reason"] = proposal.reason
    if changeset is not None:
        result["changeset"] = changeset.model_dump(mode="json")
    return result


def _proposal_is_expired(proposal: ProjectProposal) -> bool:
    if not proposal.expires_at:
        return False
    try:
        expires_at = datetime.fromisoformat(proposal.expires_at.replace("Z", "+00:00"))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        # An unreadable expiry is not usable authority: fail closed as expired.
        return True
    return datetime.now(timezone.utc) >= expires_at


def _proposal_decision(
    proposal: ProjectProposal,
    *,
    status_value: Literal["rejected", "expired", "revoked"],
    actor: str,
    reason: str,
) -> ProjectProposal:
    now = utc_now_iso()
    return proposal.model_copy(
        update={
            "status": status_value,
            "updated_at": now,
            "decision_actor": actor,
            "decision_at": now,
            "reason": reason,
        }
    )


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_project_context(job_dir: Path) -> dict[str, Any] | None:
    """Read the optional project dispatch context without widening its shape."""

    context_path = job_dir / "project_context.json"
    if not context_path.is_file() or context_path.is_symlink():
        return None
    try:
        payload = json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("project render context is unreadable") from exc
    if not isinstance(payload, dict):
        raise ValueError("project render context must be an object")
    return payload


def _project_media_readiness(
    project_id: str,
    version_no: int,
    *,
    version: ProjectVersion | None = None,
) -> dict[str, Any]:
    """Verify that a project's exact current version owns a real render.

    Project versions contain references, not media bytes.  A render is current
    only when its job context names the same project and its dispatch parent,
    its completed job contains a non-empty video and a verified manifest, and
    the version's video reference hash agrees with the bytes on disk.
    """

    result: dict[str, Any] = {
        "project_id": project_id,
        "version_no": version_no,
        "current_version": None,
        "current": False,
        "video_current": False,
        "video_present": False,
        "manifest_present": False,
        "manifest_verified": False,
        "job_id": None,
        "status": "media_unavailable",
        "reason": "a current project video and verified manifest are required",
    }
    store = _project_store()
    try:
        if not store.project_exists(project_id):
            result.update({"status": "project_not_found", "reason": "project not found"})
            return result
        current_version = store.current_version_no(project_id)
        result["current_version"] = current_version
        result["current"] = version_no == current_version
        if not result["current"]:
            result.update({
                "status": "stale_project_version",
                "reason": "acceptance is not bound to the current project head",
            })
            return result
        resolved_version = version or store.load_version(project_id, version_no)
    except (ProjectNotFoundError, ProjectVersionNotFoundError, InvalidProjectIdError):
        result.update({"status": "project_not_found", "reason": "project version not found"})
        return result

    video_assets = [asset for asset in resolved_version.asset_references if asset.kind == "video"]
    if len(video_assets) != 1:
        result.update({
            "status": "video_reference_missing",
            "reason": "exactly one current project video reference is required",
        })
        return result
    video_asset = video_assets[0]
    match = re.fullmatch(r"job:([0-9a-f]{8}):video", video_asset.asset_id)
    if match is None:
        result.update({
            "status": "video_reference_invalid",
            "reason": "current video reference is not bound to a render job",
        })
        return result
    job_id = match.group(1)
    result["job_id"] = job_id
    job_dir = _safe_job_path(JOBS_ROOT / job_id)
    if job_dir is None or not job_dir.is_dir():
        result.update({"status": "video_missing", "reason": "current render job is unavailable"})
        return result

    try:
        context = _read_project_context(job_dir)
        status_data = read_job_status(job_dir)
    except (OSError, ValueError, FileNotFoundError) as exc:
        result.update({"status": "media_unavailable", "reason": str(exc)})
        return result
    if not context or context.get("project_id") != project_id:
        result.update({
            "status": "video_reference_unbound",
            "reason": "render job is not bound to this project",
        })
        return result
    # An attached result is always the child of the version it was dispatched
    # against. This prevents an inherited asset from appearing current after a
    # later edit or an unrelated job is manually copied into a project.
    if context.get("dispatched_version") != resolved_version.parent_version:
        result.update({
            "status": "video_version_mismatch",
            "reason": "render job was dispatched against a different project version",
        })
        return result
    video_path = job_dir / "video.mp4"
    if status_data.get("status") != "completed" or not video_path.is_file() or video_path.stat().st_size < 1:
        result.update({"status": "video_missing", "reason": "a completed non-empty video is required"})
        return result
    result["video_present"] = True
    actual_hash = _sha256_path(video_path)
    if video_asset.sha256 and video_asset.sha256 != actual_hash:
        result.update({"status": "video_hash_mismatch", "reason": "current video reference hash does not match video.mp4"})
        return result

    try:
        manifest_document = read_render_manifest(job_dir)
    except (OSError, ValueError):
        manifest_document = None
    if manifest_document is None:
        result.update({"status": "manifest_missing", "reason": "a persisted render manifest is required"})
        return result
    result["manifest_present"] = True
    try:
        verification = verify_render_manifest(job_dir, manifest_document)
    except (OSError, ValueError):
        verification = {"verified": False}
    result["manifest_verified"] = verification.get("verified") is True
    result["manifest_fingerprint"] = manifest_document.get("manifest_fingerprint")
    persisted_manifest = manifest_document.get("render_manifest")
    if not result["manifest_verified"]:
        result.update({"status": "manifest_unverified", "reason": "render manifest integrity verification failed"})
        return result
    if resolved_version.render_manifest is None or persisted_manifest != resolved_version.render_manifest.model_dump(mode="json"):
        result.update({
            "status": "manifest_version_mismatch",
            "reason": "current version does not contain the persisted render manifest",
        })
        return result
    recorded_hash = manifest_document.get("video_sha256")
    if recorded_hash is not None and recorded_hash != actual_hash:
        result.update({"status": "video_hash_mismatch", "reason": "render manifest video seal does not match video.mp4"})
        return result
    result.update({"video_current": True, "status": "ready", "reason": None})
    return result


def _server_qa_for_project_version(
    project_id: str,
    version_no: int,
    *,
    media: dict[str, Any],
) -> dict[str, Any] | None:
    """Load the deterministic QA report owned by the current render job.

    Human acceptance must be based on evidence produced by the server's
    pipeline, not on a report copied into the acceptance request.  The media
    readiness result has already proved that the job is bound to this project's
    current version and that its video/manifest are current; this helper adds
    the final job-local QA identity check.
    """

    if (
        media.get("project_id") != project_id
        or media.get("version_no") != version_no
        or media.get("current") is not True
        or media.get("video_current") is not True
        or media.get("manifest_verified") is not True
    ):
        return None
    job_id = media.get("job_id")
    if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{8}", job_id):
        return None
    job_dir = _safe_job_path(JOBS_ROOT / job_id)
    if job_dir is None or not job_dir.is_dir():
        return None
    report_path = job_dir / "qa_report.json"
    try:
        if report_path.is_symlink() or not report_path.is_file() or report_path.stat().st_size == 0:
            return None
    except OSError:
        return None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(report, dict):
        return None
    identity = report.get("qa_identity")
    if report.get("job_id") != job_id or not isinstance(identity, dict):
        return None
    if identity.get("job_id") != job_id or identity.get("report_name") != "qa_report":
        return None
    return report


def _project_download_readiness(project_id: str, version_no: int) -> dict[str, Any]:
    """Combine automated QA, exact human acceptance and current media truth."""

    media = _project_media_readiness(project_id, version_no)
    server_qa = _server_qa_for_project_version(
        project_id,
        version_no,
        media=media,
    )
    # Passing an explicit (invalid) object prevents final_acceptance_readiness
    # from falling back to a previously stored client-supplied report.  A
    # missing current server report therefore remains blocked even if an older
    # acceptance record contains a structurally valid payload.
    qa_for_readiness = server_qa if server_qa is not None else {"passed": False}
    try:
        acceptance = final_acceptance_readiness(
            LOCKS_ROOT,
            project_id,
            version_no,
            automated_qa=qa_for_readiness,
        )
    except (OSError, ValueError):
        acceptance = {
            "project_id": project_id,
            "version_no": version_no,
            "automated_qa_passed": False,
            "human_accepted": False,
            "download_ready": False,
            "status": "acceptance_unavailable",
            "reason": "human acceptance record is unreadable",
        }
    ready = bool(acceptance.get("download_ready") and media.get("video_current") and media.get("manifest_verified"))
    combined = dict(acceptance)
    combined.update({
        "media": media,
        "video_current": bool(media.get("video_current")),
        "manifest_verified": bool(media.get("manifest_verified")),
        "download_ready": ready,
    })
    if server_qa is not None:
        combined["automated_qa"] = server_qa
        combined["qa_fingerprint"] = server_qa.get("fingerprint")
    if not ready and media.get("status") not in {None, "ready"}:
        combined["status"] = media.get("status")
        combined["reason"] = media.get("reason")
    return combined


def _job_project_readiness(job_id: str, job_dir: Path) -> dict[str, Any] | None:
    """Return project-bound readiness for a job, or ``None`` for legacy jobs."""

    context = _read_project_context(job_dir)
    if context is None:
        return None
    project_id = context.get("project_id")
    dispatched_version = context.get("dispatched_version")
    if not isinstance(project_id, str) or not is_valid_job_id(project_id):
        return {
            "project_id": project_id,
            "version_no": dispatched_version,
            "download_ready": False,
            "status": "project_context_invalid",
            "reason": "project render context has an invalid project_id",
        }
    if isinstance(dispatched_version, bool) or not isinstance(dispatched_version, int) or dispatched_version < 1:
        return {
            "project_id": project_id,
            "version_no": dispatched_version,
            "download_ready": False,
            "status": "project_context_invalid",
            "reason": "project render context has an invalid dispatched_version",
        }
    store = _project_store()
    try:
        current_version = store.current_version_no(project_id)
    except (InvalidProjectIdError, ProjectNotFoundError):
        current_version = 0
    if current_version < 1:
        return {
            "project_id": project_id,
            "version_no": dispatched_version,
            "current_version": current_version,
            "download_ready": False,
            "status": "project_not_found",
            "reason": "project render context points to a missing project",
        }
    readiness = _project_download_readiness(project_id, current_version)
    readiness["job_id"] = job_id
    readiness["dispatched_version"] = dispatched_version
    return readiness


def _require_job_download_ready(job_id: str, job_dir: Path) -> dict[str, Any] | None:
    """Enforce the final gate for project-bound video/export downloads."""

    try:
        readiness = _job_project_readiness(job_id, job_dir)
    except (OSError, ValueError, ProjectNotFoundError, ProjectVersionNotFoundError):
        readiness = {
            "download_ready": False,
            "status": "project_context_unavailable",
            "reason": "project render context or current version is unreadable",
        }
    if readiness is not None and readiness.get("download_ready") is not True:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "download_not_ready",
                "status": readiness.get("status"),
                "reason": readiness.get("reason") or "automated QA, current media and human acceptance are required",
                "readiness": readiness,
            },
        )
    return readiness


def _attach_project_result_record(project_id: str, req: "ResultRequest") -> dict[str, Any]:
    """Apply a completed result without crossing the public HTTP/auth seam."""
    store = _project_store()
    with store.transaction(project_id):
        if not store.project_exists(project_id):
            raise ProjectNotFoundError(project_id)
        head_no = store.current_version_no(project_id)
        if req.dispatched_version != head_no:
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
        merged = {a.asset_id: a.model_dump(mode="json") for a in head.asset_references}
        for asset in req.asset_references:
            merged[asset.asset_id] = asset.model_dump(mode="json")
        overrides: dict[str, Any] = {"asset_references": list(merged.values())}
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
    return {
        "success": True,
        "status": "attached",
        "applied": True,
        "dispatched_version": req.dispatched_version,
        "target_version": new_version.version_no,
        "version": new_version.model_dump(mode="json"),
    }


def _attach_completed_project_result(job_id: str, job_dir: Path) -> dict[str, Any] | None:
    """Attach a successful queued render to its originating project version.

    The project context is written before dispatch. If an edit advanced the
    project while the job was running, the shared result helper isolates the
    stale output rather than overwriting that edit.
    """
    context_path = job_dir / "project_context.json"
    if not context_path.is_file():
        return None
    status_data = read_job_status(job_dir)
    if status_data.get("status") != "completed":
        return None
    context = json.loads(context_path.read_text(encoding="utf-8"))
    project_id = context.get("project_id")
    dispatched_version = context.get("dispatched_version")
    if not isinstance(project_id, str) or not is_valid_job_id(project_id):
        raise ValueError("project render context has an invalid project_id")
    if not isinstance(dispatched_version, int) or dispatched_version < 1:
        raise ValueError("project render context has an invalid dispatched_version")
    video = job_dir / "video.mp4"
    if not video.is_file() or video.stat().st_size < 1:
        raise FileNotFoundError("completed project render has no video.mp4")

    render_manifest = None
    manifest_path = job_dir / "render_manifest.json"
    if manifest_path.is_file():
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        render_manifest = RenderManifest.model_validate(document.get("render_manifest"))
        recorded_hash = document.get("video_sha256")
        actual_hash = _sha256_path(video)
        if recorded_hash is not None and recorded_hash != actual_hash:
            raise ValueError("completed video hash does not match render manifest")
    else:
        actual_hash = _sha256_path(video)

    return _attach_project_result_record(
        project_id,
        ResultRequest(
            dispatched_version=dispatched_version,
            actor="generation",
            asset_references=[
                AssetReference(
                    asset_id=f"job:{job_id}:video",
                    kind="video",
                    uri=f"/api/jobs/{job_id}/video",
                    sha256=actual_hash,
                )
            ],
            render_manifest=render_manifest,
            idempotency_key=f"attach:{context.get('idempotency_key') or job_id}",
        ),
    )


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


PROJECT_RENDER_ESTIMATE_USD = 0.06


class ProjectRenderRequest(BaseModel):
    """Explicit owner approval plus the accessible render controls."""

    model_config = ConfigDict(extra="forbid")

    base_version: int = Field(ge=1)
    approved_spend_usd: float = Field(gt=0, le=100)
    actor: str = Field(default="creative-director", min_length=1, max_length=80)
    cta_text: str = Field(default="", max_length=80)
    retention_progress_bar: bool = Field(default=True, strict=True)
    animated_lower_thirds: bool = Field(default=True, strict=True)
    aspect_ratio: Literal["9:16", "16:9", "1:1"] = "9:16"
    reduced_motion: bool = Field(default=False, strict=True)


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


class HumanAcceptanceRequest(BaseModel):
    """Owner decision for one immutable project version."""

    model_config = ConfigDict(extra="forbid")

    actor: str = Field(default="creative-director", min_length=1, max_length=80)
    accepted: bool = Field(strict=True)
    automated_qa: dict[str, Any] | None = None
    qa_fingerprint: str | None = Field(default=None, min_length=1, max_length=256)
    note: str | None = Field(default=None, max_length=1000)


def _acceptance_response(project_id: str, version_no: int) -> dict[str, Any]:
    """Return the persisted decision plus the composed readiness truth."""

    return {
        "success": True,
        "project_id": project_id,
        "version_no": version_no,
        "acceptance": read_human_acceptance(LOCKS_ROOT, project_id, version_no),
        "readiness": _project_download_readiness(project_id, version_no),
    }


@app.post("/api/projects/{project_id}/versions/{version_no}/acceptance")
@app.post("/api/projects/{project_id}/versions/{version_no}/human-acceptance")
def set_project_version_acceptance(
    project_id: str,
    version_no: int,
    req: HumanAcceptanceRequest,
    request: Request,
):
    """Persist an explicit owner acceptance/rejection for one version.

    Acceptance is not itself a media approval: the response includes the
    composed readiness result, which remains false until deterministic QA,
    current verified manifest/video and this exact decision all agree.
    """

    _enforce_public_access_token(request)
    if not is_valid_job_id(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    if version_no < 1:
        raise HTTPException(status_code=400, detail="version_no must be >= 1")
    store = _project_store()
    try:
        version = store.load_version(project_id, version_no)
    except (InvalidProjectIdError, ProjectNotFoundError, ProjectVersionNotFoundError):
        raise HTTPException(status_code=404, detail="Project version not found")
    server_qa: dict[str, Any] | None = None
    if req.accepted:
        media = _project_media_readiness(project_id, version_no, version=version)
        server_qa = _server_qa_for_project_version(
            project_id,
            version_no,
            media=media,
        )
        if server_qa is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "automated_qa_unavailable",
                    "reason": "a current server-owned deterministic QA report is required",
                    "readiness": _project_download_readiness(project_id, version_no),
                },
            )
        if req.qa_fingerprint is not None and req.qa_fingerprint != server_qa.get("fingerprint"):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "qa_fingerprint_mismatch",
                    "reason": "the supplied QA fingerprint does not match the current server report",
                },
            )
    try:
        set_human_acceptance(
            LOCKS_ROOT,
            project_id,
            version_no,
            actor=req.actor,
            accepted=req.accepted,
            automated_qa=server_qa,
            qa_fingerprint=server_qa.get("fingerprint") if server_qa is not None else None,
            note=req.note,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_human_acceptance", "reason": str(exc)},
        ) from exc
    return _acceptance_response(project_id, version_no)


@app.get("/api/projects/{project_id}/versions/{version_no}/acceptance")
@app.get("/api/projects/{project_id}/versions/{version_no}/human-acceptance")
def get_project_version_acceptance(project_id: str, version_no: int):
    """Read one version-bound acceptance decision and composed readiness."""

    if not is_valid_job_id(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    if version_no < 1:
        raise HTTPException(status_code=400, detail="version_no must be >= 1")
    store = _project_store()
    try:
        store.load_version(project_id, version_no)
    except (InvalidProjectIdError, ProjectNotFoundError, ProjectVersionNotFoundError):
        raise HTTPException(status_code=404, detail="Project version not found")
    return _acceptance_response(project_id, version_no)


@app.post(
    "/api/projects/{project_id}/render",
    status_code=status.HTTP_202_ACCEPTED,
)
async def render_project(
    project_id: str,
    req: ProjectRenderRequest,
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Approve, version and dispatch one real project render.

    The caller's click supplies an explicit spend ceiling. The server owns the
    estimate, records the approval, commits a request_render version, and then
    uses the same durable queue and real pipeline as /api/generate-video.
    """
    _enforce_public_generation_access(request)
    if not is_valid_job_id(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    idempotency_key = _client_idempotency_key(request)
    if not idempotency_key:
        raise HTTPException(
            status_code=400,
            detail={"error": "idempotency_key_required", "header": "X-FYF-Idempotency-Key"},
        )

    # Replay is resolved before the stale-version check: a network retry of a
    # completed render must return its original job, not create another version.
    existing = _job_queue().resolve(f"video:{idempotency_key}")
    if existing is not None and existing.job_id:
        context_path = JOBS_ROOT / existing.job_id / "project_context.json"
        try:
            replay_context = _read_project_context(context_path.parent)
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "idempotency_conflict",
                    "reason": "existing render idempotency record has unreadable project context",
                },
            ) from exc
        if not replay_context or replay_context.get("project_id") != project_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "idempotency_conflict",
                    "reason": "idempotency key is already bound to a different project render",
                },
            )
        dispatched_version = replay_context.get("dispatched_version")
        return {
            "success": True,
            "status": "replayed",
            "job_id": existing.job_id,
            "status_url": f"/api/jobs/{existing.job_id}/status",
            "dispatched_version": dispatched_version,
            "estimated_cost_usd": PROJECT_RENDER_ESTIMATE_USD,
            "approved_spend_usd": req.approved_spend_usd,
            "approval_id": f"project-render:{idempotency_key}",
            "approval_target": f"{project_id}@v{dispatched_version}",
        }

    store = _project_store()
    if not store.project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    head_no = store.current_version_no(project_id)
    if req.base_version != head_no:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "stale_version",
                "base_version": req.base_version,
                "current_version": head_no,
                "hint": "re-fetch the current project head before approving a render",
            },
        )
    if req.approved_spend_usd < PROJECT_RENDER_ESTIMATE_USD:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "approval_below_estimate",
                "estimated_cost_usd": PROJECT_RENDER_ESTIMATE_USD,
                "approved_spend_usd": req.approved_spend_usd,
            },
        )

    approval_id = f"project-render:{idempotency_key}"
    approval_target = f"{project_id}@v{head_no}"
    record_approval(
        "request_render",
        approved_spend_usd=req.approved_spend_usd,
        decision="approved",
        actor=req.actor,
        target_ref=approval_target,
        project_id=project_id,
        approval_id=approval_id,
        idempotency_key=idempotency_key,
    )
    command = ProjectCommand.model_validate(
        {
            "project_id": project_id,
            "base_version": head_no,
            "actor": req.actor,
            "operation": "request_render",
            "selection": {"kind": "all"},
            "payload": {
                "kind": "render_request",
                "estimated_cost_usd": PROJECT_RENDER_ESTIMATE_USD,
            },
            "idempotency_key": idempotency_key,
        }
    )
    try:
        changeset = apply_command(
            store,
            command,
            lock_checker=granular_lock_checker(LOCKS_ROOT),
        )
    except ApprovalRequiredError as exc:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "approval_required",
                "operation": exc.operation,
                "reason": exc.reason,
                "budget_status": exc.budget_status,
            },
        ) from exc
    except StaleVersionError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "stale_version",
                "base_version": exc.base_version,
                "current_version": exc.current_version,
            },
        ) from exc
    except Exception:
        try:
            revoke_approval(
                approval_id,
                actor=req.actor,
                reason="render_validation_failed",
            )
        except Exception:
            logger.exception("Could not revoke failed project render approval %s", approval_id)
        raise

    dispatched_version = changeset.target_version
    if dispatched_version is None:
        raise HTTPException(status_code=500, detail="Render request did not create a version")
    version = store.load_version(project_id, dispatched_version)
    script = version.script.model_dump(mode="json")
    lock_id = create_script_lock(LOCKS_ROOT, script)
    config = version.pinned_production_config
    video_request = VideoRequest(
        lock_id=lock_id,
        style=config.style_id or version.script.style_applied or "fyf_explainer",
        studio_name=version.script.studio_name or "FYF Studio",
        language=config.language or version.script.language,
        genre=version.script.genre or "explainer",
        presenter_mode=version.script.presenter_mode or "on_screen",
        voice_actor=config.voice_actor or version.script.voice_actor or "Sadaltager",
        cta_text=req.cta_text,
        retention_progress_bar=req.retention_progress_bar,
        animated_lower_thirds=req.animated_lower_thirds,
        aspect_ratio=req.aspect_ratio,
        reduced_motion=req.reduced_motion,
    )
    claimed = False
    try:
        begin_approval(
            approval_id,
            operation="request_render",
            project_id=project_id,
            target_ref=approval_target,
            amount_usd=PROJECT_RENDER_ESTIMATE_USD,
        )
        claimed = True
        queued = await generate_video(
            video_request,
            request,
            background_tasks,
            project_id=project_id,
        )
        if not queued.job_id:
            raise HTTPException(status_code=500, detail=queued.error or "Failed to queue render")
        write_json_atomically(
            JOBS_ROOT / queued.job_id / "project_context.json",
            {
                "project_id": project_id,
                "dispatched_version": dispatched_version,
                "idempotency_key": idempotency_key,
            },
        )
        finalize_approval(approval_id)
    except HTTPException:
        if claimed:
            try:
                rollback_approval(approval_id, reason="render_enqueue_failed")
            except Exception:
                logger.exception("Could not rollback project render approval %s", approval_id)
        raise
    except Exception:
        if claimed:
            try:
                rollback_approval(approval_id, reason="render_enqueue_failed")
            except Exception:
                logger.exception("Could not rollback project render approval %s", approval_id)
        logger.exception("Project render queueing failed")
        raise HTTPException(status_code=500, detail="Failed to queue render")
    return {
        "success": True,
        "status": "queued",
        "job_id": queued.job_id,
        "status_url": queued.status_url,
        "dispatched_version": dispatched_version,
        "estimated_cost_usd": PROJECT_RENDER_ESTIMATE_USD,
        "approved_spend_usd": req.approved_spend_usd,
        "approval_id": approval_id,
        "approval_target": approval_target,
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
            store, command, rebase=rebase, lock_checker=_granular_checker()
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
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "idempotency_conflict",
                "project_id": exc.project_id,
                "idempotency_key": exc.idempotency_key,
                "hint": "use a new idempotency key for a different command",
            },
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


class SceneEditRequest(BaseModel):
    """Validated replacement fields for one or more selected scenes."""

    model_config = ConfigDict(extra="forbid")

    base_version: int = Field(ge=1)
    scene_ids: list[str] = Field(min_length=1)
    actor: str = Field(default="canvas", min_length=1, max_length=80)
    text: Optional[str] = Field(default=None, max_length=5000)
    visual_action: Optional[str] = Field(default=None, max_length=2000)
    caption: Optional[str] = Field(default=None, max_length=500)
    voice: Optional[str] = Field(default=None, max_length=500)
    duration_seconds: Optional[float] = Field(default=None, gt=0, le=600)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("scene_ids")
    @classmethod
    def _clean_scene_ids(cls, value: list[str]) -> list[str]:
        cleaned = [scene_id.strip() for scene_id in value]
        if any(not scene_id for scene_id in cleaned):
            raise ValueError("scene_ids items must not be blank")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("scene_ids must be unique")
        return cleaned

    @field_validator("text", "visual_action", "caption", "voice")
    @classmethod
    def _clean_optional_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("scene edit text fields must not be blank")
        return cleaned

    @field_validator("duration_seconds")
    @classmethod
    def _finite_duration(cls, value: Optional[float]) -> Optional[float]:
        if value is not None and not math.isfinite(value):
            raise ValueError("duration_seconds must be finite")
        return value

    @model_validator(mode="after")
    def _at_least_one_edit(self) -> "SceneEditRequest":
        fields = ("text", "visual_action", "caption", "voice", "duration_seconds")
        if not any(field in self.model_fields_set for field in fields):
            raise ValueError("scene edit requires at least one editable field")
        return self


class SceneRegenerationRequest(BaseModel):
    """A no-provider planning request bound to one exact project head."""

    model_config = ConfigDict(extra="forbid")

    base_version: int = Field(ge=1)
    scene_ids: list[str] = Field(min_length=1)
    actor: str = Field(default="creative-director", min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=1, max_length=128)
    per_segment_voice: bool = Field(default=True, strict=True)
    renderer_identity: Optional[str] = None
    voice_identity: Optional[str] = None
    visual_policy: Optional[str] = None

    @field_validator("scene_ids")
    @classmethod
    def _clean_scene_ids(cls, value: list[str]) -> list[str]:
        cleaned = [scene_id.strip() for scene_id in value]
        if any(not scene_id for scene_id in cleaned):
            raise ValueError("scene_ids items must not be blank")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("scene_ids must be unique")
        return cleaned


def _scene_edit_command(
    project_id: str,
    request: SceneEditRequest,
    head: ProjectVersion,
) -> ProjectCommand:
    """Translate a scene patch into one atomic canonical ProjectCommand."""

    requested_fields = {
        field
        for field in ("text", "visual_action", "caption", "voice", "duration_seconds")
        if field in request.model_fields_set
    }
    if not requested_fields:
        # The request model catches this in normal HTTP use. Keep this helper
        # safe for direct unit callers as well.
        raise ValueError("scene edit requires at least one editable field")
    selected_ids = set(request.scene_ids)
    replacements: list[ScriptSegment] = []
    for segment in head.script.segments:
        if segment.id not in selected_ids:
            continue
        data = segment.model_dump(mode="json")
        for field in requested_fields:
            data[field] = getattr(request, field)
        # Re-validate each replacement before constructing the command. This
        # keeps blank captions, invalid durations and future field changes out
        # of the command payload, while the command application still performs
        # the final all-scenes validation atomically.
        # Keep the validated model (rather than dumping to JSON first) so an
        # explicit ``caption=None`` or ``voice=None`` remains distinguishable
        # from an omitted field for scene-lock scope detection.
        replacements.append(ScriptSegment.model_validate(data))

    operation: str
    if requested_fields == {"visual_action"}:
        operation = "edit_visual"
    elif requested_fields == {"voice"}:
        operation = "edit_voice"
    elif requested_fields == {"duration_seconds"}:
        operation = "edit_timing"
    elif requested_fields <= {"text", "caption"}:
        operation = "edit_script"
    else:
        operation = "edit_scene"

    return ProjectCommand.model_validate(
        {
            "project_id": project_id,
            "base_version": request.base_version,
            "actor": request.actor,
            "operation": operation,
            "selection": {"kind": "scene", "scene_ids": request.scene_ids},
            "payload": {"kind": "script_segments", "segments": replacements},
            "idempotency_key": request.idempotency_key,
        }
    )


def _scene_command_http_error(exc: Exception, project_id: str) -> HTTPException:
    if isinstance(exc, StaleVersionError):
        return HTTPException(
            status_code=409,
            detail={
                "error": "stale_version",
                "project_id": exc.project_id,
                "base_version": exc.base_version,
                "current_version": exc.current_version,
                "hint": "re-fetch the current project head before editing a scene",
            },
        )
    if isinstance(exc, LockConflictError):
        return HTTPException(
            status_code=409,
            detail={
                "error": "lock_conflict",
                "project_id": project_id,
                "conflicts": exc.conflicts,
            },
        )
    if isinstance(exc, IdempotencyConflictError):
        return HTTPException(
            status_code=409,
            detail={
                "error": "idempotency_conflict",
                "project_id": exc.project_id,
                "idempotency_key": exc.idempotency_key,
                "hint": "use a new idempotency key for a different command",
            },
        )
    if isinstance(exc, CommandValidationError):
        return HTTPException(
            status_code=422,
            detail={
                "error": "validation_failed",
                "changeset": exc.changeset.model_dump(mode="json"),
            },
        )
    return HTTPException(status_code=422, detail=str(exc))


def _apply_scene_edit(project_id: str, req: SceneEditRequest, request: Request) -> dict[str, Any]:
    """Apply one validated scene patch through the shared command seam."""

    _enforce_public_access_token(request)
    if not is_valid_job_id(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    store = _project_store()
    try:
        with store.transaction(project_id):
            if not store.project_exists(project_id):
                raise HTTPException(status_code=404, detail="Project not found")
            head_no, head = _load_head_or_404(store, project_id)
            if req.base_version != head_no:
                raise StaleVersionError(project_id, req.base_version, head_no)
            command = _scene_edit_command(project_id, req, head)
            changeset = apply_command(
                store,
                command,
                rebase=False,
                lock_checker=_granular_checker(),
                _lock_held=True,
            )
            target_no = changeset.target_version
            if target_no is None:
                raise RuntimeError("scene edit did not create a version")
            version = store.load_version(project_id, target_no)
    except HTTPException:
        raise
    except (
        StaleVersionError,
        LockConflictError,
        IdempotencyConflictError,
        CommandValidationError,
        ValueError,
    ) as exc:
        raise _scene_command_http_error(exc, project_id) from exc
    except (ProjectNotFoundError, InvalidProjectIdError) as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc
    except Exception:
        logger.exception("Scene edit failed")
        raise HTTPException(status_code=500, detail="Failed to edit scene")
    return {
        "success": True,
        "status": changeset.status,
        "base_version": req.base_version,
        "target_version": target_no,
        "affected_segment_ids": changeset.affected_segment_ids,
        "changeset": changeset.model_dump(mode="json"),
        "version": version.model_dump(mode="json"),
    }


@app.post("/api/projects/{project_id}/scenes/edit")
def edit_project_scenes(project_id: str, req: SceneEditRequest, request: Request):
    """Atomically edit explicit fields on the selected scene ids."""

    return _apply_scene_edit(project_id, req, request)


@app.post("/api/projects/{project_id}/scene/{scene_id}/edit")
def edit_project_scene(project_id: str, scene_id: str, req: SceneEditRequest, request: Request):
    """Singular-scene alias that still validates the body selection atomically."""

    if req.scene_ids != [scene_id]:
        raise HTTPException(status_code=422, detail="scene_id path must match scene_ids body")
    return _apply_scene_edit(project_id, req, request)


def _scene_regeneration_plan(
    project_id: str,
    req: SceneRegenerationRequest,
    store: FileProjectStore,
) -> dict[str, Any]:
    """Build a no-provider recompute plan from one exact immutable head."""

    if not store.project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    head_no = store.current_version_no(project_id)
    if req.base_version != head_no:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "stale_version",
                "project_id": project_id,
                "base_version": req.base_version,
                "current_version": head_no,
                "hint": "re-fetch the current project head before regenerating scenes",
            },
        )
    head = store.load_version(project_id, head_no)
    missing = sorted(set(req.scene_ids) - set(head.segment_ids()))
    if missing:
        raise HTTPException(
            status_code=422,
            detail={"error": "unknown_scene", "scene_ids": missing},
        )
    scene_lock_records = read_scene_locks(store.root, project_id)
    scene_locks = {
        scene_id: list(scopes.keys())
        for scene_id, scopes in scene_lock_records.items()
    }
    timing_map = {
        timing.segment_id: timing.model_dump(mode="json")
        for timing in head.segment_timings
    }
    plan = plan_recompute_for_regeneration(
        head.script.model_dump(mode="json"),
        selected_segment_ids=req.scene_ids,
        segment_timings=timing_map,
        scene_locks=scene_locks,
        locks_root=LOCKS_ROOT,
        project_id=project_id,
        per_segment_voice=req.per_segment_voice,
        renderer_identity=req.renderer_identity,
        voice_identity=req.voice_identity,
        visual_policy=req.visual_policy,
    )
    return {
        "success": True,
        "status": "blocked" if plan.blocked else "planned",
        "project_id": project_id,
        "base_version": req.base_version,
        "current_version": head_no,
        "scene_ids": list(req.scene_ids),
        "idempotency_key": req.idempotency_key,
        "plan": plan.as_dict(),
    }


@app.post("/api/projects/{project_id}/scenes/regenerate", status_code=status.HTTP_202_ACCEPTED)
@app.post("/api/projects/{project_id}/regenerate", status_code=status.HTTP_202_ACCEPTED)
def regenerate_project_scenes(
    project_id: str,
    req: SceneRegenerationRequest,
    request: Request,
):
    """Plan selected-scene descendants without dispatching paid work."""

    _enforce_public_access_token(request)
    if not is_valid_job_id(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    store = _project_store()
    try:
        with store.transaction(project_id):
            return _scene_regeneration_plan(project_id, req, store)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Scene regeneration plan is unavailable") from exc
    except (ProjectNotFoundError, InvalidProjectIdError) as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc


@app.post(
    "/api/projects/{project_id}/scenes/{scene_id}/regenerate",
    status_code=status.HTTP_202_ACCEPTED,
)
@app.post(
    "/api/projects/{project_id}/scene/{scene_id}/regenerate",
    status_code=status.HTTP_202_ACCEPTED,
)
def regenerate_project_scene(
    project_id: str,
    scene_id: str,
    req: SceneRegenerationRequest,
    request: Request,
):
    """Singular-scene regeneration alias with selection/body identity checks."""

    if req.scene_ids != [scene_id]:
        raise HTTPException(status_code=422, detail="scene_id path must match scene_ids body")
    return regenerate_project_scenes(project_id, req, request)


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
    global_checker = granular_lock_checker(LOCKS_ROOT)

    def _checker(version, command):
        conflicts = list(global_checker(version, command))
        if version is None:
            return conflicts

        # The legacy lock_store checker covers global content/visual/timing
        # records. Scene locks are project-local and may cover all four scene
        # scopes, including voice. Resolve the exact selection before checking
        # them so a locked sibling never blocks an unrelated scene edit.
        try:
            selected_ids = resolve_selection(version, command.selection)
        except UnknownSelectionTargetError:
            # Selection validation belongs to apply_command; do not turn an
            # invalid command into a misleading lock error here.
            selected_ids = []
        touched_scopes = command_touched_scopes(version, command)
        if selected_ids and touched_scopes:
            by_scene = scene_lock_conflicts(
                _project_store().root,
                command.project_id,
                selected_ids,
                touched_scopes,
            )
            for scopes in by_scene.values():
                for scope in scopes:
                    if scope not in conflicts:
                        conflicts.append(scope)
        # ``granular_lock_checker`` intentionally knows only the historical
        # three global scopes. Preserve version LockState semantics for the new
        # voice scope as well as for an atomic multi-scope edit.
        if touched_scopes:
            # ``granular_lock_checker`` only receives a single operation scope
            # and therefore intentionally returns no registry conflicts for a
            # mixed ``edit_scene`` command (whose effective scope is ``None``).
            # Resolve the complete changed-field set here so a global lock on
            # any affected scope cannot be bypassed by an atomic scene edit.
            global_scope_locks = read_scope_locks(LOCKS_ROOT, command.project_id)
            for scope in touched_scopes:
                if global_scope_locks.get(scope, {}).get("locked") is True and scope not in conflicts:
                    conflicts.append(scope)
                if bool(getattr(version.locks, scope, False)) and scope not in conflicts:
                    conflicts.append(scope)
        return conflicts

    return _checker


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
    command_fingerprint_value: Optional[str] = None,
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
    data["command_fingerprint"] = command_fingerprint_value
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


def _derived_request_fingerprint(
    *,
    operation: str,
    project_id: str,
    base_version: int,
    actor: str,
    idempotency_key: str,
    target_version: int | None = None,
    variant_name: str | None = None,
) -> str:
    """Fingerprint undo/variant request identity for idempotent replay."""

    payload = {
        "operation": operation,
        "project_id": project_id,
        "base_version": base_version,
        "actor": actor,
        "idempotency_key": idempotency_key,
        "target_version": target_version,
        "variant_name": variant_name,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class ChatRequest(BaseModel):
    """Body for ``POST /api/projects/{id}/chat``."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)
    selection: Optional[Selection] = None
    actor: str = "creative-director"
    base_version: Optional[int] = Field(default=None, ge=0)
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=128)


class ChatProposalRequest(BaseModel):
    """A chat note that is persisted for review instead of applied immediately."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)
    selection: Optional[Selection] = None
    actor: str = Field(default="creative-director", min_length=1, max_length=80)
    base_version: Optional[int] = Field(default=None, ge=1)
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=128)
    expires_at: Optional[str] = Field(default=None, min_length=1, max_length=80)


class ProposalDecisionRequest(BaseModel):
    """Optional decision attribution/reason for reject, expire, or revoke."""

    model_config = ConfigDict(extra="forbid")

    actor: str = Field(default="creative-director", min_length=1, max_length=80)
    reason: Optional[str] = Field(default=None, max_length=500)


class UndoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_version: int = Field(ge=1)
    actor: str = "creative-director"
    # Optional for legacy callers; when supplied it must equal the current
    # head. Omitting it binds the operation to the head read in the transaction.
    base_version: Optional[int] = Field(default=None, ge=1)
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
    # Omit both for a legacy project-wide lock. Supplying either field makes
    # this a scene-scoped lock mutation; ``scene_ids`` is intentionally a list
    # so one atomic request can lock a selected set without partial writes.
    scene_id: Optional[str] = Field(default=None, min_length=1)
    scene_ids: list[str] = Field(default_factory=list)
    locked_by: Optional[str] = None
    reason: Optional[str] = None

    @field_validator("scene_id")
    @classmethod
    def _clean_scene_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("scene_id must not be blank")
        return cleaned

    @field_validator("scene_ids")
    @classmethod
    def _clean_scene_ids(cls, value: list[str]) -> list[str]:
        cleaned = [scene_id.strip() for scene_id in value]
        if any(not scene_id for scene_id in cleaned):
            raise ValueError("scene_ids items must not be blank")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("scene_ids must be unique")
        return cleaned

    @model_validator(mode="after")
    def _scene_id_fields_are_exclusive(self) -> "ScopeLockRequest":
        if self.scene_id is not None and self.scene_ids:
            raise ValueError("provide scene_id or scene_ids, not both")
        return self


class ResultRequest(BaseModel):
    """A generation result dispatched against one specific version (C7)."""

    model_config = ConfigDict(extra="forbid")

    dispatched_version: int = Field(ge=1)
    actor: str = "generation"
    asset_references: list[AssetReference] = Field(default_factory=list)
    render_manifest: Optional[RenderManifest] = None
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=128)


def _proposal_not_found(proposal_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Proposal {proposal_id} not found")


def _proposal_status_replay(proposal: ProjectProposal) -> dict[str, Any]:
    return _proposal_response(proposal, status_value="replayed")


def _recover_committed_proposal(
    store: FileProjectStore,
    proposal_store: FileProposalStore,
    proposal: ProjectProposal,
    actor: str,
) -> dict[str, Any] | None:
    """Finalize a proposal after its command committed but its save failed.

    ``apply_command`` writes the immutable version before the proposal status.
    A process crash in that narrow window leaves a proposed record beside an
    already-applied idempotency key. Recovery must happen before stale-head
    rejection and only for the exact command fingerprint.
    """

    existing = store.find_by_idempotency_key(proposal.project_id, proposal.idempotency_key)
    if existing is None:
        return None
    if existing.command_fingerprint != command_fingerprint(proposal.command):
        raise ProposalConflictError(
            f"idempotency key {proposal.idempotency_key!r} is bound to a different command"
        )
    now = utc_now_iso()
    approved = proposal.model_copy(
        update={
            "status": "approved",
            "updated_at": now,
            "decision_actor": actor,
            "decision_at": now,
            "target_version": existing.version_no,
            "reason": "approved (recovered after command commit)",
        }
    )
    proposal_store.save(approved)
    return _proposal_response(approved, status_value="approved")


def _proposal_request_matches(
    store: FileProjectStore, proposal: ProjectProposal, req: ChatProposalRequest
) -> bool:
    """Check that an idempotency replay carries the same command identity.

    A key replay must not silently turn a changed chat message, actor, base
    version, or expiry into the original proposal.  Reconstructing against the
    proposal's immutable base version keeps this check valid even after approval
    advances the current head.
    """
    if proposal.actor != req.actor or proposal.expires_at != req.expires_at:
        return False
    if req.base_version is not None and req.base_version != proposal.base_version:
        return False
    try:
        base = store.load_version(proposal.project_id, proposal.base_version)
        mapping = chat.map_message_to_command(
            project_id=proposal.project_id,
            base_version=proposal.base_version,
            message=req.message,
            version=base,
            selection=req.selection,
            actor=req.actor,
            idempotency_key=proposal.idempotency_key,
        )
    except (chat.ChatMappingError, ProjectVersionNotFoundError, ProjectNotFoundError):
        return False
    return mapping.command == proposal.command


@app.post(
    "/api/projects/{project_id}/chat/proposals",
    status_code=status.HTTP_201_CREATED,
)
@app.post(
    "/api/projects/{project_id}/chat/propose",
    status_code=status.HTTP_201_CREATED,
)
@app.post(
    "/api/projects/{project_id}/proposals",
    status_code=status.HTTP_201_CREATED,
)
def propose_project_chat(
    project_id: str, req: ChatProposalRequest, request: Request, response: Response
):
    """Map a chat note and persist a reviewable proposal without changing the head."""
    _enforce_public_access_token(request)
    if not is_valid_job_id(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    store = _project_store()
    proposal_store = _proposal_store(store)
    try:
        with store.transaction(project_id):
            if not store.project_exists(project_id):
                raise HTTPException(status_code=404, detail="Project not found")
            head_no, head = _load_head_or_404(store, project_id)
            if req.idempotency_key:
                existing = proposal_store.find_by_idempotency_key(
                    project_id, req.idempotency_key
                )
                if existing is not None:
                    if not _proposal_request_matches(store, existing, req):
                        raise ProposalConflictError(
                            f"idempotency key {req.idempotency_key!r} is already bound"
                        )
                    response.status_code = status.HTTP_200_OK
                    return _proposal_status_replay(existing)
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

            # Full command validation is read-only.  Approval repeats this
            # validation under the same project lock because head/locks may move.
            preview_command(store, mapping.command, _lock_held=True)
            now = utc_now_iso()
            proposal = ProjectProposal(
                proposal_id=new_proposal_id(),
                project_id=project_id,
                base_version=base_version,
                command=mapping.command,
                diff=_proposal_diff(mapping, head),
                # Mixed scene edits carry more than one lock scope. Derive the
                # review card from the exact fields that differ, not only the
                # operation's historical single-scope mapping.
                affected_scopes=command_touched_scopes(head, mapping.command),
                affected_segment_ids=list(mapping.affected_segment_ids),
                actor=mapping.command.actor,
                status="proposed",
                idempotency_key=mapping.command.idempotency_key,
                created_at=now,
                updated_at=now,
                expires_at=req.expires_at,
            )
            stored = proposal_store.create(proposal)
            if stored.proposal_id != proposal.proposal_id:
                response.status_code = status.HTTP_200_OK
            return _proposal_response(stored)
    except HTTPException:
        raise
    except (ProposalConflictError, ProposalCorruptError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StaleVersionError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "stale_version",
                "project_id": exc.project_id,
                "base_version": exc.base_version,
                "current_version": exc.current_version,
                "hint": "re-fetch the current project head before proposing a change",
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
    except (InvalidProjectIdError, ProjectNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc
    except Exception:
        logger.exception("Chat proposal creation failed")
        raise HTTPException(status_code=500, detail="Failed to create chat proposal")


@app.get("/api/projects/{project_id}/proposals")
def list_project_proposals(project_id: str):
    """List durable proposals newest first for a Studio reload."""
    if not is_valid_job_id(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    store = _project_store()
    if not store.project_exists(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        proposals = _proposal_store(store).list(project_id)
    except ProposalCorruptError as exc:
        raise HTTPException(status_code=500, detail="Proposal store is corrupt") from exc
    proposals.sort(key=lambda proposal: proposal.created_at, reverse=True)
    return {
        "success": True,
        "project_id": project_id,
        "proposals": [proposal.model_dump(mode="json") for proposal in proposals],
    }


@app.get("/api/projects/{project_id}/proposals/{proposal_id}")
def get_project_proposal(project_id: str, proposal_id: str):
    if not is_valid_job_id(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    try:
        proposal = _proposal_store().load(project_id, proposal_id)
    except ProposalNotFoundError as exc:
        raise _proposal_not_found(proposal_id) from exc
    except (ProposalCorruptError, ValueError) as exc:
        raise HTTPException(status_code=500, detail="Proposal is unreadable") from exc
    return _proposal_response(proposal)


@app.post("/api/projects/{project_id}/proposals/{proposal_id}/approve")
@app.post("/api/projects/{project_id}/chat/proposals/{proposal_id}/approve")
def approve_project_proposal(
    project_id: str,
    proposal_id: str,
    request: Request,
    req: ProposalDecisionRequest = Body(default=ProposalDecisionRequest()),
):
    """Revalidate and apply one proposal while holding the project transaction."""
    _enforce_public_access_token(request)
    if not is_valid_job_id(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    store = _project_store()
    proposal_store = _proposal_store(store)
    try:
        with store.transaction(project_id):
            if not store.project_exists(project_id):
                raise HTTPException(status_code=404, detail="Project not found")
            try:
                proposal = proposal_store.load(project_id, proposal_id)
            except ProposalNotFoundError as exc:
                raise _proposal_not_found(proposal_id) from exc
            if proposal.status != "proposed":
                return _proposal_status_replay(proposal)
            if _proposal_is_expired(proposal):
                expired = _proposal_decision(
                    proposal,
                    status_value="expired",
                    actor=req.actor,
                    reason="proposal_expired",
                )
                proposal_store.save(expired)
                return _proposal_response(expired)

            # Recovery is intentionally before the current-head comparison:
            # the command may already have appended the version while the
            # proposal status write crashed. The exact persisted command
            # fingerprint is the authority for this idempotent finalization.
            recovered = _recover_committed_proposal(
                store, proposal_store, proposal, req.actor
            )
            if recovered is not None:
                return recovered

            head_no = store.current_version_no(project_id)
            if head_no != proposal.base_version:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "stale_version",
                        "project_id": project_id,
                        "proposal_id": proposal_id,
                        "base_version": proposal.base_version,
                        "current_version": head_no,
                        "hint": "create a new proposal against the current project head",
                    },
                )
            try:
                changeset = apply_command(
                    store,
                    proposal.command,
                    rebase=False,
                    lock_checker=_granular_checker(),
                    _lock_held=True,
                )
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
                        "project_id": project_id,
                        "proposal_id": proposal_id,
                        "conflicts": exc.conflicts,
                        "reasons": reasons,
                        "hint": "this proposal's scope is locked; unlock it or reject the proposal",
                    },
                ) from exc
            except StaleVersionError as exc:
                # apply_command repeats the head check, protecting the seam if a
                # different ProjectStore implementation changes locking later.
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "stale_version",
                        "project_id": exc.project_id,
                        "proposal_id": proposal_id,
                        "base_version": exc.base_version,
                        "current_version": exc.current_version,
                    },
                ) from exc
            except IdempotencyConflictError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "idempotency_conflict",
                        "project_id": exc.project_id,
                        "proposal_id": proposal_id,
                        "idempotency_key": exc.idempotency_key,
                    },
                ) from exc
            except ApprovalRequiredError as exc:
                raise HTTPException(
                    status_code=402,
                    detail={
                        "error": "approval_required",
                        "operation": exc.operation,
                        "proposal_id": proposal_id,
                        "reason": exc.reason,
                        "budget_status": exc.budget_status,
                    },
                ) from exc
            except CommandValidationError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "validation_failed",
                        "proposal_id": proposal_id,
                        "changeset": exc.changeset.model_dump(mode="json"),
                    },
                ) from exc

            target_version = changeset.target_version
            if target_version is None:
                raise HTTPException(status_code=500, detail="Approved proposal created no version")
            now = utc_now_iso()
            approved = proposal.model_copy(
                update={
                    "status": "approved",
                    "updated_at": now,
                    "decision_actor": req.actor,
                    "decision_at": now,
                    "target_version": target_version,
                    "reason": "approved",
                }
            )
            proposal_store.save(approved)
            return _proposal_response(approved, status_value="approved", changeset=changeset)
    except HTTPException:
        raise
    except ProposalConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "idempotency_conflict",
                "project_id": project_id,
                "proposal_id": proposal_id,
                "reason": str(exc),
            },
        ) from exc
    except ProposalCorruptError as exc:
        raise HTTPException(status_code=500, detail="Proposal is unreadable") from exc
    except Exception:
        logger.exception("Proposal approval failed")
        raise HTTPException(status_code=500, detail="Failed to approve proposal")


def _finish_project_proposal(
    project_id: str,
    proposal_id: str,
    req: ProposalDecisionRequest,
    request: Request,
    *,
    target_status: Literal["rejected", "expired", "revoked"],
) -> dict[str, Any]:
    _enforce_public_access_token(request)
    if not is_valid_job_id(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    store = _project_store()
    proposal_store = _proposal_store(store)
    with store.transaction(project_id):
        if not store.project_exists(project_id):
            raise HTTPException(status_code=404, detail="Project not found")
        try:
            proposal = proposal_store.load(project_id, proposal_id)
        except ProposalNotFoundError as exc:
            raise _proposal_not_found(proposal_id) from exc
        if proposal.status != "proposed":
            return _proposal_status_replay(proposal)
        decision = _proposal_decision(
            proposal,
            status_value=target_status,
            actor=req.actor,
            reason=req.reason or f"proposal_{target_status}",
        )
        proposal_store.save(decision)
        return _proposal_response(decision)


@app.post("/api/projects/{project_id}/proposals/{proposal_id}/reject")
@app.post("/api/projects/{project_id}/chat/proposals/{proposal_id}/reject")
def reject_project_proposal(
    project_id: str,
    proposal_id: str,
    request: Request,
    req: ProposalDecisionRequest = Body(default=ProposalDecisionRequest()),
):
    return _finish_project_proposal(project_id, proposal_id, req, request, target_status="rejected")


@app.post("/api/projects/{project_id}/proposals/{proposal_id}/expire")
@app.post("/api/projects/{project_id}/chat/proposals/{proposal_id}/expire")
def expire_project_proposal(
    project_id: str,
    proposal_id: str,
    request: Request,
    req: ProposalDecisionRequest = Body(default=ProposalDecisionRequest()),
):
    return _finish_project_proposal(project_id, proposal_id, req, request, target_status="expired")


@app.post("/api/projects/{project_id}/proposals/{proposal_id}/revoke")
@app.post("/api/projects/{project_id}/chat/proposals/{proposal_id}/revoke")
def revoke_project_proposal(
    project_id: str,
    proposal_id: str,
    request: Request,
    req: ProposalDecisionRequest = Body(default=ProposalDecisionRequest()),
):
    return _finish_project_proposal(project_id, proposal_id, req, request, target_status="revoked")


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


@app.post("/api/projects/{project_id}/chat/apply")
@app.post("/api/projects/{project_id}/chat")
def project_chat(project_id: str, req: ChatRequest, request: Request):
    """Explicit legacy apply-now compatibility route.

    Chat never writes state directly: backend.projects.chat produces a
    ProjectCommand which is applied through apply_command - the exact seam the
    canvas uses - so chat and canvas edit the SAME canonical version.  New
    Studio clients use ``/chat/proposals`` and must approve before applying.
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
            if req.base_version is not None and req.base_version != head_no:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "stale_version",
                        "base_version": req.base_version,
                        "current_version": head_no,
                        "hint": "re-fetch the current project head before undoing",
                    },
                )
            if req.idempotency_key:
                existing = store.find_by_idempotency_key(project_id, req.idempotency_key)
                if existing is not None:
                    # When the caller omits ``base_version``, the first request
                    # binds the key to the head that it actually restored from.
                    # A replay arrives after this undo has advanced the head, so
                    # fingerprinting against the *new* head would incorrectly
                    # turn an exact retry into an idempotency conflict.
                    replay_base = (
                        existing.parent_version
                        if req.base_version is None and existing.parent_version is not None
                        else (req.base_version if req.base_version is not None else head_no)
                    )
                    request_fingerprint = _derived_request_fingerprint(
                        operation="undo",
                        project_id=project_id,
                        base_version=replay_base,
                        actor=req.actor,
                        idempotency_key=req.idempotency_key,
                        target_version=req.target_version,
                    )
                    if existing.command_fingerprint != request_fingerprint:
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "error": "idempotency_conflict",
                                "idempotency_key": req.idempotency_key,
                                "reason": "idempotency key is bound to a different undo target or base",
                            },
                        )
                    return {
                        "success": True,
                        "status": "replayed",
                        "restored_from": req.target_version,
                        "version": existing.model_dump(mode="json"),
                    }
            base_no = req.base_version if req.base_version is not None else head_no
            request_fingerprint = _derived_request_fingerprint(
                operation="undo",
                project_id=project_id,
                base_version=base_no,
                actor=req.actor,
                idempotency_key=req.idempotency_key or "",
                target_version=req.target_version,
            )
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
                command_fingerprint_value=request_fingerprint,
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
            base_no = req.base_version if req.base_version is not None else head_no
            if base_no != head_no:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "stale_version",
                        "base_version": base_no,
                        "current_version": head_no,
                        "hint": "re-fetch the current project head before saving a variant",
                    },
                )
            if req.idempotency_key:
                existing = store.find_by_idempotency_key(project_id, req.idempotency_key)
                if existing is not None:
                    replay_base = (
                        existing.parent_version
                        if req.base_version is None and existing.parent_version is not None
                        else base_no
                    )
                    request_fingerprint = _derived_request_fingerprint(
                        operation="variant",
                        project_id=project_id,
                        base_version=replay_base,
                        actor=req.actor,
                        idempotency_key=req.idempotency_key,
                        variant_name=req.variant_name,
                    )
                    if existing.command_fingerprint != request_fingerprint:
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "error": "idempotency_conflict",
                                "idempotency_key": req.idempotency_key,
                                "reason": "idempotency key is bound to a different variant name or base",
                            },
                        )
                    return {
                        "success": True,
                        "status": "replayed",
                        "variant_name": existing.variant_name,
                        "version": existing.model_dump(mode="json"),
                    }
            request_fingerprint = _derived_request_fingerprint(
                operation="variant",
                project_id=project_id,
                base_version=base_no,
                actor=req.actor,
                idempotency_key=req.idempotency_key or "",
                variant_name=req.variant_name,
            )
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
                command_fingerprint_value=request_fingerprint,
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
    """Current global and scene-scoped locks for a project."""
    if not is_valid_job_id(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    store = _project_store()
    try:
        scene_locks = read_scene_locks(store.root, project_id)
    except ValueError as exc:
        # Scene protections fail closed when their durable registry is corrupt.
        raise HTTPException(status_code=500, detail="Scene lock registry is unreadable") from exc
    return {
        "success": True,
        "project_id": project_id,
        "scopes": read_scope_locks(LOCKS_ROOT, project_id),
        "locked": locked_scopes(LOCKS_ROOT, project_id),
        "available_scopes": list(GRANULAR_LOCK_SCOPES),
        "scene_locks": scene_locks,
        "available_scene_scopes": list(SCENE_LOCK_SCOPES),
    }


@app.post("/api/projects/{project_id}/locks")
def set_project_lock(project_id: str, req: ScopeLockRequest, request: Request):
    """Set or clear one global or scene-scoped lock atomically.

    The project transaction intentionally surrounds both lock mutations and
    proposal approval, so an approval cannot pass a lock check concurrently
    with this endpoint's write.
    """
    _enforce_public_access_token(request)
    if not is_valid_job_id(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    store = _project_store()
    scene_ids = list(req.scene_ids)
    if req.scene_id is not None:
        scene_ids = [req.scene_id]
    try:
        with store.transaction(project_id):
            if not store.project_exists(project_id):
                raise HTTPException(status_code=404, detail="Project not found")
            if scene_ids:
                if req.scope not in SCENE_LOCK_SCOPES:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "error": "unknown_scene_lock_scope",
                            "scope": req.scope,
                            "available_scopes": list(SCENE_LOCK_SCOPES),
                        },
                    )
                head = store.load_version(project_id, store.current_version_no(project_id))
                missing = sorted(set(scene_ids) - set(head.segment_ids()))
                if missing:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "error": "unknown_scene",
                            "scene_ids": missing,
                        },
                    )
                set_scene_locks(
                    store.root,
                    project_id,
                    scene_ids,
                    req.scope,
                    locked=req.locked,
                    locked_by=req.locked_by,
                    reason=req.reason,
                )
                scene_locks = read_scene_locks(store.root, project_id)
                record = scene_locks.get(scene_ids[0], {}).get(req.scope)
                if not req.locked:
                    record = {"locked": False, "scope": req.scope, "scene_id": scene_ids[0]}
                return {
                    "success": True,
                    "project_id": project_id,
                    "scope": req.scope,
                    "scene_ids": scene_ids,
                    "record": record,
                    "locked": locked_scopes(LOCKS_ROOT, project_id),
                    "scene_locks": scene_locks,
                }
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
            scene_locks = read_scene_locks(store.root, project_id)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Scene lock registry is unreadable") from exc
    except (ProjectNotFoundError, InvalidProjectIdError) as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc
    return {
        "success": True,
        "project_id": project_id,
        "scope": req.scope,
        "record": record,
        "locked": locked_scopes(LOCKS_ROOT, project_id),
        "scene_locks": scene_locks,
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


class ProjectBudgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    budget_usd: float = Field(ge=0, le=1_000_000)
    actor: str = Field(default="operator", min_length=1, max_length=80)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


@app.post("/api/projects/{project_id}/budget")
def configure_project_budget_route(
    project_id: str,
    req: ProjectBudgetRequest,
    request: Request,
):
    """Set an explicit project allocation while preserving account caps."""

    _enforce_public_access_token(request)
    if not is_valid_job_id(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    store = _project_store()
    try:
        if not store.project_exists(project_id):
            raise HTTPException(status_code=404, detail="Project not found")
        configured = set_project_budget(
            project_id,
            req.budget_usd,
            actor=req.actor,
            idempotency_key=req.idempotency_key,
        )
    except HTTPException:
        raise
    except (InvalidProjectIdError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "success": True,
        "project_id": project_id,
        "configured": configured,
        "budget": get_budget_status(project_id=project_id),
    }


@app.get("/api/budget")
def get_budget(project_id: str | None = None):
    """Fail-closed budget status. Unknown cost stays ``None``, never ``0``."""

    if project_id is not None and not is_valid_job_id(project_id):
        raise HTTPException(status_code=400, detail="Invalid project ID")
    return {"success": True, "budget": get_budget_status(project_id=project_id)}
