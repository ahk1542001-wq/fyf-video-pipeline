"""Tests for public tree compliance and release boundary guard."""

import subprocess
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


def test_forbidden_term_guard_is_path_scoped_not_disabled(tmp_path):
    """Guard-integrity: the docs/COMPLIANCE_MATRIX.md exclusion must be path-scoped.

    A forbidden vendor term in any OTHER tracked file must still be flagged, and
    the same term inside the excluded path must not be. This proves the exclusion
    narrows the scan by path rather than silently disabling the whole check (an
    assertion-weakening that is not acceptable).
    """
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)

    # Excluded path: naming an excluded vendor here must NOT be flagged.
    excluded = repo / "docs" / "COMPLIANCE_MATRIX.md"
    excluded.write_text(
        "| scope | no Grafana/NLE/voice-clone | exclusion documented |\n",
        encoding="utf-8",
    )
    # Non-excluded path: the same forbidden term MUST still be flagged.
    offender = repo / "PUBLIC_NOTES.md"
    offender.write_text(
        "plan: adopt grafana dashboards across the product\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "docs/COMPLIANCE_MATRIX.md", "PUBLIC_NOTES.md"],
        cwd=str(repo),
        check=True,
    )

    violations = check_forbidden_terms(repo_root=repo)

    assert "PUBLIC_NOTES.md" in violations, (
        "guard must still flag a forbidden term in a non-excluded tracked file"
    )
    assert any(term == "grafana" for _, term, _ in violations["PUBLIC_NOTES.md"])
    assert "docs/COMPLIANCE_MATRIX.md" not in violations, (
        "exclusion must be path-scoped to docs/COMPLIANCE_MATRIX.md only"
    )
