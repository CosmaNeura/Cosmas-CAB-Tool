"""Human-selected Athena evidence attached to an existing Card Chase case.

The source client owns vendor knowledge. This service cannot find/match cases,
grant consent, verify eligibility, close cases, schedule contacts or write EHRs.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from .athena_card_chase import AthenaCardChaseError, CardChaseReadContext, map_athena_card_chase, national_phone_digits
from .athena_client import AthenaClientError, AthenaPartialResponseError, _staff_page_receipt
from .schemas import VisitBundle


class AthenaReviewError(ValueError):
    def __init__(self, code="invalid_request"):
        self.code = code
        self.status_code = {"not_authorized": 403, "not_found": 404,
            "source_unavailable": 503, "audit_required": 503,
            "stale_review": 409, "stale_discovery": 409, "operation_conflict": 409}.get(code, 422)
        super().__init__(code)


def require(condition, code="invalid_request"):
    if not condition:
        raise AthenaReviewError(code)


def text(value, maximum=128):
    require(isinstance(value, str) and 1 <= len(value) <= maximum
            and value == value.strip() and not any(
                ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value))
    return value


def uid(value):
    text(value, 36)
    try:
        require(str(UUID(value)) == value)
    except ValueError:
        raise AthenaReviewError() from None
    return value


def detached(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def digest(value):
    try:
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise AthenaReviewError() from None


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, repr=False)
class ReviewActor:
    tenant_id: str
    user_id: str
    role: str

    def __post_init__(self):
        uid(self.tenant_id)
        uid(self.user_id)
        require(self.role in ("BillingStaff", "Admin"), "not_authorized")


def _candidate(read, practice, patient, batch, observed, scope=None):
    context = (CardChaseReadContext(practice,patient,batch,observed,scope.profile.vendor_practice_id,False,scope)
               if scope is not None and scope.profile.environment=="production" else CardChaseReadContext(practice,patient,batch,observed))
    pages = read["insurancePages"]
    candidate = map_athena_card_chase(context=context, patient_response=read["patient"],
        insurance_response=pages[0] if pages else None).to_review_dict()
    policies = []
    for raw in pages:
        page = map_athena_card_chase(context=context, patient_response=None,
                                    insurance_response=raw).to_review_dict()
        policies.extend(page["insurance"]["policies"])
    ids = [row["candidateId"] for row in policies]
    require(len(ids) == len(set(ids)), "source_unavailable")
    candidate["insurance"]["policies"] = policies
    for name, issue in (("policy_record_id", "duplicate_policy_id"), ("sequence", "duplicate_sequence")):
        values = [next((str(f["value"]) for f in p["fields"] if f["name"] == name and f["state"] == "present"), None)
                  for p in policies]
        for policy, value in zip(policies, values):
            if value is not None and values.count(value) > 1 and issue not in policy["issues"]:
                policy["issues"].append(issue)
    candidate["reads"]["insurance"].update(state=read["insuranceState"],
        recordCount=len(policies), truncated=read["insuranceState"] == "partial",
        pageHashes=[hashlib.sha256(page).hexdigest() for page in pages])
    candidate["insurance"]["state"] = read["insuranceState"]
    candidate["sourceDigest"] = digest([candidate["mappingVersion"], hashlib.sha256(read["patient"]).hexdigest(),
        candidate["reads"]["insurance"]["pageHashes"], read["insuranceState"]])
    candidate["candidateId"] = digest([candidate["sourceDigest"], context.batch_id])
    candidate["readError"] = read["error"]
    candidate["reviewIssues"] = [v for v in candidate["reviewIssues"] if not v.startswith("insurance_")]
    if read["insuranceState"] != "complete":
        candidate["reviewIssues"].append("insurance_" + read["insuranceState"])
    return candidate


class AthenaReviewService:
    def __init__(self, store, client, *, clock=now, discovery_key=None, source_binding=None, intent_override=None, source_admission=None, source_scope=None):
        self.store, self.client, self.clock = store, client, clock
        self.source_binding, self.intent_override = source_binding, intent_override
        self.source_admission, self.source_scope = source_admission, source_scope
        self.practice_id, self.durable = store.practice_id, store.durable
        if discovery_key is not None and (not isinstance(discovery_key, bytes) or len(discovery_key) < 32):
            raise ValueError("Athena discovery signing configuration is invalid")
        self._discovery_key = discovery_key if discovery_key is not None else (None if self.durable else secrets.token_bytes(32))

    def current(self, chase_id, actor):
        with self.store.unit(chase_id, actor) as unit:
            view = unit.current()
            require(view["revision"] == 0 or view.get("caseBinding") == digest([self.practice_id, unit.patient_ref]), "stale_review")
            return view

    def _intent(self, kind, chase_id, actor, command):
        require(type(actor) is ReviewActor, "not_authorized")
        actor.__post_init__()
        text(chase_id)
        require(type(command) is dict)
        uid(command.get("operationId"))
        revision = command.get("expectedRevision")
        require(type(revision) is int and 0 <= revision <= 9999)
        return self.intent_override or digest([kind, chase_id, actor.tenant_id, actor.user_id, actor.role, command])

    def _candidate(self, read, patient, batch):
        candidate = _candidate(read, self.practice_id, patient, batch, self.clock(), self.source_scope)
        if self.source_binding is not None:
            candidate["sourceBinding"] = detached(self.source_binding)
            candidate["sourceDigest"] = digest([candidate["sourceDigest"], self.source_binding["scopeSha256"]])
            candidate["candidateId"] = digest([candidate["sourceDigest"], batch])
        if self.source_admission is not None:
            candidate["sourceAdmission"] = detached(self.source_admission)
            candidate["sourceDigest"] = digest([candidate["sourceDigest"],self.source_admission["grantSha256"]])
            candidate["candidateId"] = digest([candidate["sourceDigest"],batch])
        return candidate

    def _check(self, unit, command, intent):
        replay = unit.receipt(command["operationId"])
        if replay:
            require(replay["intent"] == intent, "operation_conflict")
            require(unit.current().get("caseBinding") == digest([self.practice_id, unit.patient_ref]), "stale_review")
            return True
        require(unit.current()["revision"] == command["expectedRevision"], "stale_review")
        require(self.source_binding is not None or unit.current()["schemaVersion"] not in {"athena-case-review.v2","athena-case-review.v3"}, "stale_review")
        require(unit.current()["schemaVersion"]!="athena-case-review.v3" or self.source_admission is not None,"stale_review")
        return False

    def refresh(self, chase_id, actor, command, *, audit=None):
        require(type(command) is dict and set(command) == {"sourcePatientId", "operationId", "expectedRevision"})
        intent = self._intent("refresh", chase_id, actor, command)
        patient = command["sourcePatientId"]
        require(patient == self.source_binding["sourcePatientId"] if self.source_admission is not None
                else isinstance(patient,str) and patient in {str(i) for i in range(60178,60185)})
        with self.store.unit(chase_id, actor, command["operationId"]) as unit:
            if self._check(unit, command, intent):
                unit.audit(audit, actor, "athena.refresh")
                return unit.current()
            bound_patient = unit.patient_ref
        try:
            read = self.client.read_card(patient)
            candidate = self._candidate(read, patient, command["operationId"])
        except (AthenaClientError, AthenaCardChaseError):
            raise AthenaReviewError("source_unavailable") from None
        with self.store.unit(chase_id, actor, command["operationId"]) as unit:
            if self._check(unit, command, intent):
                unit.audit(audit, actor, "athena.refresh")
                return unit.current()
            require(unit.patient_ref == bound_patient, "stale_review")
            previous = unit.current()
            view = {"schemaVersion": "athena-case-review.v1", "chaseId": chase_id, "revision": previous["revision"] + 1,
                    "reviewId": command["operationId"], "candidate": candidate,
                    "applied": previous["applied"], "mode": self.client.mode,
                    "billingImport": previous.get("billingImport"),
                    "caseBinding": digest([self.practice_id, bound_patient])}
            if self.source_binding is not None:
                view.update(schemaVersion="athena-case-review.v2", sourceBinding=detached(self.source_binding))
                if previous["revision"] and previous.get("caseBinding") != view["caseBinding"]:
                    # Evidence from another CAB patient is retained in immutable
                    # history, never copied into the new patient's current view.
                    view.update(applied=None, billingImport=None)
            if self.source_admission is not None:
                view.update(schemaVersion="athena-case-review.v3",sourceAdmission=detached(self.source_admission))
            unit.save(view, command["operationId"], intent, actor, audit, "athena.refresh")
            return detached(view)

    def apply(self, chase_id, actor, command, *, audit=None):
        required = {"reviewId", "operationId", "expectedRevision",
            "confirmedPatientMatch", "selectedPhoneField", "selectedEmail", "selectedPolicyIds",
            "confirmedPolicyReview", "reviewReason"}
        require(type(command) is dict and required <= set(command) <= required | {"selectedPhoneCountry"})
        country = command.get("selectedPhoneCountry")
        require(country in (None, "US") and (country is None or command["selectedPhoneField"] is not None))
        # Preserve replay identities of pre-existing commands with no country choice.
        command = {k: v for k, v in command.items() if k != "selectedPhoneCountry" or v is not None}
        intent = self._intent("apply", chase_id, actor, command)
        uid(command["reviewId"])
        require(command["confirmedPatientMatch"] is True)
        require(command["selectedPhoneField"] in (None, "mobile_phone", "home_phone"))
        require(type(command["selectedEmail"]) is bool and type(command["confirmedPolicyReview"]) is bool)
        text(command["reviewReason"], 600)
        selection = command["selectedPolicyIds"]
        require(type(selection) is list and len(selection) <= 15
                and all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) for v in selection)
                and len(set(selection)) == len(selection))
        require(not selection or command["confirmedPolicyReview"] is True)
        with self.store.unit(chase_id, actor, command["operationId"]) as unit:
            if self._check(unit, command, intent):
                unit.audit(audit, actor, "athena.apply")
                return unit.current()
            previous = unit.current()
            require(previous.get("caseBinding") == digest([self.practice_id, unit.patient_ref]), "stale_review")
            require(previous["reviewId"] == command["reviewId"], "stale_review")
            candidate, bound_patient = previous["candidate"], unit.patient_ref
            require(candidate["reads"]["patient"]["state"] == "complete")
            age = datetime.fromisoformat(self.clock().replace("Z", "+00:00")) - datetime.fromisoformat(
                candidate["source"]["observedAt"].replace("Z", "+00:00"))
            require(0 <= age.total_seconds() <= 900, "stale_review")
            policies = candidate["insurance"]["policies"]
            require(set(selection) <= {row["candidateId"] for row in policies})
            selected = [row for row in policies if row["candidateId"] in selection]
            contacts = []
            requested = [command["selectedPhoneField"]] if command["selectedPhoneField"] else []
            if command["selectedEmail"]:
                requested.append("email")
            for field in requested:
                match = next((c for c in candidate["contact"]["candidates"] if c["field"] and c["field"]["name"] == field), None)
                require(match is not None)
                source_value = match["field"]["value"]
                contact = {"field": field, "value": source_value, "consent": "unknown"}
                if field != "email" and match["formatStatus"] == "country_confirmation_required":
                    digits = national_phone_digits(source_value)
                    require(country == "US" and digits is not None)
                    contact.update(value="+1" + digits, normalization={
                        "sourceValue": source_value, "country": country, "confirmedByStaff": True})
                else:
                    require(match["formatStatus"] == "plausible" and (field == "email" or country is None))
                contacts.append(contact)
        # Re-read before accepting: edited/failed/partial source refresh requires
        # a new visible review. Never silently apply values the human did not see.
        try:
            fresh = self._candidate(self.client.read_card(candidate["source"]["patientId"]),
                candidate["source"]["patientId"], command["reviewId"])
        except (AthenaClientError, AthenaCardChaseError):
            raise AthenaReviewError("source_unavailable") from None
        require(fresh["sourceDigest"] == candidate["sourceDigest"], "stale_review")
        with self.store.unit(chase_id, actor, command["operationId"]) as unit:
            if self._check(unit, command, intent):
                unit.audit(audit, actor, "athena.apply")
                return unit.current()
            require(unit.patient_ref == bound_patient and unit.current()["reviewId"] == command["reviewId"], "stale_review")
            view = detached(previous)
            view["revision"] += 1
            view["applied"] = {"reviewId": command["reviewId"], "operationId": command["operationId"],
                "sourceDigest": candidate["sourceDigest"], "patient": candidate["patient"],
                "contacts": contacts, "policies": selected, "at": self.clock(),
                "actor": {"tenantId": actor.tenant_id, "userId": actor.user_id, "role": actor.role},
                "reason": command["reviewReason"], "consent": "unknown",
                "eligibilityVerified": False, "caseClosed": False}
            if self.source_binding is not None:
                view["applied"]["sourceBinding"] = detached(self.source_binding)
            if self.source_admission is not None:
                view["applied"]["sourceAdmission"] = detached(self.source_admission)
            unit.save(view, command["operationId"], intent, actor, audit, "athena.apply", contacts=contacts)
            return detached(view)

    def _discovery_binding(self, chase_id, actor, view, department):
        binding = ["athena-encounters.v1", self.practice_id, chase_id,
            actor.tenant_id, actor.user_id, actor.role, view["caseBinding"], view["revision"],
            view["reviewId"], view["candidate"]["sourceDigest"],
            view["candidate"]["source"]["patientId"], department]
        return digest(binding + ([view["sourceBinding"]["scopeSha256"]] if "sourceBinding" in view else []))

    def _discovery_time(self):
        return int(datetime.fromisoformat(self.clock().replace("Z", "+00:00")).timestamp())

    def _seal_discovery(self, value):
        if self._discovery_key is None:
            return None
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
        body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        signature = hmac.new(self._discovery_key, b"athena-encounter-discovery.v1\n" + body.encode(), hashlib.sha256).hexdigest()
        token = body + "." + signature
        require(len(token) <= 12288, "stale_discovery")
        return token

    def _open_discovery(self, token, binding):
        require(self._discovery_key is not None and isinstance(token, str)
                and re.fullmatch(r"[A-Za-z0-9_-]{1,12223}\.[a-f0-9]{64}", token), "stale_discovery")
        body, signature = token.rsplit(".", 1)
        expected = hmac.new(self._discovery_key, b"athena-encounter-discovery.v1\n" + body.encode(), hashlib.sha256).hexdigest()
        require(hmac.compare_digest(signature, expected), "stale_discovery")
        try:
            value = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            require(type(value) is dict and set(value) == {"version", "binding", "issued", "pages", "totalCount", "nextOffset"}, "stale_discovery")
            require(value["version"] == "athena-discovery.v1" and value["binding"] == binding
                    and type(value["issued"]) is int and 0 <= self._discovery_time() - value["issued"] <= 900,
                    "stale_discovery")
            pages = value["pages"]
            require(isinstance(pages, list) and 1 <= len(pages) <= 30, "stale_discovery")
            for index, page in enumerate(pages):
                _staff_page_receipt(page)
                require(index == 0 or page["offset"] == pages[index - 1]["offset"] + 5, "stale_discovery")
                require(index == len(pages) - 1 or len(page["encounterIds"]) == 5, "stale_discovery")
            ids = [identifier for page in pages for identifier in page["encounterIds"]]
            require(len(ids) == len(set(ids)), "stale_discovery")
            count, next_offset = value["totalCount"], value["nextOffset"]
            require(count is None or type(count) is int and 0 <= count <= 1_000_000, "stale_discovery")
            require(next_offset is None or type(next_offset) is int
                    and next_offset == pages[-1]["offset"] + 5 and len(pages[-1]["encounterIds"]) == 5,
                    "stale_discovery")
            return value
        except (ValueError, TypeError, KeyError, UnicodeError, AthenaClientError):
            raise AthenaReviewError("stale_discovery") from None

    def billing_read(self, chase_id, actor, *, department_id, encounter_id=None, discovery_token=None):
        view = self.current(chase_id, actor)
        require(view["applied"] is not None, "stale_review")
        require(view["applied"]["reviewId"] == view["reviewId"], "stale_review")
        patient = view["candidate"]["source"]["patientId"]
        binding = self._discovery_binding(chase_id, actor, view, department_id)
        prior = self._open_discovery(discovery_token, binding) if discovery_token is not None else None
        options, append = {}, False
        if prior is not None:
            options["expected_total"] = prior["totalCount"]
            if encounter_id is None:
                require(prior["nextOffset"] is not None, "stale_discovery")
                options["boundary"] = prior["pages"][-1]
                append = len(prior["pages"]) < 29
            else:
                page = next((p for p in prior["pages"] if encounter_id in p["encounterIds"]), None)
                require(page is not None, "stale_discovery")
                options["page"] = page
        try:
            result = self.client.read_billing(patient, department_id, encounter_id, **options)
        except AthenaPartialResponseError:
            raise AthenaReviewError("stale_discovery" if prior is not None else "source_unavailable") from None
        except (AthenaClientError, AthenaCardChaseError):
            raise AthenaReviewError("source_unavailable") from None
        current = self.current(chase_id, actor)
        require(current["revision"] == view["revision"], "stale_review")
        raw = result.pop("pagination")
        if prior is not None and encounter_id is not None:
            state, selection = prior, discovery_token
        else:
            if prior is not None:
                require(result["error"] is None, "source_unavailable")
                previous_ids = {i for p in prior["pages"] for i in p["encounterIds"]}
                require(not any(i in previous_ids for p in raw["pages"] for i in p["encounterIds"]), "stale_discovery")
            pages = (prior["pages"] if append else []) + raw["pages"]
            state = {"version": "athena-discovery.v1", "binding": binding,
                "issued": prior["issued"] if prior else self._discovery_time(),
                "pages": pages, "totalCount": raw["totalCount"], "nextOffset": raw["nextOffset"]}
            selection = self._seal_discovery(state) if pages else None
        result["pagination"] = {"selectionToken": selection,
            "nextToken": selection if state["nextOffset"] is not None else None,
            "loadedCount": sum(len(p["encounterIds"]) for p in state["pages"]),
            "windowStart": state["pages"][0]["offset"] if state["pages"] else 0,
            "append": append, "nextStartsWindow": len(state["pages"]) >= 29}
        return {**result, "caseRevision": current["revision"], "mode": self.client.mode,
                **({"sourceBinding": detached(self.source_binding)} if self.source_binding is not None else {}),
                **({"sourceAdmission": detached(self.source_admission)} if self.source_admission is not None else {})}

    def billing_apply(self, chase_id, actor, command, *, audit=None):
        # Request model is also checked at the direct service seam.
        from .athena_review_routes import BillingApply
        from pydantic import ValidationError
        try:
            validated = BillingApply.model_validate({**command, "chaseId": chase_id})
        except (ValidationError, TypeError):
            raise AthenaReviewError() from None
        command = validated.model_dump(exclude={"chaseId"})
        if command.get("discoveryToken") is None:
            command.pop("discoveryToken", None)  # Preserve pre-pagination persisted replay identity.
        intent = self._intent("billing_apply", chase_id, actor, command)
        require(all(command[key] is True for key in ("confirmedPatientMatch", "confirmedFinishedNote", "confirmedClinicalContext")))
        text(command["providerSpecialty"], 100)
        text(command["reviewReason"], 600)
        require((command["encounterType"].startswith("office_") and command["placeOfService"] == "11")
                or (command["encounterType"].startswith("telehealth_") and command["placeOfService"] in ("02", "10")))
        with self.store.unit(chase_id, actor, command["operationId"]) as unit:
            if self._check(unit, command, intent):
                unit.audit(audit, actor, "athena.billing_apply")
                return unit.current()
            prior, patient_ref = unit.current(), unit.patient_ref
        result = self.billing_read(chase_id, actor, department_id=command["departmentId"],
                                   encounter_id=command["sourceEncounterId"],
                                   discovery_token=command.get("discoveryToken"))
        note = result["note"]
        require(note is not None and note["responseSha256"] == command["expectedNoteSha256"]
                and note["metadataSha256"] == command["expectedMetadataSha256"], "stale_review")
        text(note["plainText"].replace("\n", " ").replace("\t", " ").replace("\r", " "), 131072)
        # The new encounter is operation-bound and never overwrites a previous
        # source/clinician record. Exact command retry reuses its receipt.
        encounter_id = "athena-review-" + command["operationId"]
        from .source_visit_context import athena_visit_metadata
        bundle = VisitBundle(encounter_id, command["patientAge"], command["patientSex"],
            command["encounterType"], command["placeOfService"], signed_note=note["plainText"],
            provider_specialty=command["providerSpecialty"])
        with self.store.unit(chase_id, actor, command["operationId"]) as unit:
            if self._check(unit, command, intent):
                unit.audit(audit, actor, "athena.billing_apply")
                return unit.current()
            require(unit.patient_ref == patient_ref and unit.current()["revision"] == prior["revision"], "stale_review")
            view = detached(prior)
            view["revision"] += 1
            view["billingImport"] = {"encounterId": encounter_id, "source": ("athena_staff_review_operational" if self.client.mode == "production" else "athena_staff_review_preview_synthetic" if self.client.mode == "preview" else "athena_staff_review_mock"),
                "sourceEncounterId": command["sourceEncounterId"], "departmentId": command["departmentId"],
                "departmentIdMeaning": "chart_query", "sourceDepartmentId": note["sourceDepartmentId"],
                "sourceDepartmentOrigin": note["sourceDepartmentOrigin"],
                "sourcePatientId": prior["candidate"]["source"]["patientId"],
                "sourceNoteSha256": note["responseSha256"], "sourceMetadataSha256": note["metadataSha256"],
                "sourceVisitContext": athena_visit_metadata(note),
                "plainTextSha256": hashlib.sha256(note["plainText"].encode()).hexdigest(),
                "transformVersion": note["transformVersion"], "sourcePointer": note["sourcePointer"],
                "sourceAtomicity": note["sourceAtomicity"], "reviewReason": command["reviewReason"],
                "contextOrigin": "staff_confirmed", "at": self.clock(),
                "clinicalContext": {key: command[key] for key in ("patientAge", "patientSex", "encounterType", "placeOfService", "providerSpecialty")},
                "actor": {"tenantId": actor.tenant_id, "userId": actor.user_id, "role": actor.role}}
            if self.source_binding is not None:
                view["billingImport"]["sourceBinding"] = detached(self.source_binding)
            if self.source_admission is not None:
                view["billingImport"]["sourceAdmission"] = detached(self.source_admission)
            unit.save(view, command["operationId"], intent, actor, audit, "athena.billing_apply", encounter=bundle)
            return detached(view)
