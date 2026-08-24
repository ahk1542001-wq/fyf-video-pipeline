import contextlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

try:
    from backend.render_contract import validate_render_input
    from backend.approved_visual_presets import approved_visual_preset
except ModuleNotFoundError:  # Support direct execution from backend/.
    from render_contract import validate_render_input
    from approved_visual_presets import approved_visual_preset

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REMOTION_BIN = os.path.join(REPO_ROOT, "remotion", "node_modules", ".bin", "remotion")
REMOTION_COMPOSITION_ID = "VisualSystemV3Full"


@dataclass(frozen=True)
class _RenderStaging:
    public_dir: str
    props_path: str
    render_input: dict[str, Any]


def _render_timeout() -> int:
    try:
        timeout_val = int(os.environ.get("FYF_RENDER_TIMEOUT_SECONDS", "1800"))
        return max(60, min(7200, timeout_val))
    except (ValueError, TypeError):
        return 1800


def _require_non_empty_file(path: str, description: str) -> None:
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        raise ValueError(f"{description} is missing or empty at {path}")


def _load_render_input(job_dir: str) -> dict[str, Any]:
    render_input_path = os.path.join(job_dir, "render_input.json")
    _require_non_empty_file(render_input_path, "render_input.json")
    try:
        with open(render_input_path, "r", encoding="utf-8") as handle:
            render_input = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"render_input.json is not valid JSON at {render_input_path}") from exc
    if not isinstance(render_input, dict):
        raise ValueError("render_input.json must contain an object")
    return render_input


def _find_segment(render_input: dict[str, Any], segment_id: str) -> dict[str, Any]:
    if not isinstance(segment_id, str) or not segment_id.strip():
        raise ValueError("segment ID must be a non-blank string")
    segments = render_input.get("segments")
    if not isinstance(segments, list):
        raise ValueError("render input segments must be a list")
    matches = [
        segment
        for segment in segments
        if isinstance(segment, dict)
        and (segment.get("id") or segment.get("segment_id")) == segment_id
    ]
    if len(matches) != 1:
        raise ValueError(f"segment {segment_id!r} is not present exactly once in render_input.json")
    return matches[0]


def _validate_segment_range(
    render_input: dict[str, Any],
    *,
    segment_id: str,
    start_frame: int,
    end_frame: int,
) -> None:
    if (
        not isinstance(start_frame, int)
        or isinstance(start_frame, bool)
        or not isinstance(end_frame, int)
        or isinstance(end_frame, bool)
        or start_frame < 0
        or end_frame <= start_frame
    ):
        raise ValueError("segment end_frame must be greater than start_frame")
    segment = _find_segment(render_input, segment_id)
    if segment.get("startFrame") != start_frame or segment.get("endFrame") != end_frame:
        raise ValueError(
            f"segment {segment_id!r} frame range does not match render_input.json"
        )


def _validated_segment_output_path(job_dir: str, output_path: str | Path) -> Path:
    job_root = Path(job_dir).resolve()
    candidate = Path(output_path)
    if not candidate.is_absolute():
        candidate = job_root / candidate
    candidate = candidate.resolve()
    segment_root = (job_root / "render-segments").resolve()
    try:
        candidate.relative_to(segment_root)
    except ValueError as exc:
        raise ValueError("segment output path must be inside render-segments") from exc
    if candidate == segment_root:
        raise ValueError("segment output path must name a file")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


@contextlib.contextmanager
def _stage_render_inputs(job_dir: str) -> Iterator[_RenderStaging]:
    """Stage exactly the same public files and props for every render mode."""

    job_dir = os.path.abspath(job_dir)
    if not os.path.isdir(job_dir):
        raise FileNotFoundError(f"job directory not found: {job_dir}")
    if not os.path.isfile(REMOTION_BIN) or not os.access(REMOTION_BIN, os.X_OK):
        raise FileNotFoundError(f"Remotion binary not found or not executable at {REMOTION_BIN}")

    audio_path = os.path.join(job_dir, "voice.wav")
    _require_non_empty_file(audio_path, "voice.wav")
    render_input = _load_render_input(job_dir)

    original_mascot_path = os.path.join(
        REPO_ROOT, "remotion", "public", "fyf-mascot-presenting.png"
    )
    talking_mascot_atlas_path = os.path.join(
        REPO_ROOT, "remotion", "public", "fyf-mascot-talking-atlas.png"
    )
    if not os.path.isfile(talking_mascot_atlas_path) or os.path.getsize(talking_mascot_atlas_path) == 0:
        raise FileNotFoundError(
            f"Missing or empty talking mascot atlas at {talking_mascot_atlas_path}"
        )
    cut_paper_world_path = os.path.join(
        REPO_ROOT, "remotion", "public", "fyf-cut-paper-world.png"
    )
    if not os.path.isfile(cut_paper_world_path) or os.path.getsize(cut_paper_world_path) == 0:
        raise FileNotFoundError(
            f"Missing or empty FYF cut-paper world at {cut_paper_world_path}"
        )

    with tempfile.TemporaryDirectory() as temp_dir:
        public_dir = os.path.join(temp_dir, "public")
        os.makedirs(public_dir, exist_ok=True)

        if os.path.isfile(original_mascot_path):
            shutil.copy2(
                original_mascot_path,
                os.path.join(public_dir, "fyf-mascot-presenting.png"),
            )
        shutil.copy2(talking_mascot_atlas_path, os.path.join(public_dir, "fyf-mascot-talking-atlas.png"))
        shutil.copy2(cut_paper_world_path, os.path.join(public_dir, "fyf-cut-paper-world.png"))
        shutil.copy2(audio_path, os.path.join(public_dir, "voice.wav"))

        job_visuals_path = os.path.join(job_dir, "visuals")
        if os.path.isdir(job_visuals_path):
            shutil.copytree(job_visuals_path, os.path.join(public_dir, "job-visuals"))

        validate_render_input(render_input, job_dir=job_dir)
        render_input["audioSrc"] = "voice.wav"
        approved_preset = approved_visual_preset(render_input)
        if approved_preset:
            render_input.update(approved_preset)
            for scene_assets in approved_preset["v3SceneAssets"]:
                for asset in scene_assets:
                    source = os.path.join(REPO_ROOT, "remotion", "public", asset)
                    if not os.path.isfile(source) or os.path.getsize(source) == 0:
                        raise FileNotFoundError(f"Approved V3 asset is missing or empty: {asset}")
                    destination = os.path.join(public_dir, asset)
                    os.makedirs(os.path.dirname(destination), exist_ok=True)
                    shutil.copy2(source, destination)

        # Root.tsx registers demo defaultProps (sampleInput). Remotion merges
        # composition defaults with --props, so without explicit empty keys the
        # demo v3SceneAssets leak into non-preset renders and 404 during render.
        render_input.setdefault("v3SceneAssets", [])
        render_input.setdefault("v3MascotSegments", [])

        props_path = os.path.join(temp_dir, "props.json")
        with open(props_path, "w", encoding="utf-8") as handle:
            json.dump(render_input, handle, ensure_ascii=False)

        yield _RenderStaging(
            public_dir=public_dir,
            props_path=props_path,
            render_input=render_input,
        )


def _run_remotion(
    staging: _RenderStaging,
    output_path: str,
    *,
    extra_args: list[str] | None = None,
) -> None:
    command = [
        REMOTION_BIN,
        "render",
        os.path.join(REPO_ROOT, "remotion", "src", "index.ts"),
        REMOTION_COMPOSITION_ID,
        output_path,
        "--props",
        staging.props_path,
        "--public-dir",
        staging.public_dir,
    ]
    if extra_args:
        command.extend(extra_args)
    try:
        subprocess.run(
            command,
            shell=False,
            check=True,
            timeout=_render_timeout(),
            cwd=os.path.join(REPO_ROOT, "remotion"),
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        detail = stderr[-2000:] if stderr else "No stderr output was captured."
        raise RuntimeError(
            f"Remotion render failed with exit code {exc.returncode}: {detail}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Remotion render timed out") from exc

def render_video_remotion(job_dir: str) -> str:
    """Render the complete composition using the existing monolithic contract."""

    job_dir = os.path.abspath(job_dir)
    output_mp4 = os.path.join(job_dir, "video.mp4")
    with _stage_render_inputs(job_dir) as staging:
        _run_remotion(staging, output_mp4)
    if not os.path.isfile(output_mp4) or os.path.getsize(output_mp4) == 0:
        raise RuntimeError("Remotion render produced empty or missing output")
    return output_mp4


def render_video_segment(
    job_dir: str,
    *,
    segment_id: str,
    start_frame: int,
    end_frame: int,
    output_path: str,
) -> str:
    """Render one existing global frame range as muted video-only MP4."""

    job_dir = os.path.abspath(job_dir)
    render_input = _load_render_input(job_dir)
    _validate_segment_range(
        render_input,
        segment_id=segment_id,
        start_frame=start_frame,
        end_frame=end_frame,
    )
    output = _validated_segment_output_path(job_dir, output_path)
    with _stage_render_inputs(job_dir) as staging:
        _run_remotion(
            staging,
            str(output),
            extra_args=[
                f"--frames={start_frame}-{end_frame - 1}",
                "--muted",
                "--codec=h264",
                "--pixel-format=yuv420p",
                "--color-space=bt709",
            ],
        )
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("Remotion segment render produced empty or missing output")
    return str(output)
