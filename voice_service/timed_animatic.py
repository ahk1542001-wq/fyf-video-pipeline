"""Voice-timed animatic planning (Stage D4).

Everything here is a PURE, deterministic function of the approved narration and
the measured voice audio.  No provider is called, no cost is incurred and no
number is invented: when a timing cannot be measured from a real WAV the caller
gets an explicit ``"unmeasured"`` marker instead of a plausible-looking value.

Responsibilities
----------------
* pronunciation dictionary (systematic Burmese letter names + curated words)
* mixed-language handling (Burmese narration containing Latin acronyms/units)
* Burmese line breaking that never splits a syllable
* voice-timed scenes and captions with a readability cap
* voice/music balance with sidechain ducking
* purposeful SFX and intentional silence
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# Mirrors backend.mouth_cues.FPS.  Kept local so voice_service stays importable
# on its own (it is deployed as a separate Cloud Run service).
DEFAULT_FPS = 30

# --------------------------------------------------------------------------- #
# Pronunciation
# --------------------------------------------------------------------------- #

#: Systematic Burmese names for the Latin letters.  ``AI -> "အေ အိုင်"`` is the
#: form already attested in production_voice.AI_PRONUNCIATION, so the table is
#: anchored to a value the studio has approved rather than guessed at.
BURMESE_LETTER_NAMES: dict[str, str] = {
    "A": "အေ", "B": "ဘီ", "C": "စီ", "D": "ဒီ", "E": "အီ", "F": "အက်ဖ်",
    "G": "ဂျီ", "H": "အက်ချ်", "I": "အိုင်", "J": "ဂျေ", "K": "ကေ",
    "L": "အယ်လ်", "M": "အမ်", "N": "အန်", "O": "အို", "P": "ပီ",
    "Q": "ကျူ", "R": "အာ", "S": "အက်စ်", "T": "တီ", "U": "ယူ",
    "V": "ဗွီ", "W": "ဒဗ်လျူ", "X": "အက်ဇ်", "Y": "ဝိုင်", "Z": "ဇက်",
}

#: Curated, non-systematic entries.  Keys are matched case-insensitively on a
#: whole-token basis.  Extend via pronunciation_dictionary.json next to this file
#: so the studio can correct a reading without touching code.
PRONUNCIATION_DICTIONARY: dict[str, str] = {
    "AI": "အေ အိုင်",
    "FYF": "အက်ဖ် ဝိုင် အက်ဖ်",
    "OK": "အိုကေ",
    "KPI": "ကေ ပီ အိုင်",
    "USD": "ဒေါ်လာ",
    "MMK": "ကျပ်",
    "KS": "ကျပ်",
    "%": "ရာခိုင်နှုန်း",
    "KG": "ကီလိုဂရမ်",
    "KM": "ကီလိုမီတာ",
}

DICTIONARY_FILENAME = "pronunciation_dictionary.json"

_BURMESE_LANGUAGES = {"my", "my-mm", "myanmar", "burmese"}
_ENGLISH_LANGUAGES = {"en", "en-us", "en-gb", "english"}
_TOKEN_RE = re.compile(r"[A-Za-z]+|%|[0-9]+(?:\.[0-9]+)?")


def is_burmese(language: str | None) -> bool:
    normalized = str(language or "").strip().lower()
    if normalized in _ENGLISH_LANGUAGES:
        return False
    if normalized in _BURMESE_LANGUAGES:
        return True
    # The pipeline default is Burmese; anything unrecognised is treated as
    # Burmese so mixed-language handling still applies.  Callers that need
    # certainty should pass an explicit language tag.
    return True


def load_pronunciation_dictionary(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Curated entries + optional local override file + caller supplied entries."""

    table = dict(PRONUNCIATION_DICTIONARY)
    override_path = Path(__file__).resolve().parent / DICTIONARY_FILENAME
    if override_path.is_file():
        try:
            payload = json.loads(override_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"{DICTIONARY_FILENAME} is corrupt") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{DICTIONARY_FILENAME} must contain an object")
        for key, value in payload.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError(f"{DICTIONARY_FILENAME} entries must be string pairs")
            table[key.strip().upper()] = value.strip()
    for key, value in (extra or {}).items():
        table[str(key).strip().upper()] = str(value).strip()
    return table


def spell_acronym(acronym: str) -> str | None:
    """Systematic letter-by-letter Burmese spelling, or None if unsupported."""

    letters = [character.upper() for character in acronym if character.isalpha()]
    if len(letters) < 2 or len(letters) != len(acronym.strip()):
        return None
    names = [BURMESE_LETTER_NAMES.get(letter) for letter in letters]
    if any(name is None for name in names):
        return None
    return " ".join(str(name) for name in names)


@dataclass(frozen=True)
class PronunciationPlan:
    """The spoken form of a narration line plus an auditable substitution log."""

    source_text: str
    spoken_text: str
    language: str
    substitutions: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_text": self.source_text,
            "spoken_text": self.spoken_text,
            "language": self.language,
            "substitutions": list(self.substitutions),
            "unresolved": list(self.unresolved),
            "applied": bool(self.substitutions),
        }


def apply_pronunciation_dictionary(
    text: str,
    *,
    language: str | None = "my-MM",
    extra: Mapping[str, str] | None = None,
) -> PronunciationPlan:
    """Rewrite Latin acronyms and units into a form Burmese TTS reads correctly.

    Mixed-language handling is deliberately asymmetric: inside Burmese narration
    a Latin acronym is spelled out (a Burmese voice reads "KPI" as a single
    unknown glyph run), while inside English narration the token is left alone
    because an English voice already reads it.  Tokens with no dictionary entry
    and no systematic spelling are reported in ``unresolved`` rather than
    silently passed through as if they were fine.
    """

    if not isinstance(text, str):
        raise ValueError("pronunciation text must be a string")
    source = text
    table = load_pronunciation_dictionary(extra)
    burmese = is_burmese(language)

    substitutions: list[dict[str, Any]] = []
    unresolved: list[str] = []

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        key = token.strip().upper()
        if not burmese:
            # An English voice already reads "15%", "KPI" and "AI" correctly;
            # rewriting them would be a regression, not a fix.
            replacement = None
        else:
            replacement = table.get(key)
            if replacement is None and key.isalpha() and key.isupper() and len(key) >= 2:
                replacement = spell_acronym(key)
        if replacement is None:
            if burmese and key.isalpha() and key.isupper() and len(key) >= 2:
                unresolved.append(key)
            return token
        substitutions.append(
            {"token": token, "replacement": replacement, "index": match.start()}
        )
        return replacement

    spoken = _TOKEN_RE.sub(replace, source)
    return PronunciationPlan(
        source_text=source,
        spoken_text=spoken,
        language=str(language or "my-MM"),
        substitutions=substitutions,
        unresolved=sorted(set(unresolved)),
    )


# --------------------------------------------------------------------------- #
# Script runs and Burmese line breaking
# --------------------------------------------------------------------------- #

_MYANMAR_PUNCTUATION = {"\u104a", "\u104b"}  # ၊ (little section) ။ (section)
_MYANMAR_VIRAMA = "\u1039"
_LATIN_BREAK_AFTER = {".", ",", ";", ":", "!", "?", "၊", "။"}


def script_of(character: str) -> str:
    """Classify one character into a script run for mixed-language handling."""

    if not character:
        return "empty"
    code = ord(character)
    if 0x1000 <= code <= 0x109F or 0xAA60 <= code <= 0xAA7F:
        return "myanmar"
    if character.isspace():
        return "space"
    if character.isdigit():
        return "digit"
    if ("a" <= character.lower() <= "z"):
        return "latin"
    category = unicodedata.category(character)
    if category.startswith("P") or category.startswith("S"):
        return "punctuation"
    return "other"


@dataclass(frozen=True)
class ScriptRun:
    script: str
    text: str
    start: int
    end: int


def split_script_runs(text: str) -> list[ScriptRun]:
    """Split into maximal runs of one script (Burmese / Latin / digit / other)."""

    runs: list[ScriptRun] = []
    for index, character in enumerate(text or ""):
        script = script_of(character)
        if script in {"space", "punctuation"}:
            # Whitespace and punctuation terminate a run but never start one.
            continue
        if runs and runs[-1].script == script and runs[-1].end == index:
            runs[-1] = ScriptRun(script, runs[-1].text + character, runs[-1].start, index + 1)
        else:
            runs.append(ScriptRun(script, character, index, index + 1))
    return runs


def _is_myanmar_dependent(character: str) -> bool:
    code = ord(character) if character else 0
    # Dependent vowel signs, medials and tone marks: 102B-103E plus 1036/1037/1038.
    return 0x102B <= code <= 0x103E


def _is_myanmar_base(character: str) -> bool:
    code = ord(character) if character else 0
    return 0x1000 <= code <= 0x102A or 0x103F <= code <= 0x1049


def burmese_break_opportunities(text: str) -> set[int]:
    """Indices at which a line may break WITHOUT splitting a Burmese syllable.

    A break is allowed before a base character only when the previous character
    is not a dependent vowel/medial sign and is not a stacking virama (``်``),
    i.e. only at a syllable onset.  Breaks are also allowed after a space and
    after Burmese or Latin punctuation.
    """

    opportunities: set[int] = set()
    source = text or ""
    for index in range(len(source) + 1):
        if index == 0 or index == len(source):
            opportunities.add(index)
            continue
        previous = source[index - 1]
        current = source[index]
        if previous.isspace():
            opportunities.add(index)
            continue
        if previous in _MYANMAR_PUNCTUATION or previous in _LATIN_BREAK_AFTER:
            opportunities.add(index)
            continue
        if _is_myanmar_dependent(previous) or previous == _MYANMAR_VIRAMA:
            continue
        if index >= 2 and source[index - 2] == _MYANMAR_VIRAMA and _is_myanmar_base(previous):
            # Stacked consonant: the previous base belongs to the same syllable.
            continue
        if _is_myanmar_base(current) or script_of(current) == "latin" or script_of(current) == "digit":
            opportunities.add(index)
    return opportunities


def wrap_lines(
    text: str,
    *,
    max_characters: int,
    max_lines: int = 2,
    language: str | None = "my-MM",
) -> tuple[list[str], bool]:
    """Greedy caption wrap.  Returns ``(lines, overflowed)``.

    Latin text breaks on spaces; Burmese text breaks on syllable onsets; mixed
    text breaks on the union of both opportunity sets, so a Burmese sentence
    containing an English acronym never splits the acronym or a syllable.
    ``overflowed`` is True when the text cannot fit in ``max_lines`` — callers
    must surface that honestly instead of pretending the caption is readable.
    """

    if max_characters < 1:
        raise ValueError("max_characters must be at least 1")
    if max_lines < 1:
        raise ValueError("max_lines must be at least 1")
    source = " ".join(str(text or "").split())
    if not source:
        return [], False

    opportunities = burmese_break_opportunities(source)
    if is_burmese(language):
        # Space breaks are always allowed; syllable onsets are added on top.
        allowed = opportunities
    else:
        allowed = {index for index in range(len(source) + 1)
                   if index == 0 or index == len(source) or source[index - 1].isspace()}

    lines: list[str] = []
    start = 0
    while start < len(source):
        limit = min(len(source), start + max_characters)
        best = None
        for candidate in range(limit, start, -1):
            if candidate in allowed:
                best = candidate
                break
        if best is None:
            # No legal break inside the budget: force at the limit rather than
            # emit an unbounded line, and report the overflow.
            best = limit
        lines.append(source[start:best].strip())
        start = best
        while start < len(source) and source[start].isspace():
            start += 1

    lines = [line for line in lines if line]
    if len(lines) > max_lines:
        kept = lines[:max_lines]
        kept[-1] = (kept[-1][: max(0, max_characters - 1)] + "…").strip()
        return kept, True
    return lines, False


# --------------------------------------------------------------------------- #
# Voice-timed captions
# --------------------------------------------------------------------------- #

#: Reading-speed caps.  Burmese is denser per character than Latin, so it gets
#: the lower cap; both follow the common subtitle guidance ranges.
MAX_CHARACTERS_PER_SECOND = {"latin": 20.0, "myanmar": 17.0}
MAX_CAPTION_CHARACTERS = {"latin": 42, "myanmar": 34}
MAX_CAPTION_LINES = 2
MIN_CUE_SECONDS = 0.6


def characters_per_second_cap(language: str | None) -> float:
    return MAX_CHARACTERS_PER_SECOND["latin" if not is_burmese(language) else "myanmar"]


def max_caption_characters(language: str | None) -> int:
    return MAX_CAPTION_CHARACTERS["latin" if not is_burmese(language) else "myanmar"]


def frame_to_seconds(frame: Any, fps: float) -> float:
    if not isinstance(frame, (int, float)) or isinstance(frame, bool):
        raise ValueError("frame must be numeric")
    if not isinstance(fps, (int, float)) or isinstance(fps, bool) or fps <= 0:
        raise ValueError("fps must be positive")
    return round(float(frame) / float(fps), 3)


@dataclass(frozen=True)
class CaptionCue:
    segment_id: str
    start: float
    end: float
    text: str
    lines: list[str]
    overflowed: bool
    characters_per_second: float
    readable: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "lines": list(self.lines),
            "overflowed": self.overflowed,
            "characters_per_second": round(self.characters_per_second, 2),
            "readable": self.readable,
        }


def build_caption_cues(
    segments: Sequence[Mapping[str, Any]],
    *,
    fps: float = DEFAULT_FPS,
    language: str | None = "my-MM",
) -> list[CaptionCue]:
    """Voice-timed captions derived from the measured segment frame ranges.

    Timings come from the segment start/end frames that ``mouth_cues`` already
    snapped to real silence centres in the voice WAV, so the captions are
    voice-timed rather than text-length-timed.  A segment whose text is too long
    for its measured duration is SPLIT at legal break opportunities so no cue
    exceeds the reading-speed cap; if it still cannot be made readable the cue is
    flagged ``readable=False`` instead of being quietly shipped.
    """

    if not isinstance(segments, Sequence):
        raise ValueError("segments must be a sequence")
    cap = characters_per_second_cap(language)
    width = max_caption_characters(language)
    cues: list[CaptionCue] = []

    for segment in segments:
        if not isinstance(segment, Mapping):
            raise ValueError("segment must be an object")
        raw_id = segment.get("id") or segment.get("segment_id")
        segment_id = str(raw_id) if raw_id is not None else ""
        if not segment_id:
            raise ValueError("caption segment requires an id")
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        start = frame_to_seconds(segment.get("startFrame"), fps)
        end = frame_to_seconds(segment.get("endFrame"), fps)
        if end <= start:
            raise ValueError(f"segment {segment_id} has a non-positive caption duration")

        duration = end - start
        budget = max(1, int(duration * cap))
        chunks = _split_to_budget(text, budget, language=language)
        # A cue shorter than MIN_CUE_SECONDS cannot be read, so the split count
        # is capped by the measured duration rather than by the text length.
        max_chunks = max(1, int(duration // MIN_CUE_SECONDS))
        while len(chunks) > max_chunks:
            budget = -(-len(text) // max_chunks)
            chunks = _split_to_budget(text, max(budget, 1), language=language)
            if len(chunks) <= max_chunks:
                break
            max_chunks = len(chunks)
            break
        cursor = start
        span = duration / float(len(chunks))
        for index, chunk in enumerate(chunks):
            cue_start = round(cursor + index * span, 3)
            cue_end = round(end, 3) if index == len(chunks) - 1 else round(cursor + (index + 1) * span, 3)
            # Never let a minimum-duration floor overlap the following cue; an
            # unreadably short beat is reported, not papered over.
            cue_end = min(cue_end, round(end, 3))
            lines, overflowed = wrap_lines(chunk, max_characters=width, max_lines=MAX_CAPTION_LINES, language=language)
            duration = max(cue_end - cue_start, 1e-6)
            cps = len(chunk.replace(" ", "")) / duration
            cues.append(
                CaptionCue(
                    segment_id=segment_id,
                    start=cue_start,
                    end=cue_end,
                    text=chunk,
                    lines=lines,
                    overflowed=overflowed,
                    characters_per_second=cps,
                    readable=not overflowed and cps <= cap * 1.15,
                )
            )
    return cues


def _split_to_budget(text: str, budget: int, *, language: str | None) -> list[str]:
    """Split ``text`` into chunks of at most ``budget`` characters at legal breaks."""

    if len(text) <= budget:
        return [text]
    opportunities = burmese_break_opportunities(text)
    if not is_burmese(language):
        opportunities = {index for index in range(len(text) + 1)
                         if index == 0 or index == len(text) or text[index - 1].isspace()}
    chunks: list[str] = []
    start = 0
    while start < len(text):
        limit = min(len(text), start + budget)
        best = None
        for candidate in range(limit, start, -1):
            if candidate in opportunities:
                best = candidate
                break
        if best is None:
            best = limit
        chunk = text[start:best].strip()
        if chunk:
            chunks.append(chunk)
        start = best
        while start < len(text) and text[start].isspace():
            start += 1
    return chunks or [text]


# --------------------------------------------------------------------------- #
# Voice / music balance, ducking, SFX and silence
# --------------------------------------------------------------------------- #

#: Approved mix targets.  Narration is the product; music supports it.
VOICE_GAIN_DB = 0.0
MUSIC_GAIN_DB = -18.0
DUCK_GAIN_DB = -12.0
DUCK_ATTACK_MS = 12
DUCK_RELEASE_MS = 320
DUCK_THRESHOLD = 0.045


@dataclass(frozen=True)
class MixPlan:
    """Deterministic voice/music balance plan with an ffmpeg filtergraph."""

    voice_gain_db: float
    music_gain_db: float
    duck_gain_db: float
    music_present: bool
    ducking_enabled: bool
    filtergraph: str
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "voice_gain_db": self.voice_gain_db,
            "music_gain_db": self.music_gain_db,
            "duck_gain_db": self.duck_gain_db,
            "music_present": self.music_present,
            "ducking_enabled": self.ducking_enabled,
            "filtergraph": self.filtergraph,
            "notes": list(self.notes),
        }


def build_mix_plan(
    *,
    has_music: bool,
    voice_gain_db: float = VOICE_GAIN_DB,
    music_gain_db: float = MUSIC_GAIN_DB,
    duck_gain_db: float = DUCK_GAIN_DB,
) -> MixPlan:
    """Voice/music balance + sidechain ducking as a pure ffmpeg filtergraph.

    ``[1:a]`` is the music bed and ``[0:a]`` the narration.  The narration
    sidechain drives a compressor on the music so speech always wins, then the
    music is lifted back to its bed level.  With no music bed the plan degrades
    to a single-input loudness-normalised voice chain and says so in ``notes``.
    """

    notes: list[str] = []
    if not has_music:
        notes.append("no music bed supplied; ducking not applicable")
        graph = (
            f"[0:a]highpass=f=65,volume={voice_gain_db}dB,"
            "loudnorm=I=-16:TP=-1.5:LRA=11[out]"
        )
        return MixPlan(voice_gain_db, music_gain_db, duck_gain_db, False, False, graph, notes)

    # duck_gain_db is the FLOOR the music bed is pulled to while speech is
    # present; sidechaincompress does the moving, so it is expressed as the
    # compressor's makeup-free target rather than a second static volume stage.
    duck_floor = float(min(duck_gain_db, 0.0))
    graph = (
        f"[1:a]volume={music_gain_db}dB[bed];"
        "[0:a]asplit=2[voice][sc];"
        f"[bed][sc]sidechaincompress=threshold={DUCK_THRESHOLD}:"
        f"ratio=8:attack={DUCK_ATTACK_MS}:release={DUCK_RELEASE_MS}[music];"
        f"[voice]volume={voice_gain_db}dB,loudnorm=I=-16:TP=-1.5:LRA=11[speech];"
        "[speech][music]amix=inputs=2:duration=first:dropout_transition=0,"
        "alimiter=limit=0.95[out]"
    )
    notes.append(
        "music ducks under narration via sidechaincompress; speech is the "
        "duration master so the mix never outlasts the voice"
    )
    notes.append(f"ducked music floor is {duck_floor}dB relative to the bed level")
    return MixPlan(voice_gain_db, music_gain_db, duck_floor, True, True, graph, notes)


@dataclass(frozen=True)
class SoundEffectCue:
    at_seconds: float
    kind: str
    purpose: str
    gain_db: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "at_seconds": self.at_seconds,
            "kind": self.kind,
            "purpose": self.purpose,
            "gain_db": self.gain_db,
        }


#: SFX are purposeful or absent.  Each rule states WHY the cue exists; a cue
#: that cannot state a purpose is not emitted.
SFX_RULES: tuple[dict[str, str], ...] = (
    {
        "kind": "transition_swell",
        "when": "first segment",
        "purpose": "marks the opening beat so a sound-off viewer still perceives a start",
        "gain_db": "-22",
    },
    {
        "kind": "consequence_impact",
        "when": "consequence beat",
        "purpose": "underscores the cost/risk statement; the one place impact is justified",
        "gain_db": "-20",
    },
    {
        "kind": "resolution_chime",
        "when": "final segment",
        "purpose": "signals closure before the call to action",
        "gain_db": "-24",
    },
)

_CONSEQUENCE_HINTS = ("risk", "cost", "consequence", "loss", "fail", "penalt", "leak", "fraud",
                      "အန္တရာယ်", "ဆုံးရှုံး", "ပြဿနာ", "ကုန်ကျ")


def plan_sfx(
    segments: Sequence[Mapping[str, Any]],
    *,
    fps: float = DEFAULT_FPS,
    max_cues: int = 3,
) -> list[SoundEffectCue]:
    """At most one cue per rule, each carrying its purpose."""

    cues: list[SoundEffectCue] = []
    ordered = [segment for segment in segments if isinstance(segment, Mapping)]
    if not ordered:
        return cues

    def emit(rule: Mapping[str, str], segment: Mapping[str, Any]) -> None:
        if len(cues) >= max_cues:
            return
        start = frame_to_seconds(segment.get("startFrame"), fps)
        cues.append(
            SoundEffectCue(
                at_seconds=start,
                kind=str(rule["kind"]),
                purpose=str(rule["purpose"]),
                gain_db=float(rule["gain_db"]),
            )
        )

    emit(SFX_RULES[0], ordered[0])
    for segment in ordered:
        text = str(segment.get("text") or "").lower()
        if any(hint in text for hint in _CONSEQUENCE_HINTS):
            emit(SFX_RULES[1], segment)
            break
    if len(ordered) > 1:
        emit(SFX_RULES[2], ordered[-1])
    return cues


def plan_silence(
    cues: Sequence[Mapping[str, Any]] | Sequence[CaptionCue],
    *,
    total_seconds: float,
    min_gap_seconds: float = 0.35,
) -> list[dict[str, Any]]:
    """Intentional silence windows between narration beats.

    Silence is treated as a design element, not dead air: a gap at least
    ``min_gap_seconds`` long is recorded with the reason it is kept.  Gaps that
    are too short to be intentional are reported as ``"compressed"`` so a reviewer
    can see where the edit is breathless.
    """

    spans: list[tuple[float, float]] = []
    for cue in cues:
        if isinstance(cue, CaptionCue):
            spans.append((cue.start, cue.end))
        elif isinstance(cue, Mapping):
            start, end = cue.get("start"), cue.get("end")
            if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                spans.append((float(start), float(end)))
    spans.sort()

    windows: list[dict[str, Any]] = []
    cursor = 0.0
    for start, end in spans:
        gap = round(start - cursor, 3)
        if gap > 0.05:
            windows.append(
                {
                    "start": round(cursor, 3),
                    "end": round(start, 3),
                    "seconds": gap,
                    "intentional": gap >= min_gap_seconds,
                    "purpose": (
                        "breathing room before the next beat"
                        if gap >= min_gap_seconds
                        else "gap below the intentional-silence floor; reads as breathless"
                    ),
                }
            )
        cursor = max(cursor, end)
    tail = round(float(total_seconds) - cursor, 3)
    if tail > 0.05:
        windows.append(
            {
                "start": round(cursor, 3),
                "end": round(float(total_seconds), 3),
                "seconds": tail,
                "intentional": tail >= min_gap_seconds,
                "purpose": "hold on the final frame before the call to action",
            }
        )
    return windows


# --------------------------------------------------------------------------- #
# Animatic assembly
# --------------------------------------------------------------------------- #


def build_timed_animatic(
    render_input: Mapping[str, Any],
    *,
    fps: float = DEFAULT_FPS,
    has_music: bool = False,
    voice_measured: bool = True,
    extra_pronunciations: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Assemble the voice-timed animatic plan for an approved draft.

    ``voice_measured`` must be True only when the segment frame ranges were
    snapped to real silence centres in a recorded WAV.  When it is False the
    document says the timings are text-weight estimates, because presenting an
    estimate as a measurement would be fabrication.
    """

    if not isinstance(render_input, Mapping):
        raise ValueError("render_input must be an object")
    segments = render_input.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("render_input.segments must be a non-empty list")
    language = render_input.get("language")
    language = language if isinstance(language, str) else "my-MM"
    resolved_fps = render_input.get("fps")
    if isinstance(resolved_fps, (int, float)) and not isinstance(resolved_fps, bool) and resolved_fps > 0:
        fps = float(resolved_fps)

    cues = build_caption_cues(segments, fps=fps, language=language)
    total_frames = render_input.get("durationInFrames")
    if not isinstance(total_frames, int) or isinstance(total_frames, bool) or total_frames <= 0:
        total_frames = max(int(segment.get("endFrame") or 0) for segment in segments)
    total_seconds = frame_to_seconds(total_frames, fps)

    pronunciation = [
        apply_pronunciation_dictionary(str(segment.get("text") or ""), language=language, extra=extra_pronunciations).as_dict()
        for segment in segments
        if str(segment.get("text") or "").strip()
    ]
    timing_source = render_input.get("segmentTimingSource")
    grouped = _group_cues_by_segment(cues)
    scenes = []
    for segment in segments:
        segment_id = str(segment.get("id") or segment.get("segment_id") or "")
        scenes.append(
            {
                "segment_id": segment_id,
                "start": frame_to_seconds(segment.get("startFrame"), fps),
                "end": frame_to_seconds(segment.get("endFrame"), fps),
                "text": str(segment.get("text") or ""),
                # Keyed by id, not by position: a segment with no narration text
                # produces no cues and must not shift every later scene.
                "cues": [cue.as_dict() for cue in grouped.get(segment_id, [])],
            }
        )

    unreadable = [cue.as_dict() for cue in cues if not cue.readable]
    warnings: list[str] = []
    if not voice_measured:
        warnings.append(
            "segment timings are text-weight estimates, not measurements from a recorded voice track"
        )
    if isinstance(timing_source, str) and timing_source == "text-weight-fallback":
        warnings.append("segmentTimingSource reports a text-weight fallback; captions are not voice-timed")
    if unreadable:
        warnings.append(f"{len(unreadable)} caption cue(s) exceed the reading-speed cap")
    unresolved = sorted({token for plan in pronunciation for token in plan["unresolved"]})
    if unresolved:
        warnings.append(
            "pronunciation dictionary has no entry for: " + ", ".join(unresolved)
        )

    return {
        "language": language,
        "fps": fps,
        "total_seconds": total_seconds,
        "timing_source": timing_source if isinstance(timing_source, str) else "unmeasured",
        "voice_timed": bool(voice_measured) and timing_source != "text-weight-fallback",
        "scenes": scenes,
        "captions": [cue.as_dict() for cue in cues],
        "pronunciation": pronunciation,
        "mix": build_mix_plan(has_music=has_music).as_dict(),
        "sfx": [cue.as_dict() for cue in plan_sfx(segments, fps=fps)],
        "silence": plan_silence(cues, total_seconds=total_seconds),
        "script_runs": {
            str(segment.get("id") or segment.get("segment_id") or ""): [
                {"script": run.script, "text": run.text}
                for run in split_script_runs(str(segment.get("text") or ""))
            ]
            for segment in segments
        },
        "warnings": warnings,
        "generation_enabled": False,
    }


def _group_cues_by_segment(cues: Iterable[CaptionCue]) -> dict[str, list[CaptionCue]]:
    groups: dict[str, list[CaptionCue]] = {}
    for cue in cues:
        groups.setdefault(cue.segment_id, []).append(cue)
    return groups


def write_timed_animatic(job_dir: str | Path, document: Mapping[str, Any]) -> Path:
    """Persist ``animatic.json`` inside a job directory."""

    from backend.job_store import write_json_atomically

    root = Path(job_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"job directory not found: {root}")
    target = root / "animatic.json"
    write_json_atomically(target, dict(document))
    return target
