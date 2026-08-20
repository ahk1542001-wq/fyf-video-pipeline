"""Server-owned immutable narration/visual locks for production renders."""

from pathlib import Path

from backend.job_store import create_job_dir, is_valid_job_id, write_json_atomically
from video_contract import VideoScript


def create_script_lock(locks_root: Path, script_data: dict) -> str:
    """Persist one validated immutable script and return its opaque lock ID."""
    lock_id = create_job_dir(locks_root)
    lock_dir = locks_root / lock_id
    validated = VideoScript.model_validate(script_data).model_dump(mode="json")
    write_json_atomically(lock_dir / "script.json", validated)
    return lock_id


def read_script_lock(locks_root: Path, lock_id: str) -> dict:
    if not is_valid_job_id(lock_id):
        raise ValueError("Invalid script lock ID")
    path = (locks_root / lock_id / "script.json").resolve()
    try:
        path.relative_to(locks_root.resolve())
    except ValueError as exc:
        raise ValueError("Forbidden script lock path") from exc
    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError("Script lock not found")
    return VideoScript.model_validate_json(path.read_text(encoding="utf-8")).model_dump(mode="json")
