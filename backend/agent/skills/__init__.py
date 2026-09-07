"""FYF versioned skill-module registry (pure Python facade).

The seven internal pipeline responsibilities are exposed here as *versioned skill
modules* — descriptors that WRAP the existing production code by reference. This
registry is documentation + dispatch metadata only; importing it performs no
provider call, starts no server, and constructs no ADK agent.

Architectural constraint (docs/HANDOFF_AGENTIC_BUSINESS_STUDIO.md §68):
    "Never present those pipeline responsibilities as separate agents."
The runtime therefore keeps exactly two ``LlmAgent`` instances (Producer + Data
Officer); this package adds none. A guard test in
``backend/agent/test_skills_registry.py`` asserts that invariant.

Public API
----------
    SKILL_NAMES            -> tuple of the seven canonical skill names
    list_skills()          -> list[SkillDescriptor] (registry order)
    get_skill(name)        -> SkillDescriptor (raises UnknownSkillError)
    get_module(name)       -> the skill's Python module
    version_manifest()     -> {skill_name: version}
    full_manifest()        -> list of per-skill manifest rows
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

# Mirror the sibling modules' bootstrap so the facade imports cleanly even when
# loaded outside the pytest pythonpath configuration.
_repo_root = Path(__file__).resolve().parent.parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))
_backend_root = _repo_root / "backend"
if str(_backend_root) not in sys.path:
    sys.path.insert(0, str(_backend_root))

from backend.agent.skills.base import (  # noqa: E402
    REQUIRED_FIELDS,
    Example,
    QualityCheck,
    SkillDescriptor,
    ToolRef,
    UnknownSkillError,
    is_valid_semver,
)

# Canonical skill name -> implementing module (relative to this package).
_SKILL_MODULES: dict[str, str] = {
    "brief": "backend.agent.skills.brief",
    "research_claims": "backend.agent.skills.research_claims",
    "script": "backend.agent.skills.script",
    "visual_motion": "backend.agent.skills.visual_motion",
    "voice_audio": "backend.agent.skills.voice_audio",
    "render": "backend.agent.skills.render",
    "qa": "backend.agent.skills.qa",
}

SKILL_NAMES: tuple[str, ...] = tuple(_SKILL_MODULES)

__all__ = [
    "SKILL_NAMES",
    "REQUIRED_FIELDS",
    "Example",
    "QualityCheck",
    "SkillDescriptor",
    "ToolRef",
    "UnknownSkillError",
    "is_valid_semver",
    "list_skills",
    "get_skill",
    "get_module",
    "version_manifest",
    "full_manifest",
]


def _load(name: str):
    """Import and return a skill module, or raise UnknownSkillError."""
    module_path = _SKILL_MODULES.get(name)
    if module_path is None:
        raise UnknownSkillError(name, list(SKILL_NAMES))
    return importlib.import_module(module_path)


def get_module(name: str):
    """Return the Python module implementing ``name``."""
    return _load(name)


def get_skill(name: str) -> SkillDescriptor:
    """Return the :class:`SkillDescriptor` registered under ``name``.

    Raises:
        UnknownSkillError: with a clear message listing valid names when
            ``name`` is not one of the seven canonical skills.
    """
    module = _load(name)
    descriptor = getattr(module, "DESCRIPTOR", None)
    if not isinstance(descriptor, SkillDescriptor):
        raise UnknownSkillError(name, list(SKILL_NAMES))
    return descriptor


def list_skills() -> list[SkillDescriptor]:
    """Return every registered skill descriptor in canonical order."""
    return [get_skill(name) for name in SKILL_NAMES]


def version_manifest() -> dict[str, str]:
    """Return an aggregate ``{skill_name: VERSION}`` manifest."""
    return {name: get_skill(name).version for name in SKILL_NAMES}


def full_manifest() -> list[dict[str, Any]]:
    """Return the detailed per-skill manifest rows."""
    return [get_skill(name).manifest_entry() for name in SKILL_NAMES]
