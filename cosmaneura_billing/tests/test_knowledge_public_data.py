"""Bundled public-data knowledge: POS codes, HCPCS Level II, current-FY ICD-10."""
from cosmaneura_billing.knowledge.hcpcs import HCPCSLevelII
from cosmaneura_billing.knowledge.icd10_ccsr import ICD10CCSR
from cosmaneura_billing.knowledge.pos import PlaceOfService


# ---- place of service ------------------------------------------------------- #
def test_pos_loads_full_code_set():
    pos = PlaceOfService()
    assert pos.size >= 40
    assert "telehealth" in pos.get("02").lower()
    assert pos.get("11").lower().startswith("office")


def test_pos_telehealth_detection():
    pos = PlaceOfService()
    assert pos.is_telehealth("02") and pos.is_telehealth("10")
    assert pos.is_telehealth("2")          # unpadded input
    assert not pos.is_telehealth("11")


# ---- HCPCS Level II --------------------------------------------------------- #
def test_hcpcs_loads_and_looks_up():
    h = HCPCSLevelII()
    assert h.size > 5000
    assert h.get("A1") is not None                    # dressing codes from the file head
    assert h.get("G0438") is not None                 # Medicare AWV, initial
    assert h.get("NOPE99") is None


# ---- current-FY ICD-10-CM overlay ------------------------------------------- #
def test_current_fy_overlay_loaded_by_default():
    kb = ICD10CCSR()
    assert kb.has_current_fy
    assert kb.is_active("K62.5") is True
    assert kb.is_active("A00") is False       # 3-char category header, not billable
    assert kb.is_active("Z9999ZZ") is False   # nonexistent


def test_overlay_disabled_returns_none():
    kb = ICD10CCSR(current_csv="")
    assert not kb.has_current_fy
    assert kb.is_active("K62.5") is None


def test_get_falls_back_to_current_fy_for_codes_newer_than_ccsr():
    kb = ICD10CCSR()
    # find a billable current-FY code missing from the CCSR map (new since 2023)
    newer = next((raw for raw, (billable, _) in kb._current.items()
                  if billable and raw not in kb._by_raw), None)
    if newer is None:  # CCSR map covers everything — nothing to assert
        return
    hit = kb.get(newer)
    assert hit is not None and hit.description
    assert hit.ccsr_category == ""
