"""Consensus agent: reconciles the two extraction engines.

The merge policy, and why it is shaped this way
-----------------------------------------------
Running a second engine must not be able to loosen anything. Three rules, all of
them one-directional:

1. **The E/M ceiling comes from the primary engine's facts, full stop.** TA4H
   has no MDM model, so its facts are namespaced (``ta4h_*``) and never land on
   the keys the leveling grid reads. ``merge_facts`` then *verifies* that the
   merged facts compute the same ceiling as the primary facts alone, and falls
   back to the primary facts if they ever don't. A second engine therefore
   cannot raise a level, which is exactly what ``prove_no_upcode.py`` asserts.

2. **A code only one engine found is held for review, never captured.** It rides
   in with a ``consensus_hold`` note, which the compliance guardrail honours the
   same way it honours a learned reviewer hold. Agreement is what buys a code
   the right to be *considered* for capture — the guardrail still has to agree.

3. **Either engine may veto.** If TA4H reads a finding as negated, hypothetical,
   or belonging to a family member, the matching code is rejected even if the
   primary engine proposed it confidently. Rejection is always allowed; that is
   the direction of travel this whole system is built to permit.

Two consequences of those rules that are easy to get wrong, and that
``consensus_fuzz`` exists to keep honest:

  * the secondary engine's suggestions are filtered to diagnoses before anything
    downstream sees them. The guardrail's E/M branch deliberately does not
    consult consensus holds, so an out-of-contract E/M code from a buggy or
    hostile secondary engine would be promoted on its own evidence;
  * the secondary engine's evidence is additive only to a code that *already*
    has a span. Evidence is the guardrail's necessity gate, so letting the second
    engine supply the first span would convert a held code into a captured one.

The net effect: the ensemble can only ever be more conservative than the primary
engine alone, while still surfacing everything the second engine noticed.
"""
from __future__ import annotations

from .coding import _facts_to_em
from ..schemas import CodeKind, CodeSuggestion, Disposition

# Rank for "which disposition wins when both engines have an opinion" —
# the more conservative one always does.
_CONSERVATISM = {Disposition.REJECTED: 2, Disposition.REVIEW: 1, Disposition.CAPTURED: 0}


def _norm(code: str) -> str:
    return code.replace(".", "").strip().upper()


def merge_facts(primary: dict, secondary: dict, *, new_patient: bool) -> tuple[dict, list[str]]:
    """Primary facts, plus the secondary engine's namespaced observations.

    Guaranteed not to change the E/M ceiling. The check is explicit rather than
    merely argued, so a future edit that accidentally writes a leveling key from
    secondary output is caught here instead of in a claim.
    """
    audit: list[str] = []
    merged = dict(primary)
    for key, value in (secondary or {}).items():
        if not key.startswith("ta4h_"):
            # Defence in depth: a non-namespaced key from the secondary engine
            # could touch leveling inputs, so it is dropped and reported.
            audit.append(f"CONSENSUS ignored non-namespaced secondary fact '{key}'.")
            continue
        merged[key] = value

    before = _facts_to_em(primary, new_patient=new_patient).code
    after = _facts_to_em(merged, new_patient=new_patient).code
    if before != after:
        audit.append(
            f"CONSENSUS E/M ceiling would have moved {before} -> {after} on merged facts; "
            "reverted to primary-engine facts for leveling."
        )
        merged = dict(primary)
    return merged, audit


def merge_suggestions(
    primary: list[CodeSuggestion],
    secondary: list[CodeSuggestion],
    *,
    contradictions: dict[str, str] | None = None,
) -> tuple[list[CodeSuggestion], list[str]]:
    """Union the two engines' codes, tagging provenance and agreement."""
    audit: list[str] = []
    contradictions = contradictions or {}
    # The secondary engine's contract is diagnoses only: it has no MDM model and
    # no procedure rules. Anything else it emits is out of contract and is
    # dropped here rather than passed downstream — the guardrail's E/M branch
    # deliberately does not consult consensus holds, so an out-of-contract E/M
    # code reaching it would be promoted on its own evidence.
    out_of_contract = [s for s in secondary if s.kind != CodeKind.ICD10]
    for s in out_of_contract:
        audit.append(
            f"CONSENSUS-DROPPED {s.code} ({s.kind.value}) — the secondary extraction "
            "engine proposes diagnoses only.")
    secondary = [s for s in secondary if s.kind == CodeKind.ICD10]
    by_secondary = {_norm(s.code): s for s in secondary if s.code and s.code != "(unresolved)"}
    matched: set[str] = set()
    out: list[CodeSuggestion] = []

    for s in primary:
        # TA4H never proposes E/M, so an E/M code is primary-only by definition
        # and is left exactly as it was — no agreement tag, no hold.
        if s.kind != CodeKind.ICD10:
            s.source_engine = s.source_engine or "primary"
            out.append(s)
            continue

        key = _norm(s.code)
        veto = contradictions.get(key)
        twin = by_secondary.get(key)
        if twin is not None:
            matched.add(key)

        if veto:
            s.disposition = Disposition.REJECTED
            s.source_engine = "both" if twin is not None else "primary"
            s.agreement = "contradicted_by_ta4h"
            s.rationale = (s.rationale + " " if s.rationale else "") + \
                f"Rejected on consensus: Azure Text Analytics for health reads this as {veto}."
            s.notes.append(f"ta4h_veto:{veto}")
            audit.append(f"CONSENSUS-VETO {s.code} ({s.label}): {veto}.")
            out.append(s)
            continue

        if twin is None:
            s.source_engine = "primary"
            s.agreement = "primary_only"
            s.notes.append("consensus_hold: only the primary engine proposed this code")
            audit.append(
                f"CONSENSUS {s.code} ({s.label}): primary engine only — held for review.")
            out.append(s)
            continue

        s.source_engine = "both"
        s.agreement = "both"
        # Adopt the more conservative disposition of the two, carrying the reason
        # with it — an audit line that says a code was rejected has to say why,
        # and the "why" lives on whichever engine made the call.
        if _CONSERVATISM[twin.disposition] > _CONSERVATISM[s.disposition]:
            s.disposition = twin.disposition
            if twin.rationale:
                s.rationale = (s.rationale + " " if s.rationale else "") + twin.rationale
        # Keep the offset-anchored TA4H evidence alongside the primary quote:
        # two independent pointers into the chart for the same code.
        #
        # Only ever ADDITIVE to a code that already has evidence. If the primary
        # engine found no span, the secondary engine's span must not supply the
        # first one: evidence is the guardrail's necessity gate, so that would
        # turn a held code into a captured one and break the promise that adding
        # an engine can only be more conservative.
        if s.evidence:
            for ev in twin.evidence:
                if all(ev.quote != existing.quote for existing in s.evidence):
                    s.evidence.append(ev)
        elif twin.evidence:
            s.notes.append("ta4h_evidence_withheld: primary engine found no span")
            audit.append(
                f"CONSENSUS {s.code} ({s.label}): second engine found a span the "
                "primary engine did not; held rather than captured on it.")
        s.notes.extend(n for n in twin.notes if n.startswith("ta4h_"))
        audit.append(f"CONSENSUS {s.code} ({s.label}): both engines agree.")
        out.append(s)

    # Codes only the secondary engine found — real value, but they have to be
    # confirmed by a human before they can reach a claim.
    for key, s in by_secondary.items():
        if key in matched:
            continue
        if s.disposition == Disposition.REJECTED:
            s.agreement = "ta4h_only"
            audit.append(f"CONSENSUS-VETO {s.code} ({s.label}): {s.rationale}")
            out.append(s)
            continue
        s.disposition = Disposition.REVIEW
        s.agreement = "ta4h_only"
        s.notes.append(
            "consensus_hold: found only by Azure Text Analytics for health — confirm before billing")
        audit.append(
            f"CONSENSUS {s.code} ({s.label}): second engine only — held for review.")
        out.append(s)

    return out, audit


class ConsensusAgent:
    """Combines two ``(facts, suggestions)`` pairs into one."""

    def run(
        self,
        primary: tuple[dict, list[CodeSuggestion]],
        secondary: tuple[dict, list[CodeSuggestion]],
        *,
        new_patient: bool,
    ) -> tuple[dict, list[CodeSuggestion], list[str]]:
        primary_facts, primary_codes = primary
        secondary_facts, secondary_codes = secondary

        facts, audit = merge_facts(primary_facts, secondary_facts, new_patient=new_patient)
        suggestions, merge_audit = merge_suggestions(
            primary_codes, secondary_codes,
            contradictions=secondary_facts.get("ta4h_contradictions") or {},
        )
        audit = audit + merge_audit

        # No ": " in this line on purpose — the API's audit parser treats a
        # colon as "everything before it names a code", and this line is a
        # summary, not a decision about one code.
        agreed = sum(1 for s in suggestions if s.agreement == "both")
        audit.insert(0, (
            f"CONSENSUS two extraction engines ran — {agreed} code(s) agreed, "
            f"{sum(1 for s in suggestions if s.agreement == 'primary_only')} primary-only, "
            f"{sum(1 for s in suggestions if s.agreement == 'ta4h_only')} secondary-only, "
            f"{sum(1 for s in suggestions if s.agreement == 'contradicted_by_ta4h')} vetoed."
        ))
        return facts, suggestions, audit
