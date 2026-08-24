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
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
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
from backend.budget_store import release_reservation
from backend.lock_store import create_script_lock, read_script_lock
from backend.pipeline import run_pipeline
from backend.runtime_limits import (
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
from video_contract import ExactLockRequest, StoryModesResponse, VideoScript

REPO_ROOT = Path(__file__).resolve().parent.parent
JOBS_ROOT = REPO_ROOT / "output" / "jobs"
LOCKS_ROOT = REPO_ROOT / "output" / "locks"
SCRIPT_JOBS_ROOT = REPO_ROOT / "output" / "script-jobs"

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
    duration_mode: Literal["short"] = "short"
    style: str | None = "fyf_explainer"
    use_adk_agent: bool = True

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


class VideoRequest(BaseModel):
    lock_id: str = Field(pattern=r"^[0-9a-f]{8}$")
    voice_provider: Literal["gemini"] = "gemini"
    style: str | None = "fyf_explainer"

    @field_validator("style")
    @classmethod
    def validate_style(cls, v: str | None) -> str:
        if v is None or v == "":
            return "fyf_explainer"
        valid_ids = {s["id"] for s in get_available_styles()}
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

    if not os.getenv("FYF_GENERATION_ACCESS_TOKEN"):
        return {
            "generation_available": False,
            "generation_access_required": False,
            "generation_status": "access_token_required",
            "generation_message": "Generation is disabled until the operator configures a private access token.",
        }

    return {
        "generation_available": True,
        "generation_access_required": True,
        "generation_status": "private_access_required",
        "generation_message": "Private generation access is required before a provider request can be queued.",
    }


def _enforce_public_generation_access(request: Request) -> None:
    """Fail closed before quota reservation or provider work on the public deployment."""
    if not _is_public_deployment():
        return

    runtime = _generation_runtime_state()
    if not runtime["generation_available"]:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=runtime["generation_message"])

    expected_token = os.getenv("FYF_GENERATION_ACCESS_TOKEN", "")
    submitted_token = request.headers.get("x-fyf-access-token", "")
    if not submitted_token or not hmac.compare_digest(submitted_token, expected_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Private generation access is required.")


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
    register_active_job(job_id)
    try:
        await run_pipeline(job_id, script_data, voice_provider, jobs_root)
    finally:
        release_active_job(job_id)
        from backend.budget_store import release_reservation
        release_reservation(job_id)


def _queue_video_job(
    background_tasks: BackgroundTasks,
    script_data: dict,
    voice_provider: Literal["gemini"] = "gemini",
) -> VideoJobItem:
    job = _create_video_job(script_data, voice_provider)
    background_tasks.add_task(
        _run_video_pipeline_tracked, job.job_id, script_data, voice_provider, JOBS_ROOT
    )
    return job


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
        **generation_state,
    )


@app.get("/api/video-styles")
def list_video_styles():
    return {"styles": get_available_styles()}


@app.post("/api/generate-script", status_code=status.HTTP_202_ACCEPTED, response_model=ScriptJobResponse)
async def generate_script(req: ScriptRequest, request: Request, background_tasks: BackgroundTasks):
    """Queue persisted Vertex script production and return immediately."""
    _enforce_public_generation_access(request)
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
        background_tasks.add_task(run_script_pipeline, job_id, SCRIPT_JOBS_ROOT, LOCKS_ROOT)
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
        background_tasks.add_task(run_script_pipeline, job_id, SCRIPT_JOBS_ROOT, LOCKS_ROOT)
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
            generated = generate_story_modes(req.topic_or_draft)
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
        background_tasks.add_task(
            _run_video_pipeline_tracked, job.job_id, styled_script, req.voice_provider, JOBS_ROOT
        )
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


@app.post("/api/jobs/{job_id}/resume", status_code=status.HTTP_202_ACCEPTED, response_model=VideoResponse)
async def resume_job(job_id: str, request: Request, background_tasks: BackgroundTasks):
    """Resume a failed or interrupted resumable video job."""
    _enforce_public_generation_access(request)
    if not is_valid_job_id(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID")

    job_dir = JOBS_ROOT / job_id
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

        background_tasks.add_task(
            _run_video_pipeline_tracked, job_id, script_data, "gemini", JOBS_ROOT
        )
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


@app.get("/api/jobs/{job_id}/video")
def get_job_video(job_id: str):
    if not is_valid_job_id(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID")

    job_dir = JOBS_ROOT / job_id
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
def ask_data_insights(payload: dict = Body(...)):
    """Ask the FYF Data Officer (ADK agent + mcp-clickhouse) a question about
    production telemetry. Read-only; answers from ClickHouse Cloud."""
    question = str(payload.get("question", "")).strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    if len(question) > 500:
        raise HTTPException(status_code=400, detail="question too long")
    try:
        import asyncio

        from backend.agent.data_officer import ask_data_officer

        result = asyncio.run(ask_data_officer(question))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception:
        logger.exception("Data Officer failed for question")
        raise HTTPException(status_code=502, detail="Data Officer could not answer right now")
    return {"success": True, "question": question, **result}
