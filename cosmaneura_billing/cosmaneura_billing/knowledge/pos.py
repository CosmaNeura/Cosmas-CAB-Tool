"""CMS Place of Service (POS) codes — bundled, public domain.

Source: CMS POS code set, via the Tuva Project terminology tables
(https://github.com/tuva-health/tuva, Apache-2.0; underlying code set is
CMS-maintained public data).

POS drives telehealth logic: POS 02 (telehealth other than patient's home) and
POS 10 (telehealth in patient's home) signal that an E/M service was delivered
remotely, which most payers require to carry modifier 95 (or 93 for audio-only)
and which changes facility/non-facility payment. The denial-risk agent uses
this to flag telehealth claims that look inconsistent.
"""
from __future__ import annotations

import csv
import os
from pathlib import Path

_DATA = Path(__file__).resolve().parent.parent / "data" / "place_of_service.csv"

TELEHEALTH_POS = {"02", "10"}


class PlaceOfService:
    def __init__(self, csv_path: str | os.PathLike | None = None):
        self._desc: dict[str, str] = {}
        path = Path(csv_path or _DATA)
        if path.exists():
            with open(path, newline="", encoding="utf-8") as fh:
                for row in csv.reader(fh):
                    if len(row) >= 2 and row[0].strip():
                        self._desc[row[0].strip().zfill(2)] = row[1].strip()

    def get(self, code: str) -> str | None:
        return self._desc.get(str(code).strip().zfill(2))

    @staticmethod
    def is_telehealth(code: str) -> bool:
        return str(code).strip().zfill(2) in TELEHEALTH_POS

    @property
    def size(self) -> int:
        return len(self._desc)
