"""
Azure AI Language — Text Analytics for health (TA4H) client.

TA4H is the *second* extraction engine. It does clinical NER, assertion
detection (negated / hypothetical / family-member) and entity linking to UMLS
vocabularies including ICD-10-CM. It does **not** level E/M, choose CPT, or
reason about medical necessity — that stays entirely with our own rule engine.
See ``docs/TA4H_MULTI_MODEL_ASSESSMENT.md`` for why it is integrated here and
not as a competing coder.

Two implementations, mirroring ``llm.py``:
  * ``MockTA4H``  -- deterministic, offline. Returns the clinically-correct
                     entity set for the demo encounter, with offsets resolved
                     against the actual text at call time (so the spans are
                     real, not hand-copied). This keeps CI hermetic.
  * ``AzureTA4H`` -- production adapter. Calls the async ``analyze-text/jobs``
                     endpoint and polls the returned ``operation-location``.

Kept dependency-free (urllib) on purpose — same stance as ``AnthropicLLM``, so
the core engine keeps its stdlib-only guarantee and no ``azure-*`` package is
required to run or test the pipeline.

PHI note
--------
``logging_opt_out`` defaults to **True** and is sent on every request. Azure AI
Language otherwise retains input text for up to 48 hours for troubleshooting;
with PHI in the payload that retention is a HIPAA consideration, so the opt-out
is the default and turning it off is a deliberate, documented act. Azure AI
Language is HIPAA-eligible under the Microsoft BAA (included by default via the
Product Terms / DPA); processing is stateless and inputs are not used to train
models.

Configuration (absent -> the engine is simply disabled, pipeline unchanged):
  AZURE_LANGUAGE_ENDPOINT   e.g. https://<resource>.cognitiveservices.azure.com
  AZURE_LANGUAGE_KEY        resource key (prefer a managed identity in Azure)
  AZURE_LANGUAGE_API_VERSION  optional; defaults to the GA version below
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

# GA api-version for the async analyze-text/jobs endpoint.
DEFAULT_API_VERSION = "2023-04-01"

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
class AzureTA4H:
    """Async ``analyze-text/jobs`` adapter with ``operation-location`` polling.

    One POST submits every document in the bundle (TA4H accepts a batch), then
    the job is polled until it succeeds. Failures are raised, never swallowed:
    the orchestrator decides whether a secondary-engine outage degrades to
    single-engine operation (it does — see ``orchestrator.process``).
    """

    RETRYABLE = {408, 429, 500, 502, 503, 504}

    def __init__(self, endpoint: str | None = None, api_key: str | None = None,
                 *, api_version: str | None = None, language: str = "en",
                 logging_opt_out: bool = True, max_retries: int = 3,
                 timeout: int = 60, poll_interval: float = 1.0,
                 max_poll_seconds: float = 120.0):
        self.endpoint = (endpoint or os.environ.get("AZURE_LANGUAGE_ENDPOINT", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("AZURE_LANGUAGE_KEY", "")
        self.api_version = api_version or os.environ.get(
            "AZURE_LANGUAGE_API_VERSION", DEFAULT_API_VERSION)
        self.language = language
        # See the PHI note in the module docstring before setting this False.
        self.logging_opt_out = logging_opt_out
        self.max_retries = max_retries
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.max_poll_seconds = max_poll_seconds

    # -- HTTP --------------------------------------------------------------- #
    def _headers(self) -> dict[str, str]:
        return {"content-type": "application/json", "Ocp-Apim-Subscription-Key": self.api_key}

    def _request(self, url: str, *, data: bytes | None = None,
                 method: str = "GET") -> tuple[dict[str, Any], dict[str, str]]:
        req = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                time.sleep(min(2 ** attempt, 15))
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    headers = {k.lower(): v for k, v in resp.headers.items()}
                    body = json.loads(raw) if raw else {}
                    return (body if isinstance(body, dict) else {}), headers
            except urllib.error.HTTPError as e:
                if e.code not in self.RETRYABLE:
                    raise
                last_err = e
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last_err = e
        raise RuntimeError(
            f"AzureTA4H: {method} failed after {self.max_retries + 1} attempts") from last_err

    def analyze(self, documents: dict[str, str]) -> dict[str, list[HealthEntity]]:
        docs = [{"id": doc_id, "language": self.language, "text": text}
                for doc_id, text in documents.items() if text and text.strip()]
        if not docs:
            return {}
        if not self.endpoint or not self.api_key:
            raise RuntimeError("AzureTA4H: AZURE_LANGUAGE_ENDPOINT / AZURE_LANGUAGE_KEY not set")

        body = {
            "analysisInput": {"documents": docs},
            "tasks": [{
                "kind": "Healthcare",
                "taskName": "cosmaneura-ta4h",
                "parameters": {"loggingOptOut": self.logging_opt_out},
            }],
        }
        submit_url = (f"{self.endpoint}/language/analyze-text/jobs"
                      f"?api-version={urllib.parse.quote(self.api_version)}")
        _, headers = self._request(
            submit_url, data=json.dumps(body).encode(), method="POST")
        job_url = headers.get("operation-location")
        if not job_url:
            raise RuntimeError("AzureTA4H: submit returned no operation-location header")

        deadline = time.monotonic() + self.max_poll_seconds
        while True:
            payload, _ = self._request(job_url)
            status = str(payload.get("status", "")).lower()
            if status == "succeeded":
                return self._collect(payload, documents)
            if status in ("failed", "cancelled", "rejected"):
                raise RuntimeError(f"AzureTA4H: job {status}: {payload.get('errors')}")
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"AzureTA4H: job still '{status}' after {self.max_poll_seconds:.0f}s")
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


def ta4h_configured() -> bool:
    return bool(os.environ.get("AZURE_LANGUAGE_ENDPOINT")
                and os.environ.get("AZURE_LANGUAGE_KEY"))


def get_default_ta4h_client() -> TA4HClient | None:
    """``AzureTA4H`` when configured, ``MockTA4H`` when explicitly asked for,
    otherwise ``None`` — and ``None`` means the second engine is off and the
    pipeline behaves exactly as it did before this module existed.

    Set ``COSMANEURA_TA4H=mock`` to exercise the ensemble path offline.
    """
    mode = os.environ.get("COSMANEURA_TA4H", "").strip().lower()
    if mode in ("0", "off", "false", "disabled"):
        return None
    if mode == "mock":
        return MockTA4H()
    if ta4h_configured():
        return AzureTA4H()
    return None
