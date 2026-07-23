"""HCPCS Level II codes — bundled, public domain.

Source: CMS HCPCS Level II quarterly file, via the Tuva Project terminology
tables (https://github.com/tuva-health/tuva, Apache-2.0). Unlike CPT (HCPCS
Level I, AMA-licensed), Level II codes and descriptors are CMS-maintained
public data — drugs, supplies, and the G-codes that matter for Medicare
E/M-adjacent services (e.g. G2211 visit-complexity add-on, AWV G0438/G0439).

Bundled file layout (no header): code, seq, recid, long_description,
short_description.
"""
from __future__ import annotations

import csv
import gzip
import os
from pathlib import Path

_DATA = Path(__file__).resolve().parent.parent / "data" / "hcpcs_level_2.csv.gz"


class HCPCSLevelII:
    def __init__(self, csv_path: str | os.PathLike | None = None):
        self._desc: dict[str, str] = {}
        path = Path(csv_path or _DATA)
        if path.exists():
            opener = gzip.open if str(path).endswith(".gz") else open
            with opener(path, "rt", newline="", encoding="utf-8") as fh:
                for row in csv.reader(fh):
                    if not row or not row[0].strip():
                        continue
                    code = row[0].strip().upper()
                    # prefer the short descriptor (last col), fall back to long
                    desc = (row[-1] or (row[-2] if len(row) >= 2 else "")).strip()
                    if code and desc:
                        self._desc[code] = desc

    def get(self, code: str) -> str | None:
        return self._desc.get(str(code).strip().upper())

    @property
    def size(self) -> int:
        return len(self._desc)
