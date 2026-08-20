"""Resumable orchestration for two voices sharing one approved visual plan."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from backend.job_store import read_job_status, update_job_status
from backend.paired_visuals import adopt_completed_visual_plan
from backend.pipeline import run_pipeline


logger = logging.getLogger(__name__)


async def run_paired_pipeline(
    source_job_id: str,
    target_job_id: str,
    locked_script: dict[str, Any],
    jobs_root: Path,
    *,
    source_provider: Literal["kaggle", "gemini"],
    target_provider: Literal["kaggle", "gemini"],
) -> None:
    """Finish source, adopt only its approved visuals, then render target voice."""
    source_job_dir = Path(jobs_root) / source_job_id
    target_job_dir = Path(jobs_root) / target_job_id
    source_status = read_job_status(source_job_dir)
    source_can_resume = source_status.get("status") in {
        "queued", "visuals", "voice", "rendering", "qa", "creative_qa"
    } or (
        source_status.get("status") == "failed"
        and bool(source_status.get("restart_resumable"))
    )
    if source_status.get("status") != "completed" and not source_can_resume:
        update_job_status(target_job_dir, {
            "status": "failed",
            "error": "Paired source cannot be resumed automatically; manual retry is required.",
            "restart_resumable": False,
        })
        return
    if source_status.get("status") != "completed":
        await run_pipeline(
            source_job_id,
            locked_script,
            source_provider,
            Path(jobs_root),
        )
        source_status = read_job_status(source_job_dir)
    if source_status.get("status") != "completed":
        update_job_status(target_job_dir, {
            "status": "failed",
            "error": "Paired source video is not approved yet; retry can resume safely.",
            "restart_resumable": True,
        })
        return

    try:
        adopted_script = adopt_completed_visual_plan(source_job_dir, target_job_dir)
    except (OSError, ValueError):
        logger.exception(
            "Could not adopt approved paired visuals from %s to %s",
            source_job_id,
            target_job_id,
        )
        update_job_status(target_job_dir, {
            "status": "failed",
            "error": "Approved paired visual plan could not be adopted; retry can resume safely.",
            "restart_resumable": True,
        })
        return

    await run_pipeline(
        target_job_id,
        adopted_script,
        target_provider,
        Path(jobs_root),
    )
