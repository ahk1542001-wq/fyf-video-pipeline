"""Context-integrity gate for FYF contributor-facing docs (stdlib only).

Root AGENTS.md is tracked and ships in every clean clone; this suite validates
it alongside the tracked protocol in docs/DEVELOPER_GUIDE.md. The default model
remains pinned to executable routing. No network, no non-stdlib imports.
Part of: uv run pytest -q.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_MD = REPO_ROOT / "AGENTS.md"
DEVELOPER_GUIDE_MD = REPO_ROOT / "docs" / "DEVELOPER_GUIDE.md"
PROJECT_STRUCTURE_MD = REPO_ROOT / "docs" / "PROJECT_STRUCTURE.md"
README_MD = REPO_ROOT / "README.md"
ROUTING_PY = REPO_ROOT / "vertex_model_routing.py"
PROTOCOL_HEADING = "## Change-Impact & Context Integrity Protocol"

PACKET_FIELDS = [
    "task_goal",
    "canonical_sources_read",
    "existing_implementation",
    "symbols_to_change",
    "callers_and_consumers",
    "relevant_tests",
    "affected_documentation",
    "conflicts_or_stale_claims",
    "planned_files",
    "new_file_justification",
    "unknowns",
]

SEARCH_REQUIREMENTS = [
    "exact symbol, error, and requested feature",
    "filename plus aliases and synonyms",
    "existing implementation",
    "definition → callers/imports → tests → docs",
    "existing-equivalent search",
    "source and tests define current behavior",
]


class TestDocsIntegrity:
    """Doc-drift tripwires. Every failure names the drifted doc and where to look."""

    def test_tracked_protocol_is_complete(self):
        """The clean-clone developer guide must carry the complete context gate."""
        text = DEVELOPER_GUIDE_MD.read_text(encoding="utf-8")
        assert PROTOCOL_HEADING in text, (
            "DOC DRIFT in docs/DEVELOPER_GUIDE.md - missing tracked context protocol."
        )
        protocol = text[text.index(PROTOCOL_HEADING):]
        missing = [
            token
            for token in [*PACKET_FIELDS, *SEARCH_REQUIREMENTS]
            if token not in protocol
        ]
        assert not missing, (
            "DOC DRIFT in docs/DEVELOPER_GUIDE.md - missing context-gate "
            "requirement(s): %r." % (missing,)
        )

    def test_tracked_protocol_paths_exist(self):
        """The tracked protocol's named lanes and test configuration must exist."""
        required_paths = [
            "backend",
            "voice_service",
            "remotion",
            "frontend",
            "pyproject.toml",
        ]
        missing = [path for path in required_paths if not (REPO_ROOT / path).exists()]
        assert not missing, (
            "DOC DRIFT in docs/DEVELOPER_GUIDE.md - required path(s) missing: %r."
            % (missing,)
        )

    def test_project_structure_marks_agents_md_tracked(self):
        """The tracked tree must present root AGENTS.md as shipped, not optional."""
        text = PROJECT_STRUCTURE_MD.read_text(encoding="utf-8")
        lines = [line for line in text.splitlines() if "AGENTS.md" in line]
        assert lines, "DOC DRIFT in docs/PROJECT_STRUCTURE.md - AGENTS.md entry missing."
        bad = [ln for ln in lines if "optional" in ln.lower() or "gitignored" in ln.lower()]
        assert not bad, (
            "DOC DRIFT in docs/PROJECT_STRUCTURE.md - root AGENTS.md is tracked; "
            "remove optional/gitignored labeling: %r" % (bad,)
        )

    def test_agents_md_tracked_and_consistent(self):
        """Tracked root rules must exist and never contradict protocol or source."""
        assert AGENTS_MD.exists(), (
            "AGENTS.md missing at repo root - clean clones would ship without "
            "the agent contract. Keep '/AGENTS.md' tracked (.gitignore carries "
            "'!/AGENTS.md') and restore the sanitized file."
        )

        text = AGENTS_MD.read_text(encoding="utf-8")
        banned = [
            (
                r"gemini-[0-9]",
                "hardcoded model id - model ids come from vertex_model_routing.py ROUTES/model_for()",
            ),
            (
                r"(?i)not yet created",
                "stale build-status claim - status does not belong in AGENTS.md",
            ),
            (
                r"(?i)deadline:\s*sep",
                "stale deadline - dates live in DEVPOST_SUBMISSION.md",
            ),
            (
                r"(?i)requirements\.txt`?\s+is\s+stale",
                "volatile status claim - dependency authority belongs to pyproject.toml and uv.lock",
            ),
        ]
        hits = []
        for pattern, why in banned:
            match = re.search(pattern, text)
            if match:
                line_no = text.count("\n", 0, match.start()) + 1
                hits.append(
                    "AGENTS.md:%d matches %r near %r (%s)"
                    % (line_no, pattern, text[max(0, match.start() - 30):match.end() + 30], why)
                )
        required = [
            "Source-of-Truth Hierarchy",
            "Pre-Change Context Gate",
            "README.md",
            "docs/PROJECT_STRUCTURE.md",
            "docs/decisions/",
            "vertex_model_routing.py",
            *PACKET_FIELDS,
        ]
        missing = [token for token in required if token not in text]
        broken = []
        for token in re.findall(r"`([^`\n]+)`", text):
            looks_like_path = "/" in token or token.endswith(
                (".py", ".md", ".json", ".toml")
            )
            if not looks_like_path:
                continue
            if not (REPO_ROOT / token).exists():
                broken.append(token)
        assert not (hits or missing or broken), (
            "DOC DRIFT in tracked AGENTS.md - stale=%r missing=%r broken_paths=%r. "
            "Tracked protocol: docs/DEVELOPER_GUIDE.md; executable sources: "
            "vertex_model_routing.py and the repository tree."
            % (hits, missing, broken)
        )

    def test_agents_md_has_no_private_references(self):
        """Public contributor rules must not expose local private-workspace paths."""
        text = AGENTS_MD.read_text(encoding="utf-8")
        banned = [
            (r"/(?:Users|home)/[^/\s`]+", "absolute user-home path"),
            (
                r"(?i)\b(?:private|internal)\s+(?:vault|workspace)\b",
                "private workspace reference",
            ),
        ]
        hits = []
        for pattern, why in banned:
            m = re.search(pattern, text)
            if m:
                line_no = text.count("\n", 0, m.start()) + 1
                hits.append("AGENTS.md:%d matches %r (%s)" % (line_no, m.group(0), why))
        assert not hits, (
            "PRIVACY DRIFT in AGENTS.md - remove private references; public docs "
            "describe behavior, not operator infrastructure.\n  " + "\n  ".join(hits)
        )

    def test_readme_explains_public_agent_contract_boundary(self):
        """README must distinguish the public contract from private instructions."""
        text = README_MD.read_text(encoding="utf-8")
        required = [
            "sanitized public contributor contract",
            "AGENTS.md",
            "private internal agent instructions",
        ]
        missing = [token for token in required if token not in text]
        assert not missing, (
            "PUBLIC BOUNDARY DRIFT in README.md - missing AGENTS.md boundary "
            "wording: %r." % (missing,)
        )

    def test_readme_model_matches_routing_source(self):
        """README.md must document the first default gemini-* route inside vertex_model_routing.py ROUTES."""
        source = ROUTING_PY.read_text(encoding="utf-8")
        routes_block = source[source.index("ROUTES"):]
        match = re.search(r"['\"](gemini-[0-9a-z.\-]+)['\"]", routes_block)
        assert match, (
            "vertex_model_routing.py no longer declares a gemini-* default inside "
            "ROUTES - routing source changed; update this gate deliberately."
        )
        model_id = match.group(1)
        readme = README_MD.read_text(encoding="utf-8")
        # Accept either the exact route id ("gemini-3.7-flash") or its documented
        # prose form ("Gemini 3.7 Flash"); anything else means README drifted.
        documented_forms = [model_id.casefold(), model_id.replace("-", " ").casefold()]
        assert any(form in readme.casefold() for form in documented_forms), (
            "DOC DRIFT in README.md - default route %r from vertex_model_routing.py "
            "ROUTES is not documented in README.md (accepts the exact id or "
            "'Gemini <N.N> Flash' prose). Update README.md, or investigate why "
            "vertex_model_routing.py changed." % (model_id,)
        )
