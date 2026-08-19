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


def parse_entities(doc: dict[str, Any], source: str) -> list[HealthEntity]:
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
        assertion = e.get("assertion") if isinstance(e.get("assertion"), dict) else {}
        links: dict[str, str] = {}
        for link in e.get("links") or []:
            if isinstance(link, dict) and link.get("dataSource") and link.get("id"):
                # first link per vocabulary wins (they arrive best-first)
                links.setdefault(str(link["dataSource"]), str(link["id"]))
        out.append(HealthEntity(
            text=text, category=category, offset=offset, length=length,
            source=source,
            confidence=float(e.get("confidenceScore") or 0.0),
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
                 poll_interval: float = 1.0, max_poll_seconds: float = 120.0):
        config = settings or Settings.from_env()
        self.endpoint = config.azure_language_endpoint.rstrip("/")
        self.api_version = config.azure_language_api_version
        self.language = language
        self.max_retries = max_retries
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.max_poll_seconds = max_poll_seconds
        self._token: tuple[str, float] | None = None

        if token_provider is None:
            # Validate the effective values, so a caller cannot swap a reviewed
            # Language endpoint for another host and send it a bearer token.
            Settings(
                azure_language_enabled=True,
                azure_language_endpoint=self.endpoint,
                azure_language_api_version=self.api_version,
                azure_client_id=config.azure_client_id,
            )
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
    def _request(self, url: str, *, data: bytes | None = None,
                 method: str = "GET") -> tuple[dict[str, Any], dict[str, str]]:
        headers = {"content-type": "application/json",
                   "Authorization": f"Bearer {self._bearer()}"}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                time.sleep(min(2 ** attempt, 15))
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                    body = json.loads(raw) if raw else {}
                    return (body if isinstance(body, dict) else {}), resp_headers
            except urllib.error.HTTPError as e:
                if e.code not in self.RETRYABLE:
                    raise
                last_err = e
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last_err = e
        raise RuntimeError(
            f"AzureLanguageTA4H: {method} failed after {self.max_retries + 1} attempts"
        ) from last_err

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
                "parameters": {"loggingOptOut": True},
            }],
        }
        submit_url = (f"{self.endpoint}/language/analyze-text/jobs"
                      f"?api-version={urllib.parse.quote(self.api_version)}")
        _, headers = self._request(
            submit_url, data=json.dumps(body).encode(), method="POST")
        job_url = headers.get("operation-location")
        if not job_url:
            raise RuntimeError(
                "AzureLanguageTA4H: submit returned no operation-location header")

        deadline = time.monotonic() + self.max_poll_seconds
        while True:
            payload, _ = self._request(job_url)
            status = str(payload.get("status", "")).lower()
            if status == "succeeded":
                return self._collect(payload, documents)
            if status in ("failed", "cancelled", "rejected"):
                raise RuntimeError(
                    f"AzureLanguageTA4H: job {status}: {payload.get('errors')}")
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"AzureLanguageTA4H: job still '{status}' after "
                    f"{self.max_poll_seconds:.0f}s")
            time.sleep(self.poll_interval)

    @staticmethod
    def _collect(payload: dict[str, Any],
                 documents: dict[str, str]) -> dict[str, list[HealthEntity]]:
        out: dict[str, list[HealthEntity]] = {}
        for item in (payload.get("tasks") or {}).get("items") or []:
            for doc in ((item or {}).get("results") or {}).get("documents") or []:
                doc_id = str(doc.get("id", ""))
                if doc_id in documents:
                    out.setdefault(doc_id, []).extend(parse_entities(doc, doc_id))
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
    if config.azure_language_enabled:
        return AzureLanguageTA4H(settings=config)
    return None


def describe_ta4h_client(client: TA4HClient | None) -> dict[str, Any]:
    """Which extraction path is live, for /api/health and pipeline responses.

    A stored claim has to be able to say what produced it: a single-engine run
    and a two-engine consensus run are not the same evidence.
    """
    return {
        "extractionEngines": 1 if client is None else 2,
        "secondaryExtractionMode": getattr(client, "mode", None) if client else None,
    }
