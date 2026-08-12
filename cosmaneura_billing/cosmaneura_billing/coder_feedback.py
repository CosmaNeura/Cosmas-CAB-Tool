"""
Coder feedback loop -- the mechanism by which the contract coders who review
claims make the engine better, while staying in control of every claim.

Design goal (stated by the team): the tool does not replace the coder. It
pre-codes the encounter with evidence so the coder reviews faster, explains *why*
each code was chosen so the coder is upskilled, and captures the coder's
accept / modify / reject decisions so those corrections re-enter the system.

"Integrated into the model easily" for a rule + LLM system means three concrete
things, all produced here from the captured feedback:
  1. a growing **gold set** (the coder's final codes) -> regression suite + the
     Phase-1 validation gold standard;
  2. ranked **tuning signals** -> a prioritized backlog of rule/prompt changes
     (e.g. "engine under-levels E/M when data element X", "coders keep adding
     modifier 25"); and
  3. **few-shot examples** drawn from corrected encounters for the extraction LLM.
It is calibration + prompt/rule tuning + regression data, not neural retraining --
which is the right, auditable way to change a billing engine.

Self-test:  python -m cosmaneura_billing.coder_feedback
"""
from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median


# Controlled vocabulary the review UI presents as dropdowns. Stored as strings.
class Action:
    ACCEPT = "accept"      # engine code is correct as-is
    MODIFY = "modify"      # coder changed the code/level
    REJECT = "reject"      # code should not be billed
    ADD = "add"            # coder added a code the engine missed


class Reason:
    EM_SUPPORTS_HIGHER = "em_doc_supports_higher_level"
    EM_SUPPORTS_LOWER = "em_doc_supports_lower_level"
    EM_MDM_DISAGREE = "em_mdm_element_disagreement"
    EM_TIME_BASED = "em_time_based_leveling"
    DX_NOT_SUPPORTED = "dx_not_supported_by_doc"
    DX_CONTRADICTED = "dx_contradicted_by_doc"
    DX_LOW_SPECIFICITY = "dx_needs_more_specific_code"
    DX_WRONG_PRIMARY = "dx_primary_secondary_swap"
    DX_MISSED = "dx_missed_by_engine"
    MOD_NEEDED = "modifier_required"
    PROC_BUNDLING = "procedure_bundling_ncci"
    COVERAGE = "medical_necessity_coverage"
    OTHER = "other"


@dataclass
class CodeDecision:
    kind: str                       # "EM" | "ICD10" | "PROCEDURE" | "MODIFIER"
    engine_code: str | None         # what the engine proposed (None when action == add)
    action: str                     # Action.*
    final_code: str | None = None   # coder's final code (None if rejected)
    reason_code: str | None = None  # Reason.*
    note: str = ""


@dataclass
class EncounterFeedback:
    encounter_id: str
    coder_id: str
    engine_version: str             # e.g. "cpt2025;icd10-FY2026;ncci-2026Q2;model=opus-4-8"
    submitted_em: str | None
    engine_em: str | None
    final_em: str | None
    decisions: list[CodeDecision]
    time_spent_seconds: float | None = None
    overall_note: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["decisions"] = [dec.__dict__ for dec in self.decisions]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "EncounterFeedback":
        decs = [CodeDecision(**x) for x in d.get("decisions", [])]
        return cls(**{**d, "decisions": decs})


class FeedbackStore:
    """Append-only JSONL sink. In production the same records can land in a
    Postgres table (Azure) instead -- DDL for reference:

        CREATE TABLE coder_feedback (
            id bigserial PRIMARY KEY,
            encounter_id text, coder_id text, engine_version text,
            payload jsonb NOT NULL, created_at timestamptz DEFAULT now());
    """

    def __init__(self, path: str | Path = "coder_feedback.jsonl"):
        self.path = Path(path)

    def append(self, fb: EncounterFeedback) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(fb.to_dict()) + "\n")

    def load(self) -> list[EncounterFeedback]:
        if not self.path.exists():
            return []
        return [EncounterFeedback.from_dict(json.loads(line))
                for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# Analysis: corrections -> metrics, tuning signals, gold set
# --------------------------------------------------------------------------- #
def _em_level(code: str | None) -> int:
    return int(code[-1]) if code and code[-1:].isdigit() else 0


def summarize(feedbacks: list[EncounterFeedback]) -> dict:
    n = len(feedbacks)
    all_decisions = [d for fb in feedbacks for d in fb.decisions]
    accepts = sum(1 for d in all_decisions if d.action == Action.ACCEPT)

    by_kind: dict[str, Counter] = {}
    for d in all_decisions:
        by_kind.setdefault(d.kind, Counter())[d.action] += 1

    em_exact = sum(1 for fb in feedbacks if fb.engine_em and fb.final_em and fb.engine_em == fb.final_em)
    em_within1 = sum(1 for fb in feedbacks
                     if fb.engine_em and fb.final_em and abs(_em_level(fb.engine_em) - _em_level(fb.final_em)) <= 1)
    em_over = sum(1 for fb in feedbacks   # engine proposed ABOVE the coder's final -> safety concern
                  if fb.engine_em and fb.final_em and _em_level(fb.engine_em) > _em_level(fb.final_em))
    em_under = sum(1 for fb in feedbacks
                   if fb.engine_em and fb.final_em and _em_level(fb.engine_em) < _em_level(fb.final_em))
    times = [fb.time_spent_seconds for fb in feedbacks if fb.time_spent_seconds]

    return {
        "encounters": n,
        "decisions": len(all_decisions),
        "acceptance_rate": round(accepts / len(all_decisions), 3) if all_decisions else None,
        "acceptance_by_kind": {k: round(c[Action.ACCEPT] / sum(c.values()), 3) for k, c in by_kind.items()},
        "em_exact_agreement": round(em_exact / n, 3) if n else None,
        "em_within_one": round(em_within1 / n, 3) if n else None,
        "em_engine_over_coded": em_over,            # the number that must trend to ~0
        "em_engine_under_coded": em_under,
        "median_seconds_per_encounter": round(median(times), 1) if times else None,
        "missed_dx_added_by_coder": sum(1 for d in all_decisions if d.kind == "ICD10" and d.action == Action.ADD),
    }


_SIGNAL_MAP = {
    Reason.EM_SUPPORTS_HIGHER: ("Engine UNDER-levels E/M; revisit MDM data/risk element mapping.", "rule"),
    Reason.EM_SUPPORTS_LOWER:  ("Engine OVER-levels E/M (SAFETY — investigate first); tighten MDM mapping.", "rule_safety"),
    Reason.EM_MDM_DISAGREE:    ("MDM element extraction disagreements; refine fact extraction prompt.", "prompt"),
    Reason.DX_NOT_SUPPORTED:   ("Engine captures dx without support; strengthen evidence gate.", "rule"),
    Reason.DX_CONTRADICTED:    ("Engine missed a contradiction; extend documentation-integrity checks.", "rule"),
    Reason.DX_LOW_SPECIFICITY: ("Specificity gaps; improve specificity prompting / code selection.", "prompt"),
    Reason.DX_WRONG_PRIMARY:   ("Primary/secondary ordering errors; refine primary-selection logic.", "rule"),
    Reason.DX_MISSED:          ("Diagnoses missed by the engine; add extraction coverage / few-shot examples.", "prompt"),
    Reason.MOD_NEEDED:         ("Missing modifiers; extend modifier logic (load NCCI for exact rules).", "rule"),
    Reason.PROC_BUNDLING:      ("Bundling errors; ensure NCCI PTP edits are loaded and applied.", "data"),
    Reason.COVERAGE:           ("Medical-necessity mismatches; load/refresh LCD coverage articles.", "data"),
}


def tuning_signals(feedbacks: list[EncounterFeedback]) -> list[dict]:
    """Rank reason codes into a concrete model/rule/prompt change backlog."""
    counts = Counter(d.reason_code for fb in feedbacks for d in fb.decisions
                     if d.action in (Action.MODIFY, Action.REJECT, Action.ADD) and d.reason_code)
    out = []
    for reason, n in counts.most_common():
        desc, kind = _SIGNAL_MAP.get(reason, (f"Review reason '{reason}'.", "review"))
        # commonly-missed/edited codes for this reason, to make the signal actionable
        codes = Counter(d.final_code or d.engine_code for fb in feedbacks for d in fb.decisions
                        if d.reason_code == reason and (d.final_code or d.engine_code))
        out.append({"reason": reason, "count": n, "change_type": kind,
                    "recommendation": desc, "top_codes": [c for c, _ in codes.most_common(5)]})
    return out


def export_gold(feedbacks: list[EncounterFeedback]) -> list[dict]:
    """Coder-final codes per encounter -> regression / validation gold rows."""
    gold = []
    for fb in feedbacks:
        dx = [d.final_code for d in fb.decisions
              if d.kind == "ICD10" and d.action != Action.REJECT and d.final_code]
        gold.append({"encounter_id": fb.encounter_id, "gold_em": fb.final_em, "gold_dx": dx,
                     "coder_id": fb.coder_id, "engine_version": fb.engine_version})
    return gold


def report(feedbacks: list[EncounterFeedback]) -> str:
    s = summarize(feedbacks)
    lines = ["CODER FEEDBACK SUMMARY", "=" * 40,
             f"encounters reviewed     : {s['encounters']}",
             f"suggestion acceptance   : {s['acceptance_rate']}  by kind {s['acceptance_by_kind']}",
             f"E/M exact / within-1    : {s['em_exact_agreement']} / {s['em_within_one']}",
             f"E/M engine over-coded   : {s['em_engine_over_coded']}  (target: 0)",
             f"E/M engine under-coded  : {s['em_engine_under_coded']}",
             f"median sec/encounter    : {s['median_seconds_per_encounter']}  (throughput / speed goal)",
             f"dx missed by engine     : {s['missed_dx_added_by_coder']}",
             "", "TUNING BACKLOG (ranked by frequency)", "-" * 40]
    for sig in tuning_signals(feedbacks):
        lines.append(f"[{sig['change_type']}] x{sig['count']}  {sig['recommendation']}  "
                     f"{('codes: ' + ', '.join(sig['top_codes'])) if sig['top_codes'] else ''}")
    return "\n".join(lines)


if __name__ == "__main__":
    # synthetic feedback from a few reviewed encounters
    fbs = [
        EncounterFeedback("ENC-1", "coder_A", "cpt2025;model=opus-4-8", "99213", "99214", "99214",
            [CodeDecision("EM", "99214", Action.ACCEPT, "99214"),
             CodeDecision("ICD10", "K62.5", Action.ACCEPT, "K62.5"),
             CodeDecision("ICD10", None, Action.ADD, "Z86.010", Reason.DX_MISSED, "personal hx polyps in note")],
            time_spent_seconds=95),
        EncounterFeedback("ENC-2", "coder_A", "cpt2025;model=opus-4-8", "99214", "99214", "99213",
            [CodeDecision("EM", "99214", Action.MODIFY, "99213", Reason.EM_SUPPORTS_LOWER, "data limited"),
             CodeDecision("ICD10", "R51.9", Action.MODIFY, "R51.0", Reason.DX_LOW_SPECIFICITY)],
            time_spent_seconds=140),
        EncounterFeedback("ENC-3", "coder_B", "cpt2025;model=opus-4-8", "99213", "99214", "99214",
            [CodeDecision("EM", "99214", Action.ACCEPT, "99214"),
             CodeDecision("ICD10", None, Action.ADD, "E66.9", Reason.DX_MISSED)],
            time_spent_seconds=80),
    ]
    store = FeedbackStore("/tmp/_fb_demo.jsonl")
    Path("/tmp/_fb_demo.jsonl").unlink(missing_ok=True)
    for fb in fbs:
        store.append(fb)
    loaded = store.load()
    assert len(loaded) == 3 and loaded[1].final_em == "99213"
    print(report(loaded))
    print("\nGOLD EXPORT:", export_gold(loaded)[:1])
    print("\nPASS — feedback captured, summarized, ranked into a tuning backlog, and exported as gold.")
