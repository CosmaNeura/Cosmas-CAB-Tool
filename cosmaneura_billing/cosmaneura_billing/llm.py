"""
LLM client abstraction.

Two implementations:
  * ``MockLLM``      -- deterministic, offline. Returns structured output for the
                        demo encounter so the whole pipeline runs with no network
                        and no API key. This is what powers ``run_demo.py``.
  * ``AnthropicLLM`` -- production adapter. Calls the Messages API and expects the
                        model to return a single JSON object. Selected automatically
                        when ANTHROPIC_API_KEY is set.

Swap the backend in one place; every agent depends only on ``LLMClient.complete_json``.
"""
from __future__ import annotations

import json
import os
from typing import Any, Protocol


class LLMClient(Protocol):
    def complete_json(self, system: str, user: str, *, max_tokens: int = 1500) -> dict[str, Any]:
        """Return the model's response parsed as a JSON object."""
        ...


# --------------------------------------------------------------------------- #
# Production adapter
# --------------------------------------------------------------------------- #
class AnthropicLLM:
    """Real Messages API call. Kept dependency-free (urllib) on purpose."""

    def __init__(self, model: str = "claude-opus-4-8", api_key: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    def complete_json(self, system: str, user: str, *, max_tokens: int = 1500) -> dict[str, Any]:
        import urllib.request

        body = json.dumps(
            {
                "model": self.model,
                "max_tokens": max_tokens,
                "system": system
                + "\n\nRespond with a single valid JSON object and nothing else. "
                "No markdown, no prose, no code fences.",
                "messages": [{"role": "user", "content": user}],
            }
        ).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read())
        text = "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(text)


# --------------------------------------------------------------------------- #
# Offline deterministic mock (demo)
# --------------------------------------------------------------------------- #
class MockLLM:
    """Returns canned, realistic structured output keyed on a task tag.

    Every agent prompt begins with a line ``TASK: <tag>`` so the mock can route.
    The payloads encode the clinically-correct answer for the demo encounter
    (26yo male, hematochezia + constipation + diarrhea, BMI ~37.6, colonoscopy
    referral). Real reasoning is delegated to ``AnthropicLLM`` in production.
    """

    def complete_json(self, system: str, user: str, *, max_tokens: int = 1500) -> dict[str, Any]:
        tag = ""
        for line in user.splitlines():
            if line.strip().startswith("TASK:"):
                tag = line.split("TASK:", 1)[1].strip()
                break
        handler = getattr(self, f"_{tag}", None)
        if handler is None:
            return {}
        return handler()

    # --- clinical extraction (problems, data reviewed, risk) --------------- #
    def _extract_clinical_facts(self) -> dict[str, Any]:
        return {
            "chief_complaint": "Rectal bleeding with bowel changes; patient anxious about colon cancer",
            "problems": [
                {"name": "Hematochezia / rectal bleeding", "status": "new, undiagnosed",
                 "acuity": "acute", "uncertain_prognosis": True,
                 "quote": "When I eat spicy food, I poop blood ... I poop a little bit of blood"},
                {"name": "Constipation", "status": "new", "acuity": "chronic-intermittent",
                 "quote": "Sometimes when I have constipation as well"},
                {"name": "Diarrhea", "status": "new", "acuity": "recurrent",
                 "quote": "I do have diarrhea often"},
            ],
            "data_reviewed": [
                {"item": "Outside CBC / blood tests reviewed", "category": "review_external_labs",
                 "quote": "I got blood tests done last night. Please find my reports attached."},
                {"item": "Stool occult blood test ordered", "category": "order_test"},
            ],
            "risk": {
                "level": "moderate",
                "drivers": ["Decision to refer for diagnostic colonoscopy (invasive procedure)",
                            "Undiagnosed new problem with uncertain prognosis"],
                "quote": "I will refer you for a colonoscopy to evaluate the bleeding",
            },
            "vitals": {"bmi": 37.6, "documented": True, "addressed_in_plan": False,
                       "quote": "5 feet and 9 inches tall and weigh 255 pounds"},
            "family_history_colon_cancer": False,
            "family_history_quote": "I do not have a family history of colon cancer",
        }

    # --- candidate diagnoses (will be filtered by the lookup + guardrail) -- #
    def _propose_diagnoses(self) -> dict[str, Any]:
        return {
            "diagnoses": [
                {"concept": "Hemorrhage of anus and rectum", "primary": True,
                 "quote": "When I eat spicy food, I poop blood"},
                {"concept": "Constipation, unspecified", "primary": False,
                 "quote": "Sometimes when I have constipation"},
                {"concept": "Diarrhea, unspecified", "primary": False,
                 "quote": "I do have diarrhea often"},
                # Two deliberately-tempting candidates the downstream agents must handle:
                {"concept": "Family history of malignant neoplasm of digestive organs", "primary": False,
                 "quote": "(none - patient denies)"},
                {"concept": "Obesity", "primary": False,
                 "quote": "weigh 255 pounds"},
            ]
        }

    # --- leading screening questions (dialogue RAG demo) ------------------ #
    def _leading_questions(self) -> dict[str, Any]:
        return {"questions": [
            "How long have you noticed the blood, and is it bright red or dark/tarry?",
            "Have you had any unintentional weight loss, fevers, or night sweats?",
            "Are you taking any aspirin, NSAIDs (ibuprofen/naproxen), or blood thinners?",
            "Any personal history of polyps, IBD, or prior GI bleeding?",
        ]}


def get_default_client() -> LLMClient:
    """AnthropicLLM if a key is present, else the offline MockLLM."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicLLM()
    return MockLLM()
