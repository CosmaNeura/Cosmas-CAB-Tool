"""Load and validate the bounded, wholly synthetic demo release-candidate pack.

The pack is deliberately an evaluation artifact, not a training corpus.  Its
captured-code expectations are limited to billable ICD-10-CM codes that are
written explicitly in, and anchored to an exact quote from, the signed note.
This makes the expected capture mechanically auditable without redistributing
proprietary code descriptions.
"""
from __future__ import annotations

import csv
import gzip
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .synthetic_safety import prohibited_text_shapes


_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_PACK_PATH = _DATA_DIR / "demo_release_candidate" / "pack.v1.json"
_ICD10_PATH = _DATA_DIR / "icd10cm_fy_current.csv.gz"

_CASE_ID = re.compile(r"^SYN-RC-[0-9]{4}$")
_ICD10 = re.compile(r"^[A-Z][0-9]{2}(?:\.[A-Z0-9]{1,4})?$")
_POS = re.compile(r"^[0-9]{2}$")

_NON_AFFIRMING_CAPTURE = re.compile(
    r"\b(?:no|not|never|denies|denied|without|negative\s+for|rule[ds]?\s+out|"
    r"differential|possible|possibly|suspected|family\s+history)\b",
    re.I,
)

_REQUIRED_TOP_LEVEL = {
    "schema_version",
    "pack_id",
    "allowed_use",
    "training_use",
    "synthetic_declaration",
    "contains_real_patient_data",
    "contains_deidentified_patient_data",
    "pack_invariants",
    "cases",
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
_REQUIRED_INVARIANTS = {
    "captured_codes_require_verbatim_signed_note_evidence",
    "negated_findings_are_not_captured",
    "family_history_does_not_become_patient_diagnosis",
    "differentials_are_not_captured_as_confirmed_diagnoses",
    "signed_note_overrides_unsigned_suspicion",
    "policy_tables_never_create_a_clinical_code",
    "no_em_level_without_documented_support",
}
_MUST_NOT_REASONS = {
    "negated",
    "family_history_only",
    "differential_only",
    "contradicted_by_signed_note",
    "not_in_signed_note",
}


class DemoPackValidationError(ValueError):
    """The release-candidate pack violates one or more safety constraints."""


@lru_cache(maxsize=1)
def _billable_icd10_codes() -> frozenset[str]:
    """Return normalized billable codes from the bundled current-FY CMS set."""
    codes: set[str] = set()
    with gzip.open(_ICD10_PATH, "rt", encoding="utf-8", newline="") as fh:
        for row in csv.reader(fh):
            if len(row) >= 2 and row[1].strip() == "1":
                codes.add(row[0].strip().upper().replace(".", ""))
    return frozenset(codes)


def _require_keys(value: dict[str, Any], required: set[str], location: str, errors: list[str]) -> None:
    missing = sorted(required - set(value))
    if missing:
        errors.append(f"{location}: missing keys {missing}")
    unexpected = sorted(set(value) - required)
    if unexpected:
        errors.append(f"{location}: unexpected keys {unexpected}")


def _reject_unknown_keys(value: dict[str, Any], allowed: set[str], location: str, errors: list[str]) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        errors.append(f"{location}: unknown keys {unknown}")


def _free_text_values(visit: dict[str, Any]) -> list[tuple[str, str]]:
    fields = (
        "encounter_type", "provider_specialty", "intake_text", "transcript",
        "signed_note",
    )
    return [(field, value) for field in fields if isinstance((value := visit.get(field)), str)]


def _validate_code(code: Any, location: str, errors: list[str]) -> str | None:
    if not isinstance(code, str) or not _ICD10.fullmatch(code):
        errors.append(f"{location}: expected a billable ICD-10-CM code")
        return None
    normalized = code.replace(".", "")
    if normalized not in _billable_icd10_codes():
        errors.append(f"{location}: {code!r} is not billable in the bundled current-FY code set")
        return None
    return code


def validate_demo_release_candidate(pack: Any) -> None:
    """Raise :class:`DemoPackValidationError` if *pack* is not release-safe."""
    errors: list[str] = []
    if not isinstance(pack, dict):
        raise DemoPackValidationError("pack: expected a JSON object")

    _require_keys(pack, _REQUIRED_TOP_LEVEL, "pack", errors)
    _reject_unknown_keys(pack, _REQUIRED_TOP_LEVEL, "pack", errors)
    if pack.get("schema_version") != "1.0":
        errors.append("pack.schema_version: expected '1.0'")
    if pack.get("pack_id") != "cosmaneura-demo-rc-v1":
        errors.append("pack.pack_id: unexpected pack identifier")
    if pack.get("allowed_use") != "demo_and_evaluation_only":
        errors.append("pack.allowed_use: must be 'demo_and_evaluation_only'")
    if pack.get("training_use") != "prohibited":
        errors.append("pack.training_use: must be 'prohibited'")
    if pack.get("contains_real_patient_data") is not False:
        errors.append("pack.contains_real_patient_data: must be false")
    if pack.get("contains_deidentified_patient_data") is not False:
        errors.append("pack.contains_deidentified_patient_data: must be false")
    declaration = pack.get("synthetic_declaration")
    if not isinstance(declaration, str) or "wholly synthetic" not in declaration.lower():
        errors.append("pack.synthetic_declaration: must explicitly say 'wholly synthetic'")

    invariants = pack.get("pack_invariants")
    if not isinstance(invariants, list) or not all(isinstance(item, str) for item in invariants):
        errors.append("pack.pack_invariants: expected a list of strings")
    else:
        missing_invariants = sorted(_REQUIRED_INVARIANTS - set(invariants))
        if missing_invariants:
            errors.append(f"pack.pack_invariants: missing {missing_invariants}")

    cases = pack.get("cases")
    if not isinstance(cases, list) or not 3 <= len(cases) <= 5:
        errors.append("pack.cases: expected between 3 and 5 cases")
        cases = []

    seen_ids: set[str] = set()
    for index, case in enumerate(cases):
        location = f"pack.cases[{index}]"
        if not isinstance(case, dict):
            errors.append(f"{location}: expected an object")
            continue
        case_keys = {"case_id", "provenance", "visit", "expectations"}
        _require_keys(case, case_keys, location, errors)
        _reject_unknown_keys(case, case_keys, location, errors)

        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id):
            errors.append(f"{location}.case_id: expected SYN-RC-NNNN")
        elif case_id in seen_ids:
            errors.append(f"{location}.case_id: duplicate {case_id}")
        else:
            seen_ids.add(case_id)

        provenance = case.get("provenance")
        if not isinstance(provenance, dict):
            errors.append(f"{location}.provenance: expected an object")
        else:
            _require_keys(
                provenance,
                {
                    "kind", "source_dataset", "contains_real_patient_data",
                    "contains_deidentified_patient_data",
                },
                f"{location}.provenance",
                errors,
            )
            provenance_keys = {
                "kind",
                "source_dataset",
                "contains_real_patient_data",
                "contains_deidentified_patient_data",
            }
            _require_keys(provenance, provenance_keys, f"{location}.provenance", errors)
            _reject_unknown_keys(provenance, provenance_keys, f"{location}.provenance", errors)
            if provenance.get("kind") != "wholly_synthetic":
                errors.append(f"{location}.provenance.kind: must be wholly_synthetic")
            if provenance.get("source_dataset") is not None:
                errors.append(f"{location}.provenance.source_dataset: must be null")
            if provenance.get("contains_real_patient_data") is not False:
                errors.append(f"{location}.provenance.contains_real_patient_data: must be false")
            if provenance.get("contains_deidentified_patient_data") is not False:
                errors.append(f"{location}.provenance.contains_deidentified_patient_data: must be false")

        visit = case.get("visit")
        if not isinstance(visit, dict):
            errors.append(f"{location}.visit: expected an object")
            continue
        _require_keys(visit, _REQUIRED_VISIT, f"{location}.visit", errors)
        _reject_unknown_keys(visit, _REQUIRED_VISIT, f"{location}.visit", errors)
        if visit.get("encounter_id") != case_id:
            errors.append(f"{location}.visit.encounter_id: must equal the synthetic case_id")
        age = visit.get("patient_age")
        if not isinstance(age, int) or isinstance(age, bool) or not 0 <= age <= 89:
            errors.append(f"{location}.visit.patient_age: expected an integer from 0 through 89")
        if visit.get("patient_sex") not in {"female", "male", "nonbinary", "unknown"}:
            errors.append(f"{location}.visit.patient_sex: unsupported value")
        if not isinstance(visit.get("place_of_service"), str) or not _POS.fullmatch(visit["place_of_service"]):
            errors.append(f"{location}.visit.place_of_service: expected two digits")
        for required_text in ("encounter_type", "provider_specialty", "intake_text", "transcript", "signed_note"):
            value = visit.get(required_text)
            if not isinstance(value, str) or (required_text in {"encounter_type", "provider_specialty", "signed_note"} and not value.strip()):
                errors.append(f"{location}.visit.{required_text}: expected text")
        if visit.get("submitted_em_code") is not None:
            errors.append(f"{location}.visit.submitted_em_code: pack intentionally carries no CPT expectations")
        submitted_icd10 = visit.get("submitted_icd10")
        if not isinstance(submitted_icd10, list) or submitted_icd10:
            errors.append(f"{location}.visit.submitted_icd10: expected an empty list")

        for field, text in _free_text_values(visit):
            for violation in prohibited_text_shapes(text):
                errors.append(
                    f"{location}.visit.{field}: contains a prohibited {violation}"
                )

        expectations = case.get("expectations")
        if not isinstance(expectations, dict):
            errors.append(f"{location}.expectations: expected an object")
            continue
        expectation_keys = {"must_capture", "must_not_capture", "invariants"}
        _require_keys(expectations, expectation_keys, f"{location}.expectations", errors)
        _reject_unknown_keys(expectations, expectation_keys, f"{location}.expectations", errors)
        captures = expectations.get("must_capture")
        exclusions = expectations.get("must_not_capture")
        case_invariants = expectations.get("invariants")
        if not isinstance(captures, list):
            errors.append(f"{location}.expectations.must_capture: expected a list")
            captures = []
        if not isinstance(exclusions, list):
            errors.append(f"{location}.expectations.must_not_capture: expected a list")
            exclusions = []
        if not isinstance(case_invariants, list) or not case_invariants or not all(
            isinstance(item, str) and item in _REQUIRED_INVARIANTS for item in case_invariants
        ):
            errors.append(f"{location}.expectations.invariants: expected known pack invariants")

        captured_codes: set[str] = set()
        for capture_index, capture in enumerate(captures):
            capture_location = f"{location}.expectations.must_capture[{capture_index}]"
            if not isinstance(capture, dict):
                errors.append(f"{capture_location}: expected an object")
                continue
            capture_keys = {"kind", "code", "evidence"}
            _require_keys(capture, capture_keys, capture_location, errors)
            _reject_unknown_keys(capture, capture_keys, capture_location, errors)
            if capture.get("kind") != "icd10_diagnosis":
                errors.append(f"{capture_location}.kind: only ICD-10-CM diagnosis expectations are allowed")
            code = _validate_code(capture.get("code"), f"{capture_location}.code", errors)
            evidence = capture.get("evidence")
            if not isinstance(evidence, dict):
                errors.append(f"{capture_location}.evidence: expected an object")
                continue
            _require_keys(
                evidence, {"source", "quote"}, f"{capture_location}.evidence", errors,
            )
            evidence_keys = {"source", "quote"}
            _require_keys(evidence, evidence_keys, f"{capture_location}.evidence", errors)
            _reject_unknown_keys(evidence, evidence_keys, f"{capture_location}.evidence", errors)
            if evidence.get("source") != "signed_note":
                errors.append(f"{capture_location}.evidence.source: must be signed_note")
            quote = evidence.get("quote")
            if not isinstance(quote, str) or not quote.strip():
                errors.append(f"{capture_location}.evidence.quote: expected non-empty text")
                continue
            if quote not in visit.get("signed_note", ""):
                errors.append(f"{capture_location}.evidence.quote: is not verbatim in the signed note")
            if code and f"[{code}]" not in quote:
                errors.append(f"{capture_location}.evidence.quote: must explicitly contain [{code}]")
            if not quote.startswith("Assessment: ") or _NON_AFFIRMING_CAPTURE.search(quote):
                errors.append(
                    f"{capture_location}.evidence.quote: must be an affirmative Assessment statement"
                )
            if code:
                captured_codes.add(code)

        excluded_codes: set[str] = set()
        for exclusion_index, exclusion in enumerate(exclusions):
            exclusion_location = f"{location}.expectations.must_not_capture[{exclusion_index}]"
            if not isinstance(exclusion, dict):
                errors.append(f"{exclusion_location}: expected an object")
                continue
            exclusion_keys = {"kind", "code", "reason", "source", "quote"}
            _require_keys(exclusion, exclusion_keys, exclusion_location, errors)
            _reject_unknown_keys(exclusion, exclusion_keys, exclusion_location, errors)
            if exclusion.get("kind") != "icd10_diagnosis":
                errors.append(f"{exclusion_location}.kind: only ICD-10-CM diagnosis expectations are allowed")
            code = _validate_code(exclusion.get("code"), f"{exclusion_location}.code", errors)
            if code:
                excluded_codes.add(code)
            reason = exclusion.get("reason")
            if reason not in _MUST_NOT_REASONS:
                errors.append(f"{exclusion_location}.reason: unsupported exclusion reason")
            source = exclusion.get("source")
            if source not in {"intake_text", "transcript", "signed_note"}:
                errors.append(f"{exclusion_location}.source: unsupported evidence source")
                continue
            quote = exclusion.get("quote")
            source_text = visit.get(source, "")
            if not isinstance(quote, str) or not quote.strip() or quote not in source_text:
                errors.append(f"{exclusion_location}.quote: is not verbatim in {source}")

        overlap = sorted(captured_codes & excluded_codes)
        if overlap:
            errors.append(f"{location}.expectations: codes cannot be both captured and excluded: {overlap}")

    if errors:
        raise DemoPackValidationError("\n".join(errors))


def load_demo_release_candidate(path: str | Path | None = None) -> dict[str, Any]:
    """Load the tracked JSON pack and validate it before returning any cases."""
    pack_path = Path(path) if path is not None else _PACK_PATH
    with pack_path.open(encoding="utf-8") as fh:
        pack = json.load(fh)
    validate_demo_release_candidate(pack)
    return pack


if __name__ == "__main__":  # pragma: no cover - convenience for release operators
    loaded = load_demo_release_candidate()
    print(f"PASS — {loaded['pack_id']} ({len(loaded['cases'])} wholly synthetic cases)")
