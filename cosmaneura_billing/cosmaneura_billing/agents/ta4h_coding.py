"""Secondary coding agent: Azure Text Analytics for health.

Returns the same ``(facts, suggestions)`` pair as ``CodingAgent`` so the
consensus agent can compare them and the rest of the pipeline is untouched.

What this agent does and deliberately does not do
-------------------------------------------------
DOES  propose ICD-10-CM candidates from TA4H's UMLS entity links, validated
      against our own active code set, with evidence quotes sliced straight out
      of the documentation by character offset (so they verify by construction);
DOES  report which findings TA4H reads as negated, hypothetical, or belonging to
      a family member, which the consensus agent uses to *reject* codes;
DOES  report affirmed findings the primary engine missed, as review candidates;

DOES NOT level E/M. TA4H has no notion of MDM, so this agent never returns an
      E/M suggestion and its facts never feed the leveling grid. The E/M ceiling
      is computed from the primary engine's facts alone (see ``consensus.py``) —
      that is what keeps the no-upcode invariant intact with two engines running.
"""
from __future__ import annotations

from ..knowledge.icd10_ccsr import ICD10CCSR
from ..knowledge.diagnosis_terms import anchor_context
from ..schemas import CodeKind, CodeSuggestion, Disposition, Evidence, VisitBundle
from ..ta4h import (
    DATA_CATEGORIES, DIAGNOSIS_CATEGORIES, HealthEntity, TA4HClient,
)

# Documents we send, in the order their evidence is preferred. Keys are the
# VisitBundle field names so an entity offset always has a known source text.
_SOURCES = ("signed_note", "transcript", "intake_text")


def _norm(code: str) -> str:
    return code.replace(".", "").strip().upper()


class TA4HCodingAgent:
    def __init__(self, client: TA4HClient, icd10: ICD10CCSR):
        self.client = client
        self.icd10 = icd10

    # ---- text in, entities out ------------------------------------------- #
    def _documents(self, bundle: VisitBundle) -> dict[str, str]:
        return {name: getattr(bundle, name, "") or "" for name in _SOURCES}

    def run(self, bundle: VisitBundle) -> tuple[dict, list[CodeSuggestion]]:
        docs = self._documents(bundle)
        by_doc = self.client.analyze(docs)

        entities: list[HealthEntity] = []
        for source in _SOURCES:                       # deterministic order
            entities.extend(by_doc.get(source, []))

        suggestions: list[CodeSuggestion] = []
        # Contradictions are keyed by normalized ICD-10 so the consensus agent
        # can apply them to whichever engine proposed the code.
        contradictions: dict[str, str] = {}
        seen: set[str] = set()
        data_items: list[dict] = []
        problems: list[dict] = []

        for ent in entities:
            quote = self._quote(docs, ent)

            if ent.category in DATA_CATEGORIES and ent.affirmed_for_patient:
                data_items.append({"item": ent.text, "category": f"ta4h:{ent.category}",
                                   "quote": quote})
                continue

            if ent.category not in DIAGNOSIS_CATEGORIES:
                continue

            code, how = self._resolve(ent)
            if not code:
                continue

            reason = self._contradiction_reason(ent)
            if reason:
                # Never a candidate; recorded so the consensus agent can reject
                # the same code if the primary engine proposed it.
                contradictions.setdefault(_norm(code.code), reason)
                if _norm(code.code) in seen:
                    continue
                seen.add(_norm(code.code))
                suggestions.append(CodeSuggestion(
                    kind=CodeKind.ICD10, code=code.code, label=code.description,
                    disposition=Disposition.REJECTED,
                    rationale=f"Azure Text Analytics for health: {reason}.",
                    source_engine="ta4h",
                    notes=[f"ta4h_assertion:{reason}"],
                ))
                continue

            # A UMLS link is a candidate, not a semantic evidence bypass.
            # Preserve the negative-assertion path above; affirmed candidates
            # must agree with the same source-context boundary as the primary
            # extractor, including a negation omitted from an entity span.
            context = anchor_context(quote, docs.get(ent.source, ""))
            if context is None or not self.icd10.supports(code.code, context):
                continue

            if _norm(code.code) in seen:
                continue
            seen.add(_norm(code.code))
            problems.append({"name": code.description, "quote": quote,
                             "ta4h_category": ent.category})
            suggestions.append(CodeSuggestion(
                kind=CodeKind.ICD10, code=code.code, label=code.description,
                disposition=Disposition.REVIEW,
                evidence=[Evidence(ent.source, quote,
                                   f"TA4H {ent.category} (confidence {ent.confidence:.2f})")]
                if quote else [],
                rationale=self._rationale(ent, code, how),
                source_engine="ta4h",
                notes=[f"ta4h_confidence:{ent.confidence:.2f}", f"ta4h_resolved_by:{how}"]
                + ([f"ta4h_uncertain:{ent.certainty}"] if ent.uncertain else []),
            ))

        facts = {
            # Deliberately NOT the leveling keys ("problems", "data_reviewed",
            # "risk"): those names are what _facts_to_em reads, and TA4H must
            # never be able to raise the E/M ceiling. Namespaced instead.
            "ta4h_problems": problems,
            "ta4h_data_reviewed": data_items,
            "ta4h_contradictions": contradictions,
            "ta4h_entity_count": len(entities),
        }
        return facts, suggestions

    # ---- helpers ---------------------------------------------------------- #
    @staticmethod
    def _quote(docs: dict[str, str], ent: HealthEntity, *, pad: int = 40) -> str:
        """The entity's span widened to sentence-ish context, taken verbatim from
        the source document. Character offsets mean this is the real text, so
        ``evidence.py`` verifies it exactly rather than fuzzily."""
        text = docs.get(ent.source, "")
        if not text:
            return ""
        start = max(0, ent.offset - pad)
        end = min(len(text), ent.offset + ent.length + pad)
        # trim to whitespace boundaries so we don't slice words in half
        if start > 0:
            space = text.find(" ", start)
            start = space + 1 if 0 <= space < ent.offset else start
        if end < len(text):
            space = text.rfind(" ", ent.offset + ent.length, end)
            end = space if space > ent.offset + ent.length else end
        return text[start:end].strip()

    @staticmethod
    def _contradiction_reason(ent: HealthEntity) -> str:
        if ent.negated:
            return "documentation states this finding is absent/denied"
        if ent.about_someone_else:
            return "finding is attributed to a family member, not the patient"
        if ent.conditional:
            return f"finding is {ent.conditionality}, not an active diagnosis"
        return ""

    @staticmethod
    def _rationale(ent: HealthEntity, code, how: str) -> str:
        """Say which resolution path produced the code.

        This lands in the audit trail, so it must not claim TA4H linked a code
        when the code actually came from our own descriptor matcher — those are
        very different strengths of evidence for a reviewer or an auditor.
        """
        name = f" ({ent.name})" if ent.name else ""
        if how == "umls_link":
            return (f"Azure Text Analytics for health linked \"{ent.text}\"{name} to "
                    f"{code.code} through its UMLS ICD-10-CM mapping.")
        return (f"Azure Text Analytics for health identified \"{ent.text}\"{name} as a "
                f"{ent.category} but returned no ICD-10-CM link; {code.code} is our own "
                "descriptor match for it and needs confirmation.")

    def _resolve(self, ent: HealthEntity) -> tuple[object | None, str]:
        """TA4H's ICD-10-CM link, validated against our active code set.

        The link is a *candidate*: our own current-FY overlay decides whether it
        is billable today, and a code we don't recognise is dropped rather than
        trusted. Falls back to the descriptor matcher when TA4H linked to UMLS
        but not to ICD-10-CM — the returned tag says which path was taken so the
        audit trail can be honest about it.
        """
        linked = ent.icd10
        if linked:
            hit = self.icd10.get(linked)
            if hit is not None and self.icd10.is_active(hit.code) is True:
                return hit, "umls_link"
        for candidate in self.icd10.find_by_description(ent.name or ent.text, limit=1):
            if self.icd10.is_active(candidate.code) is True:
                return candidate, "descriptor_match"
        return None, ""
