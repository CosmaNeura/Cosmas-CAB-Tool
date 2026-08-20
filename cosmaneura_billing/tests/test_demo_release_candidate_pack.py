from copy import deepcopy
from pathlib import Path

import pytest

from cosmaneura_billing.corpus.demo_release_candidate import (
    DemoPackValidationError,
    load_demo_release_candidate,
    validate_demo_release_candidate,
)


@pytest.fixture(scope="module")
def pack():
    return load_demo_release_candidate()


def test_pack_is_bounded_and_wholly_synthetic(pack):
    assert 3 <= len(pack["cases"]) <= 5
    assert pack["allowed_use"] == "demo_and_evaluation_only"
    assert pack["training_use"] == "prohibited"
    assert pack["contains_real_patient_data"] is False
    assert pack["contains_deidentified_patient_data"] is False
    for case in pack["cases"]:
        assert case["case_id"].startswith("SYN-RC-")
        assert case["visit"]["encounter_id"] == case["case_id"]
        assert case["provenance"] == {
            "kind": "wholly_synthetic",
            "source_dataset": None,
            "contains_real_patient_data": False,
            "contains_deidentified_patient_data": False,
        }


def test_pack_contains_no_cpt_expectations_or_proprietary_descriptor_fields(pack):
    forbidden_fields = {"label", "descriptor", "long_descriptor", "cpt_text"}
    for case in pack["cases"]:
        visit = case["visit"]
        assert visit["submitted_em_code"] is None
        for expectation_kind in ("must_capture", "must_not_capture"):
            for expected in case["expectations"][expectation_kind]:
                assert expected["kind"] == "icd10_diagnosis"
                assert forbidden_fields.isdisjoint(expected)


def test_unresolved_mts_dialog_is_excluded_from_production_package_data():
    project_root = Path(__file__).resolve().parents[1]
    pyproject = (project_root / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    dockerignore = (project_root / ".dockerignore").read_text(encoding="utf-8")
    assert '"data/corpora/*.csv"' not in pyproject
    assert '"data/corpora/mts_dialog_train.csv"' not in pyproject
    assert '"data/corpora/acibench_train.csv"' in pyproject
    assert '"data/corpora/acibench_valid.csv"' in pyproject
    assert (
        "cosmaneura_billing/data/corpora/mts_dialog_train.csv"
        in dockerignore.splitlines()
    )


def test_every_capture_is_a_billable_code_with_explicit_signed_note_support(pack):
    # The production validator checks the code against the bundled current-FY
    # billable set, the exact quote against the signed note, and the code token
    # against that quote.  A clean return proves all three for the tracked pack.
    validate_demo_release_candidate(pack)
    assert sum(len(case["expectations"]["must_capture"]) for case in pack["cases"]) == 3


def test_validator_rejects_a_direct_identifier(pack):
    changed = deepcopy(pack)
    changed["cases"][0]["visit"]["intake_text"] += " Contact demo.patient@example.test."
    with pytest.raises(DemoPackValidationError, match="prohibited email address"):
        validate_demo_release_candidate(changed)


def test_validator_rejects_an_unexpected_identity_field(pack):
    changed = deepcopy(pack)
    changed["cases"][0]["visit"]["patient_name"] = "Synthetic Person"
    with pytest.raises(DemoPackValidationError, match="unexpected keys.*patient_name"):
        validate_demo_release_candidate(changed)


def test_validator_rejects_a_non_synthetic_identifier(pack):
    changed = deepcopy(pack)
    changed["cases"][0]["case_id"] = "patient-1001"
    changed["cases"][0]["visit"]["encounter_id"] = "patient-1001"
    with pytest.raises(DemoPackValidationError, match="expected SYN-RC-NNNN"):
        validate_demo_release_candidate(changed)


def test_validator_rejects_identifier_fields_outside_the_schema(pack):
    changed = deepcopy(pack)
    changed["cases"][0]["visit"]["patient_name"] = "Synthetic Person"
    with pytest.raises(DemoPackValidationError, match="unknown keys.*patient_name"):
        validate_demo_release_candidate(changed)


def test_validator_rejects_unanchored_capture_evidence(pack):
    changed = deepcopy(pack)
    changed["cases"][0]["expectations"]["must_capture"][0]["evidence"]["quote"] = (
        "Assessment: Essential hypertension [I10], but this sentence is absent."
    )
    with pytest.raises(DemoPackValidationError, match="not verbatim in the signed note"):
        validate_demo_release_candidate(changed)


def test_validator_rejects_negated_capture_evidence(pack):
    changed = deepcopy(pack)
    capture = changed["cases"][0]["expectations"]["must_capture"][0]
    original = capture["evidence"]["quote"]
    negated = "Assessment: No essential hypertension [I10] is diagnosed."
    changed["cases"][0]["visit"]["signed_note"] = changed["cases"][0]["visit"][
        "signed_note"
    ].replace(original, negated)
    capture["evidence"]["quote"] = negated

    with pytest.raises(DemoPackValidationError, match="affirmative Assessment"):
        validate_demo_release_candidate(changed)


@pytest.mark.parametrize("date_value", ["2026-08-19", "August 19, 2026"])
def test_validator_rejects_additional_calendar_date_shapes(pack, date_value):
    changed = deepcopy(pack)
    changed["cases"][0]["visit"]["encounter_type"] = date_value
    with pytest.raises(DemoPackValidationError, match="calendar date"):
        validate_demo_release_candidate(changed)


def test_validator_rejects_a_nonbillable_capture_code(pack):
    changed = deepcopy(pack)
    capture = changed["cases"][0]["expectations"]["must_capture"][0]
    capture["code"] = "A00"
    capture["evidence"]["quote"] = "Assessment: Essential hypertension [A00], controlled on current therapy."
    changed["cases"][0]["visit"]["signed_note"] = changed["cases"][0]["visit"]["signed_note"].replace(
        "Assessment: Essential hypertension [I10], controlled on current therapy.",
        capture["evidence"]["quote"],
    )
    with pytest.raises(DemoPackValidationError, match="not billable"):
        validate_demo_release_candidate(changed)
