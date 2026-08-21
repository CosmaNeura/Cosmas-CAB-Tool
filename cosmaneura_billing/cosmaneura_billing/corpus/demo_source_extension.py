"""Load and validate the code-free synthetic demo source extension.

This artifact contains source encounters only. It deliberately carries no
expected codes, adjudication labels, or model outputs; those belong in a
separate blinded review process outside the installed package and image.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .synthetic_safety import prohibited_text_shapes


_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_PACK_PATH = _DATA_DIR / "demo_source_extension" / "pack.v1.json"

PACK_ID = "cosmaneura-demo-source-extension-v1"
EXTENDS_PACK_ID = "cosmaneura-demo-rc-v1"
CASE_IDS = tuple(f"SYN-RC-{number:04d}" for number in range(5, 13))

_CASE_ID = re.compile(r"^SYN-RC-[0-9]{4}$")
_POS = re.compile(r"^[0-9]{2}$")
_REQUIRED_TOP_LEVEL = {
    "schema_version",
    "pack_id",
    "extends_pack_id",
    "allowed_use",
    "training_use",
    "synthetic_declaration",
    "contains_real_patient_data",
    "contains_deidentified_patient_data",
    "source_only",
    "cases",
}
_REQUIRED_CASE = {"case_id", "provenance", "visit"}
_REQUIRED_PROVENANCE = {
    "kind",
    "source_dataset",
    "contains_real_patient_data",
    "contains_deidentified_patient_data",
}
_REQUIRED_VISIT = {
    "encounter_id",
    "patient_age",
    "patient_sex",
    "encounter_type",
    "place_of_service",
    "provider_specialty",
    "intake_text",
    "transcript",
    "signed_note",
    "submitted_em_code",
    "submitted_icd10",
}
_TEXT_FIELDS = (
    "encounter_type",
    "provider_specialty",
    "intake_text",
    "transcript",
    "signed_note",
)
_FORBIDDEN_KEYS = {
    "answer",
    "answers",
    "code",
    "codes",
    "expected",
    "expectation",
    "expectations",
    "gold",
    "label",
    "labels",
    "must_capture",
    "must_not_capture",
    "output",
    "outputs",
    "target",
    "targets",
}


class DemoSourceExtensionValidationError(ValueError):
    """The source-only extension violates one or more release constraints."""


def _require_exact_keys(
    value: dict[str, Any],
    required: set[str],
    location: str,
    errors: list[str],
) -> None:
    missing = sorted(required - set(value))
    if missing:
        errors.append(f"{location}: missing keys {missing}")
    unexpected = sorted(set(value) - required)
    if unexpected:
        errors.append(f"{location}: unexpected keys {unexpected}")


def _reject_forbidden_keys(value: Any, location: str, errors: list[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key.casefold() in _FORBIDDEN_KEYS:
                errors.append(f"{location}: contains forbidden answer-bearing key")
            _reject_forbidden_keys(child, f"{location}.{key}", errors)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden_keys(child, f"{location}[{index}]", errors)


def validate_demo_source_extension(pack: Any) -> None:
    """Raise if *pack* is not a bounded source-only synthetic artifact."""
    errors: list[str] = []
    if not isinstance(pack, dict):
        raise DemoSourceExtensionValidationError("pack: expected a JSON object")

    _require_exact_keys(pack, _REQUIRED_TOP_LEVEL, "pack", errors)
    _reject_forbidden_keys(pack, "pack", errors)
    if pack.get("schema_version") != "1.0":
        errors.append("pack.schema_version: expected '1.0'")
    if pack.get("pack_id") != PACK_ID:
        errors.append("pack.pack_id: unexpected pack identifier")
    if pack.get("extends_pack_id") != EXTENDS_PACK_ID:
        errors.append("pack.extends_pack_id: unexpected base pack identifier")
    if pack.get("allowed_use") != "demo_and_evaluation_only":
        errors.append("pack.allowed_use: must be 'demo_and_evaluation_only'")
    if pack.get("training_use") != "prohibited":
        errors.append("pack.training_use: must be 'prohibited'")
    if pack.get("contains_real_patient_data") is not False:
        errors.append("pack.contains_real_patient_data: must be false")
    if pack.get("contains_deidentified_patient_data") is not False:
        errors.append("pack.contains_deidentified_patient_data: must be false")
    if pack.get("source_only") is not True:
        errors.append("pack.source_only: must be true")
    declaration = pack.get("synthetic_declaration")
    if not isinstance(declaration, str) or "wholly synthetic" not in declaration.lower():
        errors.append("pack.synthetic_declaration: must explicitly say 'wholly synthetic'")

    cases = pack.get("cases")
    if not isinstance(cases, list) or len(cases) != 8:
        errors.append("pack.cases: expected exactly 8 cases")
        cases = []

    observed_ids: list[str] = []
    for index, case in enumerate(cases):
        location = f"pack.cases[{index}]"
        if not isinstance(case, dict):
            errors.append(f"{location}: expected an object")
            continue
        _require_exact_keys(case, _REQUIRED_CASE, location, errors)

        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id):
            errors.append(f"{location}.case_id: expected SYN-RC-NNNN")
        else:
            observed_ids.append(case_id)

        provenance = case.get("provenance")
        if not isinstance(provenance, dict):
            errors.append(f"{location}.provenance: expected an object")
        else:
            _require_exact_keys(
                provenance,
                _REQUIRED_PROVENANCE,
                f"{location}.provenance",
                errors,
            )
            if provenance.get("kind") != "wholly_synthetic":
                errors.append(f"{location}.provenance.kind: must be wholly_synthetic")
            if provenance.get("source_dataset") is not None:
                errors.append(f"{location}.provenance.source_dataset: must be null")
            if provenance.get("contains_real_patient_data") is not False:
                errors.append(
                    f"{location}.provenance.contains_real_patient_data: must be false"
                )
            if provenance.get("contains_deidentified_patient_data") is not False:
                errors.append(
                    f"{location}.provenance.contains_deidentified_patient_data: must be false"
                )

        visit = case.get("visit")
        if not isinstance(visit, dict):
            errors.append(f"{location}.visit: expected an object")
            continue
        _require_exact_keys(visit, _REQUIRED_VISIT, f"{location}.visit", errors)
        if visit.get("encounter_id") != case_id:
            errors.append(f"{location}.visit.encounter_id: must equal case_id")
        age = visit.get("patient_age")
        if not isinstance(age, int) or isinstance(age, bool) or not 0 <= age <= 89:
            errors.append(f"{location}.visit.patient_age: expected 0 through 89")
        if visit.get("patient_sex") not in {"female", "male", "nonbinary", "unknown"}:
            errors.append(f"{location}.visit.patient_sex: unsupported value")
        place_of_service = visit.get("place_of_service")
        if not isinstance(place_of_service, str) or not _POS.fullmatch(place_of_service):
            errors.append(f"{location}.visit.place_of_service: expected two digits")

        for field in _TEXT_FIELDS:
            text = visit.get(field)
            if not isinstance(text, str) or not text.strip():
                errors.append(f"{location}.visit.{field}: expected non-empty text")
                continue
            for violation in prohibited_text_shapes(text, reject_code_tokens=True):
                errors.append(
                    f"{location}.visit.{field}: contains a prohibited {violation}"
                )

        if visit.get("submitted_em_code") is not None:
            errors.append(f"{location}.visit.submitted_em_code: must be null")
        submitted_icd10 = visit.get("submitted_icd10")
        if not isinstance(submitted_icd10, list) or submitted_icd10:
            errors.append(f"{location}.visit.submitted_icd10: must be an empty list")

    if tuple(observed_ids) != CASE_IDS:
        errors.append("pack.cases: case identifiers or ordering changed")

    if errors:
        raise DemoSourceExtensionValidationError("\n".join(errors))


def load_demo_source_extension(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate the tracked source-only extension."""
    pack_path = Path(path) if path is not None else _PACK_PATH
    with pack_path.open(encoding="utf-8") as handle:
        pack = json.load(handle)
    validate_demo_source_extension(pack)
    return pack


if __name__ == "__main__":  # pragma: no cover - release-operator convenience
    loaded = load_demo_source_extension()
    print(f"PASS — {loaded['pack_id']} ({len(loaded['cases'])} source-only cases)")
