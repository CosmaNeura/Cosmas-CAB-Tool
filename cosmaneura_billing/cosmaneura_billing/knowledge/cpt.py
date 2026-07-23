"""CPT descriptor ingestion — for practices holding an AMA CPT license.

CPT codes are referenced throughout this engine by number with short generic
functional labels, because the official descriptors are AMA-licensed content.
A practice that licenses CPT (via the AMA Intelligent Platform / CPT
Distribution Program) receives the *CPT Standard Data File*; this module
ingests the descriptor files from that distribution so the UI and worksheet
can show official short descriptors instead of generic labels.

NOTHING in this module ships descriptor content. It only reads files the
practice obtained under its own license (fetch them with
``scripts/fetch_cpt_zip.py``), and it should only be enabled in deployments
covered by that license.

Accepted inputs (auto-detected):
  * classic Standard Data File text layouts — fixed "CODE<ws>DESCRIPTOR" lines
    (SHORTU.txt / MEDU.txt / LONGULT.txt style);
  * tab- or comma-delimited exports with the code in the first column and the
    descriptor in the last.

Resolution order: explicit path arg -> $CPT_DATA_DIR (first matching file) ->
not loaded (``loaded`` is False and lookups return None).
"""
from __future__ import annotations

import csv
import io
import os
import re
from pathlib import Path

# preferred filenames inside a Standard Data File distribution, best first
_CANDIDATES = ("SHORTU.txt", "SHORTU.TXT", "shortu.txt",
               "MEDU.txt", "MEDU.TXT", "LONGULT.txt", "LONGUF.txt")
_CODE_RE = re.compile(r"^\s*([0-9]{4}[0-9A-Z])\s+(.+?)\s*$")


class CPTDescriptors:
    def __init__(self, path: str | os.PathLike | None = None):
        self._desc: dict[str, str] = {}
        self._source: str | None = None
        resolved = self._resolve(path)
        if resolved is not None:
            self._load(resolved)

    @staticmethod
    def _resolve(path: str | os.PathLike | None) -> Path | None:
        if path:
            return Path(path)
        env_dir = os.environ.get("CPT_DATA_DIR")
        if env_dir:
            d = Path(env_dir)
            for name in _CANDIDATES:
                if (d / name).exists():
                    return d / name
            txts = sorted(d.glob("*.txt")) + sorted(d.glob("*.csv"))
            if txts:
                return txts[0]
        return None

    def _load(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8", errors="replace")
        if "\t" in text.splitlines()[0] or path.suffix.lower() == ".csv":
            delim = "\t" if "\t" in text.splitlines()[0] else ","
            for row in csv.reader(io.StringIO(text), delimiter=delim):
                if len(row) >= 2 and row[0].strip():
                    self._desc[row[0].strip().upper()] = row[-1].strip()
        else:
            for line in text.splitlines():
                m = _CODE_RE.match(line)
                if m:
                    self._desc[m.group(1)] = m.group(2)
        self._source = str(path)

    # ---- lookups -------------------------------------------------------- #
    def get(self, code: str) -> str | None:
        return self._desc.get(str(code).strip().upper())

    def relabel(self, suggestions) -> int:
        """Upgrade generic labels to official descriptors in place (E/M and
        procedure codes only). Returns how many labels were upgraded."""
        if not self.loaded:
            return 0
        n = 0
        for s in suggestions:
            official = self.get(s.code)
            if official and official != s.label:
                s.label = official
                n += 1
        return n

    @property
    def loaded(self) -> bool:
        return bool(self._desc)

    @property
    def size(self) -> int:
        return len(self._desc)

    @property
    def source(self) -> str | None:
        return self._source
