from __future__ import annotations

import ast
import copy
import tomllib
from pathlib import Path

import pytest
from lossless_recovery_fixture_validator import (
    CENTRAL_RULE,
    FixtureValidationError,
    consumer_visible_strings,
    load_fixture_pack,
    validate_fixture_pack,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "lossless_recovery" / "vectors.json"
FORMAT_PATH = ROOT / "tests" / "fixtures" / "lossless_recovery" / "README.md"
DESIGN_PATH = ROOT / "docs" / "design" / "lossless-recovery-consumer-contract.md"
VALIDATOR_PATH = ROOT / "tests" / "lossless_recovery_fixture_validator.py"

REQUIRED_COVERAGE = {
    "provider_absent",
    "provider_available_no_preservation",
    "one_intact_ref",
    "required_ref_missing",
    "one_byte_ref_mutation",
    "lossy_stage_failure_is_irreversible",
    "tiny_budget_ref_truncation",
    "ref_rewrite_or_ambiguous_relocation",
    "duplicate_same_ref",
    "multiple_distinct_refs_intact",
    "one_of_multiple_refs_missing_or_mutated",
    "provider_failure",
    "malformed_or_unverified_metadata",
    "json_string_envelope",
    "structured_terminal_process_json",
    "oversized_normal_budget",
    "extremely_small_budget",
    "protected_tool_unchanged",
    "unknown_tool_fail_open",
    "pre_redaction_canary_excluded",
    "noisegate_artifact_metadata_distinct",
}


def load_validated_pack() -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    pack = load_fixture_pack(FIXTURE_PATH)
    return pack, validate_fixture_pack(pack)


def case_by_id(pack: dict[str, object], case_id: str) -> dict[str, object]:
    cases = pack["cases"]
    assert isinstance(cases, list)
    return next(case for case in cases if isinstance(case, dict) and case["id"] == case_id)


def test_fixture_pack_is_well_formed_and_covers_required_matrix() -> None:
    pack, resolutions = load_validated_pack()
    cases = pack["cases"]
    assert isinstance(cases, list)
    coverage = {
        tag
        for case in cases
        if isinstance(case, dict)
        for tag in case["covers"]
        if isinstance(tag, str)
    }

    assert len(cases) >= 20
    assert len(resolutions) == len(cases)
    assert coverage >= REQUIRED_COVERAGE
    assert all(case["expected"]["decision"] for case in cases)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ('{"format_version": 1, "format_version": 1}', "duplicate JSON object key"),
        ('{"format_version": NaN}', "non-standard JSON constant"),
    ],
)
def test_fixture_loader_rejects_nonportable_json(
    tmp_path: Path, payload: str, message: str
) -> None:
    fixture = tmp_path / "nonportable.json"
    fixture.write_text(payload, encoding="utf-8")

    with pytest.raises(FixtureValidationError, match=message):
        load_fixture_pack(fixture)


@pytest.mark.parametrize("value", [True, 1.0])
def test_fixture_format_version_requires_an_integer_not_a_json_boolean_or_float(
    value: object,
) -> None:
    pack = load_fixture_pack(FIXTURE_PATH)
    pack["format_version"] = value

    with pytest.raises(FixtureValidationError, match="format version must be integer 1"):
        validate_fixture_pack(pack)


def test_fixture_decisions_are_derived_instead_of_trusting_expected_labels() -> None:
    pack = load_fixture_pack(FIXTURE_PATH)
    mutated = copy.deepcopy(pack)
    case = case_by_id(mutated, "one_intact_ref")
    stages = case["candidate_stages"]
    assert isinstance(stages, list)
    metadata = stages[-1]["protected_structured_metadata"]
    original = metadata["recovery_refs_utf8"][0]
    metadata["recovery_refs_utf8"][0] = original[:-1] + "Y"

    with pytest.raises(FixtureValidationError, match="does not match derived decision"):
        validate_fixture_pack(mutated)


def test_natural_ref_like_text_remains_ordinary_content_on_fallback() -> None:
    pack = load_fixture_pack(FIXTURE_PATH)
    mutated = copy.deepcopy(pack)
    case = case_by_id(mutated, "one_intact_ref")
    marker = mutated["synthetic_recovery_ref_marker_utf8"]
    natural_source = f'const example = "{marker}natural-source-text";\nsource read complete'
    case["canonical_sanitized_envelope_utf8"] = natural_source
    stage = case["candidate_stages"][-1]
    stage["model_visible_copy_utf8"] = natural_source
    stage["protected_structured_metadata"]["recovery_refs_utf8"] = []
    case["expected"] = {
        "decision": "full_sanitized_fallback",
        "selected_source": "canonical_sanitized_envelope",
        "protected_refs_utf8": [],
    }

    resolution = validate_fixture_pack(mutated)[case["id"]]

    assert marker in case["canonical_sanitized_envelope_utf8"]
    assert marker in stage["model_visible_copy_utf8"]
    assert resolution["model_visible_envelope_utf8"].encode("utf-8") == natural_source.encode(
        "utf-8"
    )
    assert resolution["protected_refs_utf8"] == []


def test_prefix_byte_mutation_is_a_well_formed_ref_that_derives_fallback() -> None:
    pack = load_fixture_pack(FIXTURE_PATH)
    mutated = copy.deepcopy(pack)
    case = case_by_id(mutated, "one_intact_ref")
    metadata = case["candidate_stages"][-1]["protected_structured_metadata"]
    original = metadata["recovery_refs_utf8"][0]
    metadata["recovery_refs_utf8"][0] = "x" + original[1:]
    case["expected"] = {
        "decision": "full_sanitized_fallback",
        "selected_source": "canonical_sanitized_envelope",
        "protected_refs_utf8": [],
    }

    resolution = validate_fixture_pack(mutated)[case["id"]]

    assert resolution["model_visible_envelope_utf8"].encode("utf-8") == case[
        "canonical_sanitized_envelope_utf8"
    ].encode("utf-8")
    assert resolution["protected_refs_utf8"] == []


def test_accepted_cases_preserve_complete_ordered_ref_sequence_byte_for_byte() -> None:
    pack, resolutions = load_validated_pack()
    cases = pack["cases"]
    assert isinstance(cases, list)

    for case in cases:
        resolution = resolutions[case["id"]]
        if resolution["decision"] != "accept_candidate":
            continue
        required = [
            value.encode("utf-8")
            for value in case["provider_observation"]["required_protected_refs_utf8"]
        ]
        assert [value.encode("utf-8") for value in resolution["protected_refs_utf8"]] == required
        for stage in case["candidate_stages"]:
            observed = [
                value.encode("utf-8")
                for value in stage["protected_structured_metadata"]["recovery_refs_utf8"]
            ]
            assert observed == required


def test_fallback_cases_return_exact_canonical_envelope_with_no_ref() -> None:
    pack, resolutions = load_validated_pack()
    cases = pack["cases"]
    assert isinstance(cases, list)

    fallback_count = 0
    for case in cases:
        resolution = resolutions[case["id"]]
        if resolution["decision"] != "full_sanitized_fallback":
            continue
        fallback_count += 1
        assert resolution["model_visible_envelope_utf8"].encode("utf-8") == case[
            "canonical_sanitized_envelope_utf8"
        ].encode("utf-8")
        assert resolution["protected_refs_utf8"] == []

    assert fallback_count >= 8


def test_committed_ref_slots_use_obvious_synthetic_values() -> None:
    pack = load_fixture_pack(FIXTURE_PATH)
    marker = pack["synthetic_recovery_ref_marker_utf8"]
    cases = pack["cases"]
    assert isinstance(marker, str)
    assert isinstance(cases, list)

    refs = []
    for case in cases:
        refs.extend(case["provider_observation"]["required_protected_refs_utf8"])
        refs.extend(case["expected"]["protected_refs_utf8"])
        for stage in case["candidate_stages"]:
            refs.extend(stage["protected_structured_metadata"]["recovery_refs_utf8"])

    assert refs
    assert all(ref.startswith(marker) for ref in refs)


def test_forbidden_pre_redaction_canaries_never_reach_consumer_visible_slots() -> None:
    pack, _ = load_validated_pack()
    global_canaries = pack["forbidden_pre_redaction_canaries_utf8"]
    cases = pack["cases"]
    assert isinstance(global_canaries, list)
    assert isinstance(cases, list)

    for case in cases:
        canaries = [*global_canaries, *case["forbidden_pre_redaction_canaries_utf8"]]
        visible = consumer_visible_strings(case)
        assert all(canary not in value for canary in canaries for value in visible)


def test_forbidden_pre_redaction_canary_in_semantic_slot_is_rejected() -> None:
    pack = load_fixture_pack(FIXTURE_PATH)
    mutated = copy.deepcopy(pack)
    case = case_by_id(mutated, "noisegate_artifact_metadata_coexists")
    canary = mutated["forbidden_pre_redaction_canaries_utf8"][0]
    entry = case["candidate_stages"][-1]["protected_structured_metadata"][
        "other_semantic_entries"
    ][0]
    entry["semantic_slot"] = canary

    with pytest.raises(FixtureValidationError, match="pre-redaction canary reached the consumer"):
        validate_fixture_pack(mutated)


def test_lossy_stage_failure_remains_latched_after_later_ref_restoration() -> None:
    pack, resolutions = load_validated_pack()
    case = case_by_id(pack, "lossy_stage_ref_restoration_still_falls_back")
    required = case["provider_observation"]["required_protected_refs_utf8"]
    stages = case["candidate_stages"]
    resolution = resolutions[case["id"]]

    assert stages[0]["protected_structured_metadata"]["recovery_refs_utf8"] == []
    assert stages[1]["protected_structured_metadata"]["recovery_refs_utf8"] == required
    assert resolution["decision"] == "full_sanitized_fallback"
    assert resolution["model_visible_envelope_utf8"].encode("utf-8") == case[
        "canonical_sanitized_envelope_utf8"
    ].encode("utf-8")
    assert resolution["protected_refs_utf8"] == []


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("post_lossy_transform", "lossy stage cannot follow final budget"),
        ("post_final_budget", "final budget stage must not be repeated"),
    ],
)
def test_stage_sequence_rejects_observations_after_final_budget(
    kind: str, message: str
) -> None:
    pack = load_fixture_pack(FIXTURE_PATH)
    mutated = copy.deepcopy(pack)
    case = case_by_id(mutated, "multiple_distinct_refs_intact")
    extra_stage = copy.deepcopy(case["candidate_stages"][-1])
    extra_stage["kind"] = kind
    case["candidate_stages"].append(extra_stage)

    with pytest.raises(FixtureValidationError, match=message):
        validate_fixture_pack(mutated)


def test_duplicate_and_relocated_ref_text_do_not_pass_as_protected_metadata() -> None:
    pack, resolutions = load_validated_pack()
    duplicate = case_by_id(pack, "duplicate_same_ref")
    relocated = case_by_id(pack, "ref_relocated_into_compactable_text")
    required_ref = relocated["provider_observation"]["required_protected_refs_utf8"][0]

    duplicate_stage = duplicate["candidate_stages"][-1]
    duplicate_refs = duplicate_stage["protected_structured_metadata"]["recovery_refs_utf8"]
    assert duplicate_refs[0] == duplicate_refs[1]
    assert resolutions[duplicate["id"]]["decision"] == "full_sanitized_fallback"

    relocated_stage = relocated["candidate_stages"][-1]
    assert required_ref in relocated_stage["model_visible_copy_utf8"]
    assert relocated_stage["protected_structured_metadata"]["recovery_refs_utf8"] == []
    assert resolutions[relocated["id"]]["decision"] == "full_sanitized_fallback"


def test_artifact_metadata_coexists_in_a_distinct_semantic_slot() -> None:
    pack, resolutions = load_validated_pack()
    case = case_by_id(pack, "noisegate_artifact_metadata_coexists")
    stage = case["candidate_stages"][-1]
    metadata = stage["protected_structured_metadata"]
    entries = metadata["other_semantic_entries"]

    assert resolutions[case["id"]]["decision"] == "accept_candidate"
    assert metadata["recovery_refs_utf8"] == case["provider_observation"][
        "required_protected_refs_utf8"
    ]
    assert entries == [
        {
            "semantic_slot": "existing_noisegate_opt_in_artifact_metadata",
            "value_utf8": "synthetic-artifact-id:ng_fixture_only_012345",
        }
    ]


def test_central_rule_is_stated_exactly_in_fixtures_and_design() -> None:
    pack = load_fixture_pack(FIXTURE_PATH)
    design = DESIGN_PATH.read_text(encoding="utf-8")

    assert pack["central_rule"] == CENTRAL_RULE
    assert f"> {CENTRAL_RULE}" in design


def test_fixture_harness_has_no_provider_or_lcm_runtime_dependency() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]
    validator_tree = ast.parse(VALIDATOR_PATH.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(validator_tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_roots.add(node.module.split(".", 1)[0])
    fixture_text = FIXTURE_PATH.read_text(encoding="utf-8").lower()

    assert all("lcm" not in dependency.lower() for dependency in dependencies)
    assert imported_roots.isdisjoint({"noisegate", "hermes_lcm", "lcm"})
    assert "hermes-lcm" not in fixture_text
    assert "hermes_lcm" not in fixture_text


def test_validator_and_vectors_remain_test_only_and_outside_runtime_package() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert VALIDATOR_PATH.parent == ROOT / "tests"
    assert FIXTURE_PATH.is_relative_to(ROOT / "tests")
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "noisegate"
    ]
    assert FORMAT_PATH.is_file()
