"""Validate lossless-recovery fixtures, not a future production interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CENTRAL_RULE = (
    "Treat every recovery ref as protected structured metadata. After any lossy or "
    "final-budget stage, if any required ref is absent, malformed, or differs by even "
    "one byte, reject the compact candidate and use the complete canonical sanitized "
    "result with no recovery ref."
)

_PROVIDER_STATES = {
    "absent",
    "no_preservation",
    "published",
    "failed_before_publication",
    "unusable_metadata",
}
_FAILURE_STATES = {"failed_before_publication", "unusable_metadata"}
_METADATA_STATES = {"absent", "well_formed", "malformed", "unverified"}
_OWNERS = {"host", "provider", "consumer"}


class FixtureValidationError(ValueError):
    """A vector does not conform to the documented test-only format."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise FixtureValidationError(f"duplicate JSON object key {key!r}")
        value[key] = item
    return value


def _reject_nonstandard_constant(value: str) -> None:
    raise FixtureValidationError(f"non-standard JSON constant {value!r}")


def load_fixture_pack(path: Path) -> dict[str, Any]:
    return _mapping(
        json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_nonstandard_constant,
        ),
        "fixture root",
    )


def validate_fixture_pack(pack: dict[str, Any]) -> dict[str, dict[str, Any]]:
    _keys(
        pack,
        {
            "format_version",
            "central_rule",
            "synthetic_recovery_ref_marker_utf8",
            "forbidden_pre_redaction_canaries_utf8",
            "cases",
        },
        "fixture pack",
    )
    if type(pack["format_version"]) is not int or pack["format_version"] != 1:
        raise FixtureValidationError("fixture pack: format version must be integer 1")
    if pack["central_rule"] != CENTRAL_RULE:
        raise FixtureValidationError("fixture pack: central rule differs")
    if not _string(pack["synthetic_recovery_ref_marker_utf8"], "fixture marker"):
        raise FixtureValidationError("fixture marker must not be empty")
    global_canaries = _canaries(pack["forbidden_pre_redaction_canaries_utf8"], "pack")
    cases = _array(pack["cases"], "cases")
    if not cases:
        raise FixtureValidationError("cases must not be empty")

    resolutions: dict[str, dict[str, Any]] = {}
    for index, raw_case in enumerate(cases):
        case = _mapping(raw_case, f"cases[{index}]")
        case_id = _validate_case(case, global_canaries)
        if case_id in resolutions:
            raise FixtureValidationError(f"duplicate case id {case_id!r}")
        resolution = derive_case_resolution(case)
        expected = case["expected"]
        if expected["decision"] != resolution["decision"]:
            raise FixtureValidationError(
                f"{case_id}: expected decision does not match derived decision"
            )
        if expected["selected_source"] != resolution["selected_source"]:
            raise FixtureValidationError(f"{case_id}: selected source differs")
        if not _same_bytes(expected["protected_refs_utf8"], resolution["protected_refs_utf8"]):
            raise FixtureValidationError(f"{case_id}: expected protected refs differ")
        if (
            resolution["decision"] == "full_sanitized_fallback"
            and resolution["protected_refs_utf8"]
        ):
            raise FixtureValidationError(f"{case_id}: fallback contains protected refs")
        resolutions[case_id] = resolution
    return resolutions


def derive_case_resolution(case: dict[str, Any]) -> dict[str, Any]:
    """Derive one truth-table result without consulting its expected label."""

    provider = case["provider_observation"]
    required = provider["required_protected_refs_utf8"]
    stages = case["candidate_stages"]
    fallback = provider["state"] in _FAILURE_STATES or not stages
    wanted_metadata_state = "well_formed" if required else "absent"
    for stage in stages:
        metadata = stage["protected_structured_metadata"]
        if metadata["recovery_metadata_state"] != wanted_metadata_state:
            fallback = True
        if not _same_bytes(metadata["recovery_refs_utf8"], required):
            fallback = True
    if fallback:
        return {
            "decision": "full_sanitized_fallback",
            "selected_source": "canonical_sanitized_envelope",
            "model_visible_envelope_utf8": case["canonical_sanitized_envelope_utf8"],
            "protected_refs_utf8": [],
        }
    return {
        "decision": "accept_candidate",
        "selected_source": "last_candidate_stage",
        "model_visible_envelope_utf8": stages[-1]["model_visible_copy_utf8"],
        "protected_refs_utf8": list(required),
    }


def consumer_visible_strings(case: dict[str, Any]) -> list[str]:
    """Collect every semantic slot visible at the modeled consumer boundary."""

    visible = [case["canonical_sanitized_envelope_utf8"]]
    visible.extend(case["provider_observation"]["required_protected_refs_utf8"])
    for stage in case["candidate_stages"]:
        visible.append(stage["model_visible_copy_utf8"])
        metadata = stage["protected_structured_metadata"]
        visible.extend(metadata["recovery_refs_utf8"])
        for entry in metadata["other_semantic_entries"]:
            visible.extend((entry["semantic_slot"], entry["value_utf8"]))
    return visible


def _validate_case(case: dict[str, Any], global_canaries: list[str]) -> str:
    _keys(
        case,
        {
            "id",
            "covers",
            "owner",
            "provider_observation",
            "canonical_sanitized_envelope_utf8",
            "candidate_stages",
            "expected",
            "forbidden_pre_redaction_canaries_utf8",
            "notes",
        },
        "case",
    )
    case_id = _string(case["id"], "case id")
    if not case_id or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in case_id):
        raise FixtureValidationError("case id must be lowercase ASCII snake_case")
    covers = _strings(case["covers"], f"{case_id}: covers")
    if not covers or len(covers) != len(set(covers)):
        raise FixtureValidationError(f"{case_id}: coverage tags must be non-empty and unique")
    _validate_owner(case["owner"], case_id)
    provider = _validate_provider(case["provider_observation"], case_id)
    _string(case["canonical_sanitized_envelope_utf8"], f"{case_id}: canonical")

    stages = _array(case["candidate_stages"], f"{case_id}: stages")
    seen_final_budget = False
    for index, raw_stage in enumerate(stages):
        context = f"{case_id}: stage {index}"
        stage = _mapping(raw_stage, context)
        _keys(
            stage,
            {"kind", "model_visible_copy_utf8", "protected_structured_metadata"},
            context,
        )
        kind = _string(stage["kind"], f"{context}: kind")
        if kind == "post_lossy_transform":
            if seen_final_budget:
                raise FixtureValidationError(f"{context}: lossy stage cannot follow final budget")
        elif kind == "post_final_budget":
            if seen_final_budget:
                raise FixtureValidationError(f"{context}: final budget stage must not be repeated")
            seen_final_budget = True
        else:
            raise FixtureValidationError(f"{context}: unknown stage kind")
        _string(stage["model_visible_copy_utf8"], f"{context}: visible copy")
        _validate_metadata(stage["protected_structured_metadata"], context)

    expected = _mapping(case["expected"], f"{case_id}: expected")
    _keys(expected, {"decision", "selected_source", "protected_refs_utf8"}, "expected")
    expected_refs = _refs(expected["protected_refs_utf8"], "expected refs", unique=True)
    required = provider["required_protected_refs_utf8"]
    decision = _string(expected["decision"], f"{case_id}: decision")
    selected_source = _string(expected["selected_source"], f"{case_id}: selected source")
    if decision == "accept_candidate":
        if selected_source != "last_candidate_stage" or not stages:
            raise FixtureValidationError(f"{case_id}: accepted result must select a candidate")
        if not _same_bytes(expected_refs, required):
            raise FixtureValidationError(
                f"{case_id}: accepted result must carry every required ref"
            )
    elif decision == "full_sanitized_fallback":
        if selected_source != "canonical_sanitized_envelope" or expected_refs:
            raise FixtureValidationError(f"{case_id}: fallback must select canonical with no refs")
    else:
        raise FixtureValidationError(f"{case_id}: unknown decision")

    local_canaries = _canaries(case["forbidden_pre_redaction_canaries_utf8"], case_id)
    visible = consumer_visible_strings(case)
    if any(canary in value for canary in [*global_canaries, *local_canaries] for value in visible):
        raise FixtureValidationError(f"{case_id}: pre-redaction canary reached the consumer")
    _string(case["notes"], f"{case_id}: notes")
    return case_id


def _validate_owner(value: object, case_id: str) -> None:
    owner = _mapping(value, f"{case_id}: owner")
    _keys(owner, {"primary", "supporting", "invariant"}, "owner")
    primary = _string(owner["primary"], "primary owner")
    supporting = _strings(owner["supporting"], "supporting owners")
    if primary not in _OWNERS or any(item not in _OWNERS for item in supporting):
        raise FixtureValidationError(f"{case_id}: unknown owner")
    if primary in supporting or len(supporting) != len(set(supporting)):
        raise FixtureValidationError(f"{case_id}: owners must be distinct")
    if not _string(owner["invariant"], "owner invariant"):
        raise FixtureValidationError(f"{case_id}: owner invariant must not be empty")


def _validate_provider(value: object, case_id: str) -> dict[str, Any]:
    provider = _mapping(value, f"{case_id}: provider observation")
    _keys(provider, {"state", "required_protected_refs_utf8"}, "provider observation")
    state = _string(provider["state"], "provider state")
    refs = _refs(provider["required_protected_refs_utf8"], "required refs", unique=True)
    if state not in _PROVIDER_STATES:
        raise FixtureValidationError(f"{case_id}: unknown provider state")
    if (state == "published") != bool(refs):
        raise FixtureValidationError(f"{case_id}: only published state declares required refs")
    return provider


def _validate_metadata(value: object, context: str) -> None:
    metadata = _mapping(value, f"{context}: protected metadata")
    _keys(
        metadata,
        {"recovery_metadata_state", "recovery_refs_utf8", "other_semantic_entries"},
        "protected metadata",
    )
    state = _string(metadata["recovery_metadata_state"], "recovery metadata state")
    refs = _refs(metadata["recovery_refs_utf8"], "observed refs", unique=False)
    if state not in _METADATA_STATES or (state == "absent" and refs):
        raise FixtureValidationError(f"{context}: invalid metadata state")
    for raw_entry in _array(metadata["other_semantic_entries"], "other metadata"):
        entry = _mapping(raw_entry, "other metadata entry")
        _keys(entry, {"semantic_slot", "value_utf8"}, "other metadata entry")
        if not _string(entry["semantic_slot"], "semantic slot"):
            raise FixtureValidationError("semantic slot must not be empty")
        _string(entry["value_utf8"], "semantic value")


def _refs(value: object, context: str, *, unique: bool) -> list[str]:
    refs = _strings(value, context)
    if unique and len(refs) != len(set(refs)):
        raise FixtureValidationError(f"{context}: refs must be distinct")
    return refs


def _canaries(value: object, context: str) -> list[str]:
    canaries = _strings(value, f"{context}: canaries")
    if any(not item.startswith("SYNTHETIC_PRE_REDACTION_CANARY_") for item in canaries):
        raise FixtureValidationError(f"{context}: canary is not obviously synthetic")
    if len(canaries) != len(set(canaries)):
        raise FixtureValidationError(f"{context}: canaries must be unique")
    return canaries


def _same_bytes(left: list[str], right: list[str]) -> bool:
    return [item.encode("utf-8") for item in left] == [item.encode("utf-8") for item in right]


def _strings(value: object, context: str) -> list[str]:
    return [_string(item, context) for item in _array(value, context)]


def _string(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise FixtureValidationError(f"{context}: expected string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FixtureValidationError(f"{context}: not UTF-8 encodable") from exc
    return value


def _array(value: object, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise FixtureValidationError(f"{context}: expected array")
    return value


def _mapping(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FixtureValidationError(f"{context}: expected object")
    return value


def _keys(value: dict[str, Any], expected: set[str], context: str) -> None:
    if set(value) != expected:
        unexpected = sorted(set(value) ^ expected)
        raise FixtureValidationError(f"{context}: unexpected keys {unexpected!r}")
