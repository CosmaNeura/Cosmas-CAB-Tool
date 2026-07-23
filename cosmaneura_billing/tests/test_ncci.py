"""NCCI loader: ingests the CMS PTP/MUE layouts and answers edit queries."""
import pytest

from cosmaneura_billing.knowledge.ncci import NCCIEdits


@pytest.fixture()
def ncci(tmp_path):
    ptp = tmp_path / "ptp.txt"
    ptp.write_text(
        "Column 1\tColumn 2\tEffective Date\tModifier\tRationale\n"
        "99213\t36415\t20240101\t1\tStandards of medical practice\n"
        "45378\t45380\t20240101\t0\tMisuse of column two code\n"
    )
    mue = tmp_path / "mue.txt"
    mue.write_text(
        "HCPCS/CPT Code\tPractitioner Services MUE Values\tMAI\n"
        "96372\t4\t3\n11042\t1\t2\n"
    )
    return NCCIEdits.from_files(ptp_path=str(ptp), mue_path=str(mue))


def test_ptp_modifier_may_bypass(ncci):
    hit = ncci.ptp_check("99213", "36415")
    assert hit is not None and hit.modifier_indicator == "1"


def test_ptp_no_modifier_allowed_and_order_insensitive(ncci):
    assert ncci.ptp_check("45378", "45380").modifier_indicator == "0"
    assert ncci.ptp_check("45380", "45378").modifier_indicator == "0"


def test_ptp_miss_returns_none(ncci):
    assert ncci.ptp_check("99213", "99214") is None


def test_mue_limits(ncci):
    assert ncci.mue_limit("96372") == 4
    assert ncci.mue_limit("11042") == 1
    assert ncci.mue_limit("99999") is None
