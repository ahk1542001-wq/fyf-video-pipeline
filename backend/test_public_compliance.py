"""Tests for public tree compliance and release boundary guard."""

from pathlib import Path
from backend.public_compliance import (
    check_forbidden_files,
    check_forbidden_terms,
    get_git_tracked_files,
)


def test_git_tracked_files_discovery():
    tracked = get_git_tracked_files()
    assert len(tracked) > 0
    assert "README.md" in tracked or "pyproject.toml" in tracked


def test_no_forbidden_files_in_public_tree():
    violations = check_forbidden_files()
    assert violations == [], f"Forbidden files tracked in git: {violations}"


def test_no_forbidden_terms_in_public_tree():
    violations = check_forbidden_terms()
    if violations:
        error_lines = []
        for file_path, items in violations.items():
            for line_num, term, line in items:
                error_lines.append(f"{file_path}:{line_num} (found '{term}'): {line}")
        assert not violations, (
            f"Found {len(error_lines)} forbidden term occurrences in public tree:\n"
            + "\n".join(error_lines)
        )
