"""
LLM client abstraction.

Two implementations:
  * ``MockLLM``        -- deterministic, offline. Returns structured output for
                          the demo encounter so the whole pipeline runs with no
                          network or cloud identity. This powers ``run_demo.py``.
  * ``AzureOpenAILLM`` -- production adapter. Uses the official OpenAI Python
                          SDK's Responses API against an Azure OpenAI endpoint,
                          authenticated only with the API managed identity.

Hardening in the production adapter (billing decisions ride on this):
  * Structured Outputs when the supplied schema is compatible with the strict
    JSON Schema subset, otherwise JSON mode plus the same local validation;
  * every response validated against that schema locally (``validate_json``) —
    a malformed or off-shape reply raises instead of silently flowing into the
    coding agents as empty ``.get()`` defaults;
  * retries with exponential backoff on rate limits (429), selected server
    errors, network failures, and unparseable output;
  * refusals and incomplete responses are diagnosed and raised immediately
    instead of being retried into a generic failure.

Both clients report ``mode`` and ``model_label`` so the API can tell a caller
which inference path produced a claim — see ``describe_client``.

Swap the backend in one place; every agent depends only on ``LLMClient.complete_json``.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from typing import Any, Callable, Protocol

from .runtime import safe_llm_model_label
from .settings import Settings, SettingsError
from .telemetry import create_azure_monitor_llm_sink

# Extraction over a full visit (intake + transcript + signed note) produces a
# sizeable facts object. Keep the established output ceiling across providers;
# model/deployment selection remains explicit deployment configuration.
DEFAULT_MAX_TOKENS = 8000
DEFAULT_AZURE_OPENAI_API_VERSION = "2025-04-01-preview"
_AZURE_OPENAI_SCOPE = "https://cognitiveservices.azure.com/.default"
_PROHIBITED_AZURE_OPENAI_CREDENTIAL_ENV = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_AD_TOKEN",
)


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
# Azure Structured Outputs schema preparation
# --------------------------------------------------------------------------- #
def _strict_structured_schema(schema: dict[str, Any]) -> dict[str, Any] | None:
    """Return an Azure/OpenAI strict schema without narrowing caller meaning.

    Structured Outputs requires every object field to be required and every
    object to reject additional properties. Optional properties are represented
    as nullable and stripped back out after parsing. An unconstrained object
    (for example the current clinical ``vitals`` bag) cannot be made strict
    without changing its meaning, so that schema deliberately falls back to
    JSON mode and local validation.
    """

    def convert(node: Any) -> dict[str, Any]:
        if not isinstance(node, dict):
            raise ValueError("schema node is not an object")
        converted = {
            key: value
            for key, value in node.items()
            if key not in {"properties", "required", "items", "additionalProperties"}
        }
        node_type = node.get("type")
        types = node_type if isinstance(node_type, list) else [node_type]
        if "object" in types:
            properties = node.get("properties")
            if not isinstance(properties, dict):
                raise ValueError("unconstrained object is not strict-compatible")
            required = set(node.get("required", []))
            strict_properties: dict[str, Any] = {}
            for name, child in properties.items():
                strict_child = convert(child)
                if name not in required:
                    child_type = strict_child.get("type")
                    if isinstance(child_type, str):
                        strict_child["type"] = [child_type, "null"]
                    elif isinstance(child_type, list):
                        if "null" not in child_type:
                            strict_child["type"] = [*child_type, "null"]
                    else:
                        strict_child = {
                            "anyOf": [strict_child, {"type": "null"}],
                        }
                strict_properties[name] = strict_child
            converted["properties"] = strict_properties
            converted["required"] = list(properties)
            converted["additionalProperties"] = False
        if "array" in types:
            if "items" not in node:
                raise ValueError("unconstrained array is not strict-compatible")
            converted["items"] = convert(node["items"])
        return converted

    try:
        strict = convert(schema)
    except (TypeError, ValueError):
        return None
    if strict.get("type") != "object":
        return None
    return strict


def _strip_optional_nulls(value: Any, schema: dict[str, Any]) -> Any:
    """Undo nullable placeholders introduced only for strict output syntax."""
    if isinstance(value, dict):
        required = set(schema.get("required", []))
        properties = schema.get("properties", {})
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            child_schema = properties.get(key, {}) if isinstance(properties, dict) else {}
            if key not in required and item is None:
                continue
            cleaned[key] = _strip_optional_nulls(item, child_schema)
        return cleaned
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [_strip_optional_nulls(item, schema["items"]) for item in value]
    return value


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


# --------------------------------------------------------------------------- #
# Production adapter
# --------------------------------------------------------------------------- #
class AzureOpenAILLM:
    """Azure-hosted Responses API adapter using managed identity only.

    The official SDK and Azure Identity are imported only when live inference
    is configured, so deterministic offline/demo use keeps working without the
    optional inference dependencies installed.
    """

    RETRYABLE = {408, 429, 500, 502, 503, 504, 529}
    mode = "azure_openai"

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        deployment: str | None = None,
        model: str | None = None,
        api_version: str | None = None,
        max_retries: int = 4,
        timeout: int = 120,
        settings: Settings | None = None,
        client: Any | None = None,
        clock: Callable[[], float] | None = None,
        metrics_sink: Callable[[dict[str, Any]], None] | None = None,
        logger: logging.Logger | Any | None = None,
    ) -> None:
        config = settings or Settings.from_env()
        self.endpoint = endpoint or config.azure_openai_endpoint
        self.deployment = deployment or config.azure_openai_deployment
        self.model = model or config.azure_openai_model
        self.api_version = (
            api_version
            or config.azure_openai_api_version
            or DEFAULT_AZURE_OPENAI_API_VERSION
        )
        if not all((self.endpoint, self.deployment, self.model)):
            raise SettingsError(
                "Azure OpenAI endpoint, deployment, and model must be configured"
            )
        self._managed_identity_client_id = config.azure_client_id
        self._injected_client = client is not None
        self.max_retries = max_retries
        self.timeout = timeout
        self._clock = clock or time.monotonic
        self._metrics_sink = metrics_sink
        self._logger = logger or logging.getLogger("cosmaneura_billing.llm")
        self._credential: Any | None = None
        self._network_errors: tuple[type[BaseException], ...] = (TimeoutError,)
        if client is None:
            # Validate the effective values after applying explicit overrides.
            # This prevents a caller from replacing a reviewed Azure endpoint
            # with another host and sending it a managed-identity bearer token.
            Settings(
                azure_openai_enabled=True,
                azure_openai_endpoint=self.endpoint,
                azure_openai_deployment=self.deployment,
                azure_openai_model=self.model,
                azure_openai_api_version=self.api_version,
                azure_client_id=config.azure_client_id,
            )
            client, credential, network_errors = self._build_client(
                endpoint=self.endpoint,
                api_version=self.api_version,
                managed_identity_client_id=config.azure_client_id,
                timeout=timeout,
            )
            self._credential = credential
            self._network_errors = (TimeoutError, *network_errors)
        self._client = client
        self._transport_client = client
        self._transport_identity = self._operational_transport_identity(verify_transport=False)

    def _operational_transport_identity(self, *, verify_transport=True):
        from .model_admission import ModelAdmissionError
        identity = {"endpoint": self.endpoint, "deployment": self.deployment,
                    "model": self.model, "apiVersion": self.api_version,
                    "managedIdentityClientId": self._managed_identity_client_id}
        if hasattr(self, "_transport_identity") and (
            identity != self._transport_identity or self._client is not self._transport_client
        ):
            raise ModelAdmissionError()
        if verify_transport and not self._injected_client and (
            str(self._client.base_url) != self.endpoint.rstrip("/") + "/openai/"
            or self._client.default_query.get("api-version") != self.api_version
        ):
            raise ModelAdmissionError()
        return identity

    @staticmethod
    def _build_client(
        *,
        endpoint: str,
        api_version: str,
        managed_identity_client_id: str,
        timeout: int,
    ) -> tuple[Any, Any, tuple[type[BaseException], ...]]:
        if any(os.environ.get(name) for name in _PROHIBITED_AZURE_OPENAI_CREDENTIAL_ENV):
            raise SettingsError(
                "direct Azure OpenAI credentials are prohibited; managed identity "
                "is required"
            )
        try:
            from azure.identity import (
                ManagedIdentityCredential,
                get_bearer_token_provider,
            )
            import openai
        except ImportError as exc:
            raise RuntimeError(
                "Azure OpenAI inference dependencies are not installed"
            ) from exc
        credential = ManagedIdentityCredential(client_id=managed_identity_client_id)
        token_provider = get_bearer_token_provider(
            credential,
            _AZURE_OPENAI_SCOPE,
        )
        client = openai.AzureOpenAI(
            azure_endpoint=endpoint,
            api_version=api_version,
            azure_ad_token_provider=token_provider,
            max_retries=0,
            timeout=timeout,
        )
        return client, credential, (
            openai.APIConnectionError,
            openai.APITimeoutError,
        )

    @property
    def model_label(self) -> str:
        return self.model

    def _clock_value(self) -> float | None:
        """Read the metric clock without letting telemetry break inference."""
        try:
            value = self._clock()
            if isinstance(value, bool):
                return None
            candidate = float(value)
            return candidate if math.isfinite(candidate) else None
        except Exception:
            return None

    @staticmethod
    def _token_count(payload: Any | None, field: str) -> int | None:
        """Return a provider usage count only when it is a safe scalar."""
        if payload is None:
            return None
        usage = _field(payload, "usage")
        if usage is None:
            return None
        value = _field(usage, field)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 1_000_000_000
        ):
            return None
        return value

    def _record_request_metric(
        self,
        *,
        attempt: int,
        started_at: float | None,
        status: str,
        payload: Any | None = None,
    ) -> None:
        """Emit one PHI-free metric for one provider HTTP attempt.

        Retries are separate provider requests and can each be billable, so each
        gets its own event.  The event deliberately contains no prompt,
        response, exception text, API key, URL, or patient/request identifier.
        """
        finished_at = self._clock_value()
        latency_ms: int | None = None
        if started_at is not None and finished_at is not None:
            latency_ms = min(
                86_400_000,
                max(0, int(round((finished_at - started_at) * 1000))),
            )
        # Successful/error Responses can identify the actual backing model.
        # Prefer that safe provider value so deployment/model drift cannot
        # silently misprice usage; pre-response failures use reviewed config.
        safe_model = safe_llm_model_label(_field(payload, "model"))
        if safe_model == "invalid_model_label":
            safe_model = safe_llm_model_label(self.model)
        safe_attempt = (
            attempt
            if type(attempt) is int and 1 <= attempt <= 100
            else 1
        )
        metric: dict[str, Any] = {
            "event": "llm_request",
            "provider": "azure_openai",
            "model": safe_model,
            "status": status,
            "attempt": safe_attempt,
            "latency_ms": latency_ms,
            "input_tokens": self._token_count(payload, "input_tokens"),
            "output_tokens": self._token_count(payload, "output_tokens"),
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": self._cached_input_tokens(payload),
        }
        # The closed stdout event feeds Log Analytics even when an optional
        # numeric Azure Monitor sink is enabled.  Each boundary is independent:
        # an observability failure can never change the inference result.
        try:
            self._logger.info("llm_request", extra=metric)
        except Exception:
            pass
        if self._metrics_sink is not None:
            try:
                self._metrics_sink(metric)
            except Exception:
                pass

    @staticmethod
    def _cached_input_tokens(payload: Any | None) -> int | None:
        usage = _field(payload, "usage")
        details = _field(usage, "input_tokens_details")
        value = _field(details, "cached_tokens")
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 1_000_000_000
        ):
            return None
        return value

    def shutdown_metrics(self) -> None:
        """Best-effort flush of the optional explicit numeric metric sink."""

        shutdown = getattr(self._metrics_sink, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception:
                pass
        for resource in (self._client, self._credential):
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def complete_json(
        self, system: str, user: str, *,
        max_tokens: int = DEFAULT_MAX_TOKENS, schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        strict_schema = _strict_structured_schema(schema) if schema is not None else None
        instructions = system
        request: dict[str, Any] = {
            "model": self.deployment,
            "max_output_tokens": max_tokens,
            "instructions": instructions,
            "input": user,
            # Chart prompts and model output must not become retrievable
            # Responses API state. Azure retention/abuse-monitoring controls are
            # separate contractual deployment gates; this disables API storage.
            "store": False,
        }
        if strict_schema is not None:
            # Model/version support for strict schemas must be proven by the
            # private deployment acceptance job. A provider 4xx fails closed;
            # this adapter never retries the chart through a weaker format.
            request["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "billing_result",
                    "strict": True,
                    "schema": strict_schema,
                },
            }
        else:
            # Azure requires the literal JSON marker in request input for
            # JSON-object mode. Append this code-controlled requirement only
            # when that mode is selected before the provider call; a provider
            # rejection still cannot trigger a weaker-format retry.
            request["input"] = user + "\n\nReturn a single valid JSON object."
            request["instructions"] = (
                instructions
                + "\n\nReturn a single valid JSON object and nothing else. "
                "No markdown, prose, or code fences."
            )
            if schema is not None:
                # JSON mode guarantees syntax, not shape.  Supply the same
                # code-controlled contract that deterministic validation will
                # enforce after the response.  This is selected before the
                # request only for schemas that cannot be represented without
                # narrowing their meaning in the strict Structured Outputs
                # subset; a provider rejection never triggers this fallback.
                request["instructions"] += (
                    "\nConform exactly to this JSON Schema contract: "
                    + json.dumps(schema, sort_keys=True, separators=(",", ":"))
                )
            request["text"] = {"format": {"type": "json_object"}}

        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                time.sleep(min(2 ** attempt, 30))
            from .model_admission import check_model_request
            check_model_request(self)  # After backoff, before token acquisition/HTTP; never retried.
            started_at = self._clock_value()
            payload: Any | None = None
            try:
                payload = self._client.responses.create(**request)
                result = self._extract(payload, schema)
                if schema is not None:
                    result = _strip_optional_nulls(result, schema)
                    validate_json(result, schema)
                self._record_request_metric(
                    attempt=attempt + 1,
                    started_at=started_at,
                    status="success",
                    payload=payload,
                )
                return result
            except TerminalLLMError:
                self._record_request_metric(
                    attempt=attempt + 1,
                    started_at=started_at,
                    status="terminal_response",
                    payload=payload,
                )
                raise
            except (json.JSONDecodeError, ValueError) as e:
                self._record_request_metric(
                    attempt=attempt + 1,
                    started_at=started_at,
                    status="invalid_response",
                    payload=payload,
                )
                last_err = e
            except Exception as e:
                status_code = getattr(e, "status_code", None)
                if status_code is not None and status_code not in self.RETRYABLE:
                    self._record_request_metric(
                        attempt=attempt + 1,
                        started_at=started_at,
                        status="http_error",
                    )
                    raise
                if status_code in self.RETRYABLE:
                    self._record_request_metric(
                        attempt=attempt + 1,
                        started_at=started_at,
                        status="retryable_http_error",
                    )
                    last_err = e
                    continue
                if isinstance(e, self._network_errors):
                    self._record_request_metric(
                        attempt=attempt + 1,
                        started_at=started_at,
                        status="network_error",
                    )
                    last_err = e
                    continue
                self._record_request_metric(
                    attempt=attempt + 1,
                    started_at=started_at,
                    status="unexpected_error",
                    payload=payload,
                )
                raise
        raise RuntimeError(
            f"AzureOpenAILLM: no valid response after {self.max_retries + 1} attempts"
        ) from last_err

    @staticmethod
    def _extract(payload: Any, schema: dict[str, Any] | None) -> dict[str, Any]:
        status = _field(payload, "status")
        if status == "incomplete":
            reason = _field(_field(payload, "incomplete_details"), "reason")
            if reason == "max_output_tokens":
                raise TerminalLLMError(
                    "response hit max_output_tokens before the result was complete"
                )
            raise TerminalLLMError("Azure OpenAI returned an incomplete response")
        if status == "failed" or _field(payload, "error") is not None:
            raise TerminalLLMError("Azure OpenAI returned a failed response")
        if status != "completed":
            # Responses may be queued, in progress, or cancelled.  None of
            # those states is a completed clinical extraction, even if a
            # provider object happens to expose partial schema-shaped text.
            raise TerminalLLMError("Azure OpenAI returned an unusable response status")
        for output in _field(payload, "output", []) or []:
            if _field(output, "type") != "message":
                continue
            for content in _field(output, "content", []) or []:
                if _field(content, "type") == "refusal":
                    raise TerminalLLMError(
                        "model declined the request; review the documentation or "
                        "route this encounter to manual coding"
                    )
        text = _field(payload, "output_text", "")
        if not isinstance(text, str):
            raise ValueError("response output_text is not text")
        text = text.strip()
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
    The payloads encode source facts for the demo encounter
    (26yo male, hematochezia + constipation + diarrhea, BMI ~37.6, colonoscopy
    referral). Every quote is verbatim from the demo documentation, because the
    evidence verifier (``evidence.py``) checks quotes against the actual text —
    exactly as it will for real model output. Real reasoning is delegated to
    ``AzureOpenAILLM`` in production.

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
            "mdm_problem_events": [
                {"name": name, "source": "signed_note", "identity_quote": quote,
                 "addressed_quote": quote, "status_quote": ""}
                for name, quote in (
                    ("Hematochezia", "Hematochezia - refer for diagnostic colonoscopy, FOBT today"),
                    ("Constipation", "Constipation - dietary fiber, hydration"),
                    ("Diarrhea", "Diarrhea - monitor"))
            ],
            "mdm_data_events": [
                {"action": action, "subject": subject, "source": "signed_note",
                 "identity_quote": quote, "action_quote": quote, "qualification_quote": ""}
                for action, subject, quote in (
                    ("test_result_review", "CBC", "Data: reviewed outside CBC (no anemia)."),
                    ("test_order", "fecal occult blood test", "Ordered fecal occult blood test."))
            ],
            # A referral alone establishes neither a surgery decision nor its
            # management risk. The source contains no qualifying risk claim.
            "mdm_risk_events": [],
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
    """AzureOpenAILLM if fully configured, else the offline MockLLM."""
    config = settings or Settings.from_env()
    if config.azure_openai_enabled:
        metrics_sink = create_azure_monitor_llm_sink(
            enabled=config.telemetry_enabled,
            connection_string=config.application_insights_connection_string,
            managed_identity_client_id=config.azure_client_id,
        )
        if config.telemetry_enabled and metrics_sink is None:
            # Fail readiness before chart text reaches a provider rather than
            # silently starting a deployed inference process without the
            # accounting boundary that prices every provider request.
            raise RuntimeError("LLM metrics exporter is unavailable")
        return AzureOpenAILLM(settings=config, metrics_sink=metrics_sink)
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
