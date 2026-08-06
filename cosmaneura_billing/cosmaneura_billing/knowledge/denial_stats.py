"""Denial statistics — real payer denial priors, bundled from public CMS data.

Two complementary signals (see docs/CLAIMS_DATA_SOURCES.md for the research
behind them):

  DenialPriors      per-CPT/HCPCS national Medicare denial rates, aggregated
                    from the CMS Physician/Supplier Procedure Summary (PSPS)
                    2024 file — 14.4M summary rows -> denied/submitted per
                    code (codes with >=100 submitted services). This is the
                    "how often does THIS CODE get denied" prior.
                    Source: https://data.cms.gov (public domain).

  PayerDenialRates  issuer-level denial rates and top denial-reason category
                    from the CMS Marketplace Transparency in Coverage PUF
                    (PY2026, i.e. 2024 claims experience), per state. This is
                    the "how tough is THIS PAYER" prior (in NH the spread is
                    ~8% to ~29%). Issuer-level Marketplace QHP data — a proxy
                    for a payer's commercial posture, not a per-code truth.

Both are priors for the denial-risk agent and demo benchmarks; the pilot's
own 835 remittances remain the ground truth that will eventually supersede
them (they are the only CARC/RARC-labeled data available anywhere).
"""
from __future__ import annotations

import csv
import gzip
import os
from dataclasses import dataclass
from pathlib import Path

_DATA = Path(__file__).resolve().parent.parent / "data"
_PSPS = _DATA / "psps_denial_rates_2024.csv.gz"
_PSPS_STATE = _DATA / "psps_denial_rates_nh_2024.csv.gz"   # carrier 14312 (NH) slice
_TCPUF = _DATA / "tcpuf_issuer_denials_2026.csv.gz"

# a code is worth flagging when Medicare denies it at least this often
FLAG_THRESHOLD = 0.15
HIGH_THRESHOLD = 0.30


@dataclass
class CodePrior:
    code: str
    submitted: int
    denied: int
    rate: float


@dataclass
class IssuerRate:
    state: str
    issuer_id: str
    issuer_name: str
    claims_received: int
    claims_denied: int
    denial_rate: float
    top_reason: str
    top_reason_share: float | None


class DenialPriors:
    """Per-code Medicare denial rates: national (PSPS 2024) plus a state
    overlay (the pilot MAC's carrier slice of the same file — NH is Medicare
    carrier 14312 under NGS Jurisdiction K)."""

    def __init__(self, csv_path: str | os.PathLike | None = None,
                 state_csv: str | os.PathLike | None = None, state: str = "NH"):
        self._by_code: dict[str, CodePrior] = {}
        self._state_by_code: dict[str, CodePrior] = {}
        self.state = state
        self.source_year = "2024"
        path = Path(csv_path or _PSPS)
        if path.exists():
            self._by_code = self._read(path)
        spath = Path(state_csv or _PSPS_STATE)
        if spath.exists():
            self._state_by_code = self._read(spath)

    @staticmethod
    def _read(path: Path) -> dict[str, CodePrior]:
        out: dict[str, CodePrior] = {}
        with gzip.open(path, "rt", newline="") as fh:
            for row in csv.DictReader(fh):
                code = row["hcpcs"].strip().upper()
                out[code] = CodePrior(
                    code=code, submitted=int(row["submitted_services"]),
                    denied=int(row["denied_services"]),
                    rate=float(row["denial_rate"]))
        return out

    def get(self, code: str) -> CodePrior | None:
        return self._by_code.get(str(code).strip().upper())

    def get_state(self, code: str) -> CodePrior | None:
        return self._state_by_code.get(str(code).strip().upper())

    def rate(self, code: str) -> float | None:
        p = self.get(code)
        return p.rate if p else None

    @property
    def loaded(self) -> bool:
        return bool(self._by_code)

    @property
    def size(self) -> int:
        return len(self._by_code)


class PayerDenialRates:
    """Issuer-level Marketplace denial rates by state (TC-PUF PY2026)."""

    def __init__(self, csv_path: str | os.PathLike | None = None):
        self._rows: list[IssuerRate] = []
        path = Path(csv_path or _TCPUF)
        if path.exists():
            with gzip.open(path, "rt", newline="") as fh:
                for row in csv.DictReader(fh):
                    self._rows.append(IssuerRate(
                        state=row["state"], issuer_id=row["issuer_id"],
                        issuer_name=row["issuer_name"],
                        claims_received=int(row["claims_received"]),
                        claims_denied=int(row["claims_denied"]),
                        denial_rate=float(row["denial_rate"]),
                        top_reason=row["top_reason"],
                        top_reason_share=float(row["top_reason_share"])
                        if row["top_reason_share"] else None))

    def for_state(self, state: str) -> list[IssuerRate]:
        s = state.strip().upper()
        return sorted((r for r in self._rows if r.state == s),
                      key=lambda r: -r.denial_rate)

    def lookup(self, payer_name: str, state: str | None = None) -> IssuerRate | None:
        """Loose substring match on issuer name (optionally within a state)."""
        needle = payer_name.strip().lower()
        if not needle:
            return None
        pool = self.for_state(state) if state else self._rows
        for r in pool:
            if needle in r.issuer_name.lower() or r.issuer_name.lower() in needle:
                return r
        return None

    @property
    def loaded(self) -> bool:
        return bool(self._rows)

    @property
    def size(self) -> int:
        return len(self._rows)
