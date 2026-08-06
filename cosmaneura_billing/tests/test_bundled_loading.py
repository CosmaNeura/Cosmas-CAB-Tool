"""Auto-loading of practice-supplied CMS files from data/ncci/ and data/mcd/."""
from cosmaneura_billing.knowledge.coverage import CoverageDB
from cosmaneura_billing.knowledge.ncci import NCCIEdits


def test_ncci_from_bundled_none_when_absent(tmp_path):
    assert NCCIEdits.from_bundled(tmp_path / "nope") is None
    assert NCCIEdits.from_bundled(tmp_path) is None  # exists but empty


def test_ncci_from_bundled_loads_dropped_files(tmp_path):
    (tmp_path / "ncci_practitioner_ptp_q3.txt").write_text(
        "Column 1\tColumn 2\tEffective Date\tModifier\tRationale\n"
        "99213\t36415\t20260101\t1\tStandards of medical practice\n")
    (tmp_path / "mue_practitioner_q3.txt").write_text(
        "HCPCS/CPT Code\tPractitioner Services MUE Values\tMAI\n96372\t4\t3\n")
    n = NCCIEdits.from_bundled(tmp_path)
    assert n is not None and n.loaded
    assert n.ptp_check("99213", "36415").modifier_indicator == "1"
    assert n.mue_limit("96372") == 4


def test_coverage_from_bundled_none_when_absent(tmp_path):
    assert CoverageDB.from_bundled(tmp_path / "nope") is None
    assert CoverageDB.from_bundled(tmp_path) is None


def test_coverage_from_bundled_loads_mcd_tables(tmp_path):
    (tmp_path / "article_x_hcpc.txt").write_text(
        "ARTICLE_ID|ARTICLE_VERSION|HCPC_CODE\nA56802|12|93000\n")
    (tmp_path / "article_x_icd10_covered.txt").write_text(
        "ARTICLE_ID|ICD10_CODE|GROUP_ID\nA56802|I10|1\n")
    (tmp_path / "article_x_icd10_dont_support.txt").write_text(
        "ARTICLE_ID|ICD10_CODE\nA56802|Z0000\n")
    (tmp_path / "article.txt").write_text(
        "ARTICLE_ID|TITLE|CONTRACTOR\nA56802|ECG|NGS (JK)\n")
    db = CoverageDB.from_bundled(tmp_path)
    assert db is not None and db.loaded
    assert db.supports("93000", "I10").status == "covered"
    assert db.supports("93000", "Z00.00").status == "not_covered"


def test_orchestrator_boots_without_bundled_cms_files():
    from cosmaneura_billing.orchestrator import BillingOrchestrator
    orch = BillingOrchestrator()
    assert orch.ncci is None or orch.ncci.loaded
    assert orch.coverage is None or orch.coverage.loaded
