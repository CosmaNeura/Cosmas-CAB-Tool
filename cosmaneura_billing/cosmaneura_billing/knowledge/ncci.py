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
from dataclasses import dataclass
from pathlib import Path

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


def _read_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    """Return (header, rows). Sniff comma vs tab; fall back to whitespace."""
    text_path = Path(path)
    if str(text_path).lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(text_path, read_only=True, data_only=True)
        ws = wb.active
        rows = [[("" if c is None else str(c)) for c in r] for r in ws.iter_rows(values_only=True)]
        return (rows[0] if rows else []), rows[1:]
    with _open(text_path) as fh:
        sample = fh.read(4096)
        fh.seek(0)
        delim = "\t" if sample.count("\t") >= sample.count(",") else ","
        reader = list(csv.reader(fh, delimiter=delim))
    # NCCI text files sometimes carry a title line above the header; find the header row
    for i, row in enumerate(reader):
        joined = " ".join(row).lower()
        if "column 1" in joined or ("hcpcs" in joined and "mue" in joined) or "code" in joined:
            return row, reader[i + 1:]
    return (reader[0] if reader else []), reader[1:]


def _col(header: list[str], *needles: str) -> int | None:
    for i, h in enumerate(header):
        hl = h.lower()
        if all(n in hl for n in needles):
            return i
    return None


class NCCIEdits:
    def __init__(self) -> None:
        self._ptp: dict[tuple[str, str], PTPResult] = {}
        self._mue: dict[str, int] = {}

    # ---- construction --------------------------------------------------- #
    @classmethod
    def from_files(cls, ptp_path: str | Path | None = None,
                   mue_path: str | Path | None = None) -> "NCCIEdits":
        self = cls()
        if ptp_path:
            self.load_ptp(ptp_path)
        if mue_path:
            self.load_mue(mue_path)
        return self

    def load_ptp(self, path: str | Path) -> int:
        header, rows = _read_rows(Path(path))
        c1 = _col(header, "column 1") if _col(header, "column 1") is not None else 0
        c2 = _col(header, "column 2") if _col(header, "column 2") is not None else 1
        cm = _col(header, "modifier")
        n = 0
        for r in rows:
            if len(r) <= max(c1, c2):
                continue
            a, b = _norm(r[c1]), _norm(r[c2])
            if not a or not b:
                continue
            mod = (r[cm].strip()[:1] if cm is not None and cm < len(r) and r[cm].strip() else "9")
            self._ptp[(a, b)] = PTPResult(a, b, mod, _MOD.get(mod, "unknown"))
            n += 1
        return n

    def load_mue(self, path: str | Path) -> int:
        header, rows = _read_rows(Path(path))
        cc = _col(header, "code") if _col(header, "code") is not None else 0
        cv = _col(header, "mue", "value")
        if cv is None:
            cv = _col(header, "mue")
        n = 0
        for r in rows:
            if cv is None or len(r) <= max(cc, cv):
                continue
            code = _norm(r[cc])
            try:
                self._mue[code] = int(float(r[cv]))
                n += 1
            except (ValueError, TypeError):
                continue
        return n

    # ---- queries -------------------------------------------------------- #
    def ptp_check(self, code_a: str, code_b: str) -> PTPResult | None:
        a, b = _norm(code_a), _norm(code_b)
        return self._ptp.get((a, b)) or self._ptp.get((b, a))

    def mue_limit(self, code: str) -> int | None:
        return self._mue.get(_norm(code))

    @property
    def loaded(self) -> bool:
        return bool(self._ptp or self._mue)

    @property
    def stats(self) -> str:
        return f"{len(self._ptp)} PTP pairs, {len(self._mue)} MUE limits"


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
