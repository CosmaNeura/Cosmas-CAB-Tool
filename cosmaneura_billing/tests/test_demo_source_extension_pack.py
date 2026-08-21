"""Release controls for the source-only synthetic demo extension."""
from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path

import pytest

from cosmaneura_billing.corpus.demo_source_extension import (
    CASE_IDS,
    DemoSourceExtensionValidationError,
    load_demo_source_extension,
    validate_demo_source_extension,
)


ROOT = Path(__file__).resolve().parents[1]
BASE_PACK = ROOT / "cosmaneura_billing" / "data" / "demo_release_candidate" / "pack.v1.json"
EXTENSION_PACK = ROOT / "cosmaneura_billing" / "data" / "demo_source_extension" / "pack.v1.json"


@pytest.fixture(scope="module")
def pack():
    return load_demo_source_extension()


def test_original_release_candidate_is_byte_for_byte_unchanged():
    content = BASE_PACK.read_bytes()
    assert len(content) == 8065
    assert hashlib.sha256(content).hexdigest() == (
        "0f1f178d0598f50d25d25bde19d9080480f46837025f5ac0d2007289199080b1"
    )


def test_extension_is_exactly_eight_wholly_synthetic_source_only_cases(pack):
    assert pack["pack_id"] == "cosmaneura-demo-source-extension-v1"
    assert pack["extends_pack_id"] == "cosmaneura-demo-rc-v1"
    assert pack["source_only"] is True
    assert tuple(case["case_id"] for case in pack["cases"]) == CASE_IDS
    assert len(pack["cases"]) == 8
    for case in pack["cases"]:
        assert set(case) == {"case_id", "provenance", "visit"}
        assert case["provenance"] == {
            "kind": "wholly_synthetic",
            "source_dataset": None,
            "contains_real_patient_data": False,
            "contains_deidentified_patient_data": False,
        }
        assert case["visit"]["submitted_em_code"] is None
        assert case["visit"]["submitted_icd10"] == []


def test_visit_mix_and_specialty_coverage_are_fixed(pack):
    visits = [case["visit"] for case in pack["cases"]]
    assert [visit["encounter_type"] for visit in visits].count("office_established") == 4
    assert [visit["encounter_type"] for visit in visits].count("office_new") == 2
    assert [visit["encounter_type"] for visit in visits].count("telehealth_established") == 2
    assert sum(visit["place_of_service"] == "10" for visit in visits) == 2
    assert len({visit["provider_specialty"] for visit in visits}) == 8


def test_source_cases_cover_the_eight_bounded_reasoning_shapes(pack):
    by_id = {case["case_id"]: case["visit"] for case in pack["cases"]}
    assert "Essential hypertension is stable" in by_id["SYN-RC-0005"]["signed_note"]
    assert "Mixed hyperlipidemia is stable" in by_id["SYN-RC-0005"]["signed_note"]
    assert sum(
        "hypothyroidism" in by_id["SYN-RC-0006"][field].casefold()
        for field in ("intake_text", "transcript", "signed_note")
    ) == 3
    assert "asthma is active" in by_id["SYN-RC-0007"]["signed_note"]
    assert "pneumonia is resolved" in by_id["SYN-RC-0007"]["signed_note"]
    assert "Nausea is confirmed" in by_id["SYN-RC-0008"]["signed_note"]
    assert "possible explanation but is not diagnosed" in by_id["SYN-RC-0008"]["signed_note"]
    assert "migraine without aura" in by_id["SYN-RC-0009"]["transcript"]
    assert "migraine" not in by_id["SYN-RC-0009"]["signed_note"].casefold()
    assert "hydroxychloroquine" in by_id["SYN-RC-0010"]["transcript"]
    assert "No rheumatologic condition is established" in by_id["SYN-RC-0010"]["signed_note"]
    assert "laterality not established" in by_id["SYN-RC-0011"]["signed_note"]
    # Normal screening / no active disease-or-symptom capture. This does not
    # assert zero total codes; encounter/status/screening codes are separately
    # adjudicated and reported.
    assert "screening visit" in by_id["SYN-RC-0012"]["signed_note"]
    assert "No skin lesion or dermatologic disorder is diagnosed" in by_id["SYN-RC-0012"]["signed_note"]


def test_artifact_has_no_answer_bearing_structure_or_text(pack):
    forbidden_keys = {
        "answer", "answers", "code", "codes", "expected", "expectation",
        "expectations", "gold", "label", "labels", "must_capture",
        "must_not_capture", "output", "outputs", "target", "targets",
    }

    def walk(value):
        if isinstance(value, dict):
            assert forbidden_keys.isdisjoint(key.casefold() for key in value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(pack)
    validate_demo_source_extension(pack)


@pytest.mark.parametrize(
    ("unsafe_text", "violation"),
    [
        ("See https://patient.example.test/chart", "web address"),
        ("See patient.example.health for details", "web address"),
        ("Host 192.0.2.44 responded", "IPv4 address"),
        ("Host 2001:db8:1:2:3:4:5:6 responded", "IPv6 address"),
        ("Host 2001:db8::1 responded", "IPv6 address"),
        ("Mailing area 02139", "US ZIP code"),
        ("Lives at 123 Example Street", "street address"),
        ("Reference ABCDEFGH123456", "long alphanumeric identifier"),
        ("Reference 123456789", "long numeric identifier"),
        ("Hidden\u202etext", "Unicode control or bidirectional character"),
        ("Hidden\u0000text", "Unicode control or bidirectional character"),
        ("Use diagnosis token Z99.9", "diagnosis code token"),
        ("Use lowercase diagnosis token z99.9", "diagnosis code token"),
        ("Use lowercase diagnosis token i10", "diagnosis code token"),
        ("Use alphanumeric diagnosis token C7A.00", "diagnosis code token"),
        ("Use alphanumeric diagnosis token M1A.9XX0", "diagnosis code token"),
        ("Use alphanumeric diagnosis token O9A.211", "diagnosis code token"),
        ("Use compact diagnosis token E782", "diagnosis code token"),
        ("Use compact diagnosis token M1A9XX0", "diagnosis code token"),
        ("Use compact diagnosis token O9A211", "diagnosis code token"),
        ("Conservative screening also rejects A1C", "diagnosis code token"),
        ("Conservative screening also rejects H1N1", "diagnosis code token"),
        ("Use procedure token J1234", "procedure code token"),
        ("Expected output is supplied", "answer-bearing coding language"),
    ],
)
def test_validator_rejects_identifier_code_and_control_shapes(
    pack, unsafe_text, violation,
):
    changed = deepcopy(pack)
    changed["cases"][0]["visit"]["intake_text"] = unsafe_text
    with pytest.raises(DemoSourceExtensionValidationError, match=violation):
        validate_demo_source_extension(changed)


def test_validator_rejects_an_embedded_evaluator_field(pack):
    changed = deepcopy(pack)
    changed["cases"][0]["expected"] = {"diagnosis": "hidden answer"}
    with pytest.raises(
        DemoSourceExtensionValidationError,
        match="forbidden answer-bearing key",
    ):
        validate_demo_source_extension(changed)


def test_extension_is_packaged_but_blinded_workflow_stays_out_of_image():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    workflow = ROOT / "docs" / "SYNTHETIC_EXTENSION_EVALUATION.md"

    assert '"data/demo_source_extension/*.json"' in pyproject
    assert "docs/SYNTHETIC_EXTENSION_EVALUATION.md" not in pyproject
    assert EXTENSION_PACK.is_file()
    assert workflow.is_file()
    assert "COPY cosmaneura_billing/ ./cosmaneura_billing/" in dockerfile
    assert "COPY docs/" not in dockerfile
    assert "tests" in dockerignore.splitlines()
