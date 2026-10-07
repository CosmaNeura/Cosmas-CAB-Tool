"""
NCCI edits ingestion (CMS National Correct Coding Initiative).

This is the "you download it, the tool ingests it" boundary. The NCCI edit files
are public domain but live on cms.gov (not reachable from this build environment),
so the practice downloads the quarterly ZIPs and points this loader at them:

    practitioner PTP edits  -> column-1 / column-2 pairs + modifier indicator
    practitioner MUE edits  -> max units of service per code per day

Usage:
    from cosmaneura_billing.knowledge.ncci import NCCIEdits
    ncci = NCCIEdits.from_files(ptp_path="ptp_practitioner.txt",
                                mue_path="mue_practitioner.txt")
    ncci.ptp_check("99213", "36415")   # -> PTPResult | None
    ncci.mue_limit("96372")            # -> int | None

The loader matches columns by header name (case-insensitive substring) so it
keeps working across CMS's minor format/label changes. CSV, tab-delimited TXT,
and (if openpyxl is installed) XLSX are all accepted. Run a self-test with:
    python -m cosmaneura_billing.knowledge.ncci
"""
from __future__ import annotations

import csv
import gzip
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

# modifier indicator: 0 = modifier never bypasses the edit, 1 = a modifier may
# bypass when clinically appropriate, 9 = edit not applicable / deleted.
_MOD = {"0": "no_modifier_allowed", "1": "modifier_may_bypass", "9": "not_applicable"}


@dataclass
class PTPResult:
    column_one: str          # the payable code
    column_two: str          # the bundled (denied) code unless a modifier applies
    modifier_indicator: str  # "0" | "1" | "9"
    meaning: str


def _norm(code: str) -> str:
    return code.replace(".", "").strip().upper()


def _open(path: Path):
    return gzip.open(path, "rt", newline="", encoding="utf-8", errors="ignore") \
        if str(path).endswith(".gz") else open(path, newline="", encoding="utf-8", errors="ignore")


@contextmanager
def _read_rows(path: Path) -> Iterator[tuple[list[str], Iterator[list[str]]]]:
    """Keep only the header and current row; close text/workbook on every exit."""
    text_path = Path(path)
    if str(text_path).lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(text_path, read_only=True, data_only=True)
        try:
            rows = (["" if c is None else str(c) for c in row]
                    for row in wb.active.iter_rows(values_only=True))
            # Spreadsheet behavior is intentionally first-row header, unlike TXT.
            yield next(rows, []), rows
        finally:
            wb.close()
        return
    with _open(text_path) as fh:
        sample = fh.read(4096)
        fh.seek(0)
        delim = "\t" if sample.count("\t") >= sample.count(",") else ","
        rows = csv.reader(fh, delimiter=delim)
        # Preserve the existing whole-file title/header detection and fallback.
        # If no row is recognizable, rewind rather than retaining all rows.
        for row in rows:
            joined = " ".join(row).lower()
            if "column 1" in joined or ("hcpcs" in joined and "mue" in joined) or "code" in joined:
                yield row, rows
                return
        fh.seek(0)
        rows = csv.reader(fh, delimiter=delim)
        yield next(rows, []), rows


def _col(header: list[str], *needles: str) -> int | None:
    for i, h in enumerate(header):
        hl = h.lower()
        if all(n in hl for n in needles):
            return i
    return None


_BUNDLED_DIR = Path(__file__).resolve().parent.parent / "data" / "ncci"


class NCCIEdits:
    def __init__(self) -> None:
        # Retain one modifier per directed pair, not a tuple/dataclass per pair.
        # Canonical code strings belong to this dataset instance, never sys.intern.
        self._ptp: dict[str, dict[str, str]] = {}
        self._codes: dict[str, str] = {}
        self._pair_count = 0
        self._mue: dict[str, int] = {}

    _bundled_cache: dict[str, "NCCIEdits | None"] = {}

    # ---- construction --------------------------------------------------- #
    @classmethod
    def from_bundled(cls, dir_path: str | Path | None = None) -> "NCCIEdits | None":
        """Load from ``data/ncci/`` if the practice has dropped the CMS files
        there (any file whose name contains 'ptp' / 'mue'; .gz accepted).
        Returns None when nothing is present — callers treat that as 'no NCCI'.
        The full practitioner PTP set is ~1.7M pairs (~18s parse), so results
        are memoized per directory for the life of the process."""
        d = Path(dir_path or _BUNDLED_DIR)
        key = str(d.resolve())
        if key in cls._bundled_cache:
            return cls._bundled_cache[key]
        result = cls._from_bundled_uncached(d)
        cls._bundled_cache[key] = result
        return result

    @classmethod
    def _from_bundled_uncached(cls, d: Path) -> "NCCIEdits | None":
        if not d.is_dir():
            return None

        def find(needle: str) -> Path | None:
            hits = [p for p in sorted(d.iterdir())
                    if p.is_file() and needle in p.name.lower()]
            return hits[0] if hits else None

        ptp, mue = find("ptp"), find("mue")
        if ptp is None and mue is None:
            return None
        return cls.from_files(ptp_path=ptp, mue_path=mue)

    @classmethod
    def from_files(cls, ptp_path: str | Path | None = None,
                   mue_path: str | Path | None = None) -> "NCCIEdits":
        self = cls()
        if ptp_path:
            self.load_ptp(ptp_path)
        if mue_path:
            self.load_mue(mue_path)
        return self

    def _code(self, value: str) -> str:
        code = _norm(value)
        return self._codes.setdefault(code, code)

    def load_ptp(self, path: str | Path) -> int:
        n = 0
        with _read_rows(Path(path)) as (header, rows):
            c1 = _col(header, "column 1") if _col(header, "column 1") is not None else 0
            c2 = _col(header, "column 2") if _col(header, "column 2") is not None else 1
            cm = _col(header, "modifier")
            for r in rows:
                if len(r) <= max(c1, c2):
                    continue
                a, b = _norm(r[c1]), _norm(r[c2])
                if not a or not b:
                    continue
                a, b = self._code(a), self._code(b)
                mod = (r[cm].strip()[:1] if cm is not None and cm < len(r) and r[cm].strip() else "9")
                pairs = self._ptp.setdefault(a, {})
                if b not in pairs:
                    self._pair_count += 1
                pairs[b] = mod
                n += 1
        return n

    def load_mue(self, path: str | Path) -> int:
        n = 0
        with _read_rows(Path(path)) as (header, rows):
            cc = _col(header, "code") if _col(header, "code") is not None else 0
            cv = _col(header, "mue", "value")
            if cv is None:
                cv = _col(header, "mue")
            for r in rows:
                if cv is None or len(r) <= max(cc, cv):
                    continue
                try:
                    value = int(float(r[cv]))
                except (ValueError, TypeError):
                    continue
                self._mue[self._code(r[cc])] = value
                n += 1
        return n

    # ---- queries -------------------------------------------------------- #
    def ptp_check(self, code_a: str, code_b: str) -> PTPResult | None:
        a, b = _norm(code_a), _norm(code_b)
        modifier = self._ptp.get(a, {}).get(b)
        if modifier is None:
            a, b = b, a
            modifier = self._ptp.get(a, {}).get(b)
        if modifier is None:
            return None
        return PTPResult(a, b, modifier, _MOD.get(modifier, "unknown"))

    def mue_limit(self, code: str) -> int | None:
        return self._mue.get(_norm(code))

    @property
    def loaded(self) -> bool:
        return bool(self._ptp or self._mue)

    @property
    def stats(self) -> str:
        return f"{self._pair_count} PTP pairs, {len(self._mue)} MUE limits"


if __name__ == "__main__":
    # Self-test with a tiny synthetic file in the CMS layout (no network needed).
    import tempfile, os
    d = tempfile.mkdtemp()
    ptp = os.path.join(d, "ptp.txt")
    with open(ptp, "w") as f:
        f.write("Column 1\tColumn 2\tEffective Date\tModifier\tRationale\n")
        f.write("99213\t36415\t20240101\t1\tStandards of medical practice\n")
        f.write("45378\t45380\t20240101\t0\tMisuse of column two code\n")
    mue = os.path.join(d, "mue.txt")
    with open(mue, "w") as f:
        f.write("HCPCS/CPT Code\tPractitioner Services MUE Values\tMAI\n")
        f.write("96372\t4\t3\n11042\t1\t2\n")
    n = NCCIEdits.from_files(ptp_path=ptp, mue_path=mue)
    print("loaded:", n.stats)
    print("99213 + 36415 ->", n.ptp_check("99213", "36415"))
    print("45378 + 45380 ->", n.ptp_check("45380", "45378"))
    print("MUE 96372 ->", n.mue_limit("96372"))
    assert n.ptp_check("99213", "36415").modifier_indicator == "1"
    assert n.ptp_check("45378", "45380").modifier_indicator == "0"
    assert n.mue_limit("96372") == 4
    print("PASS — NCCI loader ingests the CMS layout and answers PTP/MUE queries.")
