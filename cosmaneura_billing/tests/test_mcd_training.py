"""Fixed source fidelity, closed input contract and packaged artifact checks."""
from dataclasses import asdict
import hashlib
import json

import pytest

from cosmaneura_billing.corpus import mcd_training as training
from cosmaneura_billing.schemas import VisitBundle


def test_exact_original_inputs_and_fertility_membership():
    raw = (training._DATA_DIR / "encounters.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "a2d334c64b8b0bf6607c3ac70539d105d482329c590d018bfebf5975614e6afc"
    original = json.loads(raw)
    pack = training.load_mcd_training_pack()
    assert set(pack) == {"pack_id", "cases"}
    assert pack["pack_id"] == "cosmaneura-mcd-training-20260930-v1"
    assert tuple(case["case_id"] for case in pack["cases"]) == training.CASE_IDS
    assert len(set(training.CASE_IDS)) == 12
    assert training.FERTILITY_CASE_IDS == (
        "SYN-CAB-20260930-10", "SYN-CAB-20260930-11", "SYN-CAB-20260930-12",
    )
    for case, source in zip(pack["cases"], original):
        assert set(case) == {"case_id", "visit"}
        assert case["visit"] == source == asdict(VisitBundle(**case["visit"]))
        assert case["visit"]["signed_note"].encode("utf-8") == source["signed_note"].encode("utf-8")
        assert source["submitted_em_code"] is None and source["submitted_icd10"] == []


def test_no_caller_file_and_no_shared_mutable_return():
    with pytest.raises(TypeError):
        training.load_mcd_training_pack("arbitrary.json")
    first = training.load_mcd_training_pack()
    first["cases"][0]["visit"]["signed_note"] = "changed"
    first["cases"][0]["visit"]["submitted_icd10"].append("R69")
    current = training.load_mcd_training_pack()["cases"][0]["visit"]
    assert current["signed_note"].startswith("SYNTHETIC TEST NOTE")
    assert current["submitted_icd10"] == []


@pytest.mark.parametrize("name", ["encounters.json", "metadata.json"])
def test_any_packaged_byte_change_is_rejected(monkeypatch, tmp_path, name):
    for filename in ("encounters.json", "metadata.json"):
        (tmp_path / filename).write_bytes((training._DATA_DIR / filename).read_bytes())
    with (tmp_path / name).open("ab") as handle:
        handle.write(b" ")  # Even semantically identical JSON is not the reviewed artifact.
    monkeypatch.setattr(training, "_DATA_DIR", tmp_path)
    with pytest.raises(training.McdTrainingPackError, match="digest mismatch"):
        training.load_mcd_training_pack()


def test_missing_and_oversize_artifacts_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(training, "_DATA_DIR", tmp_path)
    with pytest.raises(training.McdTrainingPackError, match="unavailable"):
        training.load_mcd_training_pack()
    (tmp_path / "metadata.json").write_bytes(b" " * 8193)
    with pytest.raises(training.McdTrainingPackError, match="exceeds"):
        training.load_mcd_training_pack()


@pytest.mark.parametrize("field,value", [
    ("expected_codes", []), ("provenance", {"source_patient": "not-allowed"}),
    ("submitted_em_code", "99214"), ("submitted_icd10", ["R69"]),
    ("patient_age", True), ("patient_sex", "unknown"),
    ("signed_note", "SYNTHETIC TEST NOTE — SYN-CAB-20260930-01\n\x00"),
])
def test_closed_visit_schema_rejects_targets_mappings_and_invalid_types(field, value):
    visits = json.loads((training._DATA_DIR / "encounters.json").read_bytes())
    visits[0][field] = value
    with pytest.raises(training.McdTrainingPackError):
        training._validate_inputs(visits)


def test_count_duplicate_and_order_changes_are_rejected():
    visits = json.loads((training._DATA_DIR / "encounters.json").read_bytes())
    for changed in (visits[:-1], [visits[0]] * 12, list(reversed(visits))):
        with pytest.raises(training.McdTrainingPackError):
            training._validate_inputs(changed)


@pytest.mark.parametrize("field,value", [
    ("source_only", False), ("contains_real_patient_data", True),
    ("contains_deidentified_patient_data", True), ("fertility_case_ids", []),
    ("inputs_sha256", "0" * 64), ("review_scenarios", []),
])
def test_closed_metadata_rejects_unreviewed_provenance(field, value):
    metadata = json.loads((training._DATA_DIR / "metadata.json").read_bytes())
    metadata[field] = value
    with pytest.raises(training.McdTrainingPackError):
        training._validate_metadata(metadata)
