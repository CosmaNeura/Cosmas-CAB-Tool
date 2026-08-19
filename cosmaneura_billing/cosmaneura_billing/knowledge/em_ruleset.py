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
    # Which of the two 2021+ pathways produced `code`. The provider billing tab
    # shows this, and a coder re-levelling on time needs to know which basis the
    # engine used. "mdm_2of3" | "total_time"
    pathway: str = "mdm_2of3"
    # The MDM-only code, kept even when time wins, so the difference between the
    # two pathways stays visible instead of being collapsed into one number.
    mdm_code: str = ""
    # Documented total time, when it was both present AND evidenced.
    total_time_minutes: int | None = None


def _grid_to_mdm(problems: str, data: str, risk: str) -> tuple[int, list[str]]:
    """Return (mdm numeric level, elements meeting that level)."""
    vals = {"problems": _ORDER[problems], "data": _ORDER[data], "risk": _ORDER[risk]}
    # find highest level reached by >=2 elements
    for lvl in (4, 3, 2, 1):
        meeting = [k for k, v in vals.items() if v >= lvl]
        if len(meeting) >= 2:
            return lvl, meeting
    return 1, list(vals.keys())


def level_by_time(*, new_patient: bool, total_time_minutes: int) -> str | None:
    """The office-visit code supported by documented total time, or None if the
    time is below the lowest threshold (or nonsensical)."""
    if not isinstance(total_time_minutes, int) or isinstance(total_time_minutes, bool):
        return None
    if total_time_minutes <= 0:
        return None
    table = _TIME_NEW if new_patient else _TIME_ESTABLISHED
    supported = [code for threshold, code in table if total_time_minutes >= threshold]
    return supported[-1] if supported else None


def level_office_visit(
    *,
    new_patient: bool,
    problems: str,
    data: str,
    risk: str,
    total_time_minutes: int | None = None,
    time_is_evidenced: bool = False,
) -> EMResult:
    """Level by MDM, then by total time when the time is documented *and evidenced*.

    2021+ E/M allows either pathway, and the higher supported one is legitimate.
    ``time_is_evidenced`` is the guard that makes the time pathway safe: a bare
    integer from an extraction model is not documentation. Without a verified
    quote stating the time, the time pathway is not applied at all — the level
    falls back to MDM. This fails closed, so the only way time can raise a level
    is if a human can read the minutes in the chart.
    """
    mdm_num, meeting = _grid_to_mdm(problems, data, risk)
    table = _NEW if new_patient else _ESTABLISHED
    mdm_code = table[mdm_num]

    chosen, pathway = mdm_code, "mdm_2of3"
    basis = f"MDM = {_LEVEL_NAME[mdm_num]} (met by {', '.join(meeting)})"
    minutes = total_time_minutes if time_is_evidenced else None

    if minutes is not None:
        time_code = level_by_time(new_patient=new_patient, total_time_minutes=minutes)
        if time_code and int(time_code[-1]) > int(chosen[-1]):
            chosen, pathway = time_code, "total_time"
            basis = (f"Total time {minutes} min documented on the date of encounter "
                     f"supports {time_code}, above the {mdm_code} that MDM alone "
                     f"supports (MDM = {_LEVEL_NAME[mdm_num]}).")

    return EMResult(
        code=chosen,
        label=_LABEL.get(chosen, chosen),
        mdm_level=_LEVEL_NAME[mdm_num],
        elements={"problems": problems, "data": data, "risk": risk},
        met_by=meeting,
        explanation=basis,
        pathway=pathway,
        mdm_code=mdm_code,
        total_time_minutes=minutes,
    )


def max_defensible_level(
    *, new_patient: bool, problems: str, data: str, risk: str,
    total_time_minutes: int | None = None, time_is_evidenced: bool = False,
) -> str:
    """The ceiling the guardrail enforces.

    Accepts the time pathway on the same terms as ``level_office_visit``: a code
    may not exceed what the documentation supports by *either* route. Called
    without time arguments it is the MDM-only ceiling, which is what the coding
    agent reports as the "MDM ceiling" note.
    """
    mdm_num, _ = _grid_to_mdm(problems, data, risk)
    table = _NEW if new_patient else _ESTABLISHED
    ceiling = table[mdm_num]
    if time_is_evidenced and total_time_minutes:
        time_code = level_by_time(
            new_patient=new_patient, total_time_minutes=total_time_minutes)
        if time_code and int(time_code[-1]) > int(ceiling[-1]):
            return time_code
    return ceiling
