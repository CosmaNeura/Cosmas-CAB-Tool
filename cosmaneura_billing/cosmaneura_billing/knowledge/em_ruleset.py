"""
Office-visit E/M leveling (AMA 2021+ rules), implemented transparently.

We model Medical Decision Making (MDM) as the standard 2-of-3 grid across:
  (1) number & complexity of problems addressed,
  (2) amount & complexity of data reviewed/ordered,
  (3) risk of complications / morbidity from management.
Level = the highest level met by at least 2 of the 3 elements.

Why rule-based and not "ask the LLM for a level": the level must be explainable
and defensible to a payer or auditor. The LLM extracts the *facts* (problems,
data, risk); this module turns facts into a level by published rules, and shows
its work. That separation is what makes an over-level impossible to "argue" the
model into.

CPT codes are referenced by number with short generic functional labels only.
The AMA's official long descriptors are licensed content and must come from the
practice's own AMA CPT license, not from this file.
"""
from __future__ import annotations

from dataclasses import dataclass

# element level ordering
_ORDER = {"straightforward": 1, "low": 2, "moderate": 3, "high": 4}
_LEVEL_NAME = {1: "straightforward", 2: "low", 3: "moderate", 4: "high"}

# MDM level (1=straightforward, 2=low, 3=moderate, 4=high) -> CPT office-visit code.
# 99211 is the minimal/nurse-visit level and sits below straightforward; it is not
# produced by the MDM grid. New-patient 99201 was deleted in 2021.
_ESTABLISHED = {1: "99212", 2: "99213", 3: "99214", 4: "99215"}
_NEW = {1: "99202", 2: "99203", 3: "99204", 4: "99205"}

# 2021+ time thresholds (total minutes on the date of encounter), established pt
_TIME_ESTABLISHED = [(10, "99212"), (20, "99213"), (30, "99214"), (40, "99215")]
_TIME_NEW = [(15, "99202"), (30, "99203"), (45, "99204"), (60, "99205")]

_LABEL = {
    "99211": "Office visit, established pt, minimal",
    "99212": "Office visit, established pt, straightforward MDM",
    "99213": "Office visit, established pt, low MDM",
    "99214": "Office visit, established pt, moderate MDM",
    "99215": "Office visit, established pt, high MDM",
    "99202": "Office visit, new pt, straightforward MDM",
    "99203": "Office visit, new pt, low MDM",
    "99204": "Office visit, new pt, moderate MDM",
    "99205": "Office visit, new pt, high MDM",
}


@dataclass
class EMResult:
    code: str
    label: str
    mdm_level: str                  # straightforward | low | moderate | high
    elements: dict[str, str]        # the 3 elements and the level each reached
    met_by: list[str]               # which elements drove the level (the "2 of 3")
    explanation: str


def _grid_to_mdm(problems: str, data: str, risk: str) -> tuple[int, list[str]]:
    """Return (mdm numeric level, elements meeting that level)."""
    vals = {"problems": _ORDER[problems], "data": _ORDER[data], "risk": _ORDER[risk]}
    # find highest level reached by >=2 elements
    for lvl in (4, 3, 2, 1):
        meeting = [k for k, v in vals.items() if v >= lvl]
        if len(meeting) >= 2:
            return lvl, meeting
    return 1, list(vals.keys())


def level_office_visit(
    *,
    new_patient: bool,
    problems: str,
    data: str,
    risk: str,
    total_time_minutes: int | None = None,
) -> EMResult:
    """Level by MDM, then take the *higher-supported* of MDM-vs-time only if time
    is documented. We never let time silently inflate a level that MDM doesn't
    reach unless the time is explicitly recorded.
    """
    mdm_num, meeting = _grid_to_mdm(problems, data, risk)
    table = _NEW if new_patient else _ESTABLISHED
    mdm_code = table[mdm_num]

    chosen = mdm_code
    basis = f"MDM = {_LEVEL_NAME[mdm_num]} (met by {', '.join(meeting)})"

    if total_time_minutes is not None:
        time_table = _TIME_NEW if new_patient else _TIME_ESTABLISHED
        time_code = time_table[0][1]
        for threshold, code in time_table:
            if total_time_minutes >= threshold:
                time_code = code
        # take the higher of the two ONLY because time is documented
        if int(time_code[-1]) > int(chosen[-1]):
            chosen = time_code
            basis = f"Total time {total_time_minutes} min (documented) exceeds MDM level"

    return EMResult(
        code=chosen,
        label=_LABEL.get(chosen, chosen),
        mdm_level=_LEVEL_NAME[mdm_num],
        elements={"problems": problems, "data": data, "risk": risk},
        met_by=meeting,
        explanation=basis,
    )


def max_defensible_level(*, new_patient: bool, problems: str, data: str, risk: str) -> str:
    """The ceiling the guardrail enforces: a code may not exceed what MDM supports."""
    mdm_num, _ = _grid_to_mdm(problems, data, risk)
    return (_NEW if new_patient else _ESTABLISHED)[mdm_num]
