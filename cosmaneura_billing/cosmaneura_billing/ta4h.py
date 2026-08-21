"""
Azure AI Language — Text Analytics for health (TA4H) client.

TA4H is the *second* extraction engine. It does clinical NER, assertion
detection (negated / hypothetical / family-member) and entity linking to UMLS
vocabularies including ICD-10-CM. It does **not** level E/M, choose CPT, or
reason about medical necessity — that stays entirely with our own rule engine.
See ``docs/TA4H_MULTI_MODEL_ASSESSMENT.md`` for why it is integrated here and
not as a competing coder.

Two implementations, mirroring ``llm.py``:
  * ``MockTA4H``          -- deterministic, offline. Returns the clinically
                             correct entity set for the demo encounter, with
                             offsets resolved against the actual text at call
                             time (so the spans are real, not hand-copied).
                             Keeps CI hermetic and needs no cloud identity.
  * ``AzureLanguageTA4H`` -- production adapter. Calls the async
                             ``analyze-text/jobs`` endpoint and polls the
                             returned ``operation-location``, authenticated only
                             with the API's managed identity.

Both clients report ``mode`` so a caller can tell which extraction path
produced a claim — see ``describe_ta4h_client``.

Credentials
-----------
Managed identity only, exactly as with Azure OpenAI inference: there is no
API-key path, and a key present in the environment is treated as a
misconfiguration rather than silently honoured. Azure AI Language shares the
``https://cognitiveservices.azure.com/.default`` token scope with Azure OpenAI,
so the API's existing user-assigned identity needs only a *Cognitive Services
Language Reader* (or User) role assignment on the Language resource — no new
secret enters the deployment.

The transport is stdlib ``urllib`` on purpose, so enabling a second engine adds
no HTTP/SDK dependency to the core engine; only ``azure-identity`` (already
required for inference) is needed, and only when the engine is enabled.

PHI note
--------
``loggingOptOut`` is sent as true on every request. Azure AI Language otherwise
retains input text for up to 48 hours for troubleshooting; with chart text in
the payload that retention is a HIPAA consideration, so the opt-out is not
configurable here. Azure AI Language is HIPAA-eligible under the Microsoft BAA
(included via the Product Terms / DPA); processing is stateless and inputs are
not used to train models.
"""
from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from .settings import Settings, SettingsError

# Azure AI Language shares the Cognitive Services token scope with Azure OpenAI.
_AZURE_LANGUAGE_SCOPE = "https://cognitiveservices.azure.com/.default"
# Mirrors the Azure OpenAI stance: no key-based auth path exists, and a key in
# the environment is a misconfiguration we refuse rather than quietly ignore.
_PROHIBITED_AZURE_LANGUAGE_CREDENTIAL_ENV = (
    "AZURE_LANGUAGE_KEY",
    "AZURE_LANGUAGE_AD_TOKEN",
    "AZURE_AI_LANGUAGE_KEY",
)
# Refresh a bearer token this many seconds before it actually expires.
_TOKEN_SKEW_SECONDS = 300

# Assertion values that mean "this finding is not affirmed for this patient".
# TA4H only returns assertions that differ from the default (positive /
# subject / no conditionality), so an absent assertion means affirmed.
NEGATED_CERTAINTY = {"negative", "negativepossible"}
UNCERTAIN_CERTAINTY = {"positivepossible", "neutralpossible"}
# association == "other" means the concept belongs to a family member, not the
# patient — the exact case our documentation-integrity agent guards against.
OTHER_SUBJECT = "other"

# Entity categories that can carry a billable diagnosis.
DIAGNOSIS_CATEGORIES = {"Diagnosis", "SymptomOrSign"}
# Categories that evidence clinical data review / workup.
DATA_CATEGORIES = {"ExaminationName", "MeasurementName", "MeasurementValue"}


@dataclass
class HealthEntity:
    """One entity from TA4H, normalized for our use."""
    text: str
    category: str
    offset: int
    length: int
    source: str = ""                    # which VisitBundle field it came from
    confidence: float = 0.0
    name: str = ""                      # UMLS preferred name
    certainty: str = ""                 # "" = affirmed (default)
    conditionality: str = ""            # "hypothetical" | "conditional" | ""
    association: str = ""               # "other" = family member; "" = patient
    links: dict[str, str] = field(default_factory=dict)   # dataSource -> id

    @property
    def negated(self) -> bool:
        return self.certainty.lower() in NEGATED_CERTAINTY

    @property
    def uncertain(self) -> bool:
        return self.certainty.lower() in UNCERTAIN_CERTAINTY

    @property
    def conditional(self) -> bool:
        return bool(self.conditionality)

    @property
    def about_someone_else(self) -> bool:
        return self.association.lower() == OTHER_SUBJECT

    @property
    def affirmed_for_patient(self) -> bool:
        """Affirmed, unconditional, and about this patient — the only state in
        which an entity may support a billable diagnosis without review."""
        return not (self.negated or self.conditional or self.about_someone_else)

    @property
    def icd10(self) -> str:
        """ICD-10-CM code from the UMLS links, if TA4H resolved one."""
        return self.links.get("ICD10CM", "")


class TA4HClient(Protocol):
    mode: str

    def analyze(self, documents: dict[str, str]) -> dict[str, list[HealthEntity]]:
        """Map ``{document_id: text}`` -> ``{document_id: entities}``.

        Document ids are the VisitBundle field names, so entity offsets stay
        anchored to a known source text.
        """
        ...


class TA4HServiceError(RuntimeError):
    """A sanitized Azure AI Language service or response failure."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward a managed-identity bearer token through a redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def parse_entities(
    doc: dict[str, Any], source: str, source_text: str,
) -> list[HealthEntity]:
    """Parse one result document's entities. Defensive: unknown or off-shape
    fields are dropped rather than raising, so a service-side response change
    degrades to "fewer entities", never to a failed encounter."""
    out: list[HealthEntity] = []
    for e in doc.get("entities") or []:
        if not isinstance(e, dict):
            continue
        text, category = e.get("text"), e.get("category")
        offset, length = e.get("offset"), e.get("length")
        if not isinstance(text, str) or not isinstance(category, str):
            continue
        if not isinstance(offset, int) or not isinstance(length, int):
            continue
        if isinstance(offset, bool) or isinstance(length, bool):
            continue
        if offset < 0 or length < 1 or offset + length > len(source_text):
            continue
        # Requests pin UnicodeCodePoint indexing, which matches Python string
        # indices. A service-returned entity must name the exact source slice;
        # otherwise a mismatched offset could manufacture valid-looking chart
        # evidence for a different concept.
        if source_text[offset:offset + length] != text:
            continue
        confidence_raw = e.get("confidenceScore", 0.0)
        if isinstance(confidence_raw, bool) or not isinstance(
            confidence_raw, (int, float),
        ):
            continue
        confidence = float(confidence_raw)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            continue
        assertion = e.get("assertion") if isinstance(e.get("assertion"), dict) else {}
        links: dict[str, str] = {}
        for link in e.get("links") or []:
            if isinstance(link, dict) and link.get("dataSource") and link.get("id"):
                # first link per vocabulary wins (they arrive best-first)
                links.setdefault(str(link["dataSource"]), str(link["id"]))
        out.append(HealthEntity(
            text=text, category=category, offset=offset, length=length,
            source=source,
            confidence=confidence,
            name=str(e.get("name") or ""),
            certainty=str(assertion.get("certainty") or ""),
            conditionality=str(assertion.get("conditionality") or ""),
            association=str(assertion.get("association") or ""),
            links=links,
        ))
    return out


# --------------------------------------------------------------------------- #
# Production adapter
# --------------------------------------------------------------------------- #
class AzureLanguageTA4H:
    """Async ``analyze-text/jobs`` adapter with ``operation-location`` polling.

    One POST submits every document in the bundle (TA4H accepts a batch), then
    the job is polled until it succeeds. Failures are raised, never swallowed:
    the orchestrator decides whether a secondary-engine outage degrades to
    single-engine operation (it does — see ``BillingOrchestrator._extract``).
    """

    mode = "azure_language"
    RETRYABLE = {408, 429, 500, 502, 503, 504}

    def __init__(self, settings: Settings | None = None, *,
                 token_provider=None, language: str = "en",
                 max_retries: int = 3, timeout: int = 60,
                 poll_interval: float = 1.0, max_poll_seconds: float = 120.0,
                 urlopen=None):
        config = settings or Settings.from_env()
        self.endpoint = config.azure_language_endpoint.rstrip("/")
        self.api_version = config.azure_language_api_version
        self.language = language
        self.max_retries = max_retries
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.max_poll_seconds = max_poll_seconds
        self._token: tuple[str, float] | None = None
        self._urlopen = urlopen or urllib.request.build_opener(
            _NoRedirectHandler(),
        ).open

        if any(os.environ.get(name)
               for name in _PROHIBITED_AZURE_LANGUAGE_CREDENTIAL_ENV):
            raise SettingsError(
                "direct Azure AI Language credentials are prohibited; managed "
                "identity is required"
            )

        # Validate effective values even when tests inject a token provider.
        # No caller may swap the reviewed endpoint before a bearer is attached.
        Settings(
            azure_openai_enabled=config.azure_openai_enabled,
            azure_openai_endpoint=config.azure_openai_endpoint,
            azure_openai_deployment=config.azure_openai_deployment,
            azure_openai_model=config.azure_openai_model,
            azure_openai_api_version=config.azure_openai_api_version,
            azure_language_enabled=True,
            azure_language_endpoint=self.endpoint,
            azure_language_api_version=self.api_version,
            azure_client_id=config.azure_client_id,
        )
        if token_provider is None:
            token_provider = self._build_token_provider(config.azure_client_id)
        self._token_provider = token_provider

    @staticmethod
    def _build_token_provider(managed_identity_client_id: str):
        if any(os.environ.get(name)
               for name in _PROHIBITED_AZURE_LANGUAGE_CREDENTIAL_ENV):
            raise SettingsError(
                "direct Azure AI Language credentials are prohibited; managed "
                "identity is required"
            )
        try:
            from azure.identity import ManagedIdentityCredential
        except ImportError as exc:
            raise RuntimeError(
                "Azure AI Language dependencies are not installed"
            ) from exc
        credential = ManagedIdentityCredential(client_id=managed_identity_client_id)

        def provider() -> tuple[str, float]:
            token = credential.get_token(_AZURE_LANGUAGE_SCOPE)
            return token.token, float(token.expires_on)

        return provider

    def _bearer(self) -> str:
        now = time.time()
        if self._token is not None and self._token[1] - _TOKEN_SKEW_SECONDS > now:
            return self._token[0]
        token, expires_on = self._token_provider()
        self._token = (token, expires_on)
        return token

    # -- HTTP --------------------------------------------------------------- #
    def _validated_service_url(self, value: str) -> str:
        try:
            base = urllib.parse.urlsplit(self.endpoint)
            target = urllib.parse.urlsplit(value)
            target_port = target.port
        except (TypeError, ValueError):
            raise TA4HServiceError(
                "AzureLanguageTA4H returned an invalid service URL"
            ) from None
        if (
            target.scheme.lower() != "https"
            or (target.hostname or "").lower() != (base.hostname or "").lower()
            or target_port is not None
            or target.username is not None
            or target.password is not None
            or target.fragment
            or not target.path.startswith("/language/analyze-text/jobs")
        ):
            raise TA4HServiceError(
                "AzureLanguageTA4H returned an untrusted service URL"
            )
        return value

    def _request(
        self,
        url: str,
        *,
        data: bytes | None = None,
        method: str = "GET",
        deadline: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        url = self._validated_service_url(url)
        headers = {"content-type": "application/json",
                   "Authorization": f"Bearer {self._bearer()}"}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        for attempt in range(self.max_retries + 1):
            if attempt:
                delay = min(2 ** attempt, 15)
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            "AzureLanguageTA4H polling deadline expired"
                        ) from None
                    delay = min(delay, remaining)
                time.sleep(delay)
            request_timeout = self.timeout
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "AzureLanguageTA4H polling deadline expired"
                    ) from None
                request_timeout = min(request_timeout, remaining)
            try:
                with self._urlopen(req, timeout=request_timeout) as resp:
                    raw = resp.read()
                    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                    body = json.loads(raw) if raw else {}
                    return (body if isinstance(body, dict) else {}), resp_headers
            except urllib.error.HTTPError as e:
                if e.code not in self.RETRYABLE:
                    raise TA4HServiceError(
                        "AzureLanguageTA4H request was rejected"
                    ) from None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                pass
        raise TA4HServiceError(
            f"AzureLanguageTA4H: {method} failed after {self.max_retries + 1} attempts"
        ) from None

    def analyze(self, documents: dict[str, str]) -> dict[str, list[HealthEntity]]:
        docs = [{"id": doc_id, "language": self.language, "text": text}
                for doc_id, text in documents.items() if text and text.strip()]
        if not docs:
            return {}
        if not self.endpoint:
            raise SettingsError("AZURE_LANGUAGE_ENDPOINT is not configured")

        body = {
            "analysisInput": {"documents": docs},
            "tasks": [{
                "kind": "Healthcare",
                "taskName": "cosmaneura-ta4h",
                # Not configurable: chart text must not enter the provider's
                # 48-hour troubleshooting log. See the PHI note above.
                "parameters": {
                    "loggingOptOut": True,
                    "stringIndexType": "UnicodeCodePoint",
                },
            }],
        }
        submit_url = (f"{self.endpoint}/language/analyze-text/jobs"
                      f"?api-version={urllib.parse.quote(self.api_version)}")
        _, headers = self._request(
            submit_url, data=json.dumps(body).encode(), method="POST")
        job_url = headers.get("operation-location")
        if not job_url:
            raise TA4HServiceError(
                "AzureLanguageTA4H: submit returned no operation-location header")
        job_url = self._validated_service_url(job_url)

        deadline = time.monotonic() + self.max_poll_seconds
        while True:
            payload, _ = self._request(job_url, deadline=deadline)
            status = str(payload.get("status", "")).lower()
            if status == "succeeded":
                return self._collect(payload, documents)
            if status in ("failed", "cancelled", "rejected"):
                raise TA4HServiceError(
                    f"AzureLanguageTA4H job {status}"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "AzureLanguageTA4H polling deadline expired")
            time.sleep(self.poll_interval)

    @staticmethod
    def _collect(payload: dict[str, Any],
                 documents: dict[str, str]) -> dict[str, list[HealthEntity]]:
        out: dict[str, list[HealthEntity]] = {}
        expected_ids = {
            doc_id for doc_id, text in documents.items()
            if isinstance(text, str) and text.strip()
        }
        seen_ids: set[str] = set()
        if payload.get("errors"):
            raise TA4HServiceError("AzureLanguageTA4H returned task errors")
        tasks = payload.get("tasks") or {}
        if not isinstance(tasks, dict):
            raise TA4HServiceError("AzureLanguageTA4H returned a malformed response")
        items = tasks.get("items") or []
        if not isinstance(items, list):
            raise TA4HServiceError("AzureLanguageTA4H returned a malformed response")
        for item in items:
            if not isinstance(item, dict) or item.get("error") or item.get("errors"):
                raise TA4HServiceError("AzureLanguageTA4H returned task errors")
            results = item.get("results")
            if not isinstance(results, dict) or results.get("errors"):
                raise TA4HServiceError("AzureLanguageTA4H returned document errors")
            result_documents = results.get("documents") or []
            if not isinstance(result_documents, list):
                raise TA4HServiceError(
                    "AzureLanguageTA4H returned a malformed response"
                )
            for doc in result_documents:
                if not isinstance(doc, dict):
                    raise TA4HServiceError(
                        "AzureLanguageTA4H returned a malformed response"
                    )
                doc_id = str(doc.get("id", ""))
                if doc_id not in expected_ids or doc_id in seen_ids:
                    raise TA4HServiceError(
                        "AzureLanguageTA4H returned mismatched document results"
                    )
                seen_ids.add(doc_id)
                out.setdefault(doc_id, []).extend(
                    parse_entities(doc, doc_id, documents[doc_id])
                )
        if seen_ids != expected_ids:
            raise TA4HServiceError(
                "AzureLanguageTA4H returned incomplete document results"
            )
        return out


# --------------------------------------------------------------------------- #
# Offline deterministic mock (demo / CI)
# --------------------------------------------------------------------------- #
class MockTA4H:
    """The clinically-correct TA4H reading of the demo encounter, offline.

    Offsets are resolved with ``str.find`` against the text actually passed in,
    so every mocked entity points at a real span — exactly as the live service
    does, and exactly as ``evidence.py`` will check. Phrases that aren't present
    in a document are skipped, so this mock stays silent (returns nothing) on
    any encounter other than the demo one rather than inventing findings.
    """

    mode = "mock"

    # (phrase, category, ICD-10-CM link, assertion overrides)
    _DEMO: tuple[tuple[str, str, str, dict[str, str]], ...] = (
        ("poop blood", "SymptomOrSign", "K62.5", {}),
        ("hematochezia", "SymptomOrSign", "K62.5", {}),
        ("constipation", "SymptomOrSign", "K59.00", {}),
        ("diarrhea", "SymptomOrSign", "R19.7", {}),
        # The two traps: denied family history and an unaddressed BMI finding.
        ("family history of colon cancer", "Diagnosis", "Z80.0",
         {"certainty": "negative"}),
        ("colon cancer", "Diagnosis", "C18.9", {"certainty": "negativePossible"}),
        # Data review / workup evidence.
        ("CBC", "ExaminationName", "", {}),
        ("colonoscopy", "ExaminationName", "", {}),
        ("blood tests", "ExaminationName", "", {}),
    )

    def analyze(self, documents: dict[str, str]) -> dict[str, list[HealthEntity]]:
        out: dict[str, list[HealthEntity]] = {}
        for doc_id, text in documents.items():
            if not text:
                continue
            found: list[HealthEntity] = []
            lowered = text.lower()
            for phrase, category, icd10, assertion in self._DEMO:
                offset = lowered.find(phrase.lower())
                if offset < 0:
                    continue
                found.append(HealthEntity(
                    text=text[offset:offset + len(phrase)],
                    category=category, offset=offset, length=len(phrase),
                    source=doc_id, confidence=0.95,
                    name=phrase.title(),
                    certainty=assertion.get("certainty", ""),
                    conditionality=assertion.get("conditionality", ""),
                    association=assertion.get("association", ""),
                    links=({"ICD10CM": icd10, "UMLS": f"C-mock-{icd10 or category}"}
                           if icd10 else {}),
                ))
            if found:
                out[doc_id] = found
        return out


def get_default_ta4h_client(settings: Settings | None = None) -> TA4HClient | None:
    """``AzureLanguageTA4H`` when enabled, ``MockTA4H`` when explicitly asked
    for, otherwise ``None``.

    ``None`` means the second extraction engine is off and the pipeline behaves
    exactly as it did before this module existed. Off is the default, matching
    Azure OpenAI inference: a deployment opts in, it never drifts on.
    """
    config = settings or Settings.from_env()
    if config.azure_language_mock:
        return MockTA4H()
    if config.azure_language_enabled or config.azure_language_shadow_enabled:
        return AzureLanguageTA4H(settings=config)
    return None


def describe_ta4h_client(client: TA4HClient | None) -> dict[str, Any]:
    """Describe the extraction path for internal diagnostics and future shadowing.

    Reviewer-facing serialization is deliberately deferred until the approved
    surface milestone.
    """
    return {
        "extractionEngines": 1 if client is None else 2,
        "secondaryExtractionMode": getattr(client, "mode", None) if client else None,
    }
