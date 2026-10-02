"""Append-only billing headers with practice/source locks and atomic audit.

Memory storage is an explicitly injected local test seam. PostgreSQL derives
practice from PostgresContext, rechecks active membership, and locks the source
record in the same transaction as the revision and access-audit completion.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from threading import RLock

from . import billing_header as domain
from .access_audit import AccessAuditEvent
from .persistence import PostgresAccessAuditStore, PostgresEncounterStore, PostgresFeedbackStore
from .billing_header_signoff import MemoryHeaderFeedbackAdapter, SignoffStoreMixin


def _actor(actor):
    domain.require(isinstance(actor, domain.HeaderActor), "not_authorized")
    # Do not trust a dataclass constructed through an unvalidated Python seam.
    return domain.validated_actor(actor)


def _audit_check(event, practice, actor, resource, action, *, mandatory):
    if event is None:
        domain.require(not mandatory, "audit_required")
        return
    domain.require(isinstance(event, AccessAuditEvent), "audit_required")
    domain.require(event.practice_id == practice and event.actor_tenant_id == actor.tenant_id
                   and event.actor_user_id == actor.user_id and event.resource_id == resource
                   and event.action == action and event.outcome == "allowed"
                   and event.method == "POST" and type(event.status) is int
                   and 200 <= event.status < 300 and actor.role in event.actor_roles,
                   "audit_required")
    domain.uuid(event.event_id)


class _Store(SignoffStoreMixin):
    """Shared transaction algorithm; adapters only supply storage primitives."""

    def latest(self, encounter_id, *, expected_source):
        with self._unit(encounter_id) as unit:
            domain.check_source(unit.stored, expected_source)
            record = unit.latest(domain.digest(expected_source))
            return self._view(unit, record) if record else None

    @staticmethod
    def _view(unit, record):
        domain.require(record is not None, "store_invalid")
        for key, validate in (("dataReview", domain.valid_data_review),
                              ("clinicalReview", domain.valid_clinical_review)):
            if key in record["header"]:
                domain.require(validate(record["header"][key], unit.stored.bundle), "store_invalid")
        rows = unit.revisions(record["headerId"])
        domain.require(all(row["number"] == index + 1 for index, row in enumerate(rows)), "store_invalid")
        if rows:
            from .billing_header_mapping import validate_mappings
            validate_mappings(record["header"], rows[-1]["draft"]["codes"], current=False)
        result = domain.view(record["header"], rows)
        receipt = unit.signoff_receipt(header_id=record["headerId"])
        signed = domain.detached(receipt["receipt"]) if receipt else None
        result["signoff"] = signed if signed and signed["headerRevision"] == len(rows) else None
        result["previousSignoff"] = ({key: signed[key] for key in
                                     ("reviewId", "headerRevision", "createdAt", "actor")}
                                    if signed and result["signoff"] is None else None)
        return result

    @staticmethod
    def _intent(kind, encounter_id, expected_source, actor, **fields):
        return domain.digest({"kind": kind, "encounterId": encounter_id,
                              "expectedSource": expected_source,
                              "actor": {"tenantId": actor.tenant_id, "userId": actor.user_id,
                                        "role": actor.role}, **fields})

    def _finish(self, unit, record, actor, expected_source, audit_completion, action):
        domain.check_source(unit.current_source(), expected_source)
        _audit_check(audit_completion, self.practice_id, actor, unit.stored.resource_id,
                     action, mandatory=self.durable)
        unit.audit(audit_completion, actor, action, record["headerId"])

    def create(self, encounter_id, *, expected_source, generated, actor, operation_id,
               audit_completion=None, before_insert=None):
        actor = _actor(actor)
        domain.uuid(operation_id)
        intent = self._intent("create", encounter_id, expected_source, actor)
        with self._unit(encounter_id, actor, operation_id) as unit:
            domain.check_source(unit.stored, expected_source)
            receipt = unit.receipt(operation_id)
            if receipt:
                domain.require(receipt["requestDigest"] == intent, "operation_conflict")
                record = unit.header(receipt["headerId"])
                result = self._view(unit, record)
                created = False
            else:
                record = unit.latest(domain.digest(expected_source))
                created = record is None
                if created and generated is None:
                    return None, False  # Read-before-inference probe; no receipt or mutation.
                if created:
                    if before_insert is not None:
                        before_insert(unit.conn, unit.stored)
                    header = domain.make_header(generated, unit.stored, actor, operation_id, self.clock())
                    record = {"headerId": header["headerId"], "header": header,
                              "sourceDigest": domain.digest(expected_source)}
                    unit.insert_header(record, actor)
                result = self._view(unit, record)
                unit.insert_receipt(operation_id, intent, record["headerId"], result["revisionNumber"], actor, audit_completion)
            self._finish(unit, record, actor, expected_source, audit_completion, "billing_header.compute")
            return result, created

    def generation_replay(self, encounter_id, *, expected_source, operation_id,
                          actor, audit_completion=None):
        return self.create(encounter_id, expected_source=expected_source, generated=None,
                           actor=actor, operation_id=operation_id,
                           audit_completion=audit_completion)[0]

    def save(self, encounter_id, *, header_id, expected_source, expected_revision,
             draft, reason, actor, operation_id, audit_completion=None):
        actor = _actor(actor)
        domain.uuid(operation_id)
        domain.uuid(header_id)
        domain.integer(expected_revision, 0, domain.MAX_REVISIONS)
        # Hash raw validated JSON intent, not server-enriched revision metadata.
        intent = self._intent("save", encounter_id, expected_source, actor,
                              headerId=header_id, expectedRevision=expected_revision,
                              draft=draft, reason=reason)
        with self._unit(encounter_id, actor, operation_id) as unit:
            domain.check_source(unit.stored, expected_source)
            receipt = unit.receipt(operation_id)
            if receipt:
                domain.require(receipt["requestDigest"] == intent, "operation_conflict")
                record = unit.header(receipt["headerId"])
                result = self._view(unit, record)
                created = False
            else:
                record = unit.header(header_id)
                domain.require(record is not None, "header_not_found")
                domain.require(domain.canonical(record["header"]["expectedSource"]) == domain.canonical(expected_source), "source_changed")
                current = self._view(unit, record)
                revision = domain.make_revision(record["header"], current["revision"], draft,
                                                actor, reason, expected_revision, self.clock())
                unit.insert_revision(header_id, revision, actor)
                unit.insert_receipt(operation_id, intent, header_id, revision["number"], actor, audit_completion)
                result = self._view(unit, record)
                created = True
            self._finish(unit, record, actor, expected_source, audit_completion, "billing_header.save")
            return result, created


class MemoryBillingHeaderStore(_Store):
    """Local-only, detached JSON state with required server authorization hook."""
    durable = False

    def __init__(self, encounter_store, *, practice_id, authorize, clock=domain.utc_now):
        domain.text(practice_id, 100)
        domain.require(callable(authorize), "not_authorized")
        domain.require(getattr(encounter_store, "practice_id", None) == practice_id, "not_authorized")
        self.practice_id, self.encounters, self.authorize = practice_id, encounter_store, authorize
        self.clock, self._lock = clock, RLock()
        self._data = {"headers": {}, "revisions": {}, "receipts": {}, "audit": [],
                      "signoffs": {}, "feedback": {}}

    @property
    def audit_events(self):
        with self._lock:
            return domain.detached(self._data["audit"])

    @contextmanager
    def _unit(self, encounter_id, actor=None, operation_id=None):
        with self._lock:
            domain.text(encounter_id, 500)
            if actor:
                domain.require(self.authorize(actor) is True, "not_authorized")
            unit = _MemoryUnit(self, encounter_id)
            yield unit
            self._data = unit.data

    def healthcheck(self):
        return None


class _MemoryUnit:
    def __init__(self, store, encounter_id):
        self.store, self.encounter_id = store, encounter_id
        self.stored = self.current_source()
        self.data = domain.detached(store._data)

    def current_source(self):
        return self.store.encounters.get(self.encounter_id)

    def latest(self, source_digest):
        return next((row for row in self.data["headers"].values()
                     if row["sourceDigest"] == source_digest
                     and row["header"]["expectedSource"]["encounterId"] == self.encounter_id), None)

    def header(self, header_id):
        record = self.data["headers"].get(header_id)
        return record if record and record["header"]["expectedSource"]["encounterId"] == self.encounter_id else None

    def revisions(self, header_id):
        return self.data["revisions"].get(header_id, [])

    def receipt(self, operation_id):
        return self.data["receipts"].get(operation_id)

    def insert_header(self, record, actor):
        self.data["headers"][record["headerId"]] = record

    def insert_revision(self, header_id, revision, actor):
        self.data["revisions"].setdefault(header_id, []).append(revision)

    def insert_receipt(self, operation_id, intent, header_id, revision, actor, audit_completion):
        self.data["receipts"][operation_id] = {"requestDigest": intent, "headerId": header_id,
                                                "revision": revision}

    def audit(self, event, actor, action, header_id):
        self.data["audit"].append({"action": action, "headerId": header_id,
                                   "actor": actor.to_api(), "at": self.store.clock(),
                                   "accessEventId": event.event_id if event else None})

    def signoff_receipt(self, *, header_id=None, review_id=None):
        if review_id:
            return self.data["signoffs"].get(review_id)
        return max((row for row in self.data["signoffs"].values()
                    if row["receipt"]["headerId"] == header_id),
                   key=lambda row: row["receipt"]["headerRevision"], default=None)

    def append_feedback(self, feedback, store):
        domain.require(isinstance(store, MemoryHeaderFeedbackAdapter)
                       and store.header_store is self.store, "signoff_unavailable")
        return store._append(self.data, feedback)

    def insert_signoff(self, receipt, intent, actor, feedback, audit_completion):
        self.data["signoffs"][receipt["reviewId"]] = {"receipt": receipt, "requestDigest": intent}


class PostgresBillingHeaderStore(_Store):
    durable = True

    def __init__(self, context, *, clock=domain.utc_now):
        self.context, self.practice_id, self.clock = context, context.practice_id, clock

    @contextmanager
    def _unit(self, encounter_id, actor=None, operation_id=None):
        domain.text(encounter_id, 500)
        with self.context.connection() as connection:
            # One operation cannot be replayed concurrently onto another encounter.
            if operation_id:
                connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                                   (f"billing-header:{self.practice_id}:{operation_id}",))
            if actor:
                allowed = connection.execute(
                    "SELECT public.cosmaneura_billing_header_editor_allowed(%s::uuid,%s::uuid,%s) AS allowed",
                    (actor.tenant_id, actor.user_id, actor.role),
                ).fetchone()
                domain.require(allowed and allowed["allowed"] is True, "not_authorized")
            unit = _PostgresUnit(self, connection, encounter_id)
            yield unit

    def _locked_encounter(self, conn, encounter_id):
        return conn.execute(PostgresEncounterStore._select() + " WHERE practice_id=%s AND external_id=%s FOR UPDATE",
                            (self.practice_id, encounter_id)).fetchone()

    def healthcheck(self):
        with self.context.connection() as connection:
            row = connection.execute("""SELECT
                has_table_privilege(current_user, 'billing_headers', 'SELECT')
                AND has_table_privilege(current_user, 'billing_headers', 'INSERT') AS headers,
                has_table_privilege(current_user, 'billing_header_revisions', 'SELECT')
                AND has_table_privilege(current_user, 'billing_header_revisions', 'INSERT') AS revisions,
                has_table_privilege(current_user, 'billing_header_operations', 'SELECT')
                AND has_table_privilege(current_user, 'billing_header_operations', 'INSERT') AS receipts,
                has_table_privilege(current_user, 'billing_header_signoffs', 'SELECT')
                AND has_table_privilege(current_user, 'billing_header_signoffs', 'INSERT') AS signoffs
            """).fetchone()
            domain.require(row and all(row.values()), "store_invalid")


class _PostgresUnit:
    def __init__(self, store, connection, encounter_id):
        self.store, self.conn, self.practice = store, connection, store.practice_id
        self.encounter_id = encounter_id
        row = store._locked_encounter(connection, encounter_id)
        self.stored = PostgresEncounterStore._bundle(row) if row else None

    def current_source(self):
        # FOR UPDATE remains held through commit; another writer cannot change it.
        return self.stored

    @staticmethod
    def _record(row):
        if not row:
            return None
        domain.require(domain.digest(row["baseline"]) == row["baseline_sha256"], "store_invalid")
        return {"headerId": str(row["header_id"]), "header": row["baseline"],
                "sourceDigest": row["source_sha256"]}

    def latest(self, source_digest):
        row = self.conn.execute(
            "SELECT header_id,baseline,baseline_sha256,source_sha256 FROM billing_headers WHERE practice_id=%s AND encounter_uid=%s::uuid AND source_sha256=%s",
            (self.practice, self.stored.resource_id, source_digest),
        ).fetchone()
        return self._record(row)

    def header(self, header_id):
        row = self.conn.execute(
            "SELECT header_id,baseline,baseline_sha256,source_sha256 FROM billing_headers WHERE practice_id=%s AND encounter_uid=%s::uuid AND header_id=%s::uuid",
            (self.practice, self.stored.resource_id, header_id),
        ).fetchone()
        return self._record(row)

    def revisions(self, header_id):
        rows = self.conn.execute(
            "SELECT revision_payload,payload_sha256 FROM billing_header_revisions WHERE practice_id=%s AND header_id=%s::uuid ORDER BY revision LIMIT 101",
            (self.practice, header_id),
        ).fetchall()
        domain.require(len(rows) <= domain.MAX_REVISIONS, "store_invalid")
        for row in rows:
            domain.require(domain.digest(row["revision_payload"]) == row["payload_sha256"], "store_invalid")
        return [row["revision_payload"] for row in rows]

    def receipt(self, operation_id):
        row = self.conn.execute(
            "SELECT request_sha256,header_id,revision FROM billing_header_operations WHERE practice_id=%s AND operation_id=%s::uuid",
            (self.practice, operation_id),
        ).fetchone()
        return {"requestDigest": row["request_sha256"], "headerId": str(row["header_id"]),
                "revision": row["revision"]} if row else None

    def insert_header(self, record, actor):
        header = record["header"]
        self.conn.execute(
            """INSERT INTO billing_headers(practice_id,header_id,encounter_uid,encounter_version,
                 source_sha256,baseline,baseline_sha256,actor_tenant_id,actor_user_id,actor_role,created_at)
               VALUES(%s,%s::uuid,%s::uuid,%s,%s,%s,%s,%s::uuid,%s::uuid,%s,%s::timestamptz)""",
            (self.practice, record["headerId"], self.stored.resource_id, self.stored.version,
             record["sourceDigest"], self.store.context.Jsonb(header), domain.digest(header),
             actor.tenant_id, actor.user_id, actor.role, header["generatedAt"]),
        )

    def insert_revision(self, header_id, revision, actor):
        self.conn.execute(
            """INSERT INTO billing_header_revisions(practice_id,header_id,revision,revision_payload,
                 payload_sha256,actor_tenant_id,actor_user_id,actor_role,created_at)
               VALUES(%s,%s::uuid,%s,%s,%s,%s::uuid,%s::uuid,%s,%s::timestamptz)""",
            (self.practice, header_id, revision["number"], self.store.context.Jsonb(revision),
             domain.digest(revision), actor.tenant_id, actor.user_id, actor.role, revision["createdAt"]),
        )

    def insert_receipt(self, operation_id, intent, header_id, revision, actor, audit_completion):
        domain.require(isinstance(audit_completion, AccessAuditEvent), "audit_required")
        self.conn.execute(
            """INSERT INTO billing_header_operations(practice_id,operation_id,request_sha256,
                 header_id,revision,actor_tenant_id,actor_user_id,actor_role,access_event_id)
               VALUES(%s,%s::uuid,%s,%s::uuid,%s,%s::uuid,%s::uuid,%s,%s::uuid)""",
            (self.practice, operation_id, intent, header_id, revision,
             actor.tenant_id, actor.user_id, actor.role, audit_completion.event_id),
        )

    def audit(self, event, actor, action, header_id):
        PostgresAccessAuditStore._insert_completion(self.conn, event)

    def signoff_receipt(self, *, header_id=None, review_id=None):
        criterion, value = ("review_id", review_id) if review_id else ("header_id", header_id)
        row = self.conn.execute(
            f"SELECT receipt,payload_sha256,request_sha256 FROM billing_header_signoffs WHERE practice_id=%s AND {criterion}=%s::uuid ORDER BY header_revision DESC LIMIT 1",
            (self.practice, value),
        ).fetchone()
        if not row:
            return None
        domain.require(domain.digest(row["receipt"]) == row["payload_sha256"], "store_invalid")
        return {"receipt": row["receipt"], "requestDigest": row["request_sha256"]}

    def append_feedback(self, feedback, store):
        domain.require(isinstance(store, PostgresFeedbackStore)
                       and store.context.practice_id == self.practice, "signoff_unavailable")
        return store._append(self.conn, feedback)

    def insert_signoff(self, receipt, intent, actor, feedback, audit_completion):
        from .coder_feedback import feedback_payload_digest
        domain.require(isinstance(audit_completion, AccessAuditEvent), "audit_required")
        self.conn.execute("""INSERT INTO billing_header_signoffs
            (practice_id,review_id,header_id,header_revision,source_sha256,recommendation_sha256,
             request_sha256,feedback_sha256,receipt,payload_sha256,actor_tenant_id,actor_user_id,
             actor_role,created_at,access_event_id)
            VALUES(%s,%s::uuid,%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s::uuid,%s::uuid,%s,%s::timestamptz,%s::uuid)""",
            (self.practice, receipt["reviewId"], receipt["headerId"], receipt["headerRevision"],
             domain.digest(receipt["expectedSource"]), receipt["recommendationDigest"], intent,
             feedback_payload_digest(feedback), self.store.context.Jsonb(receipt), domain.digest(receipt),
             actor.tenant_id, actor.user_id, actor.role, receipt["createdAt"], audit_completion.event_id))
