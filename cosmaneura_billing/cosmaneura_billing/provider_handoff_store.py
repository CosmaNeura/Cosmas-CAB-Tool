"""One source head plus immutable actor-bound receipts; no inference in writes."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from threading import RLock
from uuid import uuid4
from urllib.parse import quote

from .billing_header import HeaderActor, validated_actor, digest, source_snapshot, utc_now
from .billing_header_store import _audit_check
from .persistence import PostgresAccessAuditStore, PostgresEncounterStore, StoredEncounter
from .provider_handoff import SOURCE, SCHEMA, require, verified_fixture
from .provider_admission import MemoryAdmissionRegistry, verify_admission


def _response(receipt, stored, replayed):
    current = stored is not None and digest(source_snapshot(stored)) == digest(receipt["headerSource"])
    return {"schemaVersion": SCHEMA, "receipt": deepcopy(receipt), "replayed": replayed,
            "current": current, "launchPath": "/?view=header&encounterId=" + quote(receipt["encounterId"], safe="") if current else None,
            "generation": "not_started"}


class HandoffStore:
    durable = False

    def accept(self, prepared, actor, *, audit_completion=None):
        actor = validated_actor(actor)
        require(actor.role in {"Provider", "Admin"}, "not_authorized")
        intent = digest({"request": prepared.intent, "actor": asdict(actor), "practice": self.practice_id})
        with self.unit(prepared, actor) as unit:
            prior, stored, head = unit.prior, unit.stored, unit.head
            if prior:
                require(prior["request_sha256"] == intent, "handoff_conflict")
                if prepared.fixture:
                    verified_fixture(stored, prepared)
                unit.audit(audit_completion, actor, stored, prepared)
                return _response(prior["receipt"], stored, True)
            admission = None if prepared.fixture else unit.admit(prepared, actor)
            if prepared.fixture:
                verified_fixture(stored, prepared)
            elif head:
                require(stored is not None and stored.source == SOURCE
                        and digest(source_snapshot(stored)) == digest(head["header_source"]), "source_changed")
                # A new command must carry current CAS even for equal content.
                require(digest(prepared.expected_source) == digest(head["header_source"]), "source_changed")
                require(prepared.source_revision >= head["source_revision"], "source_changed")
                if prepared.source_revision == head["source_revision"]:
                    require(prepared.manifest_sha256 == head["manifest_sha256"], "handoff_conflict")
            else:
                require(prepared.expected_source is None and stored is None, "source_changed")
            if not prepared.fixture and (head is None or prepared.source_revision > head["source_revision"]):
                stored = StoredEncounter(stored.resource_id if stored else str(uuid4()), prepared.bundle,
                                         stored.version + 1 if stored else 1, SOURCE)
            receipt = {"operationId": prepared.operation_id, "receiptId": str(uuid4()),
                       "acceptedAt": utc_now(), "encounterId": stored.bundle.encounter_id,
                       "encounterVersion": stored.version, "sourceRevision": prepared.source_revision,
                       "sourceManifestSha256": prepared.manifest_sha256, "headerSource": source_snapshot(stored)}
            unit.audit(audit_completion, actor, stored, prepared)
            unit.persist(prepared, stored, actor, intent, receipt, audit_completion, admission)
            return _response(receipt, stored, False)


class MemoryHandoffStore(HandoffStore):
    """Explicit local synthetic seam; never a PostgreSQL fallback."""
    def __init__(self, encounters, *, authorize=lambda actor: False):
        self.encounters, self.practice_id, self.authorize = encounters, encounters.practice_id, authorize
        self.heads, self.operations, self.lock = {}, {}, RLock()
        self.admissions = MemoryAdmissionRegistry(self.practice_id)

    def healthcheck(self):
        return None

    @contextmanager
    def unit(self, prepared, actor):
        with self.lock, self.admissions.lock:
            require(self.authorize(actor), "not_authorized")
            yield _MemoryUnit(self, prepared)


class _MemoryUnit:
    def __init__(self, store, prepared):
        self.store = store
        self.prior = store.operations.get(prepared.operation_id)
        self.head = store.heads.get(prepared.record_key)
        self.stored = store.encounters.get(prepared.bundle.encounter_id)

    def audit(self, event, actor, stored, prepared):
        _audit_check(replace(event, resource_id=stored.resource_id) if event else None,
                     self.store.practice_id, actor, stored.resource_id,
                     "provider_handoff.fixture" if prepared.fixture else "provider_handoff.accept", mandatory=False)

    def admit(self, prepared, actor):
        return self.store.admissions.admit(prepared, actor, self.head is not None)

    def persist(self, p, stored, actor, intent, receipt, event, admission):
        if admission is not None:
            self.store.admissions.record(p, admission)
        self.store.encounters._items[stored.bundle.encounter_id] = stored
        self.store.heads[p.record_key] = {"source_revision": p.source_revision,
            "manifest_sha256": p.manifest_sha256, "manifest": deepcopy(p.manifest),
            "encounter_uid": stored.resource_id, "header_source": receipt["headerSource"]}
        self.store.operations[p.operation_id] = {"request_sha256": intent, "receipt": deepcopy(receipt)}


class PostgresHandoffStore(HandoffStore):
    durable = True

    def __init__(self, context):
        self.context, self.practice_id = context, context.practice_id

    @contextmanager
    def unit(self, prepared, actor):
        with self.context.connection() as conn:
            # Operation lock first prevents reuse against another source record.
            for key in ("operation:" + prepared.operation_id, "record:" + prepared.record_key):
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                             ("provider-handoff:" + self.practice_id + ":" + key,))
            row = conn.execute("SELECT cosmaneura_billing_header_editor_allowed(%s::uuid,%s::uuid,%s) AS allowed",
                               (actor.tenant_id, actor.user_id, actor.role)).fetchone()
            require(row and row["allowed"] is True, "not_authorized")
            yield _PostgresUnit(self, conn, prepared)

    def _locked_encounter(self, conn, encounter_id):
        return conn.execute(PostgresEncounterStore._select() + " WHERE practice_id=%s AND external_id=%s FOR UPDATE",
                            (self.practice_id, encounter_id)).fetchone()

    def healthcheck(self):
        with self.context.connection() as conn:
            row = conn.execute("""SELECT
              has_table_privilege(current_user,'provider_handoff_heads','SELECT') AS heads,
              has_table_privilege(current_user,'provider_handoff_operations','SELECT') AS receipts,
              has_function_privilege(current_user,'cosmaneura_commit_provider_handoff(jsonb)','EXECUTE') AS writer,
              NOT has_function_privilege(current_user,'cosmaneura_commit_provider_handoff_019(jsonb)','EXECUTE') AS guarded,
              has_table_privilege(current_user,'provider_source_connections','SELECT') AS connections,
              has_table_privilege(current_user,'provider_source_admissions','SELECT') AS admissions,
              has_table_privilege(current_user,'provider_source_bindings','SELECT') AS bindings,
              has_table_privilege(current_user,'provider_source_acceptances','SELECT') AS provenance""").fetchone()
            require(row and all(row.values()), "handoff_unavailable")


class _PostgresUnit:
    def __init__(self, store, conn, p):
        self.store, self.conn = store, conn
        self.prior = conn.execute("SELECT request_sha256,receipt FROM provider_handoff_operations WHERE practice_id=%s AND operation_id=%s::uuid",
                                  (store.practice_id, p.operation_id)).fetchone()
        self.head = conn.execute("SELECT * FROM provider_handoff_heads WHERE practice_id=%s AND record_key=%s",
                                 (store.practice_id, p.record_key)).fetchone()
        row = store._locked_encounter(conn, p.bundle.encounter_id)
        self.stored = PostgresEncounterStore._bundle(row) if row else None

    def audit(self, event, actor, stored, prepared):
        event = replace(event, resource_id=stored.resource_id) if event else None
        _audit_check(event, self.store.practice_id, actor, stored.resource_id,
                     "provider_handoff.fixture" if prepared.fixture else "provider_handoff.accept", mandatory=True)
        PostgresAccessAuditStore._insert_completion(self.conn, event)

    def admit(self, p, actor):
        # Read-only preflight. The definer writer rechecks and locks both rows
        # atomically with source CAS; runtime has no UPDATE/row-lock privilege.
        row = self.conn.execute("""SELECT c.document AS connection, g.document AS admission,
            c.revoked_at AS connection_revoked, g.revoked_at AS grant_revoked, clock_timestamp() AS now
            FROM provider_source_connections c JOIN provider_source_admissions g
              ON c.practice_id=g.practice_id AND c.connection_id=g.connection_id
            WHERE g.practice_id=%s AND g.operation_id=%s::uuid""",
            (self.store.practice_id,p.operation_id)).fetchone()
        require(row is not None and row['connection_revoked'] is None and row['grant_revoked'] is None,
                'operational_handoff_not_approved')
        proof = verify_admission(row['connection'],row['admission'],p,actor,row['now'])
        bound = self.conn.execute("SELECT connection_id FROM provider_source_bindings WHERE practice_id=%s AND record_key=%s",
                                  (self.store.practice_id,p.record_key)).fetchone()
        require((self.head is None or bound is not None) and (bound is None or str(bound['connection_id'])==proof['connection']['connectionId']),
                'operational_handoff_not_approved')
        return proof

    def persist(self, p, stored, actor, intent, receipt, event, admission):
        command = {"practice": self.store.practice_id, "recordKey": p.record_key,
            "sourceRevision": p.source_revision, "manifest": p.manifest, "manifestSha256": p.manifest_sha256,
            "expectedSource": self.head["header_source"] if p.fixture and self.head else p.expected_source,
            "bundle": asdict(stored.bundle), "resourceId": stored.resource_id, "fixture": p.fixture,
            "actor": asdict(actor), "requestSha256": intent, "receipt": receipt, "eventId": event.event_id}
        self.conn.execute("SELECT cosmaneura_commit_provider_handoff(%s)", (self.store.context.Jsonb(command),))
