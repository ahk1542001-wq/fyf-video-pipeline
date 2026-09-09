"""Reproducibility manifests for rendered FYF videos (Stage C6).

HONEST SCOPE STATEMENT
----------------------
A :class:`~backend.projects.models.RenderManifest` guarantees reproducibility
**from stored assets only**.  It records the exact bytes of every referenced
asset, the resolved font inventory, the renderer source hash, the installed
Remotion version, the skill versions, the declared seeds and the output geometry
that produced a video, plus a per-segment SHA-256 for every rendered segment.
Given those stored inputs :func:`rerender_from_manifest` reproduces the assembly
with model generation disabled.

What it does NOT guarantee: **model regeneration is not bit-identical.**
Gemini/Vertex narration drafts, image generation and TTS synthesis are
stochastic and provider-versioned, so the same prompt can return different bytes
on a later run.  A manifest therefore never claims that re-running the creative
pipeline reproduces a video; it claims only that re-rendering the *recorded*
assets does.  Anything stronger would be a fabricated guarantee.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from backend.job_store import write_json_atomically
from backend.projects.models import OutputMetadata, RenderManifest
from video_contract import RenderControls, VideoScript


MANIFEST_FILENAME = "render_manifest.json"
MANIFEST_CONTRACT_VERSION = 1

REPO_ROOT = Path(__file__).resolve().parent.parent
REMOTION_ROOT = REPO_ROOT / "remotion"
THEME_SOURCE_PATH = REMOTION_ROOT / "src" / "theme.ts"
PUBLIC_ROOT = REMOTION_ROOT / "public"

_FONT_FILE_SUFFIXES = {".ttf", ".otf", ".woff", ".woff2"}
# Mirrors remotion/src/theme.ts getFonts(): en-US / en select englishFonts.
_ENGLISH_LANGUAGES = {"en", "en-us"}

_DEFAULT_FONT_BLOCK_RE = re.compile(r"(?<![A-Za-z0-9_])fonts:\s*\{(.*?)\}", re.DOTALL)
_ENGLISH_FONT_BLOCK_RE = re.compile(r"englishFonts:\s*\{(.*?)\}", re.DOTALL)
# theme.ts stacks are double-quoted strings that CONTAIN single-quoted family
# names, so the quote character has to be captured and back-referenced.
_FONT_ENTRY_RE = re.compile(r"""([A-Za-z0-9_]+)\s*:\s*(["'])(.*?)\2""", re.DOTALL)

_SPEC_SCHEMA_VERSION = "1"
_KNOWN_ASPECT_RATIOS = {"9:16", "16:9", "1:1"}
_CANONICAL_COMPOSITION_ID = "VisualSystemV3Full"
_MANIFEST_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def manifest_fingerprint_payload(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return the immutable payload covered by ``manifest_fingerprint``.

    Cache-hit state is operational metadata rather than render identity.  It is
    intentionally omitted so a warm-cache reassembly has the same manifest
    fingerprint as the original render.  Every other wrapper, configuration,
    output, segment identity, and video seal field remains covered.
    """

    payload = {
        str(key): value
        for key, value in document.items()
        if key not in {"manifest_fingerprint", "created_at"}
    }
    segments = payload.get("segments")
    if isinstance(segments, list):
        normalized_segments: list[Any] = []
        for entry in segments:
            if isinstance(entry, Mapping):
                normalized = dict(entry)
                normalized.pop("cache_hit", None)
                normalized_segments.append(normalized)
            else:
                normalized_segments.append(entry)
        payload["segments"] = normalized_segments
    return payload


def compute_manifest_fingerprint(document: Mapping[str, Any]) -> str:
    """Compute the canonical SHA-256 identity for a manifest wrapper."""

    if not isinstance(document, Mapping):
        raise ValueError("manifest document must be an object")
    return hashlib.sha256(
        _canonical_json(manifest_fingerprint_payload(document)).encode("utf-8")
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# VideoSpec versioning
# --------------------------------------------------------------------------- #


def video_spec_version() -> str:
    """Deterministic VideoSpec identity derived from the contract schemas.

    ``video_contract.py`` declares no version constant, so the identity is
    computed from the JSON Schemas of :class:`VideoScript` and
    :class:`RenderControls`.  Any change to a field, a literal, a bound or a
    validator changes the schema and therefore the version, which is exactly the
    invalidation signal a render manifest needs.
    """

    payload = {
        "video_script_schema": VideoScript.model_json_schema(),
        "render_controls_schema": RenderControls.model_json_schema(),
    }
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:16]
    return f"fyf-video-spec-{_SPEC_SCHEMA_VERSION}+{digest}"


# --------------------------------------------------------------------------- #
# Font inventory (C6)
# --------------------------------------------------------------------------- #


def language_font_tag(language: str | None) -> str:
    """Return the theme.ts font-group tag for a render language."""

    normalized = str(language or "").strip().lower()
    return "en" if normalized in _ENGLISH_LANGUAGES else "my"


def declared_font_stacks(language: str | None = None) -> dict[str, str]:
    """Parse the declared ``{role: stack}`` map out of ``remotion/src/theme.ts``.

    theme.ts is the single source of truth for typography; reading it directly
    means the manifest cannot drift from what the renderer actually applies.
    """

    source = THEME_SOURCE_PATH.read_text(encoding="utf-8")
    tag = language_font_tag(language)
    pattern = _ENGLISH_FONT_BLOCK_RE if tag == "en" else _DEFAULT_FONT_BLOCK_RE
    match = pattern.search(source)
    if match is None:
        raise ValueError(f"could not resolve the {tag!r} font block in {THEME_SOURCE_PATH.name}")
    stacks: dict[str, str] = {}
    for role, _quote, value in _FONT_ENTRY_RE.findall(match.group(1)):
        stacks[role] = " ".join(value.split())
    if not stacks:
        raise ValueError(f"the {tag!r} font block in {THEME_SOURCE_PATH.name} declares no stacks")
    return stacks


def declared_font_families(language: str | None = None) -> list[str]:
    """Ordered, de-duplicated family names declared for ``language``."""

    families: list[str] = []
    for stack in declared_font_stacks(language).values():
        for raw in stack.split(","):
            name = raw.strip().strip("'\"").strip()
            if name and name not in families:
                families.append(name)
    return families


def font_file_entries() -> list[str]:
    """Content-addressed entries for every shipped font file under remotion/public."""

    entries: list[str] = []
    if PUBLIC_ROOT.is_dir():
        for path in sorted(PUBLIC_ROOT.rglob("*")):
            if path.is_file() and path.suffix.lower() in _FONT_FILE_SUFFIXES:
                relative = path.relative_to(PUBLIC_ROOT).as_posix()
                entries.append(f"file:{relative}@sha256={_sha256_file(path)}")
    return entries


def resolve_font_inventory(language: str | None = None) -> list[str]:
    """Deterministic, sorted font inventory for a render language.

    Entries cover the declared stacks (so re-ordering a fallback list
    invalidates), the individual family names, and the bytes of any shipped font
    file.  The result is safe to hash into a segment fingerprint.
    """

    tag = language_font_tag(language)
    entries: set[str] = set()
    for role, stack in declared_font_stacks(language).items():
        entries.add(f"stack:{tag}:{role}={stack}")
    for family in declared_font_families(language):
        entries.add(f"family:{family}")
    entries.update(font_file_entries())
    return sorted(entries)


# --------------------------------------------------------------------------- #
# Seeds and asset hashes
# --------------------------------------------------------------------------- #


def resolve_render_seeds(render_input: Mapping[str, Any]) -> dict[str, int]:
    """Record every explicitly declared seed in the render input.

    The current pipeline is seed-free and deterministic, so this normally
    returns ``{}``.  It exists so that a future stochastic stage cannot bypass
    the manifest: any ``seed``-style integer declared on the render input or on a
    segment is captured here and hashed into the manifest fingerprint.  Nothing
    is invented when no seed is declared.
    """

    seed_keys = ("seed", "renderSeed", "randomSeed", "noiseSeed")
    seeds: dict[str, int] = {}

    def visit(node: Any, prefix: str) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                name = str(key)
                if name in seed_keys and isinstance(value, int) and not isinstance(value, bool):
                    seeds[f"{prefix}{name}" if prefix else name] = value
                else:
                    visit(value, f"{prefix}{name}.")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                visit(value, f"{prefix}{index}.")

    visit(dict(render_input), "")
    return dict(sorted(seeds.items()))


def resolve_asset_hashes(job_dir: Path, render_input: Mapping[str, Any]) -> dict[str, str]:
    """SHA-256 of every asset any segment references, keyed by file name.

    Reuses :func:`backend.segment_render_cache._asset_hashes` so the manifest and
    the segment fingerprints can never disagree about asset identity.
    """

    from backend.segment_render_cache import _asset_hashes  # local: avoids an import cycle

    segments = render_input.get("segments")
    if not isinstance(segments, list):
        raise ValueError("render input segments must be a list")
    collected: dict[str, str] = {}
    for index, segment in enumerate(segments):
        if not isinstance(segment, Mapping):
            raise ValueError("render input segment must be an object")
        paths = _segment_asset_paths(job_dir, render_input, segment, index)
        for entry in _asset_hashes(paths):
            prior = collected.get(entry["name"])
            if prior is not None and prior != entry["sha256"]:
                raise ValueError(
                    f"ambiguous asset name {entry['name']!r} resolves to different bytes"
                )
            collected[entry["name"]] = entry["sha256"]
    audio_src = render_input.get("audioSrc")
    if isinstance(audio_src, str) and audio_src.strip():
        audio_path = Path(audio_src)
        if not audio_path.is_absolute():
            audio_path = job_dir / audio_path
        audio_path = audio_path.resolve()
        if not audio_path.is_file() or audio_path.stat().st_size <= 0:
            raise FileNotFoundError(f"render input audioSrc is missing or empty: {audio_path}")
        name = f"audio:{audio_path.name}"
        collected[name] = _sha256_file(audio_path)
    return dict(sorted(collected.items()))


def _segment_asset_paths(
    job_dir: Path, render_input: Mapping[str, Any], segment: Mapping[str, Any], index: int
) -> list[Path]:
    from backend.segment_render_cache import _segment_asset_paths as resolve

    return resolve(job_dir, render_input, segment, index)


# --------------------------------------------------------------------------- #
# Manifest construction
# --------------------------------------------------------------------------- #


def resolve_skill_versions() -> dict[str, str]:
    """Skill version manifest from the D-I skills facade (imported lazily)."""

    from backend.agent import skills

    return dict(sorted(skills.version_manifest().items()))


def resolve_output_metadata(
    render_input: Mapping[str, Any], *, total_frames: int | None = None
) -> OutputMetadata:
    controls = render_input.get("render_controls")
    aspect_ratio = None
    if isinstance(controls, Mapping):
        raw_ratio = controls.get("aspect_ratio")
        if isinstance(raw_ratio, str):
            aspect_ratio = raw_ratio
    if aspect_ratio is None:
        raw_ratio = render_input.get("aspect_ratio")
        aspect_ratio = raw_ratio if isinstance(raw_ratio, str) else None

    raw_fps = render_input.get("fps")
    fps: int | None = None
    if isinstance(raw_fps, (int, float)) and not isinstance(raw_fps, bool) and raw_fps > 0:
        fps = int(round(float(raw_fps)))
    duration_seconds: float | None = None
    frames = total_frames
    if frames is None:
        raw_frames = render_input.get("durationInFrames")
        frames = raw_frames if isinstance(raw_frames, int) and not isinstance(raw_frames, bool) else None
    if frames is not None and fps:
        duration_seconds = round(frames / float(fps), 3)

    width = render_input.get("width")
    height = render_input.get("height")
    return OutputMetadata(
        # OutputMetadata.aspect_ratio is a closed Literal; an unrecognised value
        # is recorded as absent rather than coerced into a supported ratio.
        aspect_ratio=aspect_ratio if aspect_ratio in _KNOWN_ASPECT_RATIOS else None,
        width=int(width) if isinstance(width, int) and not isinstance(width, bool) else None,
        height=int(height) if isinstance(height, int) and not isinstance(height, bool) else None,
        duration_seconds=duration_seconds,
        fps=fps,
        codec="h264",
    )


def build_render_manifest(
    job_dir: str | Path,
    render_input: Mapping[str, Any],
    *,
    renderer_version: str | None = None,
    skill_versions: Mapping[str, str] | None = None,
    total_frames: int | None = None,
    include_asset_hashes: bool = True,
) -> RenderManifest:
    """Assemble a :class:`RenderManifest` for a staged job directory.

    This is the existing model in ``backend/projects/models.py`` — it is reused,
    never redefined, so persisted project versions keep validating.
    """

    root = Path(job_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"job directory not found: {root}")

    if renderer_version is None:
        from backend.segment_render_cache import _installed_remotion_version

        renderer_version = _installed_remotion_version()
    resolved_skills = (
        dict(sorted(skill_versions.items()))
        if skill_versions is not None
        else resolve_skill_versions()
    )
    asset_hashes = (
        resolve_asset_hashes(root, render_input) if include_asset_hashes else {}
    )

    return RenderManifest(
        video_spec_version=video_spec_version(),
        asset_hashes=asset_hashes,
        fonts=resolve_font_inventory(render_input.get("language")),
        renderer_version=renderer_version,
        skill_versions=resolved_skills,
        seeds=resolve_render_seeds(render_input),
        output=resolve_output_metadata(render_input, total_frames=total_frames),
    )


def manifest_spec_payload(manifest: RenderManifest) -> dict[str, Any]:
    """JSON-safe manifest payload folded into the manifest fingerprint."""

    return manifest.model_dump(mode="json")


def manifest_document(
    manifest: RenderManifest,
    *,
    results: Sequence[Any],
    manifest_fingerprint: str,
    renderer_source_hash: str,
    composition_id: str,
    remotion_version: str,
    reduced_motion: bool = False,
    created_at: str | None = None,
    video_sha256: str | None = None,
    video_bytes: int | None = None,
) -> dict[str, Any]:
    """Wrap a ``RenderManifest`` into the persisted ``render_manifest.json``.

    ``RenderManifest`` uses ``extra="forbid"`` and cannot grow fields, so the
    reproducibility evidence that is not part of the contract (per-segment
    hashes, renderer source hash, motion mode, generation marker) lives in this
    wrapper document alongside the validated manifest.
    """

    if not isinstance(manifest_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", manifest_fingerprint):
        raise ValueError("manifest_fingerprint must be a lowercase sha256 hex digest")
    if not isinstance(renderer_source_hash, str) or not renderer_source_hash:
        raise ValueError("renderer_source_hash must be a non-blank string")

    segments: list[dict[str, Any]] = []
    for result in results:
        path = Path(result.path)
        if not path.is_file() or path.stat().st_size <= 0:
            raise FileNotFoundError(f"rendered segment is missing or empty: {path}")
        try:
            segment_id = str(result.segment_id)
            fingerprint = str(result.fingerprint)
            frame_count = int(result.frame_count)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("rendered segment result is incomplete") from exc
        if not _MANIFEST_FINGERPRINT_RE.fullmatch(fingerprint):
            raise ValueError("segment fingerprint must be a lowercase SHA-256 hex digest")
        if frame_count <= 0:
            raise ValueError("segment frame_count must be positive")
        # Cache paths are canonical and content addressed.  Recording the
        # relative path makes a later segment-ID substitution detectable even
        # when an attacker leaves a same-bytes file at the substituted name.
        relative_path = f"render-segments/{path.name}"
        segments.append(
            {
                "segment_id": segment_id,
                "fingerprint": fingerprint,
                "path": relative_path,
                "frame_count": frame_count,
                "cache_hit": bool(result.cache_hit),
                "sha256": _sha256_file(path),
                "bytes": path.stat().st_size,
            }
        )

    document: dict[str, Any] = {
        "manifest_contract_version": MANIFEST_CONTRACT_VERSION,
        "created_at": created_at or utc_now_iso(),
        "generation_enabled": False,
        "reproducibility_scope": "stored-assets-only",
        "model_regeneration_bit_identical": False,
        "manifest_fingerprint": manifest_fingerprint,
        "composition_id": composition_id,
        "remotion_version": remotion_version,
        "renderer_source_hash": renderer_source_hash,
        "motion_mode": "reduced" if reduced_motion else "full",
        "render_strategy": "segmented" if segments else "monolithic",
        "output_settings": {
            "fps": manifest.output.fps,
            "codec": manifest.output.codec,
            "pixel_format": "yuv420p",
            "width": manifest.output.width,
            "height": manifest.output.height,
        },
        "source_geometry": {
            "width": manifest.output.width,
            "height": manifest.output.height,
        },
        "render_manifest": manifest.model_dump(mode="json"),
        "segments": segments,
    }
    if video_sha256 is not None:
        document["video_sha256"] = video_sha256
    if video_bytes is not None:
        document["video_bytes"] = int(video_bytes)
    return document


def seal_manifest_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy with its canonical wrapper fingerprint sealed."""

    sealed = dict(document)
    sealed["manifest_fingerprint"] = compute_manifest_fingerprint(sealed)
    return sealed


def write_render_manifest(job_dir: str | Path, document: Mapping[str, Any]) -> Path:
    """Atomically persist ``render_manifest.json`` inside a job directory."""

    root = Path(job_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"job directory not found: {root}")
    if document.get("manifest_contract_version") != MANIFEST_CONTRACT_VERSION:
        raise ValueError("refusing to persist a manifest document with an unknown contract version")
    # Fail closed rather than persist a document that cannot be revalidated.
    RenderManifest.model_validate(document["render_manifest"])
    fingerprint = document.get("manifest_fingerprint")
    if (
        not isinstance(fingerprint, str)
        or _MANIFEST_FINGERPRINT_RE.fullmatch(fingerprint) is None
        or compute_manifest_fingerprint(document) != fingerprint
    ):
        raise ValueError("refusing to persist an unsealed render manifest")
    target = root / MANIFEST_FILENAME
    write_json_atomically(target, dict(document))
    return target


def read_render_manifest(job_dir: str | Path) -> dict[str, Any] | None:
    """Read and revalidate a persisted manifest document, or return ``None``."""

    path = Path(job_dir) / MANIFEST_FILENAME
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{MANIFEST_FILENAME} is corrupt") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{MANIFEST_FILENAME} must contain an object")
    if document.get("manifest_contract_version") != MANIFEST_CONTRACT_VERSION:
        raise ValueError(
            f"unsupported manifest contract version: {document.get('manifest_contract_version')!r}"
        )
    RenderManifest.model_validate(document.get("render_manifest"))
    return document


# --------------------------------------------------------------------------- #
# Verification and re-render from stored assets
# --------------------------------------------------------------------------- #


def _load_optional_render_input(root: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """Load render_input.json when present without hiding corruption."""

    path = root / "render_input.json"
    if not path.is_file():
        return None, []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, ["render_input.json is corrupt"]
    if not isinstance(payload, dict):
        return None, ["render_input.json must contain an object"]
    return payload, []


def _manifest_wrapper_issues(
    root: Path, document: Mapping[str, Any]
) -> tuple[RenderManifest | None, list[str]]:
    """Validate the wrapper and its immutable metadata without touching media."""

    issues: list[str] = []
    if not isinstance(document, Mapping):
        return None, ["manifest document must be an object"]

    if document.get("manifest_contract_version") != MANIFEST_CONTRACT_VERSION:
        issues.append("unsupported manifest contract version")

    raw_manifest = document.get("render_manifest")
    try:
        manifest = RenderManifest.model_validate(raw_manifest)
    except Exception as exc:  # pydantic's concrete ValidationError varies by version
        return None, [f"render_manifest is invalid: {exc}"]

    fingerprint = document.get("manifest_fingerprint")
    if not isinstance(fingerprint, str) or _MANIFEST_FINGERPRINT_RE.fullmatch(fingerprint) is None:
        issues.append("manifest_fingerprint is not a lowercase SHA-256 digest")
    else:
        try:
            expected = compute_manifest_fingerprint(document)
        except (TypeError, ValueError):
            expected = None
        if expected != fingerprint:
            issues.append("manifest fingerprint does not cover the stored metadata")

    created_at = document.get("created_at")
    if not isinstance(created_at, str) or not created_at.strip():
        issues.append("manifest created_at is missing")
    else:
        try:
            datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            issues.append("manifest created_at is invalid")

    if document.get("generation_enabled") is not False:
        issues.append("manifest generation_enabled must be false")
    if document.get("reproducibility_scope") != "stored-assets-only":
        issues.append("manifest reproducibility scope is not stored-assets-only")
    if document.get("model_regeneration_bit_identical") is not False:
        issues.append("manifest cannot claim bit-identical model regeneration")
    if document.get("composition_id") != _CANONICAL_COMPOSITION_ID:
        issues.append("manifest composition_id does not match the canonical composition")

    remotion_version = document.get("remotion_version")
    if not isinstance(remotion_version, str) or not remotion_version.strip():
        issues.append("manifest remotion_version is missing")
    elif manifest.renderer_version != remotion_version:
        issues.append("manifest renderer versions disagree")

    renderer_source_hash = document.get("renderer_source_hash")
    if not isinstance(renderer_source_hash, str) or not renderer_source_hash.strip():
        issues.append("manifest renderer_source_hash is missing")

    motion_mode = document.get("motion_mode")
    if motion_mode not in {"full", "reduced"}:
        issues.append("manifest motion_mode is invalid")

    render_strategy = document.get("render_strategy")
    if render_strategy not in {"monolithic", "segmented"}:
        issues.append("manifest render_strategy is invalid")
    raw_segments = document.get("segments")
    if render_strategy == "segmented" and not raw_segments:
        issues.append("segmented manifest requires segment evidence")
    if render_strategy == "monolithic" and raw_segments:
        issues.append("monolithic manifest cannot contain segment evidence")

    output = manifest.output.model_dump(mode="json")
    required_output = ("fps", "width", "height", "codec")
    if any(output.get(field) is None for field in required_output):
        issues.append("manifest output metadata is incomplete")
    expected_settings = {
        "fps": output.get("fps"),
        "codec": output.get("codec"),
        "pixel_format": "yuv420p",
        "width": output.get("width"),
        "height": output.get("height"),
    }
    if document.get("output_settings") != expected_settings:
        issues.append("manifest output_settings disagree with render_manifest.output")
    if document.get("source_geometry") != {
        "width": output.get("width"),
        "height": output.get("height"),
    }:
        issues.append("manifest source geometry disagrees with render_manifest.output")

    # A seal is optional for a standalone manifest fixture, but whenever it is
    # present both fields must authenticate the assembled output on disk.
    has_video_sha = "video_sha256" in document
    has_video_bytes = "video_bytes" in document
    if has_video_sha != has_video_bytes:
        issues.append("manifest video seal must include both hash and byte count")
    if has_video_sha:
        video_sha = document.get("video_sha256")
        video_bytes = document.get("video_bytes")
        if not isinstance(video_sha, str) or _MANIFEST_FINGERPRINT_RE.fullmatch(video_sha) is None:
            issues.append("manifest video_sha256 is invalid")
        if not isinstance(video_bytes, int) or isinstance(video_bytes, bool) or video_bytes <= 0:
            issues.append("manifest video_bytes is invalid")
        video_path = root / "video.mp4"
        if not video_path.is_file() or video_path.stat().st_size <= 0:
            issues.append("sealed manifest video.mp4 is missing or empty")
        elif isinstance(video_sha, str) and isinstance(video_bytes, int):
            if video_path.stat().st_size != video_bytes or _sha256_file(video_path) != video_sha:
                issues.append("manifest video seal does not match video.mp4")

    return manifest, issues


def verify_render_manifest(
    job_dir: str | Path, document: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Compare a manifest wrapper, its config, assets, segments, and output seal."""

    root = Path(job_dir)
    resolved = dict(document) if document is not None else (read_render_manifest(root) or {})
    if not resolved:
        raise FileNotFoundError(f"{MANIFEST_FILENAME} not found in {root}")

    manifest, integrity_mismatches = _manifest_wrapper_issues(root, resolved)
    if manifest is None:
        return {
            "verified": False,
            "manifest_fingerprint": resolved.get("manifest_fingerprint"),
            "video_spec_version": None,
            "fonts": [],
            "asset_mismatches": [],
            "missing_assets": [],
            "segment_mismatches": [],
            "missing_segments": [],
            "integrity_mismatches": integrity_mismatches,
            "generation_enabled": bool(resolved.get("generation_enabled")),
            "model_regeneration_bit_identical": bool(
                resolved.get("model_regeneration_bit_identical")
            ),
        }

    render_input, input_issues = _load_optional_render_input(root)
    integrity_mismatches.extend(input_issues)
    if render_input is not None:
        output = manifest.output
        raw_fps = render_input.get("fps")
        if isinstance(raw_fps, (int, float)) and not isinstance(raw_fps, bool):
            if output.fps != int(round(float(raw_fps))):
                integrity_mismatches.append("manifest FPS disagrees with render_input.json")
        for field in ("width", "height"):
            raw_value = render_input.get(field)
            if isinstance(raw_value, int) and not isinstance(raw_value, bool):
                if getattr(output, field) != raw_value:
                    integrity_mismatches.append(
                        f"manifest {field} disagrees with render_input.json"
                    )
        raw_duration = render_input.get("durationInFrames")
        if isinstance(raw_duration, int) and not isinstance(raw_duration, bool) and output.fps:
            expected_duration = round(raw_duration / float(output.fps), 3)
            if output.duration_seconds != expected_duration:
                integrity_mismatches.append("manifest duration disagrees with render_input.json")
        requested_motion = render_input.get("reduced_motion")
        if isinstance(requested_motion, bool):
            expected_motion = "reduced" if requested_motion else "full"
            if resolved.get("motion_mode") != expected_motion:
                integrity_mismatches.append("manifest motion mode disagrees with render_input.json")

        raw_segments = render_input.get("segments")
        manifest_segments = resolved.get("segments")
        if isinstance(raw_segments, list) and isinstance(manifest_segments, list) and manifest_segments:
            input_ids = [
                str(entry.get("id") or entry.get("segment_id"))
                for entry in raw_segments
                if isinstance(entry, Mapping)
            ]
            stored_ids = [
                str(entry.get("segment_id"))
                for entry in manifest_segments
                if isinstance(entry, Mapping)
            ]
            if input_ids != stored_ids:
                integrity_mismatches.append("manifest segment identities disagree with render_input.json")

    asset_mismatches: list[dict[str, str]] = []
    missing_assets: list[str] = []
    for name, expected in sorted(manifest.asset_hashes.items()):
        matches: list[str] = []
        if name.startswith("audio:") and render_input is not None:
            audio_src = render_input.get("audioSrc")
            if isinstance(audio_src, str) and audio_src.strip():
                audio_path = Path(audio_src)
                if not audio_path.is_absolute():
                    audio_path = root / audio_path
                audio_path = audio_path.resolve()
                if audio_path.name == name.removeprefix("audio:") and audio_path.is_file():
                    matches = [str(audio_path)]
        elif not name.startswith("audio:"):
            matches = sorted({str(path) for path in root.rglob(name) if path.is_file()})
        if not matches and not name.startswith("audio:"):
            public = PUBLIC_ROOT / name
            matches = [str(public)] if public.is_file() else []
        if not matches:
            missing_assets.append(name)
            continue
        if not any(_sha256_file(Path(candidate)) == expected for candidate in matches):
            asset_mismatches.append({"name": name, "expected_sha256": expected})

    segment_mismatches: list[dict[str, str]] = []
    missing_segments: list[str] = []
    seen_segment_ids: set[str] = set()
    cache_root = root / "render-segments"
    raw_segment_entries = resolved.get("segments")
    if not isinstance(raw_segment_entries, list):
        integrity_mismatches.append("manifest segments must be a list")
        raw_segment_entries = []
    for entry in raw_segment_entries:
        if not isinstance(entry, Mapping):
            integrity_mismatches.append("manifest segment entry must be an object")
            continue
        segment_id = entry.get("segment_id")
        fingerprint = entry.get("fingerprint")
        if not isinstance(segment_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", segment_id):
            integrity_mismatches.append("manifest segment ID is unsafe")
            continue
        if segment_id in seen_segment_ids:
            integrity_mismatches.append(f"manifest contains duplicate segment ID: {segment_id}")
            continue
        seen_segment_ids.add(segment_id)
        if not isinstance(fingerprint, str) or _MANIFEST_FINGERPRINT_RE.fullmatch(fingerprint) is None:
            integrity_mismatches.append(f"manifest segment fingerprint is invalid: {segment_id}")
            continue
        candidate = cache_root / f"{segment_id}-{fingerprint[:16]}.mp4"
        recorded_path = entry.get("path")
        if recorded_path is not None and recorded_path != f"render-segments/{candidate.name}":
            integrity_mismatches.append(f"manifest segment path does not match identity: {segment_id}")
        if not candidate.is_file():
            missing_segments.append(segment_id)
            continue
        expected_sha = entry.get("sha256")
        expected_bytes = entry.get("bytes")
        if not isinstance(expected_sha, str) or _MANIFEST_FINGERPRINT_RE.fullmatch(expected_sha) is None:
            integrity_mismatches.append(f"manifest segment hash is invalid: {segment_id}")
            continue
        actual_bytes = candidate.stat().st_size
        actual_sha = _sha256_file(candidate)
        if not isinstance(expected_bytes, int) or isinstance(expected_bytes, bool) or expected_bytes != actual_bytes:
            segment_mismatches.append(
                {"segment_id": segment_id, "expected_sha256": expected_sha, "actual_sha256": actual_sha}
            )
        elif actual_sha != expected_sha:
            segment_mismatches.append(
                {"segment_id": segment_id, "expected_sha256": expected_sha, "actual_sha256": actual_sha}
            )

    verified = not (
        integrity_mismatches
        or asset_mismatches
        or missing_assets
        or segment_mismatches
        or missing_segments
    )
    return {
        "verified": verified,
        "manifest_fingerprint": resolved.get("manifest_fingerprint"),
        "video_spec_version": manifest.video_spec_version,
        "fonts": list(manifest.fonts),
        "asset_mismatches": asset_mismatches,
        "missing_assets": missing_assets,
        "segment_mismatches": segment_mismatches,
        "missing_segments": missing_segments,
        "integrity_mismatches": integrity_mismatches,
        "generation_enabled": bool(resolved.get("generation_enabled")),
        "model_regeneration_bit_identical": bool(
            resolved.get("model_regeneration_bit_identical")
        ),
    }


def rerender_from_manifest(
    job_dir: str | Path,
    *,
    document: Mapping[str, Any] | None = None,
    render: bool = True,
) -> dict[str, Any]:
    """Re-render entrypoint that reproduces output from STORED ASSETS ONLY.

    Model generation is disabled by construction: this function accepts no
    script, no provider and no prompt, and the only processes it may start are
    the Remotion renderer and ffmpeg operating on the bytes already present in
    ``job_dir``.  It first verifies that every asset recorded in the manifest
    still hashes to the recorded value and fails closed if not, because
    re-rendering against substituted assets would silently produce a different
    video under the same manifest fingerprint.

    The returned report states plainly whether the reproduction matched, and
    repeats the honest scope marker: regenerating the creative models is not
    bit-identical, so a match here is evidence about stored assets only.
    """

    root = Path(job_dir)
    resolved = dict(document) if document is not None else (read_render_manifest(root) or {})
    if not resolved:
        raise FileNotFoundError(f"{MANIFEST_FILENAME} not found in {root}")
    if resolved.get("generation_enabled") is not False:
        raise ValueError("manifest was produced with generation enabled; refusing to treat it as reproducible")

    verification = verify_render_manifest(root, resolved)
    if not verification["verified"]:
        return {
            "reproduced": False,
            "reason": "stored inputs no longer match the manifest",
            "rendered": False,
            "model_regeneration_bit_identical": False,
            **verification,
        }

    report: dict[str, Any] = {
        "verified": True,
        "rendered": False,
        "generation_enabled": False,
        "model_regeneration_bit_identical": False,
        "reproducibility_scope": "stored-assets-only",
        "expected_manifest_fingerprint": resolved.get("manifest_fingerprint"),
        **verification,
    }
    if not render:
        report["reproduced"] = True
        report["reason"] = "verification only; no render requested"
        return report

    strategy = str(resolved["render_strategy"])
    expected_fingerprint = resolved.get("manifest_fingerprint")
    report["render_strategy"] = strategy

    if strategy == "segmented":
        from backend.segment_render_cache import render_segments_and_assemble

        assembly = render_segments_and_assemble(str(root))
        output_path = str(assembly.output_path)
        report.update(
            {
                "assembly_manifest_fingerprint": assembly.manifest_fingerprint,
                "cache_hits": assembly.cache_hits,
                "rendered_segments": assembly.rendered_segments,
            }
        )
    else:
        from backend.render_video import render_video_remotion

        output_path = str(render_video_remotion(str(root)))

    # Both renderers persist the authoritative sealed wrapper.  The segmented
    # renderer also exposes a cache/assembly fingerprint, but that fingerprint
    # deliberately has a different scope and must never be compared with the
    # sealed document identity.
    actual_document = read_render_manifest(root)
    if actual_document is None:
        raise RuntimeError("renderer did not persist a render manifest")
    actual_verification = verify_render_manifest(root, actual_document)
    actual_fingerprint = actual_document.get("manifest_fingerprint")

    expected_segments = {
        entry["segment_id"]: entry["sha256"]
        for entry in resolved.get("segments") or []
    }
    actual_segments = {
        entry["segment_id"]: entry["sha256"]
        for entry in actual_document.get("segments") or []
    }
    segments_match = expected_segments == actual_segments
    report.update(
        {
            "rendered": True,
            "reproduced": bool(actual_verification["verified"])
            and actual_document.get("render_strategy") == strategy
            and actual_fingerprint == expected_fingerprint
            and segments_match,
            "actual_manifest_fingerprint": actual_fingerprint,
            "segments_total": len(expected_segments),
            "segments_reproduced": (
                len(expected_segments) if segments_match else 0
            ),
            "output_path": output_path,
            "post_render_verification": actual_verification,
        }
    )
    report["reason"] = (
        "assembly reproduced from stored assets"
        if report["reproduced"]
        else "assembly did not reproduce; see fingerprints above"
    )
    return report
