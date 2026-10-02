"""Adaptive preference layer — coder corrections re-enter the engine, safely.

Every accept / edit / reject a reviewer makes (coder_feedback.py) is a data
point. This module turns the accumulated feedback into *runtime* adjustments,
applied per user and — with a higher bar — practice-wide, under hard
compliance constraints:

  * A learned adjustment can HOLD a code for review or REFINE it to a more
    specific code in the same ICD-10 family. It can NEVER add a code, raise
    an E/M level, or promote a disposition. The direction of learned travel
    is the same as the guardrail's: conservative.
  * Every applied preference is written to the audit trail with its
    provenance ("declined 3x by this reviewer: not supported by
    documentation"), so a payer/auditor can see exactly why the engine
    deviated from its base behavior.
  * Preferences are derived deterministically from the append-only feedback
    log — rebuild the log, get the same engine. E/M-leveling disagreements
    are surfaced as notes and tuning-backlog items only; leveling rules
    change through versioned releases (see VALIDATION_PROTOCOL.md §13),
    never silently at runtime.

What is learned, and when:

  SUPPRESSION  reviewer rejected code X with reason "not supported" /
               "contradicted" at least MIN_COUNT times, and has rejected it
               more often than accepted it -> future proposals of X for that
               reviewer are HELD for review (not dropped), with the history
               attached.
  REFINEMENT   reviewer recoded X -> Y (same 3-character ICD-10 category)
               with reason "needs a more specific code" at least MIN_COUNT
               times -> the engine proposes Y directly, keeping the original
               evidence, and says why.
  E/M NOTES    repeated "documentation supports a lower/higher level"
               feedback attaches an informational note to E/M suggestions;
               the level itself never moves in response to feedback.

Practice-wide ("everyone benefits") versions of SUPPRESSION and REFINEMENT
require the same signal from at least GLOBAL_MIN_USERS distinct reviewers.

Thresholds are env-tunable (COSMANEURA_PREF_MIN, COSMANEURA_PREF_GLOBAL_USERS;
COSMANEURA_PREFS=0 disables the layer entirely).
"""
from __future__ import annotations

import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .coder_feedback import Action, FeedbackStore, Reason
from .schemas import CodeKind, CodeSuggestion
from .settings import Settings

_SUPPRESS_REASONS = {Reason.DX_NOT_SUPPORTED, Reason.DX_CONTRADICTED}

# Human-readable labels for audit lines / UI hints.
_REASON_LABEL = {
    Reason.DX_NOT_SUPPORTED: "not supported by documentation",
    Reason.DX_CONTRADICTED: "contradicted by documentation",
    Reason.DX_LOW_SPECIFICITY: "needs a more specific code",
}


def _norm(code: str | None) -> str:
    return (code or "").replace(".", "").strip().upper()


def _family(code: str | None) -> str:
    return _norm(code)[:3]


@dataclass
class Suppression:
    code: str                 # engine-format code, e.g. "K59.00"
    count: int
    top_reason: str
    scope: str                # "you" | "practice"

    def hint(self) -> str:
        who = "you have" if self.scope == "you" else "reviewers at this practice have"
        return f"{who} declined {self.code} {self.count}x ({_REASON_LABEL.get(self.top_reason, self.top_reason)})"


@dataclass
class Refinement:
    from_code: str
    to_code: str
    count: int
    scope: str

    def hint(self) -> str:
        who = "your" if self.scope == "you" else "this practice's"
        return f"recoded per {who} correction history ({self.from_code} -> {self.to_code}, {self.count}x)"


@dataclass
class Preferences:
    """Resolved adjustments for one user (their own signals + practice-wide)."""
    suppress: dict[str, Suppression] = field(default_factory=dict)   # key: _norm(code)
    refine: dict[str, Refinement] = field(default_factory=dict)      # key: _norm(from_code)
    em_notes: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.suppress or self.refine or self.em_notes)


class PreferenceStore:
    """Derive conservative preferences from durable coder feedback.

    Local callers cache on file metadata. PostgreSQL callers inject a feedback
    source whose monotonic revision is shared by every API replica.
    """

    def __init__(self, path: str | os.PathLike | None = None,
                 min_count: int | None = None, global_min_users: int | None = None,
                 settings: Settings | None = None, feedback_store=None):
        self._path_arg = path
        self._min_count_arg = min_count
        self._global_min_users_arg = global_min_users
        self._settings = settings
        self._feedback_store = feedback_store
        self._cache_key: tuple | None = None
        self._derived: dict | None = None

    def _config(self) -> Settings:
        # Injected deployment settings stay fixed. CLI/test callers without an
        # injection retain lazy environment resolution.
        return self._settings or Settings.from_env()

    @property
    def path(self) -> Path:
        return Path(self._path_arg or self._config().feedback_path)

    @property
    def min_count(self) -> int:
        return self._min_count_arg if self._min_count_arg is not None else \
            self._config().preference_min_count

    @property
    def global_min_users(self) -> int:
        return self._global_min_users_arg if self._global_min_users_arg is not None else \
            self._config().preference_global_min_users

    # ---- derivation ------------------------------------------------------ #
    def _load(self) -> dict:
        if self._feedback_store is not None:
            key = ("feedback-store", self._feedback_store.revision())
            if self._derived is not None and key == self._cache_key:
                return self._derived
            feedbacks = self._feedback_store.load()
            self._cache_key = key
            self._derived = self._derive(feedbacks)
            return self._derived
        p = self.path
        key = (str(p), p.stat().st_mtime if p.exists() else -1.0)
        if self._derived is not None and key == self._cache_key:
            return self._derived
        self._cache_key = key
        self._derived = self._derive(FeedbackStore(p).load())
        return self._derived

    def _derive(self, feedbacks) -> dict:
        # per (user, code): counters
        rejects: dict = defaultdict(lambda: defaultdict(Counter))   # user -> code -> Counter(reason)
        accepts: dict = defaultdict(Counter)                        # user -> code -> n
        remaps: dict = defaultdict(lambda: defaultdict(Counter))    # user -> from -> Counter(to)
        em_low: Counter = Counter()                                 # user -> n
        em_high: Counter = Counter()
        code_display: dict[str, str] = {}

        for fb in feedbacks:
            user = fb.coder_id or "unknown"
            for d in fb.decisions:
                if d.kind != "ICD10" or not d.engine_code:
                    if d.kind == "EM":
                        if d.reason_code == Reason.EM_SUPPORTS_LOWER:
                            em_low[user] += 1
                        elif d.reason_code == Reason.EM_SUPPORTS_HIGHER:
                            em_high[user] += 1
                    continue
                key = _norm(d.engine_code)
                code_display[key] = d.engine_code
                if d.action == Action.REJECT and d.reason_code in _SUPPRESS_REASONS:
                    rejects[user][key][d.reason_code] += 1
                elif d.action == Action.ACCEPT:
                    accepts[user][key] += 1
                elif (d.action == Action.MODIFY and d.final_code
                      and d.reason_code == Reason.DX_LOW_SPECIFICITY
                      and _family(d.final_code) == _family(d.engine_code)
                      and _norm(d.final_code) != key):
                    remaps[user][key][_norm(d.final_code)] += 1
                    code_display[_norm(d.final_code)] = d.final_code
        return {"rejects": rejects, "accepts": accepts, "remaps": remaps,
                "em_low": em_low, "em_high": em_high, "display": code_display}

    # ---- resolution ------------------------------------------------------ #
    def for_user(self, user_id: str | None) -> Preferences:
        if not self._config().preferences_enabled:
            return Preferences()
        d = self._load()
        prefs = Preferences()
        disp = d["display"]

        def add_suppression(key, count, reason, scope):
            if key not in prefs.suppress:
                prefs.suppress[key] = Suppression(
                    code=disp.get(key, key), count=count, top_reason=reason, scope=scope)

        def add_refinement(key, target, count, scope):
            if key not in prefs.refine and key not in prefs.suppress:
                prefs.refine[key] = Refinement(
                    from_code=disp.get(key, key), to_code=disp.get(target, target),
                    count=count, scope=scope)

        # user-scoped signals first (they win over practice-wide)
        if user_id:
            for key, reasons in d["rejects"].get(user_id, {}).items():
                n = sum(reasons.values())
                if n >= self.min_count and n > d["accepts"][user_id][key]:
                    add_suppression(key, n, reasons.most_common(1)[0][0], "you")
            for key, targets in d["remaps"].get(user_id, {}).items():
                target, n = targets.most_common(1)[0]
                if n >= self.min_count:
                    add_refinement(key, target, n, "you")
            if d["em_low"].get(user_id, 0) >= self.min_count:
                prefs.em_notes.append(
                    f"you have re-leveled E/M lower {d['em_low'][user_id]}x - "
                    "leveling rules only change via versioned releases; this is on the tuning backlog")
            if d["em_high"].get(user_id, 0) >= self.min_count:
                prefs.em_notes.append(
                    f"you have re-leveled E/M higher {d['em_high'][user_id]}x - "
                    "raising a level requires the documented MDM to support it; on the tuning backlog")

        # practice-wide: same signal from >= global_min_users distinct reviewers
        pop_rejects: dict = defaultdict(lambda: {"n": 0, "users": set(), "reasons": Counter()})
        pop_accepts: Counter = Counter()
        for user, per_code in d["rejects"].items():
            for key, reasons in per_code.items():
                pop_rejects[key]["n"] += sum(reasons.values())
                pop_rejects[key]["users"].add(user)
                pop_rejects[key]["reasons"].update(reasons)
        for user, per_code in d["accepts"].items():
            for key, n in per_code.items():
                pop_accepts[key] += n
        for key, agg in pop_rejects.items():
            if (len(agg["users"]) >= self.global_min_users
                    and agg["n"] > pop_accepts[key]):
                add_suppression(key, agg["n"], agg["reasons"].most_common(1)[0][0], "practice")

        pop_remaps: dict = defaultdict(lambda: defaultdict(lambda: {"n": 0, "users": set()}))
        for user, per_code in d["remaps"].items():
            for key, targets in per_code.items():
                for target, n in targets.items():
                    pop_remaps[key][target]["n"] += n
                    pop_remaps[key][target]["users"].add(user)
        for key, targets in pop_remaps.items():
            best, agg = max(targets.items(), key=lambda kv: kv[1]["n"])
            if len(agg["users"]) >= self.global_min_users:
                add_refinement(key, best, agg["n"], "practice")

        return prefs


# --------------------------------------------------------------------------- #
# Application — runs before the guardrail; can only hold, refine, or annotate
# --------------------------------------------------------------------------- #
def apply_preferences(suggestions: list[CodeSuggestion], prefs: Preferences,
                      icd10=None) -> list[str]:
    """Apply resolved preferences in place. Returns audit lines.

    Structural guarantees: iterates only existing suggestions (never adds);
    touches only ICD-10 codes for hold/refine (E/M gets notes at most);
    learned refinements are review hints, not automatic code replacements.
    Sharing a three-character family does not prove the same diagnosis or
    complication. A reviewer must apply a correction for this encounter.
    """
    audit: list[str] = []
    if prefs.empty:
        return audit

    for s in suggestions:
        if s.kind == CodeKind.EM:
            for note in prefs.em_notes:
                s.notes.append(f"reviewer history: {note}")
            continue
        if s.kind != CodeKind.ICD10:
            continue

        key = _norm(s.code)
        ref = prefs.refine.get(key)
        if ref and _family(ref.to_code) == _family(s.code):
            s.notes.append("preference_hold: Prior reviewer corrections require "
                           "a fresh diagnosis review for this encounter.")
            audit.append(f"HOLD    {s.code}: learned refinement requires current "
                         "encounter review; original code and evidence retained.")

        sup = prefs.suppress.get(key)
        if sup:
            s.notes.append(f"preference_hold: {sup.hint()}")
            audit.append(f"HOLD    {s.code} ({s.label}): {sup.hint()}; "
                         "held for review, not auto-dropped.")
    return audit
