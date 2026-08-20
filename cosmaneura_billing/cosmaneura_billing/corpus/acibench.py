"""ACI-Bench and MTS-Dialog corpus loaders with distinct release postures.

These are the best public stand-ins for this pipeline's real input (visit
transcript + signed note), and the engine's first multi-encounter test bed.

ACI-Bench: 207 full ambient clinical visits (actor/clinician-created
doctor-patient dialogues with complete clinical notes). Yim et al., "ACI-BENCH:
a novel ambient clinical intelligence dataset for benchmarking automatic visit
note generation", Scientific Data 10, 586 (2023). CC-BY-4.0.
Source: https://github.com/wyim/aci-bench

ACI-Bench's peer-reviewed data descriptor documents constructed/role-played
encounters. MTS-Dialog provides ~1,700 short doctor-patient conversation
snippets paired with clinical-note sections (Ben Abacha et al., EACL 2023,
CC-BY-4.0), but its synthetic-only provenance remains unresolved for the
release gate. A permissive copyright license does not establish PHI posture.
Source: https://github.com/abachaa/MTS-Dialog

The train/valid splits are bundled under ``data/corpora/``. Attribution,
licenses, and allowed-use controls are in ``data/corpora/README.md`` and
``cosmaneura_billing/docs/DATA_PROVENANCE_CATALOG.md``; other splits can be fetched with
``scripts/fetch_public_data.py`` only after applying those controls.
"""
from __future__ import annotations

import csv
import os
from pathlib import Path

from ..schemas import VisitBundle

_CORPORA = Path(__file__).resolve().parent.parent / "data" / "corpora"

# ACI-Bench dialogues have no separate intake document, and patient
# demographics live inside the dialogue text, so those VisitBundle fields are
# left neutral. The point of this corpus is exercising extraction -> coding ->
# guardrail on realistic transcript+note pairs.
_ACI_SPLITS = {"train": "acibench_train.csv", "valid": "acibench_valid.csv"}


def load_acibench(split: str = "train",
                  csv_path: str | os.PathLike | None = None) -> list[VisitBundle]:
    """Load an ACI-Bench split as VisitBundles (dialogue -> transcript,
    note -> signed_note)."""
    if csv_path is None:
        fname = _ACI_SPLITS.get(split)
        if fname is None:
            raise ValueError(f"unknown ACI-Bench split '{split}' (have: {sorted(_ACI_SPLITS)})")
        csv_path = _CORPORA / fname
    bundles: list[VisitBundle] = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if not row.get("dialogue") or not row.get("note"):
                continue
            bundles.append(VisitBundle(
                encounter_id=f"aci-{row.get('dataset', 'x')}-{row.get('encounter_id', len(bundles))}",
                patient_age=0, patient_sex="unknown",
                encounter_type="office_established",
                place_of_service="11",
                transcript=row["dialogue"],
                signed_note=row["note"],
            ))
    return bundles


def load_mts_dialog(csv_path: str | os.PathLike | None = None) -> list[dict]:
    """Load MTS-Dialog rows: {id, section_header, section_text, dialogue}.
    These are note-section fragments (not whole visits) — useful for testing
    section-targeted extraction, not full pipeline runs."""
    path = Path(csv_path or _CORPORA / "mts_dialog_train.csv")
    out: list[dict] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out.append({
                "id": row.get("ID", ""),
                "section_header": row.get("section_header", ""),
                "section_text": row.get("section_text", ""),
                "dialogue": row.get("dialogue", ""),
            })
    return out
