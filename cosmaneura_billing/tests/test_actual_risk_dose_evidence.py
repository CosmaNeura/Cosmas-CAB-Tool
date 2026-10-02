"""Original cached risk events against the unchanged packaged synthetic notes."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from cosmaneura_billing import mdm_risk as risk
from cosmaneura_billing.corpus.mcd_training import load_mcd_training_pack
from cosmaneura_billing.schemas import VisitBundle


FIXTURE = json.loads((Path(__file__).parent / "fixtures/actual_risk_dose_events.json").read_text())
CASES = {row["caseId"]: row for row in FIXTURE["cases"]}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


@pytest.mark.parametrize("number,bare", [(5, "prednisone"), (9, "levothyroxine")])
def test_actual_dose_subject_and_bare_product_have_identical_exact_risk_proof(number, bare):
    case_id = f"SYN-CAB-20260930-{number:02d}"
    case = CASES[case_id]
    visit = next(row["visit"] for row in load_mcd_training_pack()["cases"]
                 if row["case_id"] == case_id)
    assert digest(visit) == case["sourceContextObjectSha256"]
    bundle = VisitBundle(**visit)
    assert hashlib.sha256(bundle.signed_note.encode()).hexdigest() == case["signedNoteUtf8Sha256"]
    raw = deepcopy(case["riskEvent"]); original = deepcopy(raw)
    assert digest(raw) == case["riskEventSha256"]
    full = risk.qualify_risk([raw], bundle)
    short = risk.qualify_risk([{**raw, "subject": bare}], bundle)
    assert full.level == short.level == "moderate"
    assert full.evidence() == short.evidence()
    assert full.public["events"][0]["subject"] == raw["subject"]
    assert full.matches([raw]) and not full.matches([{**raw, "subject": bare}])
    assert raw == original
