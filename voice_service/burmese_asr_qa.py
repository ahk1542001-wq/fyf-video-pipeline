import difflib
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class ASRQAReport:
    normalized_expected: str
    normalized_transcription: str
    coverage_ratio: float
    missing_spans: list[str] = field(default_factory=list)
    extra_spans: list[str] = field(default_factory=list)
    protected_term_presence: dict[str, bool] = field(default_factory=dict)
    review_required: bool = False


def clean_text(text: str) -> str:
    """
    Normalizes to NFC, removes punctuation, whitespace, and format/zero-width chars.
    """
    text = unicodedata.normalize('NFC', text)
    cleaned = []
    for char in text:
        cat = unicodedata.category(char)
        # Remove punctuation, whitespace, format/control (zero-width) chars
        if cat.startswith('P') or cat.startswith('Z') or cat.startswith('C'):
            continue
        cleaned.append(char)
    return "".join(cleaned)

def segment_burmese(text: str) -> list[str]:
    """
    Segments Burmese text into clusters safely keeping combining marks attached to the base,
    and keeping virama-linked sequences (\u1039) together.
    """
    clusters = []
    current_cluster = []
    prev_char = None

    for char in text:
        cat = unicodedata.category(char)
        is_mark = cat.startswith('M')

        # Start a new cluster if it's not a mark AND the previous char wasn't a virama (\u1039)
        if current_cluster and not is_mark and prev_char != '\u1039':
            clusters.append(''.join(current_cluster))
            current_cluster = []

        current_cluster.append(char)
        prev_char = char

    if current_cluster:
        clusters.append(''.join(current_cluster))

    return clusters

def compare_asr(
    expected: str,
    transcription: str,
    threshold: float = 0.97,
    protected_terms: Iterable[str] | None = None,
) -> ASRQAReport:
    if not (0.0 <= threshold <= 1.0):
        raise ValueError("Threshold must be between 0.0 and 1.0")

    protected_terms = tuple(protected_terms or ())

    expected_clean = clean_text(expected)
    transcription_clean = clean_text(transcription)

    expected_segments = segment_burmese(expected_clean)
    transcription_segments = segment_burmese(transcription_clean)

    sm = difflib.SequenceMatcher(
        None,
        expected_segments,
        transcription_segments,
        autojunk=False,
    )
    matched_expected = sum(block.size for block in sm.get_matching_blocks())
    if expected_segments:
        coverage_ratio = matched_expected / len(expected_segments)
    else:
        coverage_ratio = 1.0 if not transcription_segments else 0.0

    missing_spans = []
    extra_spans = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'delete':
            missing_spans.append("".join(expected_segments[i1:i2]))
        elif tag == 'insert':
            extra_spans.append("".join(transcription_segments[j1:j2]))
        elif tag == 'replace':
            missing_spans.append("".join(expected_segments[i1:i2]))
            extra_spans.append("".join(transcription_segments[j1:j2]))

    protected_term_presence = {}
    for term in protected_terms:
        term_clean = clean_text(term)
        protected_term_presence[term] = term_clean in transcription_clean

    # ASR mismatch is a review warning, never an automatic production rejection,
    # so we return a review_required flag
    # Unexpected speech is always surfaced for human review. This remains a
    # warning signal only; callers must not treat it as automatic rejection.
    review_required = coverage_ratio < threshold or bool(extra_spans)
    for term, present in protected_term_presence.items():
        if not present:
            review_required = True

    return ASRQAReport(
        normalized_expected=expected_clean,
        normalized_transcription=transcription_clean,
        coverage_ratio=coverage_ratio,
        missing_spans=missing_spans,
        extra_spans=extra_spans,
        protected_term_presence=protected_term_presence,
        review_required=review_required,
    )
