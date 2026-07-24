"""
Medicare coverage (LCD/NCD) ingestion -- the medical-necessity layer.

Coverage policies say, per service, which ICD-10 diagnoses support medical
necessity (and which explicitly do not). The data is public but lives on
cms.gov, and -- since CPT/HCPCS and ICD-10 code lists now live in Billing &
Coding *Articles* rather than the LCDs themselves -- the authoritative machine
-readable source is the MCD downloadable database (the relational tables, e.g.
``ARTICLE_X_ICD10_COVERED``). NH's MAC is National Government Services
(Jurisdiction K); load the Article tables for that contractor.

Download: https://www.cms.gov/medicare-coverage-database/downloads/downloads.aspx
(the ZIP ships the tables + a Data Dictionary PDF; CPT descriptors inside are
AMA-licensed, but the *code numbers* and the coverage relationships are what we
use here.)

Two ingestion paths, both tested:
  * ``CoverageDB.from_mcd_tables(...)`` -- point at the MCD article tables
    (delimiter auto-detected; columns matched by name so CMS label changes don't
    break it). Joins HCPCS<->ICD-10 by article.
  * ``CoverageDB.from_policies([...])`` / ``from_json(path)`` -- hand-author a
    policy from an article you've read. Always works, ideal for pre-loading the
    handful of services your pilot actually bills (in-office labs, EKG, injections,
    spirometry, etc.). Office-visit E/M codes are generally NOT governed by an LCD
    diagnosis list, so coverage mostly matters for ancillary services.

Self-test:  python -m cosmaneura_billing.knowledge.coverage
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path


def _norm_icd(code: str) -> str:
    return code.replace(".", "").strip().upper()


def _norm_cpt(code: str) -> str:
    return code.strip().upper()


@dataclass
class CoveragePolicy:
    policy_id: str                       # e.g. "A56802" (article) or "L33394" (LCD) / NCD id
    title: str = ""
    contractor: str = ""                 # MAC, e.g. "National Government Services (JK)"
    cpt_codes: set[str] = field(default_factory=set)
    covered_icd10: set[str] = field(default_factory=set)      # support medical necessity
    noncovered_icd10: set[str] = field(default_factory=set)   # explicitly do NOT support


@dataclass
class CoverageResult:
    status: str            # "covered" | "not_supported" | "not_covered" | "no_policy"
    policy_id: str | None
    message: str


def _prefix_hit(code: str, code_set: set[str]) -> bool:
    """LCD lists are usually code-by-code, but sometimes list a parent (e.g. 'E11'
    covering all E11.x). Match exact, or any listed entry that is a prefix."""
    if code in code_set:
        return True
    return any(len(e) < len(code) and code.startswith(e) for e in code_set)


class CoverageDB:
    def __init__(self) -> None:
        self._policies: dict[str, CoveragePolicy] = {}
        self._by_cpt: dict[str, list[str]] = {}   # cpt -> [policy_id, ...]

    # ---- construction --------------------------------------------------- #
    def add(self, p: CoveragePolicy) -> None:
        self._policies[p.policy_id] = p
        for c in p.cpt_codes:
            self._by_cpt.setdefault(_norm_cpt(c), []).append(p.policy_id)

    @classmethod
    def from_policies(cls, policies: list[CoveragePolicy]) -> "CoverageDB":
        db = cls()
        for p in policies:
            p.cpt_codes = {_norm_cpt(c) for c in p.cpt_codes}
            p.covered_icd10 = {_norm_icd(c) for c in p.covered_icd10}
            p.noncovered_icd10 = {_norm_icd(c) for c in p.noncovered_icd10}
            db.add(p)
        return db

    @classmethod
    def from_json(cls, path: str | Path) -> "CoverageDB":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_policies([CoveragePolicy(
            policy_id=d["policy_id"], title=d.get("title", ""), contractor=d.get("contractor", ""),
            cpt_codes=set(d.get("cpt_codes", [])),
            covered_icd10=set(d.get("covered_icd10", [])),
            noncovered_icd10=set(d.get("noncovered_icd10", [])),
        ) for d in data])

    @classmethod
    def from_bundled(cls, dir_path: str | Path | None = None) -> "CoverageDB | None":
        """Load from ``data/mcd/`` if the practice has dropped the MCD Article
        tables there. Files are matched by name: *hcpc* (code list),
        *icd10*covered* (supporting dx), *dont*/*noncovered* (non-supporting
        dx), and article.* (titles/contractor). Returns None when absent."""
        d = Path(dir_path or Path(__file__).resolve().parent.parent / "data" / "mcd")
        if not d.is_dir():
            return None
        files = [p for p in sorted(d.iterdir()) if p.is_file()]

        def find(want, exclude=()):
            for p in files:
                n = p.name.lower()
                if all(w in n for w in want) and not any(x in n for x in exclude):
                    return p
            return None

        hcpc = find(("hcpc",))
        covered = find(("icd10", "covered"), exclude=("dont", "non"))
        if hcpc is None or covered is None:
            return None
        dont = find(("dont",)) or find(("noncovered",))
        article = next((p for p in files if p.stem.lower() == "article"), None)
        return cls.from_mcd_tables(hcpc_file=hcpc, covered_file=covered,
                                   dont_support_file=dont, article_file=article)

    @classmethod
    def from_mcd_tables(cls, *, hcpc_file: str | Path, covered_file: str | Path,
                        dont_support_file: str | Path | None = None,
                        article_file: str | Path | None = None) -> "CoverageDB":
        """Build from the MCD downloadable Article tables."""
        articles: dict[str, CoveragePolicy] = {}

        def policy(aid: str) -> CoveragePolicy:
            return articles.setdefault(aid, CoveragePolicy(policy_id=aid))

        # optional master table for titles / contractor
        if article_file:
            for row in _rows(article_file):
                aid = _get(row, "article", "id") or _get(row, "id")
                if not aid:
                    continue
                p = policy(aid.strip())
                p.title = _get(row, "title") or p.title
                p.contractor = _get(row, "contractor") or _get(row, "jurisdiction") or p.contractor

        for row in _rows(hcpc_file):
            aid = _get(row, "article", "id") or _get(row, "id")
            code = _get(row, "hcpc") or _get(row, "cpt") or _get(row, "code")
            if aid and code:
                policy(aid.strip()).cpt_codes.add(_norm_cpt(code))

        for row in _rows(covered_file):
            aid = _get(row, "article", "id") or _get(row, "id")
            code = _get(row, "icd")  or _get(row, "code")
            if aid and code:
                policy(aid.strip()).covered_icd10.add(_norm_icd(code))

        if dont_support_file:
            for row in _rows(dont_support_file):
                aid = _get(row, "article", "id") or _get(row, "id")
                code = _get(row, "icd") or _get(row, "code")
                if aid and code:
                    policy(aid.strip()).noncovered_icd10.add(_norm_icd(code))

        return cls.from_policies(list(articles.values()))

    # ---- query ---------------------------------------------------------- #
    def supports(self, cpt: str, icd10: str) -> CoverageResult:
        cpt, icd = _norm_cpt(cpt), _norm_icd(icd10)
        pids = self._by_cpt.get(cpt, [])
        if not pids:
            return CoverageResult("no_policy", None,
                                  f"No coverage policy lists {cpt}; medical necessity is documentation-driven.")
        for pid in pids:
            p = self._policies[pid]
            if _prefix_hit(icd, p.noncovered_icd10):
                return CoverageResult("not_covered", pid,
                                      f"{p.policy_id}: {icd10} is on the does-not-support list for {cpt}.")
        for pid in pids:
            p = self._policies[pid]
            if p.covered_icd10 and _prefix_hit(icd, p.covered_icd10):
                return CoverageResult("covered", pid, f"{p.policy_id}: {icd10} supports {cpt}.")
        # service is policy-governed and has a covered list, but dx isn't on it
        with_lists = [pid for pid in pids if self._policies[pid].covered_icd10]
        if with_lists:
            return CoverageResult("not_supported", with_lists[0],
                                  f"{with_lists[0]}: {icd10} is not on the covered-diagnosis list for {cpt}; "
                                  "claim may deny for medical necessity (consider an ABN or revise).")
        return CoverageResult("covered", pids[0], f"{pids[0]}: {cpt} not restricted by a diagnosis list.")

    @property
    def loaded(self) -> bool:
        return bool(self._policies)

    @property
    def stats(self) -> str:
        return (f"{len(self._policies)} policies, "
                f"{sum(len(p.cpt_codes) for p in self._policies.values())} CPT links")


# --------------------------------------------------------------------------- #
# delimited-table helpers (pipe/comma/tab auto-detect; column match by name)
# --------------------------------------------------------------------------- #
def _rows(path: str | Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8", errors="ignore") as fh:
        sample = fh.read(4096); fh.seek(0)
        delim = max(["|", "\t", ","], key=sample.count)
        reader = csv.reader(fh, delimiter=delim)
        rows = list(reader)
    if not rows:
        return []
    header = [h.strip() for h in rows[0]]
    return [dict(zip(header, r)) for r in rows[1:] if r]


def _get(row: dict[str, str], *needles: str) -> str | None:
    for k, v in row.items():
        kl = k.lower()
        if all(n in kl for n in needles):
            return (v or "").strip()
    return None


if __name__ == "__main__":
    import tempfile, os
    d = tempfile.mkdtemp()

    # synthetic MCD article tables (pipe-delimited, as CMS ships them)
    def write(name, header, rows):
        p = os.path.join(d, name)
        with open(p, "w") as f:
            f.write("|".join(header) + "\n")
            for r in rows:
                f.write("|".join(r) + "\n")
        return p

    hcpc = write("article_x_hcpc.txt", ["ARTICLE_ID", "ARTICLE_VERSION", "HCPC_CODE"],
                 [["A56802", "12", "93000"], ["A56802", "12", "93010"]])           # ECG
    cov = write("article_x_icd10_covered.txt", ["ARTICLE_ID", "ICD10_CODE", "GROUP_ID"],
                [["A56802", "I10", "1"], ["A56802", "R0789", "1"], ["A56802", "R000", "1"]])
    dont = write("article_x_icd10_dont_support.txt", ["ARTICLE_ID", "ICD10_CODE"],
                 [["A56802", "Z0000"]])
    art = write("article.txt", ["ARTICLE_ID", "TITLE", "CONTRACTOR"],
                [["A56802", "Electrocardiography", "National Government Services (JK)"]])

    db = CoverageDB.from_mcd_tables(hcpc_file=hcpc, covered_file=cov,
                                    dont_support_file=dont, article_file=art)
    print("loaded:", db.stats)
    for cpt, icd, expect in [("93000", "I10", "covered"),
                             ("93000", "R07.89", "covered"),
                             ("93000", "Z00.00", "not_covered"),
                             ("93000", "K59.00", "not_supported"),
                             ("99213", "I10", "no_policy")]:
        r = db.supports(cpt, icd)
        ok = "OK" if r.status == expect else f"FAIL(expected {expect})"
        print(f"  {cpt} + {icd:<8} -> {r.status:<14} {ok}")
        assert r.status == expect, (cpt, icd, r.status, expect)

    # hand-authored JSON path
    jpath = os.path.join(d, "policies.json")
    Path(jpath).write_text(json.dumps([{
        "policy_id": "L34567", "title": "Vitamin D testing", "contractor": "NGS (JK)",
        "cpt_codes": ["82306"], "covered_icd10": ["E55.9", "M83"], "noncovered_icd10": [],
    }]))
    db2 = CoverageDB.from_json(jpath)
    assert db2.supports("82306", "M83.9").status == "covered"   # prefix: M83 covers M83.9
    assert db2.supports("82306", "E78.5").status == "not_supported"
    print("\nPASS — coverage ingester reads MCD article tables + hand-authored policies; "
          "answers covered / not_supported / not_covered / no_policy.")
