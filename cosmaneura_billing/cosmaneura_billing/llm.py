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
    overloaded responses (529), network failures, and unparseable output;
  * terminal stop reasons (``refusal``, ``max_tokens``) are diagnosed and raised
    immediately instead of being retried into a generic failure.

Both clients report ``mode`` and ``model_label`` so the API can tell a caller
which inference path produced a claim — see ``describe_client``.

Swap the backend in one place; every agent depends only on ``LLMClient.complete_json``.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Protocol

from .settings import Settings

# Current-generation default. Note the interaction with max_tokens: on this model
# family adaptive thinking is on unless disabled, and max_tokens caps thinking
# *plus* the tool output together — so the budget has to cover both.
DEFAULT_MODEL = "claude-opus-5"

# Extraction over a full visit (intake + transcript + signed note) produces a
# sizeable facts object, and thinking shares this budget. The previous 1500
# default was sized for output alone: with thinking on it truncates before the
# emit_result tool call completes, which surfaces as a missing tool_use block —
# indistinguishable from a malformed reply, and retried five times before
# failing. Size for both, and diagnose truncation explicitly (see _extract).
DEFAULT_MAX_TOKENS = 8000


class TerminalLLMError(RuntimeError):
    """A failure a re-ask cannot fix (refusal, exhausted token budget).

    Deliberately not a ``ValueError`` subclass: the retry loop treats those as
    "malformed, ask again", which is exactly the wrong response here.
    """


class LLMClient(Protocol):
    mode: str

    def complete_json(
        self, system: str, user: str, *,
        max_tokens: int = DEFAULT_MAX_TOKENS, schema: dict[str, Any] | None = None,
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
    mode = "anthropic"

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 max_retries: int = 4, timeout: int = 120,
                 effort: str | None = None, settings: Settings | None = None):
        config = settings or Settings.from_env()
        self.model = model or config.anthropic_model
        self.api_key = api_key or config.anthropic_api_key
        self.max_retries = max_retries
        self.timeout = timeout
        self.effort = effort or config.anthropic_effort

    @property
    def model_label(self) -> str:
        return self.model

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
        max_tokens: int = DEFAULT_MAX_TOKENS, schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            # Thinking is left on deliberately. Disabling it on this model family
            # can make the model write a tool call as plain text instead of a
            # tool_use block — for a forced-tool-use extractor that is a silent
            # wrong answer, which is the one failure mode this pipeline must not
            # have. Depth is controlled by effort instead.
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.effort},
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
            except TerminalLLMError:
                # a refusal or a blown token budget will not fix itself on a
                # re-ask; fail closed with the real reason
                raise
            except (json.JSONDecodeError, ValueError) as e:
                # malformed / off-schema output — re-ask rather than propagate
                last_err = e
        raise RuntimeError(
            f"AnthropicLLM: no valid response after {self.max_retries + 1} attempts"
        ) from last_err

    @staticmethod
    def _extract(payload: dict[str, Any], schema: dict[str, Any] | None) -> dict[str, Any]:
        blocks = payload.get("content", [])
        # Two stop reasons return HTTP 200 with unusable content. Naming them is
        # the difference between an actionable error and five wasted retries
        # ending in "no valid response".
        stop = payload.get("stop_reason")
        if stop == "refusal":
            detail = (payload.get("stop_details") or {}).get("category") or "unspecified"
            raise TerminalLLMError(
                f"model declined the request (refusal category: {detail}). "
                "Clinical documentation can trip safety classifiers; review the "
                "note or route this encounter to manual coding."
            )
        if stop == "max_tokens":
            raise TerminalLLMError(
                "response hit max_tokens before the result was complete. Thinking "
                "and output share this budget — raise max_tokens (or lower "
                "ANTHROPIC_EFFORT) rather than retrying."
            )
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

    **It answers only for the encounter it models.** Returning these facts for
    any other documentation would be fabrication: the quotes belong to a
    different patient. The evidence verifier would strip them downstream, but
    relying on that means the mock hands the pipeline invented findings and
    trusts a later stage to catch it — and it makes every non-demo encounter
    look like a GI bleed to anything reading the extraction directly. Unknown
    documentation returns ``{}``, and ``supports()`` lets callers say why the
    result is empty instead of rendering a blank claim.
    """

    mode = "mock"
    model_label = "offline-demo"

    # A verbatim span from the demo intake. If it isn't in the documentation,
    # this isn't the encounter these canned payloads describe.
    SIGNATURE = "when i eat spicy food, i poop blood"

    # Tasks whose canned payload asserts findings about a specific patient.
    # These are the ones that must not answer for someone else's documentation.
    # (`leading_questions` is excluded on purpose: it returns generic follow-up
    # prompts, not claims about the patient in front of you.)
    ENCOUNTER_SPECIFIC = frozenset({"extract_clinical_facts", "propose_diagnoses"})

    @classmethod
    def supports(cls, documentation: str) -> bool:
        """True when ``documentation`` is the encounter this mock actually models."""
        return cls.SIGNATURE in " ".join((documentation or "").lower().split())

    def complete_json(
        self, system: str, user: str, *,
        max_tokens: int = DEFAULT_MAX_TOKENS, schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tag = ""
        for line in user.splitlines():
            if line.strip().startswith("TASK:"):
                tag = line.split("TASK:", 1)[1].strip()
                break
        if tag in self.ENCOUNTER_SPECIFIC and not self.supports(user):
            return {}
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


def get_default_client(settings: Settings | None = None) -> LLMClient:
    """AnthropicLLM if a key is present, else the offline MockLLM."""
    config = settings or Settings.from_env()
    if config.anthropic_api_key:
        return AnthropicLLM(settings=config)
    return MockLLM()


def describe_client(client: LLMClient) -> dict[str, Any]:
    """Which inference path is live, for /api/health and every pipeline response.

    A stored claim has to be able to say what produced it: an offline demo run
    and a real-inference run are not the same evidence, and a reviewer looking
    at an empty result needs to know which one they got.
    """
    return {
        "inferenceMode": getattr(client, "mode", "unknown"),
        "inferenceModel": getattr(client, "model_label", None),
    }
