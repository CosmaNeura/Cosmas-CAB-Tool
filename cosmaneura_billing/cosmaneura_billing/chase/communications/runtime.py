"""Authenticated staff composition; outbound credentials never enter this API."""
from datetime import timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ...direct_contract import DirectContractList
from ...persistence import PostgresAccessAuditStore
from .dispatch import CommunicationsDispatcher, DispatchPolicy, SenderConfig
from .postgres import PostgresCommunicationsStore, SmsPreparationEligibility
from .event_review import CommunicationsEventReview
from .store import CommunicationsError, Eligibility

router = APIRouter()
CALLBACK_PATHS = {"sms_inbound": "/callbacks/twilio/sms-inbound",
    "sms_status": "/callbacks/twilio/sms-status", "voice_status": "/callbacks/twilio/voice-status",
    "voice_gather": "/callbacks/twilio/voice-action"}


def public_sender(config):
    base = config.twilio_callback_base_url.rstrip("/")
    return SenderConfig(account_sid=config.twilio_account_sid, from_number=config.twilio_from_number,
        sms_inbound_url=base + CALLBACK_PATHS["sms_inbound"], sms_status_url=base + CALLBACK_PATHS["sms_status"],
        voice_status_url=base + CALLBACK_PATHS["voice_status"], voice_action_url=base + CALLBACK_PATHS["voice_gather"],
        practice_name=config.practice_name, messaging_service_sid=config.twilio_messaging_service_sid)


def dispatch_policy(config):
    return DispatchPolicy(practice_name=config.practice_name, timezone=config.practice_timezone,
        contact_max_age_hours=config.contact_snapshot_max_age_hours,
        scheduler_enabled=config.chase_scheduler_enabled and config.chase_transport == "twilio",
        patient_messaging_enabled=config.patient_messaging_enabled,
        operationally_activated=config.patient_messaging_operationally_activated,
        enabled_channels=config.communications_enabled_channels,
        test_mode=config.communications_test_mode, test_recipients=config.communications_test_recipients,
        test_max_dispatch_starts=config.communications_test_max_dispatch_starts)


def dispatcher_from_settings(config, *, dsn=None, adapter=None, audit=None):
    if not config.communications_enabled:
        raise ValueError("communications are disabled")
    store = PostgresCommunicationsStore(dsn or config.chase_dsn, practice_id=config.practice_id,
        transaction_audit=audit, purpose_consent_enabled=True,
        enabled_channels=config.communications_enabled_channels)
    return CommunicationsDispatcher(store, policy=dispatch_policy(config), sender=public_sender(config),
        roster=DirectContractList(settings=config), adapter=adapter, transaction_audit=audit)


def check_schema(config):
    dispatcher = dispatcher_from_settings(config)
    with dispatcher.store._db() as db:
        db.execute("SELECT error_code FROM chase_communication_provider_events LIMIT 0")
        for suffix in ("senders", "bindings", "operations", "events", "suppressions",
                       "policy_decisions", "consent_decisions", "authorizations",
                       "dispatch_starts", "dispatch_receipts", "provider_events", "dispatch_checks", "cancellations", "unknown_resolutions", "event_reviews"):
            table = "chase_communication_" + suffix
            row = db.execute("SELECT has_table_privilege(current_user,%s,'SELECT') AS allowed", (table,)).fetchone()
            if not row or row["allowed"] is not True:
                raise CommunicationsError(503, "communications_schema_unavailable")
        for view in ("chase_communication_review_sources", "chase_communication_unassigned_sources"):
            if not db.execute("SELECT has_table_privilege(current_user,%s,'SELECT') AS allowed", (view,)).fetchone()["allowed"]:
                raise CommunicationsError(503, "communications_schema_unavailable")
        for suffix in ("senders", "bindings", "operations", "events", "policy_decisions", "consent_decisions", "authorizations", "cancellations", "unknown_resolutions", "event_reviews"):
            row = db.execute("SELECT has_table_privilege(current_user,%s,'INSERT') AS allowed",
                             ("chase_communication_" + suffix,)).fetchone()
            if not row or row["allowed"] is not True:
                raise CommunicationsError(503, "communications_schema_unavailable")


class CaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    chaseId: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class BindRequest(CaseRequest):
    channel: str = Field(pattern=r"^(sms|automated_voice)$")
    contactSnapshotId: str = Field(min_length=36, max_length=36)
    confirmedContact: bool


class ConsentRequest(CaseRequest):
    actionId: str = Field(min_length=36, max_length=36)
    bindingId: str = Field(min_length=36, max_length=36)
    granted: bool
    sourceReference: str = Field(min_length=1, max_length=200)
    confirmed: bool


class PrepareRequest(CaseRequest):
    actionId: str = Field(min_length=36, max_length=36)
    channel: str = Field(pattern=r"^(sms|automated_voice)$")
    text: str | None = Field(default=None, min_length=1, max_length=1000)


class AuthorizeRequest(CaseRequest):
    operationId: str = Field(min_length=36, max_length=36)
    confirmed: bool


class CancelRequest(AuthorizeRequest):
    pass


class ResolveUnknownRequest(AuthorizeRequest):
    decision: str = Field(pattern=r"^(confirmed_delivered|confirmed_not_sent|confirmed_attempted)$")
    vendorReference: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:/-]+$")


class ReviewEventRequest(CaseRequest):
    actionId: str = Field(min_length=36, max_length=36)
    eventRef: str = Field(pattern=r"^[0-9a-f]{64}$")
    sourceHash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: str = Field(pattern=r"^(handled|attribute|leave_unassigned)$")
    contactSnapshotId: str | None = Field(default=None, min_length=36, max_length=36)
    confirmed: bool


class PolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    actionId: str = Field(min_length=36, max_length=36)
    approved: bool
    expectedPolicyHash: str = Field(pattern=r"^[0-9a-f]{64}$")


def _context(request, chase_id=None, *, admin=False):
    from ... import api
    principal = api._require_chase_staff(request)
    if admin and not principal.has_any_role(("Admin",)):
        raise HTTPException(403, "communications_admin_required")
    config = api._request_settings(request)
    if not config.communications_enabled:
        raise HTTPException(404, "communications_unavailable")
    if not request.app.state.atomic_chase_audit or principal.practice_id != config.practice_id:
        raise HTTPException(503, "communications_audit_unavailable")
    chase = api._request_chase_context(request)["store"]
    if chase_id:
        if chase.get(chase_id) is None:
            raise HTTPException(404, "communications_case_not_found")
        api._set_audit_resource(request, chase.audit_resource_id(chase_id))
    def audit(db, action, case_id, operation_id):
        if case_id is not None and case_id != chase_id:
            raise CommunicationsError(409, "communications_case_mismatch")
        PostgresAccessAuditStore._insert_completion(db, api._completion_event(request))
    return dispatcher_from_settings(config, audit=audit), principal


def _contact(dispatcher, case_id):
    store = dispatcher.store
    with store._db() as db:
        snapshot = db.execute("""SELECT contact_snapshot_id,source_system,verified_at FROM chase_contact_snapshots
            WHERE practice_id=%s AND chase_id=%s ORDER BY verified_at DESC,created_at DESC,contact_snapshot_id DESC LIMIT 1""",
            (store.practice_id, case_id)).fetchone()
        if not snapshot:
            return None, {}
        routes = db.execute("SELECT channel,destination,consent_status FROM chase_contact_routes WHERE practice_id=%s AND contact_snapshot_id=%s AND channel IN ('sms','phone')",
            (store.practice_id, snapshot["contact_snapshot_id"])).fetchall()
        bindings = db.execute("""SELECT b.*,c.granted FROM chase_communication_bindings b
            LEFT JOIN LATERAL (SELECT granted FROM chase_communication_consent_decisions c
              WHERE c.practice_id=b.practice_id AND c.binding_id=b.binding_id ORDER BY sequence_no DESC LIMIT 1) c ON true
            WHERE b.practice_id=%s AND b.case_id=%s AND b.contact_snapshot_id=%s AND b.account_sid=%s AND b.sender=%s""",
            (store.practice_id, case_id, snapshot["contact_snapshot_id"], dispatcher.sender.account_sid,
             dispatcher.sender.from_number)).fetchall()
        return {"snapshotId": str(snapshot["contact_snapshot_id"]), "source": snapshot["source_system"],
                "verifiedAt": snapshot["verified_at"].astimezone(timezone.utc).isoformat(),
                "routes": [{"channel": "sms" if r["channel"] == "sms" else "automated_voice",
                    "destination": r["destination"], "consentStatus": r["consent_status"]} for r in routes]}, {
                    b["channel"]: {"bindingId": str(b["binding_id"]), "consentGranted": b["granted"] is True} for b in bindings}


def _candidate(dispatcher, principal, case_id, channel, binding_id, text, operation_id=None):
    return {"operation_id": operation_id or "00000000-0000-4000-8000-000000000000", "case_id": case_id,
        "channel": channel, "binding_id": binding_id, "text": text,
        "actor_tenant_id": principal.tenant_id, "actor_id": principal.user_id}


def staff_view(dispatcher, principal, case_id):
    view = dispatcher.case_view(case_id)
    contact, bindings = _contact(dispatcher, case_id)
    actions = {}
    with dispatcher.store._db() as db:
        def availability(channel, phase):
            binding = bindings.get(channel)
            reason = "communications_contact_not_bound"
            if channel not in dispatcher.policy.enabled_channels:
                reason = "communications_channel_disabled"
            elif binding:
                text = dispatcher.policy.templates[0]["text"] if channel == "sms" else dispatcher.call_script
                try:
                    dispatcher._gates(db, _candidate(dispatcher, principal, case_id, channel,
                        binding["bindingId"], text), phase=phase)
                    reason = ""
                except CommunicationsError as exc:
                    reason = exc.code
            return {"allowed": not reason, "reason": reason}
        for channel, label in (("sms", "sms"), ("automated_voice", "automatedVoice")):
            actions[label] = availability(channel, "prepare")
            if channel == "sms":
                actions[label]["authorization"] = availability(channel, "authorize")
        allowance = dispatcher.test_dispatch_allowance(db)
        try:
            dispatcher._policy_decision(db)
            approved = True
        except CommunicationsError:
            approved = False
    return {**view, "schemaVersion": "chase-communications.v1", "chaseId": case_id,
        "actions": actions, "contact": contact, "bindings": bindings,
        "policy": {"hash": dispatcher.policy_hash, "approved": approved,
            "canApprove": principal.has_any_role(("Admin",)), "practiceName": dispatcher.policy.practice_name,
            "timezone": dispatcher.policy.timezone, "quietHours": list(dispatcher.policy.quiet_hours),
            "maxTotalTouches": dispatcher.policy.max_total_touches, "maxTouches24h": dispatcher.policy.max_touches_24h,
            "testDispatchAllowance": allowance,
            "outboundEnabled": all((dispatcher.policy.scheduler_enabled, dispatcher.policy.patient_messaging_enabled,
                                     dispatcher.policy.operationally_activated))}}


def _safe(exc):
    return HTTPException(exc.status, exc.code)


def read(request, req):
    try:
        dispatcher, principal = _context(request, req.chaseId)
        return staff_view(dispatcher, principal, req.chaseId)
    except CommunicationsError as exc:
        raise _safe(exc) from None


def prepare(request, req):
    try:
        dispatcher, principal = _context(request, req.chaseId)
        dispatcher.require_channel(req.channel)
        _contact_data, bindings = _contact(dispatcher, req.chaseId)
        binding = bindings.get(req.channel)
        if not binding:
            raise CommunicationsError(409, "communications_contact_not_bound")
        if req.channel == "automated_voice" and req.text is not None:
            raise CommunicationsError(422, "communications_invalid_message")
        text = req.text if req.channel == "sms" else dispatcher.call_script
        if not text:
            raise CommunicationsError(422, "communications_invalid_message")
        with dispatcher.store._db() as db:
            dispatcher._gates(db, _candidate(dispatcher, principal, req.chaseId, req.channel,
                binding["bindingId"], text, req.actionId), phase="prepare")
        eligibility = (SmsPreparationEligibility(role_allowed=True, case_open=True,
            direct_contract_clear=True, consent_valid=True, practice_enabled=True,
            policy_approved=True, touch_limit_clear=True) if req.channel == "sms" else
            Eligibility(role_allowed=True, case_open=True, direct_contract_clear=True,
                consent_valid=True, practice_enabled=True, quiet_hours_clear=True, touch_limit_clear=True))
        dispatcher.store.prepare(case_id=req.chaseId, binding_id=binding["bindingId"], operation_id=req.actionId,
            channel=req.channel, text=text, actor_tenant_id=principal.tenant_id, actor_id=principal.user_id,
            eligibility=eligibility)
        request.state.audit_completion_committed = True
        return staff_view(dispatcher, principal, req.chaseId)
    except CommunicationsError as exc:
        raise _safe(exc) from None


@router.post("/api/chase/communications/bind")
def bind(request: Request, req: BindRequest):
    try:
        dispatcher, principal = _context(request, req.chaseId)
        dispatcher.require_channel(req.channel)
        if req.confirmedContact is not True:
            raise CommunicationsError(422, "communications_confirmation_required")
        dispatcher.store.bind_case(case_id=req.chaseId, channel=req.channel, contact_snapshot_id=req.contactSnapshotId,
            sender=dispatcher.sender.from_number, account_sid=dispatcher.sender.account_sid,
            actor_tenant_id=principal.tenant_id, actor_id=principal.user_id)
        request.state.audit_completion_committed = True
        return staff_view(dispatcher, principal, req.chaseId)
    except CommunicationsError as exc:
        raise _safe(exc) from None


@router.post("/api/chase/communications/consent")
def consent(request: Request, req: ConsentRequest):
    try:
        dispatcher, principal = _context(request, req.chaseId)
        _data, bindings = _contact(dispatcher, req.chaseId)
        if req.confirmed is not True or req.bindingId not in {b["bindingId"] for b in bindings.values()}:
            raise CommunicationsError(409, "communications_contact_stale")
        dispatcher.record_consent(decision_id=req.actionId, binding_id=req.bindingId, granted=req.granted,
            source_reference=req.sourceReference, actor_tenant_id=principal.tenant_id, actor_id=principal.user_id)
        request.state.audit_completion_committed = True
        return staff_view(dispatcher, principal, req.chaseId)
    except CommunicationsError as exc:
        raise _safe(exc) from None


@router.post("/api/chase/communications/authorize")
def authorize(request: Request, req: AuthorizeRequest):
    try:
        dispatcher, principal = _context(request, req.chaseId)
        with dispatcher.store._db() as db:
            operation = dispatcher._operation(db, req.operationId)
            if operation["case_id"] != req.chaseId or req.confirmed is not True:
                raise CommunicationsError(409, "communications_case_mismatch")
        dispatcher.authorize(req.operationId, actor_tenant_id=principal.tenant_id, actor_id=principal.user_id)
        request.state.audit_completion_committed = True
        return staff_view(dispatcher, principal, req.chaseId)
    except CommunicationsError as exc:
        raise _safe(exc) from None


@router.post("/api/chase/communications/cancel")
def cancel(request: Request, req: CancelRequest):
    try:
        dispatcher, principal = _context(request, req.chaseId)
        if req.confirmed is not True:
            raise CommunicationsError(422, "communications_confirmation_required")
        with dispatcher.store._db() as db:
            operation = dispatcher._operation(db, req.operationId)
            if operation["case_id"] != req.chaseId:
                raise CommunicationsError(409, "communications_case_mismatch")
        dispatcher.cancel(req.operationId, actor_tenant_id=principal.tenant_id, actor_id=principal.user_id)
        request.state.audit_completion_committed = True
        return staff_view(dispatcher, principal, req.chaseId)
    except CommunicationsError as exc:
        raise _safe(exc) from None


@router.post("/api/chase/communications/resolve-unknown")
def resolve_unknown(request: Request, req: ResolveUnknownRequest):
    try:
        dispatcher, principal = _context(request, req.chaseId)
        if req.confirmed is not True:
            raise CommunicationsError(422, "communications_confirmation_required")
        with dispatcher.store._db() as db:
            operation = dispatcher._operation(db, req.operationId)
            if operation["case_id"] != req.chaseId:
                raise CommunicationsError(409, "communications_case_mismatch")
        dispatcher.resolve_unknown(req.operationId, decision=req.decision, vendor_reference=req.vendorReference,
            actor_tenant_id=principal.tenant_id, actor_id=principal.user_id)
        request.state.audit_completion_committed = True
        return staff_view(dispatcher, principal, req.chaseId)
    except CommunicationsError as exc:
        raise _safe(exc) from None


@router.post("/api/chase/communications/policy/decision")
def policy_decision(request: Request, req: PolicyRequest):
    try:
        dispatcher, principal = _context(request, admin=True)
        if req.expectedPolicyHash != dispatcher.policy_hash:
            raise CommunicationsError(409, "communications_policy_changed")
        result = dispatcher.approve_policy(decision_id=req.actionId, approved=req.approved,
            actor_tenant_id=principal.tenant_id, actor_id=principal.user_id)
        request.state.audit_completion_committed = True
        return result
    except CommunicationsError as exc:
        raise _safe(exc) from None


@router.post("/api/chase/communications/review-events")
def review_events(request: Request, req: CaseRequest):
    try:
        dispatcher, _principal = _context(request, req.chaseId)
        return CommunicationsEventReview(dispatcher).read(req.chaseId)
    except CommunicationsError as exc:
        raise _safe(exc) from None


@router.post("/api/chase/communications/review-event")
def review_event(request: Request, req: ReviewEventRequest):
    try:
        dispatcher, principal = _context(request, req.chaseId)
        if req.confirmed is not True:
            raise CommunicationsError(422, "communications_confirmation_required")
        result = CommunicationsEventReview(dispatcher).decide(req.chaseId,
            action_id=req.actionId, event_ref=req.eventRef, source_hash=req.sourceHash,
            decision=req.decision, contact_snapshot_id=req.contactSnapshotId,
            actor_tenant_id=principal.tenant_id, actor_id=principal.user_id)
        request.state.audit_completion_committed = True
        return result
    except CommunicationsError as exc:
        raise _safe(exc) from None


class InboxCountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class InboxPageRequest(InboxCountRequest):
    state: str = Field(default="pending", pattern=r"^(pending|reviewed)$")
    cursor: str | None = Field(default=None, max_length=700)
    limit: int = Field(default=50, ge=1, le=50)


class InboxSourceRequest(InboxCountRequest):
    eventRef: str = Field(pattern=r"^[0-9a-f]{64}$")
    sourceHash: str = Field(pattern=r"^[0-9a-f]{64}$")


class InboxCasesRequest(InboxSourceRequest):
    cursor: str | None = Field(default=None, max_length=700)
    limit: int = Field(default=20, ge=1, le=20)


class InboxDecisionRequest(InboxSourceRequest):
    actionId: str = Field(min_length=36, max_length=36)
    decision: str = Field(pattern=r"^(attribute|leave_unassigned)$")
    chaseId: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    contactSnapshotId: str | None = Field(default=None, min_length=36, max_length=36)
    confirmed: bool


@router.post("/api/chase/communications/unassigned-texts/count")
def unassigned_count(request: Request, req: InboxCountRequest):
    try:
        dispatcher, _ = _context(request)
        return {"schemaVersion": "communications-unassigned-count.v1",
                "pendingCount": CommunicationsEventReview(dispatcher).unassigned_count()}
    except CommunicationsError as exc:
        raise _safe(exc) from None


@router.post("/api/chase/communications/unassigned-texts")
def unassigned_texts(request: Request, req: InboxPageRequest):
    try:
        dispatcher, _ = _context(request)
        return CommunicationsEventReview(dispatcher).unassigned(state=req.state, cursor=req.cursor, limit=req.limit)
    except CommunicationsError as exc:
        raise _safe(exc) from None


@router.post("/api/chase/communications/unassigned-text-cases")
def unassigned_text_cases(request: Request, req: InboxCasesRequest):
    try:
        dispatcher, _ = _context(request)
        return CommunicationsEventReview(dispatcher).unassigned_cases(event_ref=req.eventRef,
            source_hash=req.sourceHash, cursor=req.cursor, limit=req.limit)
    except CommunicationsError as exc:
        raise _safe(exc) from None


@router.post("/api/chase/communications/review-unassigned-text")
def review_unassigned_text(request: Request, req: InboxDecisionRequest):
    try:
        dispatcher, principal = _context(request, req.chaseId)
        if req.confirmed is not True:
            raise CommunicationsError(422, "communications_confirmation_required")
        result = CommunicationsEventReview(dispatcher).decide_unassigned(action_id=req.actionId,
            event_ref=req.eventRef, source_hash=req.sourceHash, decision=req.decision,
            case_id=req.chaseId, contact_snapshot_id=req.contactSnapshotId,
            actor_tenant_id=principal.tenant_id, actor_id=principal.user_id)
        request.state.audit_completion_committed = True
        return result
    except CommunicationsError as exc:
        raise _safe(exc) from None
