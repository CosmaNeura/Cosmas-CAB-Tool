"""Authored source fixtures for guardrail/consensus verification, never runtime data.

These use the real source qualifiers. They keep positive-path proofs meaningful
when hostile raw JSON alone correctly yields no qualified clinical elements.
"""
from copy import deepcopy
from functools import lru_cache

from ..schemas import VisitBundle


@lru_cache(maxsize=4)
def _case(level):
    from ..agents.coding import sanitize_facts
    from ..em_time import qualify_em_time
    from ..mdm_data import qualify_mdm_data
    from ..mdm_problems import qualify_problems

    if level not in (2, 3, 4, 5):
        raise ValueError("Unsupported synthetic case")
    if level == 2:
        problems = [("insect bite", "Insect bite: self-limited problem; I evaluated it and discussed care.")]
    elif level == 3:
        problems = [("hypertension", "Hypertension is at individualized goal; continue the current treatment.")]
    else:
        problems = [("hypertension", "Hypertension is at individualized goal; continue the current treatment."),
                    ("hypothyroidism", "Hypothyroidism is at goal; continue replacement therapy.")]
    tests = ("CBC", "basic metabolic panel", "hemoglobin A1c")[:min(level - 1, 3)]
    raw_problems = [dict(name=name, source="signed_note", identity_quote=quote,
                         addressed_quote=quote, status_quote=quote) for name, quote in problems]
    raw_data = [dict(action="test_order", subject=name, source="signed_note",
                    identity_quote=f"I ordered {name} today.", action_quote=f"I ordered {name} today.",
                    qualification_quote="") for name in tests]
    time_quote = "Total physician time on the date of service: 65 minutes." if level == 5 else ""
    note = "\n\n".join([p[1] for p in problems] + [e["action_quote"] for e in raw_data]
                       + ([time_quote] if time_quote else []))
    bundle = VisitBundle(f"SYN-GUARDRAIL-{level}", 40, "F", "office_established", "11", signed_note=note)
    facts = sanitize_facts({"chief_complaint": "Synthetic follow-up", "mdm_problem_events": raw_problems,
        "mdm_data_events": raw_data, "mdm_risk_events": [],
        "total_time_minutes": 65 if time_quote else None, "total_time_quote": time_quote})
    facts["_qualified_problems"] = qualify_problems(raw_problems, bundle)
    facts["_qualified_mdm_data"] = qualify_mdm_data(raw_data, bundle)
    facts["_qualified_em_time"] = qualify_em_time(facts["total_time_minutes"], time_quote, bundle).attestation
    evidence = facts["_qualified_problems"].evidence() + facts["_qualified_mdm_data"].evidence()
    if time_quote:
        from ..schemas import Evidence
        evidence.append(Evidence("signed_note", time_quote, "documents 65 min total time"))
    return facts, evidence, bundle


def qualified_case(level=4):
    """Return detached mutable facts/evidence with immutable real qualifications."""
    return deepcopy(_case(level))


def evidence_text():
    return "\n\n".join(_case(level)[2].signed_note for level in (2, 3, 4, 5))


def fixture_llm(level=4):
    """Offline extraction for the matching authored source fixture only."""
    from ..llm import MockLLM
    facts, _, source = qualified_case(level)
    class FixtureLLM(MockLLM):
        def complete_json(self, **kwargs):
            if source.signed_note not in kwargs["user"]:
                return {}
            if "extract_clinical_facts" in kwargs["user"]:
                return deepcopy({k: v for k, v in facts.items() if not k.startswith("_")})
            return {"diagnoses": []}
    return FixtureLLM()
