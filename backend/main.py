"""
FYF Video Pipeline - FastAPI Backend
Connects the Next.js frontend to the Gemini Writer/Producer Agent and Gemini-TTS Voice Generation.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.job_store import (
    create_job_dir,
    initialize_job_status,
    is_valid_job_id,
    read_job_status,
    update_job_status,
    write_json_atomically,
)
from backend.lock_store import create_script_lock, read_script_lock
from backend.pipeline import run_pipeline
from backend.runtime_limits import (
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


class RecentApprovedVideo(BaseModel):
    job_id: str = Field(pattern=r"^[0-9a-f]{8}$")
    title: str = Field(min_length=1)
    voice_provider: Literal["gemini"]
    updated_at: str = Field(min_length=1)
    video_url: str


def _should_resume_script_job(data: dict) -> bool:
    """Resume only jobs interrupted before a terminal failure was recorded."""
    status_val = data.get("status")
    return status_val in {"queued", "writing"}


def _create_video_job(
    script_data: dict,
    voice_provider: Literal["gemini"] = "gemini",
) -> VideoJobItem:
    job_id = create_job_dir(JOBS_ROOT)
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


@app.get("/api/runtime", response_model=RuntimeResponse)
def get_runtime():
    return RuntimeResponse(
        runtime_mode="hackathon",
        allowed_voice_providers=["gemini"],
        script_model=model_for("script"),
        fallback_model=model_for("story_fallback"),
    )


@app.get("/api/video-styles")
def list_video_styles():
    return {"styles": get_available_styles()}


@app.post("/api/generate-script", status_code=status.HTTP_202_ACCEPTED, response_model=ScriptJobResponse)
async def generate_script(req: ScriptRequest, request: Request, background_tasks: BackgroundTasks):
    """Queue persisted Vertex script production and return immediately."""
    job_id = create_job_dir(SCRIPT_JOBS_ROOT)
    enforce_generation_guardrails(
        request=request,
        operation_id=job_id,
        estimated_charge_usd=0.04,
        job_roots=(JOBS_ROOT, SCRIPT_JOBS_ROOT),
    )
    job_dir = SCRIPT_JOBS_ROOT / job_id
    write_json_atomically(job_dir / "request.json", req.model_dump(mode="json"))
    write_json_atomically(job_dir / "status.json", {
        "job_id": job_id, "status": "queued", "stage": "queued", "progress": 0,
        "batch": None, "batch_count": None, "lock_id": None, "error": None,
        "retry_count": 0, "restart_resumable": True,
    })
    background_tasks.add_task(run_script_pipeline, job_id, SCRIPT_JOBS_ROOT, LOCKS_ROOT)
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

    if status_data.get("status") != "needs_attention" and not status_data.get("restart_resumable"):
        raise HTTPException(status_code=400, detail="Job is not in a resumable needs_attention state")

    resume_count = int(status_data.get("resume_count", 0)) + 1
    if resume_count > 3:
        raise HTTPException(status_code=400, detail="Maximum script resume attempts (3) exceeded")

    enforce_generation_guardrails(
        request=request,
        operation_id=f"script_resume_{job_id}_{resume_count}",
        estimated_charge_usd=0.04,
        job_roots=(JOBS_ROOT, SCRIPT_JOBS_ROOT),
    )

    update_script_status(
        job_dir,
        status="queued",
        stage="queued",
        error=None,
        resume_count=resume_count,
        retry_count=0,
    )
    background_tasks.add_task(run_script_pipeline, job_id, SCRIPT_JOBS_ROOT, LOCKS_ROOT)
    return ScriptJobResponse(
        success=True,
        job_id=job_id,
        status_url=f"/api/script-jobs/{job_id}/status",
        restart_resumable=True,
    )


@app.on_event("startup")
async def resume_interrupted_script_jobs():
    """Resume persisted script and video jobs after a backend restart."""
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
                asyncio.create_task(asyncio.to_thread(
                    run_script_pipeline, job_dir.name, SCRIPT_JOBS_ROOT, LOCKS_ROOT
                ))

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
            update_job_status(job_dir, {
                "status": "queued",
                "error": None,
                "resume_count": resume_count + 1,
                "restart_resumable": True,
            })
            asyncio.create_task(run_pipeline(
                job_dir.name, script_data, "gemini", JOBS_ROOT
            ))


@app.post("/api/story-polish", response_model=StoryPolishResponse)
async def story_polish(req: StoryPolishRequest, request: Request):
    """Vertex-only FYF story variants; never queues a video."""
    enforce_generation_guardrails(
        request=request,
        operation_id=f"polish_{int(datetime.now().timestamp() * 1000)}",
        estimated_charge_usd=0.02,
        job_roots=(JOBS_ROOT, SCRIPT_JOBS_ROOT),
    )
    try:
        from writer_agent_vertex import generate_story_modes
        generated = generate_story_modes(req.topic_or_draft)
        result = StoryModesResponse.model_validate({"variants": generated["variants"]})
        return StoryPolishResponse(
            success=True,
            variants=[v.model_dump(mode="json") for v in result.variants],
            model_used=generated.get("model_used"),
        )
    except Exception:
        logger.exception("Story polish failed")
        raise HTTPException(status_code=500, detail="Story polish failed")


@app.post("/api/story-lock", response_model=StoryLockResponse)
async def story_lock(req: ExactLockRequest, request: Request):
    """Use Vertex only for visuals; server verifies approved narration byte-for-byte."""
    enforce_generation_guardrails(
        request=request,
        operation_id=f"lock_{int(datetime.now().timestamp() * 1000)}",
        estimated_charge_usd=0.03,
        job_roots=(JOBS_ROOT, SCRIPT_JOBS_ROOT),
    )
    try:
        from writer_agent_vertex import generate_exact_lock
        data = VideoScript.model_validate(generate_exact_lock(req.model_dump(mode="json")))
        directed = apply_director_pass(data.model_dump(mode="json"))
        data = VideoScript.model_validate(directed)
        lock_id = create_script_lock(LOCKS_ROOT, directed)
        return StoryLockResponse(success=True, data=data, lock_id=lock_id)
    except Exception:
        logger.exception("Story lock failed")
        raise HTTPException(status_code=500, detail="Story lock failed")


@app.post("/api/generate-video", status_code=status.HTTP_202_ACCEPTED, response_model=VideoResponse)
async def generate_video(req: VideoRequest, request: Request, background_tasks: BackgroundTasks):
    """Takes the approved script lock, creates job, and queues pipeline."""
    try:
        try:
            script_data = read_script_lock(LOCKS_ROOT, req.lock_id)
        except FileNotFoundError as exc:
            raise ValueError("Approved script lock not found") from exc

        styled_script = apply_video_style(script_data, req.style)
        job = _create_video_job(styled_script, "gemini")

        enforce_generation_guardrails(
            request=request,
            operation_id=job.job_id,
            estimated_charge_usd=0.06,
            job_roots=(JOBS_ROOT, SCRIPT_JOBS_ROOT),
        )

        background_tasks.add_task(
            _run_video_pipeline_tracked, job.job_id, styled_script, "gemini", JOBS_ROOT
        )

        return VideoResponse(
            success=True,
            job_id=job.job_id,
            status_url=job.status_url,
            restart_resumable=True
        )

    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except HTTPException:
        raise
    except Exception:
        logger.exception("Video generation queueing error:")
        raise HTTPException(status_code=500, detail="Internal server error")


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
    if not is_valid_job_id(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID")

    job_dir = JOBS_ROOT / job_id
    if not job_dir.is_dir():
        raise HTTPException(status_code=404, detail="Job not found")

    try:
        status_data = read_job_status(job_dir)
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=400, detail="Job status unreadable")

    if not status_data.get("restart_resumable"):
        raise HTTPException(status_code=400, detail="Job is not marked as resumable")

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

    enforce_generation_guardrails(
        request=request,
        operation_id=f"video_resume_{job_id}_{resume_count}",
        estimated_charge_usd=0.06,
        job_roots=(JOBS_ROOT, SCRIPT_JOBS_ROOT),
    )

    update_job_status(job_dir, {
        "status": "queued",
        "error": None,
        "resume_count": resume_count,
        "restart_resumable": True,
    })

    background_tasks.add_task(
        _run_video_pipeline_tracked, job_id, script_data, "gemini", JOBS_ROOT
    )

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
