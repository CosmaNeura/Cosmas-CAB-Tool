"""Coverage (LCD/NCD article) ingestion: MCD tables and hand-authored policies."""
import json

import pytest

from cosmaneura_billing.knowledge.coverage import CoverageDB


@pytest.fixture()
def db(tmp_path):
    def write(name, header, rows):
        p = tmp_path / name
        p.write_text("|".join(header) + "\n" + "\n".join("|".join(r) for r in rows) + "\n")
        return str(p)

    hcpc = write("article_x_hcpc.txt", ["ARTICLE_ID", "ARTICLE_VERSION", "HCPC_CODE"],
                 [["A56802", "12", "93000"], ["A56802", "12", "93010"]])
    cov = write("article_x_icd10_covered.txt", ["ARTICLE_ID", "ICD10_CODE", "GROUP_ID"],
                [["A56802", "I10", "1"], ["A56802", "R0789", "1"], ["A56802", "R000", "1"]])
    dont = write("article_x_icd10_dont_support.txt", ["ARTICLE_ID", "ICD10_CODE"],
                 [["A56802", "Z0000"]])
    art = write("article.txt", ["ARTICLE_ID", "TITLE", "CONTRACTOR"],
                [["A56802", "Electrocardiography", "National Government Services (JK)"]])
    return CoverageDB.from_mcd_tables(hcpc_file=hcpc, covered_file=cov,
                                      dont_support_file=dont, article_file=art)


@pytest.mark.parametrize("cpt,icd,expected", [
    ("93000", "I10", "covered"),
    ("93000", "R07.89", "covered"),
    ("93000", "Z00.00", "not_covered"),
    ("93000", "K59.00", "not_supported"),
    ("99213", "I10", "no_policy"),
])
def test_mcd_tables(db, cpt, icd, expected):
    assert db.supports(cpt, icd).status == expected


def test_hand_authored_json_policies(tmp_path):
    jpath = tmp_path / "policies.json"
    jpath.write_text(json.dumps([{
        "policy_id": "L34567", "title": "Vitamin D testing", "contractor": "NGS (JK)",
        "cpt_codes": ["82306"], "covered_icd10": ["E55.9", "M83"], "noncovered_icd10": [],
    }]))
    db = CoverageDB.from_json(str(jpath))
    assert db.supports("82306", "M83.9").status == "covered"   # prefix match
    assert db.supports("82306", "E78.5").status == "not_supported"
