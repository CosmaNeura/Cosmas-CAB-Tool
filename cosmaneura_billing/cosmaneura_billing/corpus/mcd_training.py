"""Fixed, source-only fictional visits for MCD staff training.

This is not a generic patient import or a model-training/gold-label dataset.
The artifact hashes admit only the reviewed twelve visits. Their fictional
dates and completion markers are preserved, not treated as clinician approval.
"""
from __future__ import annotations

from dataclasses import fields
import hashlib
import json
from pathlib import Path
from typing import Any

from ..schemas import VisitBundle

PACK_ID = "cosmaneura-mcd-training-20260930-v1"
CASE_IDS = tuple(f"SYN-CAB-20260930-{number:02d}" for number in range(1, 13))
FERTILITY_CASE_IDS = CASE_IDS[9:]
INPUTS_SHA256 = "a2d334c64b8b0bf6607c3ac70539d105d482329c590d018bfebf5975614e6afc"
METADATA_SHA256 = "82dc266067c63123a5a384dc5cc45c9506d1ce05f0fd34c06c4886e7131f00f6"
_MANIFEST_SHA256 = "4d8408bf84a5d7b0a0ee2f7395084718a63b4cba701d0ff4db625d84149a504f"
_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "mcd_training"
_VISIT_KEYS = {
    "encounter_id", "patient_age", "patient_sex", "encounter_type",
    "place_of_service", "provider_specialty", "intake_text", "transcript",
    "signed_note", "submitted_em_code", "submitted_icd10",
}
_METADATA_KEYS = {
    "schema_version", "pack_id", "allowed_use", "source_only",
    "contains_real_patient_data", "contains_deidentified_patient_data",
    "inputs_sha256", "original_manifest_sha256", "source_dataset", "source_url",
    "narrative_origin", "completion_status", "fertility_case_ids", "licensing",
}


class McdTrainingPackError(ValueError):
    """The fixed training artifact is missing, changed or malformed."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise McdTrainingPackError(message)


def _read_json(name: str, expected_sha256: str, maximum: int) -> Any:
    try:
        with (_DATA_DIR / name).open("rb") as handle:
            raw = handle.read(maximum + 1)
        _require(len(raw) <= maximum, "training artifact exceeds its bound")
        _require(hashlib.sha256(raw).hexdigest() == expected_sha256,
                 "training artifact digest mismatch")
        return json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise McdTrainingPackError("training artifact unavailable or malformed") from exc


def _validate_metadata(metadata: Any) -> None:
    _require(type(metadata) is dict and set(metadata) == _METADATA_KEYS,
             "training metadata schema mismatch")
    _require(metadata["schema_version"] == "cab.mcd-training-source.v1"
             and metadata["pack_id"] == PACK_ID
             and metadata["allowed_use"] == "synthetic_staff_training"
             and metadata["source_only"] is True
             and metadata["contains_real_patient_data"] is False
             and metadata["contains_deidentified_patient_data"] is False,
             "training source declaration mismatch")
    _require(metadata["inputs_sha256"] == INPUTS_SHA256
             and metadata["original_manifest_sha256"] == _MANIFEST_SHA256
             and metadata["fertility_case_ids"] == list(FERTILITY_CASE_IDS),
             "training provenance mismatch")
    for key in ("source_dataset", "source_url", "narrative_origin", "completion_status", "licensing"):
        _require(type(metadata[key]) is str and 0 < len(metadata[key]) <= 1000,
                 "training provenance text malformed")


def _validate_inputs(visits: Any) -> None:
    _require(type(visits) is list and len(visits) == len(CASE_IDS),
             "training visit count mismatch")
    _require({field.name for field in fields(VisitBundle)} == _VISIT_KEYS,
             "training VisitBundle schema changed")
    for case_id, visit in zip(CASE_IDS, visits):
        _require(type(visit) is dict and set(visit) == _VISIT_KEYS,
                 "training visit schema mismatch")
        _require(visit["encounter_id"] == case_id, "training identities or order changed")
        _require(type(visit["patient_age"]) is int and 0 <= visit["patient_age"] <= 89,
                 "training age malformed")
        _require(visit["patient_sex"] in ("F", "M")
                 and visit["encounter_type"] in ("office_new", "office_established", "telehealth_established")
                 and visit["place_of_service"] in ("10", "11"),
                 "training visit context malformed")
        for key in ("provider_specialty", "intake_text", "transcript", "signed_note"):
            value = visit[key]
            _require(type(value) is str and len(value) <= 20000,
                     "training source text malformed")
            _require(not any((ord(c) < 32 and c not in "\n\t") or ord(c) == 127
                             or 0xD800 <= ord(c) <= 0xDFFF for c in value),
                     "training source text contains invalid controls")
        _require(bool(visit["provider_specialty"].strip())
                 and visit["signed_note"].startswith(f"SYNTHETIC TEST NOTE — {case_id}\n"),
                 "training fictional source marker missing")
        _require(visit["submitted_em_code"] is None
                 and type(visit["submitted_icd10"]) is list and visit["submitted_icd10"] == [],
                 "training inputs must not contain submitted code targets")


def load_mcd_training_pack() -> dict[str, Any]:
    """Return fresh original visit dictionaries after fixed-byte validation.

    No caller path or payload is accepted. Loading neither persists visits nor
    grants access or inference authority; the protected seed workflow does that.
    """
    metadata = _read_json("metadata.json", METADATA_SHA256, 8192)
    _validate_metadata(metadata)
    visits = _read_json("encounters.json", INPUTS_SHA256, 65536)
    _validate_inputs(visits)
    return {"pack_id": PACK_ID, "cases": [
        {"case_id": case_id, "visit": visit} for case_id, visit in zip(CASE_IDS, visits)
    ]}
