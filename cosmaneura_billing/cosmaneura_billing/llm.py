"""
LLM client abstraction.

Two implementations:
  * ``MockLLM``      -- deterministic, offline. Returns structured output for the
                        demo encounter so the whole pipeline runs with no network
                        and no API key. This is what powers ``run_demo.py``.
  * ``AnthropicLLM`` -- production adapter. Calls the Messages API and, when the
                        caller supplies a JSON schema, forces the model to answer
                        through a tool call so the reply is schema-shaped JSON —
                        no fence-stripping, no prose. Selected automatically when
                        ANTHROPIC_API_KEY is set.

Hardening in the production adapter (billing decisions ride on this):
  * structured output via forced tool use when a ``schema`` is provided;
  * every response validated against that schema locally (``validate_json``) —
    a malformed or off-shape reply raises instead of silently flowing into the
    coding agents as empty ``.get()`` defaults;
  * retries with exponential backoff on rate limits (429), server errors (5xx),
    overloaded responses (529), network failures, and unparseable output.

Swap the backend in one place; every agent depends only on ``LLMClient.complete_json``.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Protocol


class LLMClient(Protocol):
    def complete_json(
        self, system: str, user: str, *,
        max_tokens: int = 1500, schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return the model's response parsed as a JSON object (schema-shaped
        when ``schema`` is given)."""
        ...


# --------------------------------------------------------------------------- #
# Minimal JSON-schema validation (stdlib only, on purpose)
# --------------------------------------------------------------------------- #
def validate_json(obj: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Validate the subset of JSON Schema we use (type / required / properties /
    items, with union types as lists). Raises ValueError with the failing path."""
    stype = schema.get("type")
    if stype:
        types = stype if isinstance(stype, list) else [stype]
        py: dict[str, tuple] = {
            "object": (dict,), "array": (list,), "string": (str,),
            "number": (int, float), "integer": (int,),
            "boolean": (bool,), "null": (type(None),),
        }
        allowed = tuple(t for name in types for t in py.get(name, ()))
        if not isinstance(obj, allowed) or (
            isinstance(obj, bool) and "boolean" not in types and "null" not in types
        ):
            raise ValueError(f"{path}: expected {types}, got {type(obj).__name__}")
    if isinstance(obj, dict):
        for key in schema.get("required", []):
            if key not in obj:
                raise ValueError(f"{path}: missing required key '{key}'")
        for key, sub in schema.get("properties", {}).items():
            if key in obj:
                validate_json(obj[key], sub, f"{path}.{key}")
    if isinstance(obj, list) and "items" in schema:
        for i, item in enumerate(obj):
            validate_json(item, schema["items"], f"{path}[{i}]")


# --------------------------------------------------------------------------- #
# Production adapter
# --------------------------------------------------------------------------- #
class AnthropicLLM:
    """Messages API adapter. Kept dependency-free (urllib) on purpose.

    With ``schema``: the model is forced to call an ``emit_result`` tool whose
    input_schema is your schema, and the tool input is returned — structured
    output, enforced server-side and re-validated locally.
    Without ``schema``: falls back to ask-for-bare-JSON and parses the text.
    """

    RETRYABLE = {408, 429, 500, 502, 503, 504, 529}

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 max_retries: int = 4, timeout: int = 120):
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.max_retries = max_retries
        self.timeout = timeout

    # -- one HTTP round trip ------------------------------------------------ #
    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(body).encode(),
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())

    def complete_json(
        self, system: str, user: str, *,
        max_tokens: int = 1500, schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if schema is not None:
            body["tools"] = [{
                "name": "emit_result",
                "description": "Emit the structured result of the task.",
                "input_schema": schema,
            }]
            body["tool_choice"] = {"type": "tool", "name": "emit_result"}
        else:
            body["system"] = (
                system + "\n\nRespond with a single valid JSON object and nothing "
                "else. No markdown, no prose, no code fences."
            )

        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                time.sleep(min(2 ** attempt, 30))
            try:
                payload = self._post(body)
                result = self._extract(payload, schema)
                if schema is not None:
                    validate_json(result, schema)
                return result
            except urllib.error.HTTPError as e:
                if e.code not in self.RETRYABLE:
                    raise
                last_err = e
            except (urllib.error.URLError, TimeoutError) as e:
                last_err = e
            except (json.JSONDecodeError, ValueError) as e:
                # malformed / off-schema output — re-ask rather than propagate
                last_err = e
        raise RuntimeError(
            f"AnthropicLLM: no valid response after {self.max_retries + 1} attempts"
        ) from last_err

    @staticmethod
    def _extract(payload: dict[str, Any], schema: dict[str, Any] | None) -> dict[str, Any]:
        blocks = payload.get("content", [])
        if schema is not None:
            for b in blocks:
                if b.get("type") == "tool_use" and b.get("name") == "emit_result":
                    if not isinstance(b.get("input"), dict):
                        raise ValueError("tool_use input is not an object")
                    return b["input"]
            raise ValueError("no emit_result tool_use block in response")
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        obj = json.loads(text)
        if not isinstance(obj, dict):
            raise ValueError("model returned JSON that is not an object")
        return obj


# --------------------------------------------------------------------------- #
# Offline deterministic mock (demo)
# --------------------------------------------------------------------------- #
class MockLLM:
    """Returns canned, realistic structured output keyed on a task tag.

    Every agent prompt begins with a line ``TASK: <tag>`` so the mock can route.
    The payloads encode the clinically-correct answer for the demo encounter
    (26yo male, hematochezia + constipation + diarrhea, BMI ~37.6, colonoscopy
    referral). Every quote is verbatim from the demo documentation, because the
    evidence verifier (``evidence.py``) checks quotes against the actual text —
    exactly as it will for real model output. Real reasoning is delegated to
    ``AnthropicLLM`` in production.
    """

    def complete_json(
        self, system: str, user: str, *,
        max_tokens: int = 1500, schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
                "quote": "refer you for a colonoscopy to be safe",
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
