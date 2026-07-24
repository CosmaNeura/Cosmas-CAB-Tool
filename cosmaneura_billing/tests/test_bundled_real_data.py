"""The real bundled CMS data: 2026Q3 practitioner NCCI edits and the
NGS-jurisdiction MCD coverage articles. Skipped automatically if the data
directories are removed (e.g. a slimmed deployment)."""
from pathlib import Path

import pytest

from cosmaneura_billing.knowledge.coverage import CoverageDB
from cosmaneura_billing.knowledge.ncci import NCCIEdits

_DATA = Path(__file__).resolve().parents[1] / "cosmaneura_billing" / "data"

pytestmark = []


@pytest.mark.skipif(not (_DATA / "ncci").is_dir(), reason="no bundled NCCI data")
class TestBundledNCCI:
    @pytest.fixture(scope="class")
    def ncci(self):
        n = NCCIEdits.from_bundled()
        assert n is not None
        return n

    def test_full_practitioner_ptp_set_loads(self, ncci):
        assert ncci.loaded
        assert "PTP" in ncci.stats and "MUE" in ncci.stats

    def test_known_ptp_edit(self, ncci):
        hit = ncci.ptp_check("0232T", "36415")
        assert hit is not None and hit.modifier_indicator == "1"

    def test_known_mue_values(self, ncci):
        assert ncci.mue_limit("96372") == 4   # therapeutic injection: 4/day
        assert ncci.mue_limit("G0438") == 1   # initial AWV: once
        assert ncci.mue_limit("36415") == 2

    def test_memoized_across_calls(self, ncci):
        assert NCCIEdits.from_bundled() is ncci


@pytest.mark.skipif(not (_DATA / "mcd").is_dir(), reason="no bundled MCD data")
class TestBundledCoverage:
    @pytest.fixture(scope="class")
    def db(self):
        db = CoverageDB.from_bundled()
        assert db is not None
        return db

    def test_ngs_articles_load(self, db):
        assert db.loaded
        assert len(db._policies) >= 50

    def test_a_real_covered_pair_exists(self, db):
        pol = next(p for p in db._policies.values() if p.cpt_codes and p.covered_icd10)
        cpt, icd = next(iter(pol.cpt_codes)), next(iter(pol.covered_icd10))
        assert db.supports(cpt, icd).status == "covered"

    def test_em_office_visit_has_no_policy(self, db):
        # office E/M is not LCD-governed; the NGS filter should leave it clean
        assert db.supports("99213", "I10").status == "no_policy"
