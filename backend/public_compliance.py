"""Public compliance guard for the Google-only Hackathon release boundary.

Scans all git-tracked files for prohibited runtime terms, untracked/leaked secrets,
and forbidden dependencies or assets.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Set, Tuple

FORBIDDEN_TERMS: Set[str] = {
    "claude",
    "voxcpm",
    "openbmb",
    "kaggle",
    "colab-mcp",
    "mcp-server-colab",
    "clickhouse",
    "grafana",
    "elevenlabs",
}

FORBIDDEN_FILE_PATTERNS: List[re.Pattern] = [
    re.compile(r"(^|/)\.env$"),
    re.compile(r"(^|/)\.env\.local$"),
    re.compile(r"(^|/)gcp-key\.json$"),
    re.compile(r"(^|/)output/.*"),
    re.compile(r".*\.(wav|mp3|ogg|flac)$"),
    re.compile(r".*raw_payload.*\.json$"),
]

# Allowable documentation history exclusions if strictly necessary
ALLOWED_TERM_EXCLUSIONS: Set[str] = {
    "docs/COMPLIANCE_BASELINE.md",
    "backend/public_compliance.py",
    "backend/test_public_compliance.py",
}


def get_git_tracked_files(repo_root: Path | None = None) -> List[str]:
    """Retrieve all files currently tracked by git."""
    root = repo_root or Path(__file__).resolve().parent.parent
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def check_forbidden_files(repo_root: Path | None = None) -> List[str]:
    """Check for tracked files matching forbidden patterns (secrets, outputs, audio)."""
    tracked_files = get_git_tracked_files(repo_root)
    violations: List[str] = []
    for file_path in tracked_files:
        for pattern in FORBIDDEN_FILE_PATTERNS:
            if pattern.search(file_path):
                violations.append(file_path)
                break
    return violations


def check_forbidden_terms(
    repo_root: Path | None = None,
) -> Dict[str, List[Tuple[int, str, str]]]:
    """Scan tracked text files for forbidden runtime/vendor terms."""
    root = repo_root or Path(__file__).resolve().parent.parent
    tracked_files = get_git_tracked_files(root)
    violations: Dict[str, List[Tuple[int, str, str]]] = {}

    pattern = re.compile(
        r"\b(" + "|".join(re.escape(term) for term in FORBIDDEN_TERMS) + r")\b",
        re.IGNORECASE,
    )

    for rel_path in tracked_files:
        if rel_path in ALLOWED_TERM_EXCLUSIONS:
            continue

        file_path = root / rel_path
        if not file_path.is_file():
            continue

        # Skip binary files
        try:
            content = file_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue

        file_violations: List[Tuple[int, str, str]] = []
        for line_num, line in enumerate(content.splitlines(), start=1):
            matches = pattern.findall(line)
            if matches:
                for match in matches:
                    file_violations.append((line_num, match.lower(), line.strip()))

        if file_violations:
            violations[rel_path] = file_violations

    return violations
