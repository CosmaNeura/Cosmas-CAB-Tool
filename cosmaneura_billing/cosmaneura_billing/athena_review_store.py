"""Append-only, practice-scoped Athena review receipts and selected evidence."""
from contextlib import contextmanager
from threading import RLock
from uuid import NAMESPACE_URL, uuid5

from .athena_review import ReviewActor, detached, require, text, uid
from .access_audit import AccessAuditEvent
from .persistence import MemoryEncounterStore, PostgresAccessAuditStore


def _empty(chase_id):
    return {"schemaVersion": "athena-case-review.v1", "chaseId": chase_id, "revision": 0,
            "reviewId": None, "candidate": None, "applied": None}


def _audit(event, actor, practice, resource, action, *, mandatory):
    if event is None:
        require(not mandatory, "audit_required")
        return
    require(type(event) is AccessAuditEvent and event.practice_id == practice
            and event.actor_tenant_id == actor.tenant_id and event.actor_user_id == actor.user_id
            and actor.role in event.actor_roles and event.resource_id == resource
            and event.action == action and event.method == "POST" and event.outcome == "allowed"
            and type(event.status) is int and 200 <= event.status < 300, "audit_required")
    uid(event.event_id)


class MemoryAthenaReviewStore:
    durable = False

    def __init__(self, chase_store, *, practice_id, authorize, encounter_store=None):
        self.chase_store, self.practice_id, self.authorize = chase_store, text(practice_id), authorize
        self.encounters = encounter_store or MemoryEncounterStore(practice_id=practice_id)
        require(self.encounters.practice_id == practice_id, "not_authorized")
        self._lock, self._data = RLock(), {"views": {}, "receipts": {}, "audit": [], "contacts": {}}

    @contextmanager
    def unit(self, chase_id, actor, operation_id=None):
        require(type(actor) is ReviewActor and self.authorize(actor) is True, "not_authorized")
        actor.__post_init__()
        text(chase_id)
        if operation_id:
            uid(operation_id)
        with self._lock:
            case = self.chase_store.get(chase_id)
            require(case is not None, "not_found")
            unit = _MemoryUnit(self, chase_id, case.patient_ref)
            yield unit
            if unit.encounter is not None:
                self.encounters.upsert(unit.encounter, source=unit.encounter_source)
            self._data = unit.data

    def healthcheck(self):
        pass


class _MemoryUnit:
    def __init__(self, store, chase_id, patient):
        self.store, self.chase_id, self.patient_ref = store, chase_id, patient
        self.data = detached(store._data)
        self.encounter = None
        self.encounter_source = None
        self.resource = str(uuid5(NAMESPACE_URL, f"{store.practice_id}:{chase_id}"))

    def current(self):
        return detached(self.data["views"].get(self.chase_id, _empty(self.chase_id)))

    def receipt(self, operation):
        return self.data["receipts"].get(operation)

    def audit(self, event, actor, action):
        _audit(event, actor, self.store.practice_id, self.resource, action, mandatory=False)
        self.data["audit"].append({"action": action, "operation": event.event_id if event else None})

    def save(self, view, operation, intent, actor, event, action, contacts=None, encounter=None):
        self.audit(event, actor, action)
        self.encounter = encounter
        self.encounter_source = view.get("billingImport", {}).get("source") if encounter else None
        self.data["views"][self.chase_id] = detached(view)
        self.data["receipts"][operation] = {"intent": intent, "payload": detached(view), "action": action}
        if contacts is not None:
            self.data["contacts"][self.chase_id] = detached(contacts)


class PostgresAthenaReviewStore:
    durable = True

    def __init__(self, context, *, bindings=None, deployment_id="", storage_id=""):
        self.context, self.practice_id = context, context.practice_id
        self.bindings = bindings
        self.deployment_id,self.storage_id=deployment_id,storage_id

    @contextmanager
    def unit(self, chase_id, actor, operation_id=None, *, resolve_binding=False, allow_missing_binding=False):
        require(type(actor) is ReviewActor, "not_authorized")
        actor.__post_init__()
        text(chase_id)
        with self.context.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                         (f"athena-review-case:{self.practice_id}:{chase_id}",))
            if operation_id:
                uid(operation_id)
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                             (f"athena-review:{self.practice_id}:{operation_id}",))
            allowed = conn.execute("SELECT public.cosmaneura_athena_reviewer_allowed(%s::uuid,%s::uuid,%s) AS allowed",
                (actor.tenant_id, actor.user_id, actor.role)).fetchone()
            require(allowed and allowed["allowed"] is True, "not_authorized")
            scope, binding_error = None, None
            if resolve_binding:
                from .athena_bindings import AthenaBindingError
                require(self.bindings is not None, "source_unavailable")
                try:
                    scope = self.bindings.in_transaction(conn, chase_id, actor)
                except AthenaBindingError as exc:
                    if not allow_missing_binding or exc.code in {"not_authorized", "not_found"}:
                        require(False, exc.code if exc.code in {"not_authorized", "not_found"} else "source_unavailable")
                    binding_error = exc.code
            case = conn.execute("SELECT patient_ref,chase_uid FROM chase_items WHERE practice_id=%s AND chase_id=%s FOR UPDATE",
                                (self.practice_id, chase_id)).fetchone()
            require(case is not None, "not_found")
            unit = _PostgresUnit(self, conn, chase_id, case)
            unit.bound_scope = scope
            unit.binding_error = binding_error
            from .athena_admission import resolve_lease,AthenaAdmissionError
            try:unit.read_lease=resolve_lease(conn,scope,actor,self.deployment_id,self.storage_id) if scope else None
            except AthenaAdmissionError:require(False,"source_unavailable")
            try:yield unit
            finally:
                if unit.read_lease:unit.read_lease.close()

    def healthcheck(self):
        if self.bindings is not None:
            self.bindings.healthcheck()
        with self.context.connection() as conn:
            row = conn.execute("SELECT has_table_privilege(current_user,'athena_case_review_events','SELECT') AND has_table_privilege(current_user,'athena_case_review_events','INSERT') AS ok").fetchone()
            require(row and row["ok"], "source_unavailable")
            if self.bindings is not None:
                guard = conn.execute("SELECT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid='athena_case_review_events'::regclass AND tgname='athena_configured_review_guard' AND tgenabled='A') AS ok").fetchone()
                require(guard and guard["ok"], "source_unavailable")
                if self.deployment_id or self.storage_id:
                    row=conn.execute("SELECT has_function_privilege(current_user,'public.cosmaneura_athena_read_admission(uuid,uuid,text,text,text,text)','EXECUTE') AND NOT has_table_privilege(current_user,'public.athena_staff_read_admissions','SELECT,INSERT,UPDATE,DELETE,TRUNCATE') AS ok").fetchone()
                    require(row and row["ok"],"source_unavailable")


class _PostgresUnit:
    def __init__(self, store, conn, chase_id, case):
        self.store, self.conn, self.chase_id = store, conn, chase_id
        self.patient_ref, self.resource = case["patient_ref"], str(case["chase_uid"])
        self.practice = store.practice_id

    def current(self):
        row = self.conn.execute("SELECT payload FROM athena_case_review_events WHERE practice_id=%s AND chase_id=%s ORDER BY revision DESC LIMIT 1",
                                (self.practice, self.chase_id)).fetchone()
        return detached(row["payload"]) if row else _empty(self.chase_id)

    def receipt(self, operation):
        row = self.conn.execute("SELECT request_sha256,payload,action FROM athena_case_review_events WHERE practice_id=%s AND operation_id=%s::uuid",
                                (self.practice, operation)).fetchone()
        return {"intent": row["request_sha256"], "payload": detached(row["payload"]), "action": row["action"]} if row else None

    def audit(self, event, actor, action):
        _audit(event, actor, self.practice, self.resource, action, mandatory=True)
        PostgresAccessAuditStore._insert_completion(self.conn, event)

    def save(self, view, operation, intent, actor, event, action, contacts=None, encounter=None):
        if view.get("schemaVersion")=="athena-case-review.v3":
            from .athena_admission import AthenaAdmissionError
            require(self.read_lease is not None and self.read_lease.public==view.get("sourceAdmission"),"source_unavailable")
            try:self.read_lease.assert_active(self.bound_scope)
            except AthenaAdmissionError:require(False,"source_unavailable")
        self.audit(event, actor, action)
        if encounter is not None:
            self.conn.execute("""INSERT INTO encounters(practice_id,external_id,patient_age,patient_sex,
                encounter_type,place_of_service,intake_text,transcript,signed_note,provider_specialty,
                submitted_em_code,submitted_icd10,source,ingested_by_tenant_id,ingested_by_user_id)
                VALUES(%s,%s,%s,%s,%s,%s,'','',%s,%s,NULL,ARRAY[]::text[],%s,%s::uuid,%s::uuid)""",
                (self.practice, encounter.encounter_id, encounter.patient_age, encounter.patient_sex,
                 encounter.encounter_type, encounter.place_of_service, encounter.signed_note,
                 encounter.provider_specialty, view["billingImport"]["source"], actor.tenant_id, actor.user_id))
        self.conn.execute("""INSERT INTO athena_case_review_events(practice_id,chase_id,revision,operation_id,
            request_sha256,payload,actor_tenant_id,actor_user_id,actor_role,access_event_id,action)
            VALUES(%s,%s,%s,%s::uuid,%s,%s,%s::uuid,%s::uuid,%s,%s::uuid,%s)""",
            (self.practice, self.chase_id, view["revision"], operation, intent, self.store.context.Jsonb(view),
             actor.tenant_id, actor.user_id, actor.role, event.event_id, action))
        if contacts is not None:
            # Typed contact destinations remain separate from transport outbox.
            # Every channel is unknown-consent; this does not select a cadence.
            self.conn.execute("""INSERT INTO chase_contact_snapshots(practice_id,contact_snapshot_id,chase_id,
                source_system,source_version,observed_at,verified_at,verification_method,
                verified_by_tenant_id,verified_by_user_id)
                VALUES(%s,%s::uuid,%s,'athena.staff_review',%s,%s,now(),'staff',%s::uuid,%s::uuid)""",
                (self.practice, operation, self.chase_id, view["candidate"]["sourceDigest"],
                 view["candidate"]["source"]["observedAt"], actor.tenant_id, actor.user_id))
            for contact in contacts:
                channels = ("email",) if contact["field"] == "email" else ("phone", "sms")
                for channel in channels:
                    self.conn.execute("""INSERT INTO chase_contact_routes(practice_id,contact_snapshot_id,
                        channel,destination,consent_status) VALUES(%s,%s::uuid,%s,%s,'unknown')""",
                        (self.practice, operation, channel, contact["value"]))
