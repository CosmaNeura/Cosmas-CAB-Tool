"""
E/M coding benchmarks from CMS Medicare Physician & Other Practitioners data.

What this is for -- and a hard boundary
---------------------------------------
This module answers PRACTICE-level questions: how does a practice's distribution
of office-visit E/M levels compare to its specialty's national distribution, what
real Medicare dollars attach to each level, and is the practice an over-coding
outlier? It is used for opportunity-sizing, the validation study's dollar
benchmark, and compliance/outlier screening.

It is **not** an input to coding any individual encounter. The engine levels each
visit from its documentation, with evidence; nudging a specific encounter toward a
population average would be statistical upcoding -- exactly what the guardrail
prevents. A benchmark gap says "your aggregate mix looks low (or high) for your
specialty, so prioritize chart review here," not "code this visit higher."

Data: data/em_benchmark_by_specialty.csv (built by scripts/build_em_benchmark.py
from the CMS file; code numbers + numeric stats only, no CPT descriptors).

    from cosmaneura_billing.benchmarks import EMBenchmark
    bm = EMBenchmark()
    bm.mix("Internal Medicine")                       # established-visit level mix
    bm.allowed("99214", specialty="Internal Medicine")
    bm.compare({"99213": 600, "99214": 300, "99215": 40}, "Internal Medicine")

Self-test:  python -m cosmaneura_billing.benchmarks.em_benchmark
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

_CSV = Path(__file__).resolve().parent / "data" / "em_benchmark_by_specialty.csv"

ESTABLISHED = ["99211", "99212", "99213", "99214", "99215"]
NEW = ["99202", "99203", "99204", "99205"]


@dataclass
class _Cell:
    n_providers: int
    services: int
    benes: int
    mean_allowed: float


class EMBenchmark:
    def __init__(self, csv_path: str | Path | None = None, place: str = "O"):
        self.place = place
        self._by_spec: dict[str, dict[str, _Cell]] = {}     # specialty -> {code: cell}
        self._national: dict[str, _Cell] = {}               # code -> aggregated cell
        self._load(Path(csv_path or _CSV))

    def _load(self, path: Path) -> None:
        nat_sum: dict[str, list[float]] = {}
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r["place_of_service"].strip() != self.place:
                    continue
                code = r["hcpcs"].strip()
                cell = _Cell(int(r["n_providers"]), int(r["total_services"]),
                             int(r["total_benes"]), float(r["mean_allowed_amt"]))
                self._by_spec.setdefault(r["specialty"].strip(), {})[code] = cell
                s = nat_sum.setdefault(code, [0.0, 0.0, 0.0, 0.0])
                s[0] += cell.n_providers; s[1] += cell.services
                s[2] += cell.benes; s[3] += cell.mean_allowed * cell.services
        self._national = {c: _Cell(int(v[0]), int(v[1]), int(v[2]),
                                   round(v[3] / v[1], 2) if v[1] else 0.0)
                          for c, v in nat_sum.items()}

    # ---- queries -------------------------------------------------------- #
    def specialties(self) -> list[str]:
        return sorted(self._by_spec)

    def _family(self, family: str) -> list[str]:
        return ESTABLISHED if family.startswith("est") else NEW

    def mix(self, specialty: str | None = None, family: str = "established") -> dict[str, float]:
        """Share of services across the family's levels (sums to ~1)."""
        codes = self._family(family)
        src = self._by_spec.get(specialty, {}) if specialty else self._national
        svc = {c: (src[c].services if c in src else 0) for c in codes}
        tot = sum(svc.values())
        return {c: round(svc[c] / tot, 4) if tot else 0.0 for c in codes}

    def allowed(self, code: str, specialty: str | None = None) -> float | None:
        cell = (self._by_spec.get(specialty, {}) if specialty else self._national).get(code)
        if cell is None and specialty is not None:
            cell = self._national.get(code)
        return cell.mean_allowed if cell else None

    def compare(self, practice_counts: dict[str, int], specialty: str,
                family: str = "established") -> dict:
        """Compare a practice's E/M mix to its specialty benchmark."""
        codes = self._family(family)
        ptot = sum(practice_counts.get(c, 0) for c in codes)
        pmix = {c: (practice_counts.get(c, 0) / ptot if ptot else 0.0) for c in codes}
        bmix = self.mix(specialty, family)

        def mean_allowed(mix):
            return sum(mix[c] * (self.allowed(c, specialty) or 0.0) for c in codes)

        p_mean, b_mean = mean_allowed(pmix), mean_allowed(bmix)
        per_visit_gap = round(b_mean - p_mean, 2)            # + => practice below peers
        annual_gap = round(per_visit_gap * ptot, 2)

        # over-coding outlier: share of the top two levels vs benchmark
        top = (pmix.get(codes[-1], 0) + pmix.get(codes[-2], 0))
        btop = (bmix.get(codes[-1], 0) + bmix.get(codes[-2], 0))
        outlier_high = top > btop + 0.15 or (btop > 0 and top / btop > 1.5)

        return {
            "specialty": specialty, "family": family, "n_visits": ptot,
            "practice_mix": pmix, "benchmark_mix": bmix,
            "practice_mean_allowed_per_visit": round(p_mean, 2),
            "benchmark_mean_allowed_per_visit": round(b_mean, 2),
            "per_visit_gap_vs_benchmark": per_visit_gap,
            "annualized_gap": annual_gap,
            "direction": ("below_peers_review_for_undercoding" if per_visit_gap > 0
                          else "above_peers"),
            "over_coding_outlier": bool(outlier_high),
            "caveat": ("Population benchmark only. Confirm per encounter from documentation; "
                       "the gap prioritizes chart review, it is not a coding target."),
        }


if __name__ == "__main__":
    bm = EMBenchmark()
    print(f"specialties loaded: {len(bm.specialties())}")
    spec = "Internal Medicine"
    print(f"\n{spec} — established office-visit mix (national Medicare 2024):")
    for c, share in bm.mix(spec).items():
        print(f"  {c}: {share:6.1%}   allowed ${bm.allowed(c, spec)}")

    print("\nExample comparison — a practice skewed low (lots of 99213):")
    res = bm.compare({"99212": 100, "99213": 700, "99214": 180, "99215": 20}, spec)
    print(f"  practice 99214+99215 share: {res['practice_mix']['99214']+res['practice_mix']['99215']:.1%}"
          f"  vs benchmark {res['benchmark_mix']['99214']+res['benchmark_mix']['99215']:.1%}")
    print(f"  mean allowed/visit: practice ${res['practice_mean_allowed_per_visit']} "
          f"vs peers ${res['benchmark_mean_allowed_per_visit']}")
    print(f"  per-visit gap ${res['per_visit_gap_vs_benchmark']} -> annualized ${res['annualized_gap']:,} "
          f"({res['direction']})")
    print(f"  over-coding outlier: {res['over_coding_outlier']}")
    assert res["per_visit_gap_vs_benchmark"] > 0  # this practice looks low vs peers
    print("\nPASS — benchmark loads real CMS allowed amounts, computes specialty mix, "
          "sizes the opportunity, and screens for over-coding outliers.")


# --------------------------------------------------------------------------- #
# State-level E/M benchmark (pilot market). Same hard boundary as above:
# population data for opportunity-sizing and outlier screening only — never an
# input to coding an individual encounter.
# --------------------------------------------------------------------------- #
_STATE_CSV = Path(__file__).resolve().parent / "data" / "em_nh_2024.csv"


class StateEMBenchmark:
    """E/M utilization for one state, from the CMS Medicare Physician & Other
    Practitioners by-Provider-and-Service data (2024; code numbers + numeric
    stats only, no CPT descriptors). Bundled: New Hampshire — the pilot market."""

    def __init__(self, csv_path: str | Path | None = None, state: str = "NH"):
        self.state = state
        self._rows: dict[tuple[str, str], tuple[int, int, float]] = {}
        path = Path(csv_path or _STATE_CSV)
        if path.exists():
            with open(path, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    self._rows[(r["specialty"].strip(), r["hcpcs"].strip())] = (
                        int(r["providers"]), int(r["total_services"]),
                        float(r["avg_allowed_weighted"] or 0))

    @property
    def loaded(self) -> bool:
        return bool(self._rows)

    def specialties(self) -> list[str]:
        return sorted({s for s, _ in self._rows})

    def mix(self, specialty: str | None = None, family: str = "established") -> dict[str, float]:
        """Share of services per E/M level, for one specialty or statewide."""
        codes = ESTABLISHED if family.startswith("est") else NEW
        totals = {c: 0 for c in codes}
        for (spec, code), (_p, services, _a) in self._rows.items():
            if code in totals and (specialty is None or spec == specialty):
                totals[code] += services
        n = sum(totals.values())
        return {c: (v / n if n else 0.0) for c, v in totals.items()}

    def allowed(self, code: str, specialty: str | None = None) -> float | None:
        """Weighted mean Medicare allowed amount in this state."""
        num = den = 0.0
        for (spec, c), (_p, services, avg) in self._rows.items():
            if c == code and avg and (specialty is None or spec == specialty):
                num += avg * services
                den += services
        return round(num / den, 2) if den else None
