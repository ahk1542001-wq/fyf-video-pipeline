"""Shared descriptor types for the FYF versioned skill-module facade.

This package is a *documentation and dispatch facade only*. It WRAPS the
existing production pipeline (``backend.pipeline``, ``writer_agent_vertex``,
``visual_evidence_vertex``, ``voice_service.*``, the QA trio and the two ADK
agents) and NEVER rewrites or re-implements that logic.

Architectural constraint (docs/HANDOFF_AGENTIC_BUSINESS_STUDIO.md §68):
    "Never present those pipeline responsibilities as separate agents."

Therefore the seven internal responsibilities (Brief/Strategy, Research/Claims,
Script, Visual/Motion, Voice/Audio, Render, QA) are exposed here as *skill
modules* — pure Python descriptors plus lazy delegation — and the runtime still
contains exactly two ADK ``LlmAgent`` instances (the Producer and the Data
Officer). Nothing in this package constructs an ``Agent``/``LlmAgent``.

Every schema referenced by a skill is an existing strict ``video_contract``
Pydantic type (``ConfigDict(extra="forbid")`` convention). No duplicate schema
is defined here. Every tool / quality-check reference points at an existing,
importable callable by module path + attribute name; no executable logic is
copied into this facade.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = [
    "SEMVER_PATTERN",
    "is_valid_semver",
    "ToolRef",
    "QualityCheck",
    "Example",
    "SkillDescriptor",
    "UnknownSkillError",
    "REQUIRED_FIELDS",
]

# Canonical semver 2.0.0 pattern (major.minor.patch with optional pre-release
# and build metadata). Used by the registry tests to prove every skill
# publishes a well-formed VERSION.
SEMVER_PATTERN = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+(?P<buildmetadata>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


def is_valid_semver(value: str) -> bool:
    """Return True when ``value`` is a valid semantic-version string."""
    return isinstance(value, str) and SEMVER_PATTERN.match(value) is not None


class UnknownSkillError(KeyError):
    """Raised when a skill name is not present in the registry.

    Subclasses ``KeyError`` so callers that already guard dictionary lookups
    keep working, while carrying a clear, human-readable message that lists the
    valid skill names instead of crashing with an opaque traceback.
    """

    def __init__(self, name: str, available: list[str]) -> None:
        self.name = name
        self.available = list(available)
        message = (
            f"Unknown skill {name!r}. Available skills: "
            f"{', '.join(sorted(self.available)) or '<none>'}"
        )
        super().__init__(message)
        self._message = message

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self._message


@dataclass(frozen=True)
class ToolRef:
    """A *reference by name* to an existing tool or pipeline function.

    The facade never copies the referenced executable code; it only records
    where the authoritative implementation lives so the registry tests can
    prove the reference is real (importable and callable) rather than phantom.
    """

    module: str
    name: str
    role: str = ""

    @property
    def dotted(self) -> str:
        return f"{self.module}.{self.name}"

    def resolve(self) -> Callable[..., Any]:
        """Import the owning module and return the referenced callable."""
        module = importlib.import_module(self.module)
        return getattr(module, self.name)

    def exists(self) -> bool:
        """True when the referenced attribute is present on its module."""
        module = importlib.import_module(self.module)
        return hasattr(module, self.name)


@dataclass(frozen=True)
class QualityCheck:
    """A *delegation* to an existing quality-check implementation.

    ``module``/``attribute`` point at the authoritative deterministic check
    (e.g. ``backend.output_qa.qa_job_directory``). The skill facade does not
    re-implement the check; callers dispatch through :meth:`resolve`.
    """

    name: str
    module: str
    attribute: str
    description: str = ""
    kind: str = "technical"  # "technical" | "creative" | "human_acceptance"

    @property
    def dotted(self) -> str:
        return f"{self.module}.{self.attribute}"

    def resolve(self) -> Callable[..., Any]:
        module = importlib.import_module(self.module)
        return getattr(module, self.attribute)

    def exists(self) -> bool:
        module = importlib.import_module(self.module)
        return hasattr(module, self.attribute)


@dataclass(frozen=True)
class Example:
    """A deterministic input -> output illustration.

    Contains only static, JSON-serialisable data. No provider call, network
    access, or filesystem access is performed to produce or consume it.
    """

    name: str
    input: dict[str, Any]
    output: dict[str, Any]
    note: str = ""


@dataclass(frozen=True)
class SkillDescriptor:
    """The full contract of one internal pipeline responsibility.

    Field names deliberately mirror the eight required module-level constants
    (``TRIGGER``, ``INPUT_SCHEMA``, ``OUTPUT_SCHEMA``, ``ALLOWED_TOOLS``,
    ``INVARIANTS``, ``EXAMPLES``, ``QUALITY_CHECKS``, ``VERSION``) so the
    registry tests can validate a skill through either the module constants or
    this descriptor.
    """

    name: str
    responsibility: str
    trigger: str
    input_schema: tuple[type, ...]
    output_schema: tuple[type, ...]
    allowed_tools: tuple[ToolRef, ...]
    invariants: tuple[str, ...]
    examples: tuple[Example, ...]
    quality_checks: tuple[QualityCheck, ...]
    version: str
    wrapped_modules: tuple[str, ...] = field(default_factory=tuple)
    notes: str = ""

    def is_complete(self) -> bool:
        """True when all eight required fields are populated and well-formed."""
        return bool(
            self.name
            and self.trigger
            and self.input_schema
            and self.output_schema
            and self.allowed_tools
            and self.invariants
            and self.examples
            and self.quality_checks
            and is_valid_semver(self.version)
        )

    def manifest_entry(self) -> dict[str, Any]:
        """Return the version-manifest row for this skill."""
        return {
            "name": self.name,
            "responsibility": self.responsibility,
            "version": self.version,
        }


# The eight constants every skill module MUST expose at module level.
REQUIRED_FIELDS: tuple[str, ...] = (
    "TRIGGER",
    "INPUT_SCHEMA",
    "OUTPUT_SCHEMA",
    "ALLOWED_TOOLS",
    "INVARIANTS",
    "EXAMPLES",
    "QUALITY_CHECKS",
    "VERSION",
)
