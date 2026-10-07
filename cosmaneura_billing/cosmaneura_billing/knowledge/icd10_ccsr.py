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

The legacy semantic-search helper (``embed_search``) is development-only and
not part of the Azure pilot runtime; the demo resolves concepts to codes by
description match instead.
"""
from __future__ import annotations

import csv
import gzip
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..settings import Settings, SettingsError
from .diagnosis_terms import concept_signature, meaning_key, supported_by_quote, uncertain
from .icd10_index_cache import load_index

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


@lru_cache(maxsize=4)
def _description_index(entries: tuple[tuple[str, str], ...]) -> dict:
    """Share the immutable terminology index across encounter orchestrators."""
    index: dict = {}
    for raw, description in entries:
        key = meaning_key(description)
        if key:
            index.setdefault(key, []).append(raw)
    return {key: tuple(raws) for key, raws in index.items()}


class ICD10CCSR:
    def __init__(self, csv_path: str | os.PathLike | None = None,
                 current_csv: str | os.PathLike | None = None,
                 settings: Settings | None = None):
        config = settings or Settings.from_env()
        path = Path(csv_path or config.icd10_ccsr_csv or _default_csv())
        self._by_raw: dict[str, ICD10Code] = {}
        self._rows: list[ICD10Code] = []
        self._load(path)
        # current-FY overlay: raw code -> (billable, description). Pass
        # current_csv="" to disable explicitly.
        self._current: dict[str, tuple[bool, str]] = {}
        cur = _FY_CURRENT_CSV if current_csv is None else current_csv
        self._current_path = Path(cur) if cur else None
        if cur and Path(cur).exists():
            self._load_current(Path(cur))
        self._terminology_index: dict | None = None
        self._more_specific: dict = {}

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
        if raw in self._current:
            # Current CMS descriptions govern meaning. CCSR remains a category
            # overlay and must not restore an obsolete descriptor.
            _, desc = self._current[raw]
            return ICD10Code(code=_format_icd10(raw), raw=raw, description=desc,
                             ccsr_category=hit.ccsr_category if hit else "",
                             ccsr_description=hit.ccsr_description if hit else "")
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

    def billable_entries(self) -> tuple[ICD10Code, ...]:
        """Current local catalog for explicit human browsing; no inferred match."""
        return tuple(self.get(raw) for raw, (billable, _) in sorted(self._current.items()) if billable)

    def find_by_description(self, text: str, limit: int = 5) -> list[ICD10Code]:
        """Return a unique full-meaning match, never a ranked partial match.

        Kept as a compatibility API for callers that only need terminology.
        Coding callers must also check the source quote with ``resolve``.
        A limit of one must not turn an ambiguous concept into a guessed code.
        """
        code, _ = self.resolve(text)
        return [code] if code is not None and limit > 0 else []

    def resolve(self, text: str, *, quote: str | None = None
                ) -> tuple[ICD10Code | None, str]:
        if not self.has_current_fy:
            return None, "Current ICD-10-CM catalogue unavailable; coder mapping required."
        if uncertain(text):
            return None, "Diagnosis or defining attribute is uncertain; coder review required."
        self._ensure_index()
        hits = self._terminology_index.get(meaning_key(text), ())
        if not hits:
            return None, "No full-meaning ICD-10-CM terminology match; coder mapping required."
        if len(hits) != 1:
            return None, "Clinical terminology matches multiple codes; coder mapping required."
        code = self.get(hits[0])
        if quote is not None and not self.supports(code.code, quote):
            return None, ("The source quote does not establish this diagnosis and its "
                          "code-defining attributes; coder review required.")
        return code, "Unique current ICD-10-CM terminology match."

    def _ensure_index(self) -> None:
        if self._terminology_index is None:
            entries = tuple(
                (raw, desc) for raw, (billable, desc) in self._current.items() if billable
            )
            cached = load_index(entries, self._current_path) if self._current_path else None
            self._terminology_index = cached if cached is not None else _description_index(entries)

    def supports(self, code: str, quote: str) -> bool:
        """Support check for an existing candidate, including learned remaps."""
        entry = self.get(code)
        if entry is None or self.is_active(code) is not True:
            return False
        self._ensure_index()
        key = concept_signature(entry.description)
        if key not in self._more_specific:
            signatures = (frozenset(positive) | negative for positive, negative in self._terminology_index)
            self._more_specific[key] = tuple(other for other in signatures if key < other)
        return supported_by_quote(entry.description, quote, more_specific=self._more_specific[key])

    def codes_in_category(self, ccsr_category: str) -> list[ICD10Code]:
        return [c for c in self._rows if c.ccsr_category == ccsr_category]

    @property
    def size(self) -> int:
        return len(self._rows)


# --------------------------------------------------------------------------- #
# Optional legacy semantic search (ported from prototype feature2). It is
# development-only and not part of the Azure pilot runtime.
# --------------------------------------------------------------------------- #
def embed_search(text: str, reference_feather: str, count: int = 5,
                 api_key: str | None = None,
                 settings: Settings | None = None):  # pragma: no cover
    config = settings or Settings.from_env()
    if config.environment in {"staging", "production"}:
        raise SettingsError(
            "direct OpenAI embeddings are disabled in deployed environments"
        )

    import numpy as np
    import openai
    import pandas as pd

    resolved_api_key = api_key or config.openai_api_key
    if not resolved_api_key:
        raise SettingsError("OPENAI_API_KEY is required for ICD-10 semantic search")
    openai.api_key = resolved_api_key
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
