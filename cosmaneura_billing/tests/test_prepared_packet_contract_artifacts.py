"""Mechanical checks for the draft prepared-packet contract artifacts.

These tests intentionally use only the standard library. They validate the
small JSON Schema keyword profile declared by the contract; they do not claim
to implement all of JSON Schema Draft 2020-12.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from cosmaneura_billing.completeness import RULE_PACKS
from cosmaneura_billing.seed_demo_release_candidate import (
    EXPECTED_CASE_DIGESTS,
    EXPECTED_EXTENSION_CASE_DIGESTS,
    EXPECTED_EXTENSION_PACK_DIGEST,
    EXPECTED_PACK_DIGEST,
    EXPECTED_SEED_DIGEST,
    prepare_synthetic_demo_seed,
)


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "docs" / "contracts" / "prepared-billing-packet" / "v1"
EXAMPLES = CONTRACT / "examples"
SCHEMA_PATH = CONTRACT / "contracts.schema.json"
VECTORS_PATH = CONTRACT / "canonicalization-vectors.v1.json"
MAX_SAFE_INTEGER = 9_007_199_254_740_991


class ContractValidationError(AssertionError):
    """A value is outside the checked contract profile."""


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _strict_integer(value: str) -> int:
    if value == "-0":
        raise ValueError("negative zero is prohibited")
    parsed = int(value)
    if not -MAX_SAFE_INTEGER <= parsed <= MAX_SAFE_INTEGER:
        raise ValueError("integer is outside the cross-language safe range")
    return parsed


def _reject_float(value: str) -> None:
    raise ValueError(f"floating-point JSON number: {value}")


def _strict_load_bytes(raw: bytes) -> Any:
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("UTF-8 BOM is prohibited")
    return json.loads(
        raw.decode("utf-8", errors="strict"),
        object_pairs_hook=_object_without_duplicates,
        parse_constant=_reject_nonfinite,
        parse_float=_reject_float,
        parse_int=_strict_integer,
    )


def _load(path: Path) -> Any:
    return _strict_load_bytes(path.read_bytes())


def _check_canonical_value(value: Any) -> None:
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise ValueError("integer is outside the cross-language safe range")
        return
    if type(value) is float:
        raise ValueError("floats are prohibited")
    if type(value) is str:
        value.encode("utf-8", errors="strict")
        return
    if type(value) is list:
        for item in value:
            _check_canonical_value(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or not key.isascii():
                raise ValueError("canonical object keys must be ASCII strings")
            _check_canonical_value(item)
        return
    raise ValueError(f"non-JSON canonical value: {type(value).__name__}")


def _canonical(value: Any) -> bytes:
    _check_canonical_value(value)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8", errors="strict")


def _same_json_value(left: Any, right: Any) -> bool:
    return type(left) is type(right) and left == right


def _matches_type(value: Any, expected: str) -> bool:
    return {
        "null": value is None,
        "boolean": type(value) is bool,
        "integer": type(value) is int,
        "number": type(value) in {int, float} and type(value) is not bool,
        "string": type(value) is str,
        "array": type(value) is list,
        "object": type(value) is dict,
    }[expected]


def _resolve_ref(root: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/$defs/"):
        raise ContractValidationError("only local $defs references are allowed")
    name = reference.removeprefix("#/$defs/")
    try:
        resolved = root["$defs"][name]
    except KeyError as exc:
        raise ContractValidationError(f"unknown schema reference: {reference}") from exc
    return resolved


def _validate(value: Any, schema: dict[str, Any], root: dict[str, Any], path: str = "$") -> None:
    if "$ref" in schema:
        _validate(value, _resolve_ref(root, schema["$ref"]), root, path)
        return
    if "oneOf" in schema:
        matches = 0
        for option in schema["oneOf"]:
            try:
                _validate(value, option, root, path)
            except ContractValidationError:
                continue
            matches += 1
        if matches != 1:
            raise ContractValidationError(f"{path}: expected exactly one schema match")
        return
    if "const" in schema and not _same_json_value(value, schema["const"]):
        raise ContractValidationError(f"{path}: const mismatch")
    if "enum" in schema and not any(
        _same_json_value(value, allowed) for allowed in schema["enum"]
    ):
        raise ContractValidationError(f"{path}: value is outside enum")
    expected = schema.get("type")
    if expected is not None:
        choices = [expected] if isinstance(expected, str) else expected
        if not any(_matches_type(value, choice) for choice in choices):
            raise ContractValidationError(f"{path}: wrong type")
    if type(value) is dict:
        required = set(schema.get("required", []))
        missing = required - value.keys()
        if missing:
            raise ContractValidationError(f"{path}: missing {sorted(missing)}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = value.keys() - properties.keys()
            if extra:
                raise ContractValidationError(f"{path}: extra {sorted(extra)}")
        for key, item in value.items():
            if key in properties:
                _validate(item, properties[key], root, f"{path}.{key}")
    elif type(value) is list:
        if len(value) < schema.get("minItems", 0):
            raise ContractValidationError(f"{path}: too few items")
        if len(value) > schema.get("maxItems", MAX_SAFE_INTEGER):
            raise ContractValidationError(f"{path}: too many items")
        if schema.get("uniqueItems"):
            encoded = [_canonical(item) for item in value]
            if len(encoded) != len(set(encoded)):
                raise ContractValidationError(f"{path}: duplicate items")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                _validate(item, item_schema, root, f"{path}[{index}]")
    elif type(value) is str:
        if len(value) < schema.get("minLength", 0):
            raise ContractValidationError(f"{path}: string is too short")
        if len(value) > schema.get("maxLength", MAX_SAFE_INTEGER):
            raise ContractValidationError(f"{path}: string is too long")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise ContractValidationError(f"{path}: pattern mismatch")
    elif type(value) is int and type(value) is not bool:
        if value < schema.get("minimum", -MAX_SAFE_INTEGER):
            raise ContractValidationError(f"{path}: number is too small")
        if value > schema.get("maximum", MAX_SAFE_INTEGER):
            raise ContractValidationError(f"{path}: number is too large")


def _definition(schema: dict[str, Any], name: str) -> dict[str, Any]:
    return {"$ref": f"#/$defs/{name}"}


def _verify_sealed(value: dict[str, Any], digest_field: str) -> bytes:
    digest = value[digest_field]
    body = dict(value)
    del body[digest_field]
    body_bytes = _canonical(body)
    assert hashlib.sha256(body_bytes).hexdigest() == digest
    sealed = _canonical(value)
    assert _strict_load_bytes(sealed) == value
    return sealed


def _validate_segments(source: bytes, segments: list[dict[str, Any]]) -> None:
    previous_end = -1
    for segment in segments:
        start, end = segment["startByte"], segment["endByte"]
        if not previous_end < start < end <= len(source):
            raise ValueError("segments must be ordered, separated, and in bounds")
        selected = source[start:end]
        if selected.decode("utf-8", errors="strict") != segment["text"]:
            raise ValueError("segment text does not match source bytes")
        if hashlib.sha256(selected).hexdigest() != segment["textDigest"]:
            raise ValueError("segment digest does not match source bytes")
        previous_end = end


def _validate_ruleset_order(rulesets: list[dict[str, Any]]) -> None:
    keys = [(item["name"], item["version"]) for item in rulesets]
    names = [item["name"] for item in rulesets]
    if keys != sorted(keys) or len(names) != len(set(names)):
        raise ContractValidationError("rulesets must have unique names in lexical order")


def _validate_positive_human_evidence(
    snapshot: dict[str, Any],
    decisions: list[dict[str, Any]],
    manual_additions: list[dict[str, Any]],
) -> None:
    purpose_by_id = {
        item["evidenceId"]: item["purpose"] for item in snapshot["evidence"]
    }
    for decision in decisions:
        if decision["action"] == "edit" and not any(
            purpose_by_id.get(evidence_id) == "supporting"
            for evidence_id in decision["evidenceIds"]
        ):
            raise ContractValidationError("edited code requires supporting evidence")
    for addition in manual_additions:
        evidence = addition["evidence"]
        if "evidenceId" in evidence and purpose_by_id.get(evidence["evidenceId"]) != (
            "supporting"
        ):
            raise ContractValidationError("manual code requires supporting evidence")


def test_artifacts_are_strict_json_and_schema_profile_is_closed() -> None:
    json_paths = sorted(CONTRACT.glob("*.json")) + sorted(EXAMPLES.glob("*.json"))
    assert {path.name for path in json_paths} >= {
        "contracts.schema.json",
        "canonicalization-vectors.v1.json",
        "review-snapshot-request.golden.json",
        "review-snapshot.golden.json",
        "prepare-request.golden.json",
        "prepared-packet.golden.json",
    }
    for path in json_paths:
        _load(path)

    schema = _load(SCHEMA_PATH)
    allowed = {
        "$schema", "$id", "$comment", "$defs", "$ref", "title",
        "description", "oneOf", "type", "const", "enum", "pattern",
        "minLength", "maxLength", "minimum", "maximum", "properties",
        "required", "additionalProperties", "items", "minItems", "maxItems",
        "uniqueItems",
    }

    def audit(node: dict[str, Any], path: str) -> None:
        assert set(node) <= allowed, f"unsupported schema keyword at {path}"
        if "$ref" in node:
            _resolve_ref(schema, node["$ref"])
        object_type = node.get("type") == "object" or (
            isinstance(node.get("type"), list) and "object" in node["type"]
        )
        if object_type:
            assert node.get("additionalProperties") is False, path
            assert set(node.get("required", [])) == set(node.get("properties", {})), path
        for container in ("$defs", "properties"):
            for name, child in node.get(container, {}).items():
                audit(child, f"{path}/{container}/{name}")
        if isinstance(node.get("items"), dict):
            audit(node["items"], f"{path}/items")
        for index, child in enumerate(node.get("oneOf", [])):
            audit(child, f"{path}/oneOf/{index}")

    audit(schema, "#")


def test_all_positive_examples_match_their_contracts() -> None:
    schema = _load(SCHEMA_PATH)
    for filename, definition in {
        "review-snapshot-request.golden.json": "reviewSnapshotRequest",
        "review-snapshot.golden.json": "reviewSnapshot",
        "prepare-request.golden.json": "prepareRequest",
        "prepared-packet.golden.json": "preparedPacket",
    }.items():
        value = _load(EXAMPLES / filename)
        _validate(value, _definition(schema, definition), schema)
        _validate(value, schema, schema)


def test_contract_accepts_every_current_runtime_completeness_rule_id() -> None:
    schema = _load(SCHEMA_PATH)
    for rules in RULE_PACKS.values():
        for rule in rules:
            _validate(
                {
                    "ruleId": rule.rule_id,
                    "status": "missing",
                    "evidenceIds": [],
                },
                _definition(schema, "completenessResult"),
                schema,
            )


@pytest.mark.parametrize(
    "rule_id",
    [
        "Telehealth.modality",
        "telehealth..modality",
        "telehealth.",
        ".telehealth",
        "telehealth-modality",
    ],
)
def test_contract_rejects_malformed_completeness_rule_ids(rule_id: str) -> None:
    schema = _load(SCHEMA_PATH)
    with pytest.raises(ContractValidationError):
        _validate(
            {"ruleId": rule_id, "status": "missing", "evidenceIds": []},
            _definition(schema, "completenessResult"),
            schema,
        )


def test_recommendation_requires_a_terminal_guardrail_trace() -> None:
    schema = _load(SCHEMA_PATH)
    snapshot = _load(EXAMPLES / "review-snapshot.golden.json")
    recommendation = deepcopy(snapshot["recommendations"][0])
    recommendation["guardrailActions"] = []

    with pytest.raises(ContractValidationError):
        _validate(
            recommendation,
            _definition(schema, "recommendation"),
            schema,
        )


def test_schema_rejects_missing_extra_wrong_type_and_wrong_enum() -> None:
    schema = _load(SCHEMA_PATH)
    packet = _load(EXAMPLES / "prepared-packet.golden.json")
    mutations = []
    missing = deepcopy(packet)
    del missing["status"]
    mutations.append(missing)
    extra = deepcopy(packet)
    extra["claimSubmitted"] = True
    mutations.append(extra)
    wrong_type = deepcopy(packet)
    wrong_type["packetVersion"] = 1.0
    mutations.append(wrong_type)
    wrong_enum = deepcopy(packet)
    wrong_enum["status"] = "submitted"
    mutations.append(wrong_enum)
    active_multi_engine = deepcopy(packet)
    active_multi_engine["reviewSnapshot"]["engineProvenance"][
        "multiEngineDecisioningApplied"
    ] = True
    mutations.append(active_multi_engine)
    for mutation in mutations:
        with pytest.raises(ContractValidationError):
            _validate(mutation, _definition(schema, "preparedPacket"), schema)


def test_request_variants_are_closed_and_evidence_bearing() -> None:
    schema = _load(SCHEMA_PATH)
    snapshot = _load(EXAMPLES / "review-snapshot.golden.json")
    edit = {
        "recommendationId": "44444444-4444-4444-8444-444444444441",
        "action": "edit",
        "replacementCode": "I11.9",
        "reasonCode": "dx_needs_more_specific_code",
        "rationale": "Synthetic shape-only rationale.",
        "evidenceIds": ["33333333-3333-4333-8333-333333333331"],
    }
    addition = {
        "clientAdditionId": "66666666-6666-4666-8666-666666666666",
        "kind": "MODIFIER",
        "code": "95",
        "reasonCode": "modifier_required",
        "rationale": "Synthetic shape-only rationale.",
        "evidence": {"evidenceId": "33333333-3333-4333-8333-333333333331"},
    }
    _validate(edit, _definition(schema, "decisionRequest"), schema)
    _validate(addition, _definition(schema, "manualAdditionRequest"), schema)
    _validate_positive_human_evidence(snapshot, [edit], [addition])
    invalid_accept = {
        "recommendationId": edit["recommendationId"],
        "action": "accept",
        "reasonCode": "other",
    }
    with pytest.raises(ContractValidationError):
        _validate(invalid_accept, _definition(schema, "decisionRequest"), schema)

    contradicting_id = snapshot["evidence"][1]["evidenceId"]
    invalid_edit_evidence = deepcopy(edit)
    invalid_edit_evidence["evidenceIds"] = [contradicting_id]
    _validate(invalid_edit_evidence, _definition(schema, "decisionRequest"), schema)
    invalid_addition_evidence = deepcopy(addition)
    invalid_addition_evidence["evidence"] = {"evidenceId": contradicting_id}
    _validate(
        invalid_addition_evidence,
        _definition(schema, "manualAdditionRequest"),
        schema,
    )
    with pytest.raises(ContractValidationError):
        _validate_positive_human_evidence(snapshot, [invalid_edit_evidence], [])
    with pytest.raises(ContractValidationError):
        _validate_positive_human_evidence(snapshot, [], [invalid_addition_evidence])


def test_golden_objects_are_cross_bound_and_semantically_safe() -> None:
    snapshot = _load(EXAMPLES / "review-snapshot.golden.json")
    request = _load(EXAMPLES / "prepare-request.golden.json")
    packet = _load(EXAMPLES / "prepared-packet.golden.json")
    assert packet["reviewSnapshot"] == snapshot
    assert request["reviewId"] == snapshot["reviewId"]
    assert request["reviewSnapshotId"] == snapshot["snapshotId"]
    assert request["reviewSnapshotDigest"] == snapshot["snapshotDigest"]
    recommendation_ids = [item["recommendationId"] for item in snapshot["recommendations"]]
    assert len(recommendation_ids) == len(set(recommendation_ids))
    assert [item["ordinal"] for item in snapshot["recommendations"]] == list(
        range(len(recommendation_ids))
    )
    assert [item["recommendationId"] for item in request["decisions"]] == recommendation_ids
    assert [item["recommendationId"] for item in packet["decisions"]] == recommendation_ids
    rulesets = snapshot["engineProvenance"]["rulesets"]
    _validate_ruleset_order(rulesets)
    with pytest.raises(ContractValidationError):
        _validate_ruleset_order(list(reversed(rulesets)))
    duplicate_name = deepcopy(rulesets)
    duplicate_name[-1]["name"] = duplicate_name[0]["name"]
    duplicate_name[-1]["version"] = "different-version"
    with pytest.raises(ContractValidationError):
        _validate_ruleset_order(duplicate_name)
    evidence_id_list = [item["evidenceId"] for item in snapshot["evidence"]]
    assert len(evidence_id_list) == len(set(evidence_id_list))
    evidence_ids = set(evidence_id_list)
    purpose_by_id = {
        item["evidenceId"]: item["purpose"] for item in snapshot["evidence"]
    }
    for recommendation in snapshot["recommendations"]:
        assert set(recommendation["evidenceIds"]) <= evidence_ids
        if recommendation["disposition"] == "rejected":
            assert any(
                evidence["purpose"] == "contradicting"
                for evidence in snapshot["evidence"]
                if evidence["evidenceId"] in recommendation["evidenceIds"]
            )
    assert snapshot["engineProvenance"]["adaptivePreferencesApplied"] is False
    assert snapshot["engineProvenance"]["multiEngineDecisioningApplied"] is False
    assert snapshot["completeness"] == {
        "status": "not_applicable",
        "rulepackVersion": None,
        "providerAttestationApplied": False,
        "results": [],
    }
    assert packet["status"] == "prepared_not_transmitted"
    assert packet["transmission"] == {"performed": False, "destination": None}
    assert packet["clinicalNoteModified"] is False
    assert "replayed" not in packet
    assert packet["packetVersion"] == 1 and packet["priorPacketId"] is None
    recommendation_by_id = {
        item["recommendationId"]: item for item in snapshot["recommendations"]
    }
    for decision in packet["decisions"]:
        original = recommendation_by_id[decision["recommendationId"]]
        assert decision["originalCode"] == original["code"]
        if decision["action"] == "accept":
            assert original["disposition"] == "captured"
            assert decision["finalCode"] == original["code"]
            assert decision["reasonCode"] is None and decision["rationale"] is None
            assert decision["evidenceIds"] == original["evidenceIds"]
        else:
            assert decision["reasonCode"] and decision["rationale"]
            assert decision["evidenceIds"]
    for final_code in packet["finalCodes"]:
        assert any(
            purpose_by_id.get(evidence_id) == "supporting"
            for evidence_id in final_code["evidenceIds"]
        )
    final_pairs = [(item["kind"], item["code"]) for item in packet["finalCodes"]]
    excluded_pairs = [(item["kind"], item["code"]) for item in packet["excludedCodes"]]
    assert len(final_pairs) == len(set(final_pairs))
    assert len(excluded_pairs) == len(set(excluded_pairs))
    assert set(final_pairs).isdisjoint(excluded_pairs)


def test_snapshot_evidence_matches_the_immutable_synthetic_source_bytes() -> None:
    snapshot_request = _load(EXAMPLES / "review-snapshot-request.golden.json")
    pack = _load(
        ROOT / "cosmaneura_billing" / "cosmaneura_billing" / "data"
        / "demo_release_candidate" / "pack.v1.json"
    )
    assert pack["contains_real_patient_data"] is False
    assert pack["contains_deidentified_patient_data"] is False
    selected_case = next(
        case for case in pack["cases"]
        if case["case_id"] == snapshot_request["encounterId"]
    )
    assert selected_case["provenance"]["kind"] == "wholly_synthetic"
    assert selected_case["provenance"]["contains_real_patient_data"] is False
    assert selected_case["provenance"]["contains_deidentified_patient_data"] is False
    visit = selected_case["visit"]
    snapshot = _load(EXAMPLES / "review-snapshot.golden.json")
    packet = _load(EXAMPLES / "prepared-packet.golden.json")
    provenance = snapshot["sourceProvenance"]
    assert provenance["fixturePackId"] == pack["pack_id"]
    assert provenance["fixtureCaseDigest"] == EXPECTED_CASE_DIGESTS[
        selected_case["case_id"]
    ]
    assert provenance["fixtureCombinedSeedDigest"] == EXPECTED_SEED_DIGEST
    source_body = {"encounterVersion": snapshot["encounterVersion"], "visit": visit}
    assert hashlib.sha256(_canonical(source_body)).hexdigest() == (
        provenance["sourceSnapshotDigest"]
    )
    sources = {
        "intake_text": visit["intake_text"].encode("utf-8"),
        "transcript": visit["transcript"].encode("utf-8"),
        "signed_note": visit["signed_note"].encode("utf-8"),
    }
    total_evidence_bytes = 0
    for evidence in snapshot["evidence"]:
        source = sources[evidence["source"]]
        previous_end = -1
        for segment in evidence["segments"]:
            start, end = segment["startByte"], segment["endByte"]
            assert previous_end < start < end
            selected_bytes = source[start:end]
            assert end - start == len(selected_bytes) <= 8192
            assert selected_bytes.decode("utf-8", errors="strict") == segment["text"]
            assert hashlib.sha256(selected_bytes).hexdigest() == segment["textDigest"]
            total_evidence_bytes += len(selected_bytes)
            previous_end = end
    assert total_evidence_bytes <= 65536

    evidence_by_id = {
        item["evidenceId"]: item for item in snapshot["evidence"]
    }
    recommendation_list = snapshot["recommendations"]
    recommendations = {
        (item["kind"], item["code"]): item for item in recommendation_list
    }
    assert len(recommendation_list) == len(recommendations)
    expected_captured = {
        ("ICD10", item["code"]): item for item in selected_case["expectations"]["must_capture"]
    }
    expected_rejected = {
        ("ICD10", item["code"]): item for item in selected_case["expectations"]["must_not_capture"]
    }
    assert set(recommendations) == set(expected_captured) | set(expected_rejected)
    assert {
        key for key, item in recommendations.items() if item["disposition"] == "captured"
    } == set(expected_captured)
    assert {
        key for key, item in recommendations.items() if item["disposition"] == "rejected"
    } == set(expected_rejected)
    for key, expectation in {**expected_captured, **expected_rejected}.items():
        recommendation = recommendations[key]
        expected_quote = expectation.get("evidence", {}).get("quote") or expectation["quote"]
        expected_purpose = (
            "supporting" if recommendation["disposition"] == "captured" else "contradicting"
        )
        assert any(
            evidence_by_id[evidence_id]["purpose"] == expected_purpose
            and any(
                segment["text"] == expected_quote
                for segment in evidence_by_id[evidence_id]["segments"]
            )
            for evidence_id in recommendation["evidenceIds"]
        )
    assert {(item["kind"], item["code"]) for item in packet["finalCodes"]} == set(
        expected_captured
    )
    assert {(item["kind"], item["code"]) for item in packet["excludedCodes"]} == set(
        expected_rejected
    )


def test_snapshot_and_packet_root_digests_are_pinned() -> None:
    snapshot = _load(EXAMPLES / "review-snapshot.golden.json")
    packet = _load(EXAMPLES / "prepared-packet.golden.json")
    assert len(_verify_sealed(snapshot, "snapshotDigest")) <= 256 * 1024
    assert len(_verify_sealed(packet, "packetDigest")) <= 512 * 1024


def test_entire_request_hashes_are_pinned_for_exact_retry() -> None:
    vectors = _load(VECTORS_PATH)["requestDigests"]
    snapshot_request = _load(EXAMPLES / "review-snapshot-request.golden.json")
    prepare_request = _load(EXAMPLES / "prepare-request.golden.json")
    assert hashlib.sha256(_canonical(snapshot_request)).hexdigest() == (
        vectors["reviewSnapshotRequestSha256"]
    )
    assert hashlib.sha256(_canonical(prepare_request)).hexdigest() == (
        vectors["prepareRequestSha256"]
    )


def test_language_neutral_canonicalization_vectors_are_literal() -> None:
    vectors = _load(VECTORS_PATH)
    for vector in vectors["positive"]:
        expected = base64.b64decode(vector["canonicalUtf8Base64"], validate=True)
        assert _canonical(vector["value"]) == expected
        assert hashlib.sha256(expected).hexdigest() == vector["sha256"]
    assert vectors["positive"][1]["sha256"] != vectors["positive"][2]["sha256"]
    assert vectors["positive"][4]["sha256"] != vectors["positive"][5]["sha256"]

    exclusion = vectors["digestExclusion"]
    body = dict(exclusion["sealedValue"])
    del body[exclusion["digestField"]]
    expected_body = base64.b64decode(exclusion["digestInputUtf8Base64"], validate=True)
    assert _canonical(body) == expected_body
    assert hashlib.sha256(expected_body).hexdigest() == exclusion["sha256"]
    assert _canonical(exclusion["sealedValue"]) == base64.b64decode(
        exclusion["sealedCanonicalUtf8Base64"], validate=True,
    )


def test_invalid_canonical_inputs_fail_closed() -> None:
    vectors = _load(VECTORS_PATH)
    for vector in vectors["invalidJsonUtf8Base64"]:
        raw = base64.b64decode(vector["value"], validate=True)
        with pytest.raises((UnicodeError, ValueError)):
            _canonical(_strict_load_bytes(raw))


def test_unicode_and_reanchoring_vectors_pin_exact_bytes() -> None:
    vectors = _load(VECTORS_PATH)
    source = base64.b64decode(vectors["evidenceUtf8"]["sourceUtf8Base64"], validate=True)
    segments = vectors["evidenceUtf8"]["segments"]
    _validate_segments(source, segments)
    for case in vectors["evidenceAnchoring"]:
        raw = case["source"].encode("utf-8")
        if case["expectedStatus"] != "anchored":
            assert case["segments"] == []
            continue
        _validate_segments(raw, case["segments"])

    invalid = []
    overlap = deepcopy(segments[:2])
    overlap[1]["startByte"] = 4
    invalid.append(overlap)
    adjacent = deepcopy(segments[:2])
    adjacent[1]["startByte"] = adjacent[0]["endByte"]
    invalid.append(adjacent)
    reversed_segment = deepcopy(segments[:1])
    reversed_segment[0]["endByte"] = reversed_segment[0]["startByte"]
    invalid.append(reversed_segment)
    mid_code_point = deepcopy(segments[:1])
    mid_code_point[0].update(startByte=4, endByte=5, text="é")
    invalid.append(mid_code_point)
    out_of_range = deepcopy(segments[:1])
    out_of_range[0]["endByte"] = len(source) + 1
    invalid.append(out_of_range)
    wrong_digest = deepcopy(segments[:1])
    wrong_digest[0]["textDigest"] = "0" * 64
    invalid.append(wrong_digest)
    for invalid_segments in invalid:
        with pytest.raises((UnicodeError, ValueError)):
            _validate_segments(source, invalid_segments)


def test_fixture_manifest_values_and_protocol_separation_are_pinned() -> None:
    vectors = _load(VECTORS_PATH)
    expected = vectors["fixtureManifestExpected"]
    assert expected["basePackDigest"] == EXPECTED_PACK_DIGEST
    assert expected["extensionPackDigest"] == EXPECTED_EXTENSION_PACK_DIGEST
    assert expected["combinedSeedDigest"] == EXPECTED_SEED_DIGEST
    code_cases = {**EXPECTED_CASE_DIGESTS, **EXPECTED_EXTENSION_CASE_DIGESTS}
    assert {item["caseId"]: item["sha256"] for item in expected["caseDigests"]} == code_cases
    assert prepare_synthetic_demo_seed().digest == EXPECTED_SEED_DIGEST

    separation = vectors["fixtureProtocolSeparation"]
    fixture_bytes = json.dumps(
        separation["value"], sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    assert hashlib.sha256(fixture_bytes).hexdigest() == separation["fixtureManifestV1Sha256"]
    assert hashlib.sha256(_canonical(separation["value"])).hexdigest() == (
        separation["packetCanonicalJsonV1Sha256"]
    )
    assert separation["fixtureManifestV1Sha256"] != separation["packetCanonicalJsonV1Sha256"]
