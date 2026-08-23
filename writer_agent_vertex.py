import os
import json
import time
import logging
from google import genai
from google.genai import types
from video_contract import ClaimCoverageResponse, CompactVisualPlanResponse, EvidenceClaimsResponse, ExactLockRequest, MotionGraphicSpec, StoryboardResponse, StoryDraftModesResponse, StoryDraftScript, StoryModesResponse, VideoScript, VisualTreatment
from vertex_model_routing import model_for
from backend.vertex_telemetry import telemetry_retry_attempt, track_client
from backend.vertex_thinking import generation_config_for

DEFAULT_LOCATION = "global"
DEFAULT_STORY_LOCATION = "global"
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_VERTEX_RETRY_BASE_SECONDS = 10.0
DEFAULT_VERTEX_RETRY_MAX_SECONDS = 60.0
DEFAULT_VERTEX_CALL_TIMEOUT_SECONDS = 120
logger = logging.getLogger(__name__)


def _vertex_json_schema(model: type) -> dict:
    """Convert Pydantic JSON Schema to Vertex's supported JSON subset."""
    unsupported = {
        "default", "discriminator", "examples", "minLength", "maxLength",
        "minItems", "maxItems", "minimum", "maximum", "exclusiveMinimum",
        "exclusiveMaximum",
    }

    def clean(value):
        if isinstance(value, list):
            return [clean(item) for item in value]
        if not isinstance(value, dict):
            return value

        cleaned = {}
        for key, item in value.items():
            if key in unsupported:
                continue
            if key == "const":
                cleaned["enum"] = [item]
                continue
            cleaned[key] = clean(item)
        return cleaned

    return clean(model.model_json_schema())


def _max_attempts() -> int:
    try:
        configured = int(
            os.getenv("FYF_VERTEX_MAX_ATTEMPTS", str(DEFAULT_MAX_ATTEMPTS))
        )
    except ValueError:
        configured = DEFAULT_MAX_ATTEMPTS
    return max(1, min(3, configured))


def _stage_model(stage: str, attempt: int = 0) -> str:
    if attempt > 0:
        return model_for("story_fallback")
    route = {"script": "script", "story": "story_polish", "lock": "lock"}[stage]
    return model_for(route)


def _vertex_call_timeout_ms() -> int:
    try:
        timeout_seconds = int(os.getenv(
            "FYF_VERTEX_CALL_TIMEOUT_SECONDS",
            str(DEFAULT_VERTEX_CALL_TIMEOUT_SECONDS),
        ))
    except ValueError:
        timeout_seconds = DEFAULT_VERTEX_CALL_TIMEOUT_SECONDS
    return max(10, min(300, timeout_seconds)) * 1000


def _transient_vertex_error(error: Exception) -> bool:
    code = getattr(error, "code", None) or getattr(error, "status_code", None)
    return code in {429, 500, 502, 503, 504}


def _sleep_before_vertex_retry(attempt: int) -> None:
    try:
        base_delay = float(os.getenv(
            "FYF_VERTEX_RETRY_BASE_SECONDS",
            str(DEFAULT_VERTEX_RETRY_BASE_SECONDS),
        ))
    except ValueError:
        base_delay = DEFAULT_VERTEX_RETRY_BASE_SECONDS
    try:
        max_delay = float(os.getenv(
            "FYF_VERTEX_RETRY_MAX_SECONDS",
            str(DEFAULT_VERTEX_RETRY_MAX_SECONDS),
        ))
    except ValueError:
        max_delay = DEFAULT_VERTEX_RETRY_MAX_SECONDS
    delay = min(
        max(0.0, base_delay) * (2 ** max(0, attempt)),
        max(0.0, max_delay),
    )
    if delay:
        time.sleep(delay)


def _drop_invalid_optional_treatments(payload: dict) -> None:
    """Discard malformed optional director metadata so shot evidence can still be validated."""
    for segment in payload.get("segments", []):
        if not isinstance(segment, dict):
            continue
        visual = segment.get("visual")
        shots = segment.get("evidence_shots")
        if isinstance(visual, dict):
            shots = visual.get("evidence_shots", shots)
        if not isinstance(shots, list):
            continue
        for shot in shots:
            if not isinstance(shot, dict) or shot.get("treatment") is None:
                continue
            try:
                VisualTreatment.model_validate(shot["treatment"])
            except Exception:
                shot["treatment"] = None


def _stage_client(stage: str, attempt: int = 0) -> genai.Client:
    from backend.vertex_client import vertex_client_kwargs
    client = genai.Client(
        **vertex_client_kwargs(location=_stage_location(stage, attempt)),
        http_options=types.HttpOptions(
            timeout=_vertex_call_timeout_ms(),
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
    return track_client(client, stage=stage, attempt=attempt + 1)


def _extract_evidence_claims(
    request: ExactLockRequest,
    repair_feedback: str | None = None,
    attempt: int = 0,
) -> EvidenceClaimsResponse:
    """Vertex Fact Agent: extracts claims only and cannot author visuals or narration."""
    client = _stage_client("fact")
    prompt = "Extract only claims that a viewer must directly see. Preserve IDs.\n"
    for segment in request.approved_segments:
        prompt += f"ID {segment.id}: {segment.text}\n"
    if repair_feedback:
        prompt += (
            "\nA separate coverage auditor found omissions. Re-extract the complete claim "
            "set and correct every issue without changing narration:\n" + repair_feedback
        )
    with telemetry_retry_attempt("fact_claims", attempt + 1):
        response = client.models.generate_content(
            model=model_for("fact_extraction"),
            contents=prompt,
            config=generation_config_for("fact",
                system_instruction=(
                    "You are the FYF Fact Agent. Return factual and conceptual claims only. "
                    "For exact quantities use evidence_type count and put normalized values in values. "
                    "For contrasts use comparison, for causal or ordered events use sequence or relationship. "
                    "Do not produce visual prompts, captions, narration, timestamps, or assets."
                ),
                response_mime_type="application/json",
                response_json_schema=_vertex_json_schema(EvidenceClaimsResponse),
            ),
        )
    result = EvidenceClaimsResponse.model_validate_json(response.text or "")
    expected = {segment.id for segment in request.approved_segments}
    if {segment.id for segment in result.segments} != expected:
        raise ValueError("Fact Agent segment IDs do not match approved narration IDs")
    return result


def _verify_claim_completeness(
    request: ExactLockRequest,
    claims: EvidenceClaimsResponse,
    attempt: int = 0,
) -> ClaimCoverageResponse:
    """Independent Vertex gate against a Fact Agent silently omitting narration meaning."""
    client = _stage_client("fact")
    with telemetry_retry_attempt("fact_coverage", attempt + 1):
        response = client.models.generate_content(
            model=model_for("visual_verification"),
            contents=(
                "Audit whether the extracted claims completely cover every factual assertion, "
                "causal relationship, ordered action, contrast, and decision rule in each exact "
                "narration segment that the finished visual must communicate. A paraphrase is "
                "allowed, omission or altered meaning is not. Ignore rhetorical audience questions "
                "that ask for the viewer's personal answer; they are engagement copy, not visual evidence. "
                "Return the same segment IDs.\nNarration: "
                + request.model_dump_json()
                + "\nExtracted claims: "
                + claims.model_dump_json()
            ),
            config=generation_config_for("fact_coverage",
                response_mime_type="application/json",
                response_json_schema=_vertex_json_schema(ClaimCoverageResponse),
            ),
        )
    coverage = ClaimCoverageResponse.model_validate_json(response.text or "")
    expected_ids = {segment.id for segment in request.approved_segments}
    if {segment.id for segment in coverage.segments} != expected_ids:
        raise ValueError("Claim completeness verifier segment IDs do not match narration")
    failed = [segment for segment in coverage.segments if not segment.passed or segment.missing_claims]
    if failed:
        detail = "; ".join(
            f"{item.id}: {', '.join(item.missing_claims + item.issues) or 'incomplete claims'}"
            for item in failed
        )
        raise ValueError(f"Fact Agent claim coverage incomplete: {detail}")
    return coverage


def _extract_complete_evidence_claims(request: ExactLockRequest) -> EvidenceClaimsResponse:
    """Retry transient Fact Agent calls locally, then fail closed on missing coverage."""
    feedback: str | None = None
    last_error: ValueError | None = None
    last_request_error: Exception | None = None
    attempts = _max_attempts()
    for attempt in range(attempts):
        try:
            claims = _extract_evidence_claims(request, feedback, attempt)
            _verify_claim_completeness(request, claims, attempt)
            return claims
        except ValueError as exc:
            last_error = exc
            feedback = str(exc)
        except Exception as exc:
            last_request_error = exc
            if attempt + 1 == attempts:
                raise RuntimeError(f"Vertex AI fact verification failed: {exc}") from exc
            if _transient_vertex_error(exc):
                _sleep_before_vertex_retry(attempt)
            else:
                raise
    if last_request_error and not last_error:
        raise RuntimeError(f"Vertex AI fact verification failed: {last_request_error}")
    raise last_error or ValueError("Fact Agent claim coverage incomplete")


def _direct_storyboard(
    request: ExactLockRequest,
    visual_plan: CompactVisualPlanResponse,
    attempt: int = 0,
) -> StoryboardResponse:
    """Vertex Storyboard Director turns evidence requirements into paced shot coverage."""
    client = _stage_client("storyboard", attempt)
    storyboard_model = (
        model_for("storyboard_direction")
        if attempt == 0
        else model_for("story_fallback")
    )
    with telemetry_retry_attempt("storyboard", attempt + 1):
        response = client.models.generate_content(
            model=storyboard_model,
            contents=(
                "Create the final ordered evidence-shot storyboard for these exact narration "
            "segments and immutable claims. Return only segment IDs and evidence_shots. "
            "Use 1-3 shots per segment, one clear focal action per shot, varied compositions, "
            "and hold fractions summing to 1 per segment. Split different claims into separate "
            "shots when one frame cannot prove both. Every sequence claim MUST be covered by "
            "a deterministic motion_graphic with layout sequence, even when an optional "
            "generated_video is also used, so video fallback never loses the sequence. Exact "
            "A relationship claim that states a cause or consequence MUST share at least one "
            "deterministic sequence shot with the related sequence claim; do not isolate the "
            "consequence in a still image that hides its cause. "
            "counts/comparisons use motion_graphic. Concept and relationship claims must be "
            "visibly explicit through objects/actions or concise Burmese labels. Do not use "
            "generic paper cards. Across a story with four or more segments, use at least two "
            "generated_image or generated_video shots for concrete story moments and reserve "
            "motion_graphic for claims that truly need diagrammatic precision. Never make the "
            "entire video a repeated row of boxes or cards. Choose transition, composition, mascot_presence, and motion "
            "preset intentionally. Every non-kinetic treatment MUST include focal_object, action, and change; only "
            "kinetic_type may leave those fields empty. The motion_spec.relation_mode field may be present only when "
            "the layout is relationship; omit relation_mode for every other layout. Leave assets null and verification "
            "planned. Do not change, "
            "omit, or invent claim IDs.\nApproved narration: "
            + request.model_dump_json()
            + "\nEvidence plan: "
                + visual_plan.model_dump_json()
            ),
            config=generation_config_for("storyboard",
                response_mime_type="application/json",
                response_json_schema=_vertex_json_schema(StoryboardResponse),
            ),
        )
    raw_storyboard = json.loads(response.text or "")
    _drop_invalid_optional_treatments(raw_storyboard)
    for raw_segment in raw_storyboard.get("segments", []):
        for raw_shot in raw_segment.get("evidence_shots", []):
            if raw_shot.get("media_type") == "motion_graphic" and not raw_shot.get("motion_spec"):
                raw_shot["media_type"] = "generated_image"
                raw_shot["motion_spec"] = None
    storyboard = StoryboardResponse.model_validate(raw_storyboard)
    expected_ids = {segment.id for segment in visual_plan.segments}
    if {segment.id for segment in storyboard.segments} != expected_ids:
        storyboard = _reconcile_storyboard_segment_ids(storyboard, visual_plan)
    storyboard = _normalize_deterministic_sequence_shots(storyboard, visual_plan)
    plans = {segment.id: segment for segment in visual_plan.segments}
    if len(storyboard.segments) >= 4:
        generated_count = sum(
            shot.media_type in {"generated_image", "generated_video"}
            for segment in storyboard.segments
            for shot in segment.evidence_shots
        )
        if generated_count < 2:
            for segment in storyboard.segments:
                claim_types = {
                    claim.evidence_type
                    for claim in plans[segment.id].evidence_claims
                }
                if not claim_types or not claim_types.issubset({"concept", "relationship"}):
                    continue
                for shot in segment.evidence_shots:
                    if shot.media_type != "motion_graphic":
                        continue
                    shot.media_type = "generated_image"
                    shot.motion_spec = None
                    generated_count += 1
                    break
                if generated_count >= 2:
                    break
    for segment in storyboard.segments:
        expected_claims = {claim.claim_id: claim for claim in plans[segment.id].evidence_claims}
        covered = {claim_id for shot in segment.evidence_shots for claim_id in shot.proves_claim_ids}
        if covered != set(expected_claims):
            raise ValueError(f"Storyboard claim coverage mismatch for segment {segment.id}")
        total = sum(shot.hold_fraction for shot in segment.evidence_shots)
        if abs(total - 1) > 0.02:
            raise ValueError(f"Storyboard hold fractions must sum to 1 for segment {segment.id}")
        sequence_ids = {
            claim_id for claim_id, claim in expected_claims.items()
            if claim.evidence_type == "sequence"
        }
        deterministic_sequence = {
            claim_id for shot in segment.evidence_shots
            if shot.media_type == "motion_graphic" and shot.motion_spec and shot.motion_spec.layout == "sequence"
            for claim_id in shot.proves_claim_ids
        }
        if not sequence_ids.issubset(deterministic_sequence):
            raise ValueError(f"Storyboard sequence claims require deterministic sequence shots for segment {segment.id}")
        relationship_ids = {
            claim_id for claim_id, claim in expected_claims.items()
            if claim.evidence_type == "relationship"
        }
        for relationship_id in relationship_ids:
            if sequence_ids and not any(
                shot.media_type == "motion_graphic"
                and shot.motion_spec
                and shot.motion_spec.layout == "sequence"
                and relationship_id in shot.proves_claim_ids
                and bool(sequence_ids.intersection(shot.proves_claim_ids))
                for shot in segment.evidence_shots
            ):
                raise ValueError(
                    f"Storyboard relationship claim {relationship_id} must share a deterministic "
                    f"sequence shot with its causal sequence in segment {segment.id}"
                )
    if len(storyboard.segments) >= 4:
        generated_shots = [
            shot
            for segment in storyboard.segments
            for shot in segment.evidence_shots
            if shot.media_type in {"generated_image", "generated_video"}
        ]
        if len(generated_shots) < 2:
            raise ValueError(
                "Storyboard visual variety requires at least two generated story-scene shots; "
                "do not render every segment as cards or diagrams"
            )
    return storyboard


def _normalize_deterministic_sequence_shots(
    storyboard: StoryboardResponse,
    visual_plan: CompactVisualPlanResponse,
) -> StoryboardResponse:
    """Make already-covered ordered claims deterministic without changing claim meaning."""
    plans = {segment.id: segment for segment in visual_plan.segments}
    for segment in storyboard.segments:
        claims = plans[segment.id].evidence_claims
        claims_by_id = {claim.claim_id: claim for claim in claims}
        covered_claim_ids = {
            claim_id
            for shot in segment.evidence_shots
            for claim_id in shot.proves_claim_ids
        }
        if covered_claim_ids != set(claims_by_id):
            raise ValueError(
                f"Storyboard claim coverage mismatch for segment {segment.id}"
            )

        def sequence_spec_for(claim_ids: list[str]) -> MotionGraphicSpec:
            values = [
                value
                for claim_id in claim_ids
                for value in claims_by_id[claim_id].values
            ]
            if len(values) > 6:
                raise ValueError(
                    f"Storyboard sequence motion values exceed contract limit for segment {segment.id}"
                )
            return MotionGraphicSpec(
                layout="sequence",
                labels=[claims_by_id[claim_id].statement for claim_id in claim_ids],
                values=values,
            )

        sequence_ids = [
            claim.claim_id for claim in claims if claim.evidence_type == "sequence"
        ]
        relationship_ids = [
            claim.claim_id for claim in claims if claim.evidence_type == "relationship"
        ]
        if not sequence_ids:
            continue

        deterministic_shots = []
        for sequence_id in sequence_ids:
            existing = next(
                (
                    shot
                    for shot in segment.evidence_shots
                    if sequence_id in shot.proves_claim_ids
                    and shot.media_type == "motion_graphic"
                    and shot.motion_spec
                    and shot.motion_spec.layout == "sequence"
                ),
                None,
            )
            if existing is not None:
                deterministic_shots.append(existing)
                continue

            candidate = next(
                (
                    shot
                    for shot in segment.evidence_shots
                    if sequence_id in shot.proves_claim_ids
                ),
                None,
            )
            if candidate is None:
                continue

            candidate.media_type = "motion_graphic"
            candidate.motion_preset = "static"
            candidate.treatment = None
            candidate.motion_spec = sequence_spec_for(candidate.proves_claim_ids)
            deterministic_shots.append(candidate)

        if not deterministic_shots:
            continue

        shared_shot = deterministic_shots[0]
        added_relationship_ids = []
        for relationship_id in relationship_ids:
            if relationship_id not in shared_shot.proves_claim_ids:
                shared_shot.proves_claim_ids.append(relationship_id)
                added_relationship_ids.append(relationship_id)
        if added_relationship_ids:
            assert shared_shot.motion_spec is not None
            labels = list(shared_shot.motion_spec.labels)
            values = list(shared_shot.motion_spec.values)
            for relationship_id in added_relationship_ids:
                statement = claims_by_id[relationship_id].statement
                if statement not in labels:
                    labels.append(statement)
                for value in claims_by_id[relationship_id].values:
                    if value not in values:
                        values.append(value)
            if len(labels) > 6 or len(values) > 6:
                raise ValueError(
                    f"Storyboard shared sequence evidence exceeds contract limit for segment {segment.id}"
                )
            shared_shot.motion_spec = shared_shot.motion_spec.model_copy(
                update={"labels": labels, "values": values}
            )

    return StoryboardResponse.model_validate(storyboard.model_dump(mode="json"))


def _reconcile_storyboard_segment_ids(
    storyboard: StoryboardResponse,
    visual_plan: CompactVisualPlanResponse,
) -> StoryboardResponse:
    """Repair an ID-only storyboard drift when immutable claim ownership is unambiguous.

    Vertex sometimes returns the requested shots with labels such as ``segment_1``
    instead of the supplied narration IDs. We can safely repair that narrow case
    by matching each storyboard segment's covered claim IDs to exactly one visual
    plan segment. We never infer ownership from position alone, and we fail closed
    when the model omitted, mixed, or invented claims.
    """
    expected_by_id = {segment.id: segment for segment in visual_plan.segments}
    if {segment.id for segment in storyboard.segments} == set(expected_by_id):
        return storyboard
    if len(storyboard.segments) != len(expected_by_id):
        raise ValueError("Storyboard segment IDs do not match visual plan")

    plan_claim_ids = {
        segment.id: {claim.claim_id for claim in segment.evidence_claims}
        for segment in visual_plan.segments
    }
    assignments: dict[str, str] = {}
    used_plan_ids: set[str] = set()
    for storyboard_segment in storyboard.segments:
        covered_claim_ids = {
            claim_id
            for shot in storyboard_segment.evidence_shots
            for claim_id in shot.proves_claim_ids
        }
        candidates = [
            segment_id
            for segment_id, claim_ids in plan_claim_ids.items()
            if covered_claim_ids and covered_claim_ids.issubset(claim_ids)
        ]
        if len(candidates) != 1 or candidates[0] in used_plan_ids:
            assignments = {}
            break
        assignments[storyboard_segment.id] = candidates[0]
        used_plan_ids.add(candidates[0])

    if not assignments:
        positional_assignments: dict[str, str] = {}
        for storyboard_segment, plan_segment in zip(
            storyboard.segments,
            visual_plan.segments,
            strict=True,
        ):
            covered_claim_ids = {
                claim_id
                for shot in storyboard_segment.evidence_shots
                for claim_id in shot.proves_claim_ids
            }
            if covered_claim_ids != plan_claim_ids[plan_segment.id]:
                raise ValueError("Storyboard segment IDs do not match visual plan")
            positional_assignments[storyboard_segment.id] = plan_segment.id
        assignments = positional_assignments

    logger.warning(
        "Storyboard returned non-canonical segment IDs; reconciled by immutable claim ownership: %s",
        assignments,
    )
    return StoryboardResponse.model_validate({
        "segments": [
            segment.model_dump(mode="json") | {"id": assignments[segment.id]}
            for segment in storyboard.segments
        ]
    })


def _stage_location(stage: str, attempt: int = 0) -> str:
    if stage in {"story", "lock", "fact", "storyboard"} and attempt == 0:
        return os.getenv("FYF_VERTEX_STORY_LOCATION", DEFAULT_STORY_LOCATION)
    return os.getenv("GOOGLE_CLOUD_LOCATION", DEFAULT_LOCATION)

def generate_narration_script(topic_or_draft: str, duration_mode: str = "short") -> dict:
    """
    Stage 1: The Writer Agent (Vertex AI Mode)
    Uses the official Google GenAI SDK initialized with Vertex AI service account credentials.
    """

    topic_or_draft = topic_or_draft.strip()
    if not topic_or_draft:
        raise ValueError("topic_or_draft must not be blank")

    client = _stage_client("script")

    duration_rules = {
        "short": "Write 5-8 concise segments for roughly 30-60 seconds.",
        "medium": "Write 10-16 concise segments for roughly 1-2 minutes.",
        "long": "Write 18-30 concise segments for more than 2 minutes. Preserve the supplied detail; do not summarize it into a short.",
    }
    if duration_mode not in duration_rules:
        raise ValueError("duration_mode must be short, medium, or long")

    system_instruction = f"""
    You are the FYF AI Chief Content Strategist. Your job is to take a raw topic or draft and turn it into a high-retention video narration of the requested duration.

    RULES:
    1. The language must be Burmese (Myanmar).
    2. Tone: A thoughtful Burmese builder explaining complex AI systems to a capable friend. Calm, authoritative, no hype.
    3. Output strict narration-first JSON matching the supplied schema. Use language "my-MM".
    4. Each segment contains narration plus the lightweight semantic fields
       visual_action, scene_type, mascot_action, emotion, and emphasis words.
    5. Never create timestamps, seconds, startFrame, or endFrame. The audio
       timeline compiler owns timing after the approved narration WAV is generated.
    6. Do not create typed visuals here. Production visuals are added by the
       separate fact, storyboard, generation, and verification stages.
    7. This schema is the production Vertex narration contract. Do not reference
       or request output from any non-Vertex model or development worker.
    8. Duration requirement: {duration_rules[duration_mode]}
    """

    prompt = f"Please create a script for the following topic/draft:\n\n{topic_or_draft}"

    print("Generating a validated video script using Vertex AI...")

    attempts = _max_attempts()
    last_validation_error: str | None = None
    last_request_error: Exception | None = None
    for attempt in range(attempts):
        repair_context = ""
        if last_validation_error:
            repair_context = (
                "\n\nThe previous response failed schema validation. Regenerate the "
                "entire JSON from scratch and correct this contract error:\n"
                f"{last_validation_error[:600]}"
            )
        try:
            with telemetry_retry_attempt("script_narration", attempt + 1):
                response = client.models.generate_content(
                    model=_stage_model("script", attempt),
                    contents=prompt + repair_context,
                    config=generation_config_for("script",
                        system_instruction=system_instruction,
                        response_mime_type="application/json",
                        response_json_schema=_vertex_json_schema(StoryDraftScript),
                    ),
                )
            if not response.text:
                raise ValueError("Vertex returned an empty response")
            return StoryDraftScript.model_validate_json(response.text).model_dump(mode="json")
        except (json.JSONDecodeError, ValueError) as exc:
            last_validation_error = str(exc)
        except Exception as exc:
            last_request_error = exc
            if attempt + 1 == attempts:
                raise RuntimeError(f"Vertex AI request failed: {exc}") from exc
            if _transient_vertex_error(exc):
                _sleep_before_vertex_retry(attempt)

    if last_request_error and not last_validation_error:
        raise RuntimeError(f"Vertex AI request failed: {last_request_error}")
    raise ValueError(
        "Vertex could not produce a valid video script after "
        f"{attempts} attempts: {last_validation_error}"
    )


def lock_narration_in_batches(draft_data: dict, *, batch_size: int = 5) -> dict:
    """Lock long narration in restart-friendly bounded Vertex batches."""
    if batch_size < 1 or batch_size > 8:
        raise ValueError("batch_size must be between 1 and 8")
    draft = StoryDraftScript.model_validate(draft_data)
    merged: list[dict] = []
    for start in range(0, len(draft.segments), batch_size):
        batch = draft.segments[start : start + batch_size]
        locked = generate_exact_lock({
            "title": draft.title,
            "approved_segments": [
                {"id": segment.id, "text": segment.text} for segment in batch
            ],
        })
        merged.extend(locked["segments"])
    return VideoScript.model_validate({
        "title": draft.title,
        "language": "my-MM",
        "segments": merged,
    }).model_dump(mode="json")


def generate_video_script(topic_or_draft: str, duration_mode: str = "short") -> dict:
    return lock_narration_in_batches(generate_narration_script(topic_or_draft, duration_mode))

def generate_story_modes(topic_or_draft: str) -> dict:
    """
    Implements fyf_polish story mode.
    Takes a raw topic/draft and asks Vertex for exactly 3 named, structurally distinct FYF story variants.
    """
    topic_or_draft = topic_or_draft.strip()
    if not topic_or_draft:
        raise ValueError("topic_or_draft must not be blank")

    system_instruction = """
    You are the FYF AI Chief Content Strategist. Your job is to take a raw topic or draft and return exactly 3 structurally distinct FYF story variants.

    RULES:
    1. Each variant MUST follow this structure: scene -> wrong action/consequence -> root cause/context -> human boundary -> practical ending.
    2. Output strict JSON matching the supplied narration-first story schema.
    3. The language must be Burmese (Myanmar).
    4. Provide exactly 3 variants, named distinctly.
    5. Each variant must contain at least 5 concise narration segments. Do not
       generate typed visual objects yet; production visuals are added only
       after a human approves and locks one narration.
    6. Write for a Burmese beginner who may be asking "AI ဆိုတာဘာလဲ" and has
       no technical background. Start with an ordinary, concrete situation.
    7. When AI, AI Agent, workflow, system, or another English term first
       appears, explain its meaning immediately in simple Burmese. Use as few
       English terms as possible and never stack several unexplained terms in
       one sentence.
    8. Keep one idea per sentence. Prefer natural spoken Burmese over formal
       report language, hype, slogans, or abstract definitions.
    9. Make the three angles distinct but equally beginner-friendly:
       (a) an everyday scene, (b) a common misunderstanding corrected, and
       (c) a simple step-by-step explanation.
    """

    prompt = f"Please provide 3 story variants for the following topic/draft:\n\n{topic_or_draft}"

    print("Generating story variants using Vertex AI...")

    attempts = _max_attempts()
    last_validation_error: str | None = None
    last_request_error: Exception | None = None
    for attempt in range(attempts):
        model_id = _stage_model("story", attempt)
        client = _stage_client("story", attempt)
        repair_context = ""
        if last_validation_error:
            repair_context = (
                "\n\nThe previous response failed schema validation. Regenerate the "
                "entire JSON from scratch and correct this contract error:\n"
                f"{last_validation_error[:600]}"
            )
        try:
            with telemetry_retry_attempt("story_modes", attempt + 1):
                response = client.models.generate_content(
                    model=model_id,
                    contents=prompt + repair_context,
                    config=generation_config_for("story",
                        system_instruction=system_instruction,
                        response_mime_type="application/json",
                        response_json_schema=_vertex_json_schema(StoryDraftModesResponse),
                    ),
                )
            if not response.text:
                raise ValueError("Vertex returned an empty response")
            result_dict = json.loads(response.text)
            draft = StoryDraftModesResponse.model_validate(result_dict)
            result = StoryModesResponse.model_validate(
                draft.model_dump(mode="json")
            ).model_dump(mode="json")
            result["model_used"] = model_id
            return result
        except (json.JSONDecodeError, ValueError) as exc:
            last_validation_error = str(exc)
        except Exception as exc:
            last_request_error = exc
            if attempt + 1 == attempts:
                raise RuntimeError(f"Vertex AI request failed: {exc}") from exc
            if _transient_vertex_error(exc):
                _sleep_before_vertex_retry(attempt)

    if last_request_error and not last_validation_error:
        raise RuntimeError(f"Vertex AI request failed: {last_request_error}")
    raise ValueError(
        "Vertex could not produce valid story variants after "
        f"{attempts} attempts: {last_validation_error}"
    )


def generate_exact_lock(request_data: dict) -> dict:
    """
    Implements exact_lock story mode.
    Takes user-approved narration and preserves narration text exactly while Vertex only supplies compliant scene metadata and typed visuals.
    """
    request = ExactLockRequest.model_validate(request_data)
    claim_response = _extract_complete_evidence_claims(request)
    claims_by_id = {segment.id: segment.claims for segment in claim_response.segments}

    client = _stage_client("lock")

    system_instruction = """
    You are the FYF AI Video Producer. Your job is to add visual metadata to an approved script.

    CRITICAL RULE:
    Do not return or rewrite narration text. Your ONLY job is to return each
    supplied segment ID with compliant scene metadata (visual_action,
    scene_type, mascot_action, emotion, emphasis) and a typed visual.

    All screen_text and other viewer-visible labels must be concise, natural,
    beginner-friendly Burmese. Keep only unavoidable product names such as AI.
    The screen_text field MUST contain 1 or 2 strings only. Never return 3 or
    more screen_text labels for one segment.
    The motion_spec.relation_mode field may be present only when the layout is
    relationship; omit relation_mode for every other layout.
    Every non-kinetic treatment MUST include focal_object, action, and change;
    only kinetic_type may leave those fields empty.

    For every segment, extract the concrete factual or conceptual claims that
    the viewer must directly see into visual.evidence_claims. Then create one
    or more visual.evidence_shots that cover every claim ID. Each shot prompt
    must describe content-specific visible evidence rather than a generic mood,
    paper card, caption, or decorative background. Exact counts must be shown
    as countable objects; comparisons must show both states; sequences must
    show their ordered action. Leave asset_path null and verification_status
    planned because the production media agent owns generation and verification.

    Choose the best media_type per shot instead of repeating one style:
    - motion_graphic for exact counts, comparisons, diagrams, ordered processes,
      and data where deterministic clarity matters most;
    - generated_image for illustrative environments, objects, and conceptual scenes;
    - generated_video only for a short hero/action moment whose real movement adds
      meaning. Never choose generated_video merely as decoration. Every generated
      video must remain understandable through a verified still fallback.
    Select a restrained motion_preset that supports the narration.
    A motion_graphic shot MUST include motion_spec. Put every visible label in
    motion_spec.labels, every exact factual value in motion_spec.values, and use
    object_count only when the viewer should count repeated objects. Keep
    object_count at 30 or fewer; otherwise show a labeled value. Other media
    types MUST leave motion_spec null.

    Output strict JSON matching the supplied metadata-only schema.
    """

    prompt = f"Please supply visual metadata for the following approved script. PRESERVE THE NARRATION EXACTLY.\n\nTitle: {request.title}\nSegments:\n"
    for i, seg in enumerate(request.approved_segments):
        claims = [claim.model_dump(mode="json") for claim in claims_by_id[seg.id]]
        prompt += f"Segment {i+1} (ID: {seg.id}):\nText: {seg.text}\nFact Agent claims: {json.dumps(claims, ensure_ascii=False)}\n\n"

    print("Generating exact lock visual metadata using Vertex AI...")

    attempts = _max_attempts()
    last_validation_error: str | None = None
    last_request_error: Exception | None = None
    metadata: CompactVisualPlanResponse | None = None
    metadata_by_id: dict[str, object] | None = None
    for attempt in range(attempts):
        repair_context = ""
        if last_validation_error:
            repair_context = (
                "\n\nThe previous response failed schema validation. Regenerate the "
                "entire JSON from scratch and correct this contract error:\n"
                f"{last_validation_error[:600]}"
            )
        try:
            if metadata is None:
                with telemetry_retry_attempt("lock_metadata", attempt + 1):
                    response = client.models.generate_content(
                        model=_stage_model("lock", attempt),
                        contents=prompt + repair_context,
                        config=generation_config_for("lock",
                            system_instruction=system_instruction,
                            response_mime_type="application/json",
                            response_json_schema=_vertex_json_schema(CompactVisualPlanResponse),
                        ),
                    )
                if not response.text:
                    raise ValueError("Vertex returned an empty response")

                result_dict = json.loads(response.text)
                _drop_invalid_optional_treatments(result_dict)
                metadata = CompactVisualPlanResponse.model_validate(result_dict)
                metadata_by_id = {segment.id: segment for segment in metadata.segments}

                if len(metadata.segments) != len(request.approved_segments):
                    raise ValueError("Output segment count does not match input segment count")
                if set(metadata_by_id) != {segment.id for segment in request.approved_segments}:
                    raise ValueError("Output segment IDs do not match approved narration IDs")
                for segment in metadata.segments:
                    expected_claims = [claim.model_dump(mode="json") for claim in claims_by_id[segment.id]]
                    actual_claims = [claim.model_dump(mode="json") for claim in segment.evidence_claims]
                    if actual_claims != expected_claims:
                        logger.warning(
                            "Visual Director changed Fact Agent claims for segment %s; "
                            "using the Fact Agent claims as canonical evidence",
                            segment.id,
                        )
                        segment.evidence_claims = [
                            claim.model_copy(deep=True) for claim in claims_by_id[segment.id]
                        ]

            assert metadata is not None
            assert metadata_by_id is not None

            storyboard = _direct_storyboard(request, metadata, attempt)
            storyboard_by_id = {segment.id: segment for segment in storyboard.segments}

            merged_segments = []
            for approved in request.approved_segments:
                segment_data = metadata_by_id[approved.id].model_dump(mode="json")
                visual = {
                    "kind": "generic",
                    "phase": segment_data.pop("phase"),
                    "camera": segment_data.pop("camera"),
                    "screen_text": segment_data.pop("screen_text"),
                    "evidence_claims": segment_data.pop("evidence_claims"),
                    "evidence_shots": [
                        shot.model_dump(mode="json")
                        for shot in storyboard_by_id[approved.id].evidence_shots
                    ],
                }
                segment_data.pop("evidence_shots")
                merged_segments.append({**segment_data, "text": approved.text, "visual": visual})

            return VideoScript.model_validate(
                {"title": request.title, "language": "my-MM", "segments": merged_segments}
            ).model_dump(mode="json")

        except (json.JSONDecodeError, ValueError) as exc:
            last_validation_error = str(exc)
        except Exception as exc:
            last_request_error = exc
            if attempt + 1 == attempts:
                raise RuntimeError(f"Vertex AI request failed: {exc}") from exc
            if _transient_vertex_error(exc):
                _sleep_before_vertex_retry(attempt)

    if last_request_error and not last_validation_error:
        raise RuntimeError(f"Vertex AI request failed: {last_request_error}")
    raise ValueError(
        "Vertex could not produce a valid exact lock script after "
        f"{attempts} attempts: {last_validation_error}"
    )
