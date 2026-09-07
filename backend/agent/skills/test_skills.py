"""Deterministic tests for the FYF versioned skill-module facade.

Zero provider calls, zero network, zero filesystem writes. Every test either
inspects the registry/descriptors statically or performs an import + attribute
check to prove references are real (no phantom tools, no duplicate schemas).
"""

from __future__ import annotations

import importlib

import pytest
from pydantic import BaseModel

import video_contract
from backend.agent.skills import (
    REQUIRED_FIELDS,
    SKILL_NAMES,
    SkillDescriptor,
    UnknownSkillError,
    get_module,
    get_skill,
    is_valid_semver,
    list_skills,
    version_manifest,
)

EXPECTED_SKILLS = {
    "brief",
    "research_claims",
    "script",
    "visual_motion",
    "voice_audio",
    "render",
    "qa",
}


# --- Requirement 1: all 7 skills present with all 8 required fields ---------
def test_registry_contains_exactly_seven_canonical_skills():
    assert set(SKILL_NAMES) == EXPECTED_SKILLS
    assert len(SKILL_NAMES) == 7
    assert len(list_skills()) == 7


@pytest.mark.parametrize("name", sorted(EXPECTED_SKILLS))
def test_each_skill_module_exposes_all_required_fields(name):
    module = get_module(name)
    for field_name in REQUIRED_FIELDS:
        assert hasattr(module, field_name), f"{name} missing {field_name}"
        value = getattr(module, field_name)
        assert value not in (None, "", (), [], {}), (
            f"{name}.{field_name} is empty"
        )


@pytest.mark.parametrize("name", sorted(EXPECTED_SKILLS))
def test_each_descriptor_is_complete_and_consistent(name):
    descriptor = get_skill(name)
    assert isinstance(descriptor, SkillDescriptor)
    assert descriptor.name == name
    assert descriptor.responsibility
    assert descriptor.is_complete(), f"{name} descriptor incomplete"
    # Descriptor fields must mirror the module-level constants.
    module = get_module(name)
    assert descriptor.trigger == module.TRIGGER
    assert descriptor.input_schema == module.INPUT_SCHEMA
    assert descriptor.output_schema == module.OUTPUT_SCHEMA
    assert descriptor.allowed_tools == module.ALLOWED_TOOLS
    assert descriptor.invariants == module.INVARIANTS
    assert descriptor.examples == module.EXAMPLES
    assert descriptor.quality_checks == module.QUALITY_CHECKS
    assert descriptor.version == module.VERSION
    # Tuple-typed fields must be non-empty tuples of the right element types.
    assert isinstance(descriptor.invariants, tuple) and descriptor.invariants
    assert all(isinstance(x, str) for x in descriptor.invariants)
    assert isinstance(descriptor.allowed_tools, tuple) and descriptor.allowed_tools
    assert isinstance(descriptor.quality_checks, tuple) and descriptor.quality_checks
    assert isinstance(descriptor.examples, tuple) and descriptor.examples


# --- Requirement 2: VERSION is valid semver ---------------------------------
@pytest.mark.parametrize("name", sorted(EXPECTED_SKILLS))
def test_version_is_valid_semver(name):
    version = get_skill(name).version
    assert is_valid_semver(version), f"{name} VERSION {version!r} is not semver"


def test_version_manifest_covers_all_skills():
    manifest = version_manifest()
    assert set(manifest) == EXPECTED_SKILLS
    assert all(is_valid_semver(v) for v in manifest.values())


def test_is_valid_semver_rejects_malformed():
    assert is_valid_semver("1.0.0")
    assert is_valid_semver("0.1.0")
    assert is_valid_semver("1.2.3-rc.1")
    assert not is_valid_semver("1.0")
    assert not is_valid_semver("v1.0.0")
    assert not is_valid_semver("1.0.0.0")
    assert not is_valid_semver("")


# --- Requirement 3: schemas reference existing video_contract types ---------
@pytest.mark.parametrize("name", sorted(EXPECTED_SKILLS))
def test_schemas_reference_video_contract_types(name):
    descriptor = get_skill(name)
    for schema in (*descriptor.input_schema, *descriptor.output_schema):
        assert isinstance(schema, type), f"{name} schema {schema!r} not a class"
        assert issubclass(schema, BaseModel), (
            f"{name} schema {schema.__name__} is not a pydantic model"
        )
        assert schema.__module__ == "video_contract", (
            f"{name}.{schema.__name__} not defined in video_contract "
            f"(module={schema.__module__}) — duplicate definition suspected"
        )
        # Identity check proves it is the imported original, not a re-definition.
        assert getattr(video_contract, schema.__name__, None) is schema


# --- Requirement 4: every ALLOWED_TOOLS reference actually exists -----------
@pytest.mark.parametrize("name", sorted(EXPECTED_SKILLS))
def test_allowed_tools_are_not_phantom(name):
    descriptor = get_skill(name)
    for tool in descriptor.allowed_tools:
        module = importlib.import_module(tool.module)
        assert hasattr(module, tool.name), (
            f"{name}: phantom tool {tool.dotted}"
        )
        resolved = tool.resolve()
        assert callable(resolved), f"{name}: {tool.dotted} is not callable"
        assert tool.exists()


# --- Requirement 5: QUALITY_CHECKS delegate to real callables ---------------
@pytest.mark.parametrize("name", sorted(EXPECTED_SKILLS))
def test_quality_checks_delegate_to_callable(name):
    descriptor = get_skill(name)
    for check in descriptor.quality_checks:
        module = importlib.import_module(check.module)
        assert hasattr(module, check.attribute), (
            f"{name}: phantom quality check {check.dotted}"
        )
        resolved = check.resolve()
        assert callable(resolved), f"{name}: {check.dotted} is not callable"
        assert check.exists()
        assert check.kind in {"technical", "creative", "human_acceptance"}


def test_qa_skill_separates_technical_from_creative_lanes():
    """QA invariant: technical and creative/human acceptance stay distinct."""
    qa = get_skill("qa")
    kinds = {check.kind for check in qa.quality_checks}
    assert "technical" in kinds
    assert "creative" in kinds or "human_acceptance" in kinds
    joined = " ".join(qa.invariants).lower()
    assert "distinct" in joined or "separ" in joined


def test_visual_motion_enforces_motion_primitive_invariant():
    """The no-arbitrary-generated-render-code invariant must be documented."""
    vm = get_skill("visual_motion")
    joined = " ".join(vm.invariants).lower()
    assert "motion primitive" in joined
    assert "executable" in joined and "code" in joined


# --- Requirement 6: unknown skill name -> clear error, not a crash ----------
def test_unknown_skill_raises_clear_error():
    with pytest.raises(UnknownSkillError) as excinfo:
        get_skill("does_not_exist")
    # UnknownSkillError is a KeyError subclass with a helpful message.
    assert isinstance(excinfo.value, KeyError)
    message = str(excinfo.value)
    assert "does_not_exist" in message
    assert "brief" in message  # lists valid names


def test_unknown_skill_error_is_not_a_generic_crash():
    err = UnknownSkillError("nope", list(SKILL_NAMES))
    assert err.name == "nope"
    assert set(err.available) == EXPECTED_SKILLS
    assert "Available skills" in str(err)


# --- Examples are deterministic static data (no provider calls) ------------
@pytest.mark.parametrize("name", sorted(EXPECTED_SKILLS))
def test_examples_are_static_json_data(name):
    descriptor = get_skill(name)
    for example in descriptor.examples:
        assert example.name
        assert isinstance(example.input, dict)
        assert isinstance(example.output, dict)
