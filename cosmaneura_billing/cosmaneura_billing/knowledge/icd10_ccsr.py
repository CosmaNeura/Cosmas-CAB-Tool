"""
ICD-10-CM <-> CCSR lookup.

Built directly on the AHRQ DXCCSR data shipped in Homen's prototype
(``datasets/DXCCSR/``), which is public domain (CMS ICD-10-CM + AHRQ CCSR).

Resolution order for the code set:
  1. ``csv_path=`` argument, if given.
  2. ``ICD10_CCSR_CSV`` env var, if set (e.g. point it at the raw AHRQ
     ``DXCCSR_v2023-1.csv`` — the 18-column layout is auto-detected).
  3. The bundled FULL set ``data/icd10cm_ccsr_full.csv.gz`` (~74k codes), if present.
  4. The tiny offline subset ``data/icd10_ccsr_subset.csv`` (demo fallback).

Two on-disk schemas are accepted automatically:
  * slim 4-column ``code, description, ccsr_category, ccsr_description``
    (what the bundled full + subset files use), and
  * the raw 18-column AHRQ DXCCSR layout.
``.gz`` files are read transparently.

The semantic-search helper (``embed_search``) is ported from the prototype's
feature2 and is only used when an OpenAI embeddings key is configured; the demo
path resolves concepts to codes by description match instead.
"""
from __future__ import annotations

import csv
import gzip
import os
import re
from dataclasses import dataclass
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_FULL_CSV = _DATA_DIR / "icd10cm_ccsr_full.csv.gz"
_SUBSET_CSV = _DATA_DIR / "icd10_ccsr_subset.csv"
# Current-FY ICD-10-CM code set (CMS via Tuva Project terminology; public
# domain). Layout, no header: code, billable_flag(0/1), short_desc, long_desc.
# The CCSR map lags the fiscal-year cycle, so this overlay is what tells us a
# code is actually valid to bill TODAY (new codes appear here first; deleted
# codes disappear).
_FY_CURRENT_CSV = _DATA_DIR / "icd10cm_fy_current.csv.gz"


def _default_csv() -> Path:
    return _FULL_CSV if _FULL_CSV.exists() else _SUBSET_CSV


@dataclass
class ICD10Code:
    code: str          # formatted with the decimal, e.g. "K62.5"
    raw: str           # as stored by CMS, e.g. "K625"
    description: str   # official CMS descriptor (public domain)
    ccsr_category: str
    ccsr_description: str


def _format_icd10(raw: str) -> str:
    """CMS stores codes without the decimal; insert it after the 3rd char."""
    raw = raw.strip().strip("'\" ")
    if len(raw) > 3 and raw[:3].isalnum():
        return f"{raw[:3]}.{raw[3:]}"
    return raw


class ICD10CCSR:
    def __init__(self, csv_path: str | os.PathLike | None = None,
                 current_csv: str | os.PathLike | None = None):
        path = Path(csv_path or os.environ.get("ICD10_CCSR_CSV") or _default_csv())
        self._by_raw: dict[str, ICD10Code] = {}
        self._rows: list[ICD10Code] = []
        self._load(path)
        # current-FY overlay: raw code -> (billable, description). Pass
        # current_csv="" to disable explicitly.
        self._current: dict[str, tuple[bool, str]] = {}
        cur = _FY_CURRENT_CSV if current_csv is None else current_csv
        if cur and Path(cur).exists():
            self._load_current(Path(cur))

    def _load_current(self, path: Path) -> None:
        opener = gzip.open if str(path).endswith(".gz") else open
        with opener(path, "rt", newline="", encoding="utf-8") as fh:
            for row in csv.reader(fh):
                if len(row) >= 3 and row[0].strip():
                    raw = row[0].strip().strip("'\" ").upper()
                    self._current[raw] = (row[1].strip() == "1", row[-1].strip())

    def _load(self, path: Path) -> None:
        opener = gzip.open if str(path).endswith(".gz") else open
        with opener(path, "rt", newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh, quotechar='"')
            header = next(reader)
            slim = len(header) <= 6   # slim 4-col schema vs raw 18-col DXCCSR
            cat_i, catd_i = (2, 3) if slim else (4, 5)   # OP default CCSR cols in DXCCSR
            for row in reader:
                if not row or len(row) <= catd_i:
                    continue
                raw = row[0].strip().strip("'\" ")
                if not raw or raw.upper().startswith("ICD-10"):
                    continue
                code = ICD10Code(
                    code=_format_icd10(raw), raw=raw,
                    description=row[1].strip().strip("'\" "),
                    ccsr_category=row[cat_i].strip().strip("'\" "),
                    ccsr_description=row[catd_i].strip().strip("'\" "),
                )
                self._by_raw[raw] = code
                self._rows.append(code)

    # ---- lookups -------------------------------------------------------- #
    def get(self, code: str) -> ICD10Code | None:
        raw = code.replace(".", "").strip().upper()
        hit = self._by_raw.get(raw)
        if hit is None and raw in self._current:
            # in the current FY set but newer than the CCSR map
            _, desc = self._current[raw]
            return ICD10Code(code=_format_icd10(raw), raw=raw, description=desc,
                             ccsr_category="", ccsr_description="")
        return hit

    @property
    def has_current_fy(self) -> bool:
        return bool(self._current)

    def is_active(self, code: str) -> bool | None:
        """Is the code billable in the current FY code set?

        True  = present and billable; False = absent or a non-billable header
        code; None = no current-FY data loaded (can't say).
        """
        if not self._current:
            return None
        entry = self._current.get(code.replace(".", "").strip().upper())
        return bool(entry and entry[0])

    def find_by_description(self, text: str, limit: int = 5) -> list[ICD10Code]:
        """Lightweight keyword match against official descriptors (offline path)."""
        terms = [t for t in re.split(r"[^a-z]+", text.lower()) if len(t) > 3]
        scored: list[tuple[int, ICD10Code]] = []
        for c in self._rows:
            d = c.description.lower()
            score = sum(1 for t in terms if t in d)
            if score:
                scored.append((score, c))
        scored.sort(key=lambda x: (-x[0], len(x[1].description)))
        return [c for _, c in scored[:limit]]

    def codes_in_category(self, ccsr_category: str) -> list[ICD10Code]:
        return [c for c in self._rows if c.ccsr_category == ccsr_category]

    @property
    def size(self) -> int:
        return len(self._rows)


# --------------------------------------------------------------------------- #
# Optional: semantic search (ported from prototype feature2). Requires an
# OpenAI embeddings key + the reference-file embeddings; unused in the demo.
# --------------------------------------------------------------------------- #
def embed_search(text: str, reference_feather: str, count: int = 5):  # pragma: no cover
    import numpy as np
    import openai
    import pandas as pd

    openai.api_key = os.environ["OPENAI_API_KEY"]
    ref = pd.read_feather(reference_feather)

    def emb(content: str):
        content = content.encode("ASCII", errors="ignore").decode()
        return openai.Embedding.create(input=content, engine="text-embedding-ada-002")["data"][0]["embedding"]

    q = np.array(emb(text))
    def sim(v):
        v = np.array(v)
        return float(np.dot(q, v) / (np.linalg.norm(q) * np.linalg.norm(v)))

    scored = [{"category": cat, "description": desc, "score": sim(vec)}
              for cat, desc, vec in zip(ref["CCSR Category"], ref["CCSR Category Description"], ref["Embeddings"])]
    scored.sort(key=lambda d: d["score"], reverse=True)
    return scored[:count]
