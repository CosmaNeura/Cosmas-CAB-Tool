"""Practice-scoped persistence services for the Azure pilot.

Production uses one least-privilege PostgreSQL identity with forced row-level
security. Every connection sets the configured practice inside its transaction;
request data can never select the tenant. Local memory/file stores remain only
for development and deterministic tests.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .access_audit import AccessAuditEvent
from .auth import (
    AuthPrincipal, PrincipalRoleError, PrincipalStoreUnavailable,
)
from .coder_feedback import (
    CodeDecision, EncounterFeedback, FeedbackConflict,
    feedback_payload_digest, validate_feedback, validate_feedback_shape,
)
from .schemas import VisitBundle
from .settings import Settings


class PersistenceUnavailable(RuntimeError):
    """The selected durable store cannot safely serve the request."""


class UnknownEncounter(LookupError):
    """No encounter exists in the configured practice."""


class StaleEncounter(RuntimeError):
    """Feedback targets an encounter version that is no longer current."""


class ReviewerNotAuthorized(PermissionError):
    """The feedback actor is not an active Coder or Admin membership."""


SYNTHETIC_DEMO_SOURCE = "synthetic_demo_release_candidate"
ATHENA_PREVIEW_SYNTHETIC_SOURCE = "athena_preview_synthetic"
READ_ONLY_SYNTHETIC_ENCOUNTER_SOURCES = frozenset({
    SYNTHETIC_DEMO_SOURCE,
    ATHENA_PREVIEW_SYNTHETIC_SOURCE,
})


@dataclass(frozen=True)
class StoredEncounter:
    resource_id: str
    bundle: VisitBundle
    version: int = 1
    # Server-owned ingestion provenance. Presentation logic must use this
    # persisted value, never an external-ID naming convention, when deciding
    # whether an encounter is a read-only synthetic demo fixture.
    source: str | None = None


class MemoryEncounterStore:
    """Explicit local-only encounter store seeded with synthetic fixtures."""

    def __init__(
        self, encounters: Iterable[VisitBundle] = (), *, practice_id: str = "local",
    ) -> None:
        self.practice_id = practice_id or "local"
        self._items: dict[str, StoredEncounter] = {}
        for bundle in encounters:
            self.upsert(bundle)

    def _resource_id(self, encounter_id: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"{self.practice_id}:{encounter_id}"))

    def upsert(
        self,
        bundle: VisitBundle,
        *,
        source: str | None = None,
        **_kwargs,
    ) -> StoredEncounter:
        if (
            source in READ_ONLY_SYNTHETIC_ENCOUNTER_SOURCES
            and source != SYNTHETIC_DEMO_SOURCE
        ):
            raise PersistenceUnavailable(
                "reserved import provenance requires the source import store"
            )
        prior = self._items.get(bundle.encounter_id)
        if (
            prior is not None
            and prior.source == SYNTHETIC_DEMO_SOURCE
            and (
                prior.bundle != bundle
                or source not in {None, SYNTHETIC_DEMO_SOURCE}
            )
        ):
            raise PersistenceUnavailable(
                "read-only synthetic encounter cannot be changed"
            )
        row = StoredEncounter(
            self._resource_id(bundle.encounter_id), bundle,
            version=(
                prior.version + 1
                if prior is not None and prior.bundle != bundle
                else prior.version if prior is not None
                else 1
            ),
            source=(
                source
                if source is not None
                else prior.source if prior is not None
                else None
            ),
        )
        self._items[bundle.encounter_id] = row
        return row

    def get(self, encounter_id: str) -> StoredEncounter | None:
        return self._items.get(encounter_id)

    def all(self) -> list[StoredEncounter]:
        return list(self._items.values())

    def healthcheck(self) -> None:
        return None


class PostgresContext:
    """Immutable connection configuration shared by the Postgres adapters."""

    def __init__(self, dsn: str, *, practice_id: str) -> None:
        if not dsn.strip() or not practice_id.strip():
            raise ValueError("Postgres persistence requires a DSN and practice ID")
        try:
            import psycopg
            from psycopg.rows import dict_row
            from psycopg.types.json import Jsonb
        except ImportError as exc:  # pragma: no cover - deployment wiring
            raise RuntimeError(
                "Postgres persistence requires the 'postgres' package extra"
            ) from exc
        self.dsn = dsn
        self.practice_id = practice_id
        self.psycopg = psycopg
        self.dict_row = dict_row
        self.Jsonb = Jsonb

    def __repr__(self) -> str:
        return (
            f"PostgresContext(practice_id={self.practice_id!r}, "
            "dsn='<redacted>')"
        )

    @contextmanager
    def connection(self):
        try:
            with self.psycopg.connect(
                self.dsn, row_factory=self.dict_row, connect_timeout=3,
            ) as conn:
                conn.execute(
                    "SELECT set_config('cosmaneura.practice_id', %s, true)",
                    (self.practice_id,),
                )
                yield conn
        except self.psycopg.Error as exc:
            raise PersistenceUnavailable("Postgres persistence is unavailable") from exc


class PostgresMembershipStore:
    """Resolve an authenticated Entra identity to an explicit local membership."""

    def __init__(self, context: PostgresContext, *, tenant_id: str) -> None:
        self.context = context
        self.tenant_id = str(UUID(tenant_id))

    def authorize(self, principal: AuthPrincipal) -> AuthPrincipal:
        try:
            with self.context.connection() as conn:
                row = conn.execute(
                    """SELECT pm.roles
                         FROM practice_memberships pm
                         JOIN practices p
                           ON p.practice_id = pm.practice_id
                         JOIN users u
                           ON u.tenant_id = pm.tenant_id
                          AND u.user_id = pm.user_id
                        WHERE pm.practice_id = %s
                          AND pm.tenant_id = %s::uuid
                          AND pm.user_id = %s::uuid
                          AND pm.active AND p.active AND u.active""",
                    (
                        self.context.practice_id,
                        principal.tenant_id,
                        principal.user_id,
                    ),
                ).fetchone()
        except PersistenceUnavailable as exc:
            raise PrincipalStoreUnavailable from exc
        if row is None:
            raise PrincipalRoleError(principal)
        asserted = {role.casefold(): role for role in principal.roles}
        effective = frozenset(
            asserted[role.casefold()]
            for role in row["roles"]
            if role.casefold() in asserted
        )
        if not effective:
            raise PrincipalRoleError(principal)
        return AuthPrincipal(
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            practice_id=self.context.practice_id,
            roles=effective,
        )

    def validate_assignee(self, user_id: str, *, connection=None) -> str:
        try:
            normalized = str(UUID(user_id))
        except ValueError as exc:
            raise ValueError("assignee must be an active staff object ID") from exc
        def query(conn):
            return conn.execute(
                """SELECT cosmaneura_staff_assignee_allowed(
                           %s::uuid, %s::uuid
                       ) AS allowed""",
                (self.tenant_id, normalized),
            ).fetchone()

        if connection is None:
            with self.context.connection() as conn:
                row = query(conn)
        else:
            row = query(connection)
        if row is None or not row["allowed"]:
            raise ValueError("assignee is not active billing staff at this practice")
        return normalized

    def healthcheck(self) -> None:
        with self.context.connection() as conn:
            row = conn.execute(
                """SELECT 1
                     FROM practices p
                     JOIN practice_memberships pm
                       ON pm.practice_id = p.practice_id
                     JOIN users u
                       ON u.tenant_id = pm.tenant_id
                      AND u.user_id = pm.user_id
                    WHERE p.practice_id = %s
                      AND p.entra_tenant_id = %s::uuid
                      AND p.active AND pm.active AND u.active
                      AND pm.roles @> ARRAY['Admin']::text[]
                    LIMIT 1""",
                (self.context.practice_id, self.tenant_id),
            ).fetchone()
        if row is None:
            raise PersistenceUnavailable(
                "practice has no active provisioned administrator"
            )


class PostgresEncounterStore:
    def __init__(self, context: PostgresContext) -> None:
        self.context = context

    @staticmethod
    def _bundle(row: dict) -> StoredEncounter:
        return StoredEncounter(
            resource_id=str(row["encounter_uid"]),
            bundle=VisitBundle(
                encounter_id=row["external_id"],
                patient_age=row["patient_age"],
                patient_sex=row["patient_sex"],
                encounter_type=row["encounter_type"],
                place_of_service=row["place_of_service"],
                intake_text=row["intake_text"],
                transcript=row["transcript"],
                signed_note=row["signed_note"],
                provider_specialty=row["provider_specialty"],
                submitted_em_code=row["submitted_em_code"],
                submitted_icd10=list(row["submitted_icd10"] or []),
            ),
            version=int(row["version"]),
            source=row["source"],
        )

    @staticmethod
    def _select() -> str:
        return """SELECT encounter_uid, external_id, patient_age, patient_sex,
                         encounter_type, place_of_service, intake_text,
                         transcript, signed_note, provider_specialty,
                         submitted_em_code, submitted_icd10, version, source
                    FROM encounters"""

    def get(self, encounter_id: str) -> StoredEncounter | None:
        with self.context.connection() as conn:
            row = conn.execute(
                self._select() + " WHERE practice_id = %s AND external_id = %s",
                (self.context.practice_id, encounter_id),
            ).fetchone()
        return self._bundle(row) if row else None

    def all(self) -> list[StoredEncounter]:
        with self.context.connection() as conn:
            rows = conn.execute(
                self._select()
                + " WHERE practice_id = %s ORDER BY updated_at DESC, external_id",
                (self.context.practice_id,),
            ).fetchall()
        return [self._bundle(row) for row in rows]

    def upsert(
        self,
        bundle: VisitBundle,
        *,
        source: str | None = None,
        ingested_by_tenant_id: str | None = None,
        ingested_by_user_id: str | None = None,
    ) -> StoredEncounter:
        if source in READ_ONLY_SYNTHETIC_ENCOUNTER_SOURCES:
            if source == SYNTHETIC_DEMO_SOURCE:
                raise PersistenceUnavailable(
                    "reserved synthetic provenance requires the fixed seeder"
                )
            raise PersistenceUnavailable(
                "reserved import provenance requires the source import store"
            )
        effective_source = source or "explicit_import"
        resource_id = str(uuid4())
        with self.context.connection() as conn:
            row = conn.execute(
                """INSERT INTO encounters (
                       practice_id, encounter_uid, external_id, patient_age,
                       patient_sex, encounter_type, place_of_service, intake_text,
                       transcript, signed_note, provider_specialty,
                       submitted_em_code, submitted_icd10, source,
                       ingested_by_tenant_id, ingested_by_user_id
                   ) VALUES (
                       %s, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s::uuid, %s::uuid
                   )
                   ON CONFLICT (practice_id, external_id) DO UPDATE SET
                       patient_age = EXCLUDED.patient_age,
                       patient_sex = EXCLUDED.patient_sex,
                       encounter_type = EXCLUDED.encounter_type,
                       place_of_service = EXCLUDED.place_of_service,
                       intake_text = EXCLUDED.intake_text,
                       transcript = EXCLUDED.transcript,
                       signed_note = EXCLUDED.signed_note,
                       provider_specialty = EXCLUDED.provider_specialty,
                       submitted_em_code = EXCLUDED.submitted_em_code,
                       submitted_icd10 = EXCLUDED.submitted_icd10,
                       source = CASE
                           WHEN %s THEN EXCLUDED.source
                           ELSE encounters.source
                       END,
                       ingested_by_tenant_id = EXCLUDED.ingested_by_tenant_id,
                       ingested_by_user_id = EXCLUDED.ingested_by_user_id,
                       version = encounters.version + CASE WHEN ROW(
                           encounters.patient_age,
                           encounters.patient_sex,
                           encounters.encounter_type,
                           encounters.place_of_service,
                           encounters.intake_text,
                           encounters.transcript,
                           encounters.signed_note,
                           encounters.provider_specialty,
                           encounters.submitted_em_code,
                           encounters.submitted_icd10,
                           encounters.source
                       ) IS DISTINCT FROM ROW(
                           EXCLUDED.patient_age,
                           EXCLUDED.patient_sex,
                           EXCLUDED.encounter_type,
                           EXCLUDED.place_of_service,
                           EXCLUDED.intake_text,
                           EXCLUDED.transcript,
                           EXCLUDED.signed_note,
                           EXCLUDED.provider_specialty,
                           EXCLUDED.submitted_em_code,
                           EXCLUDED.submitted_icd10,
                           CASE
                               WHEN %s THEN EXCLUDED.source
                               ELSE encounters.source
                           END
                       ) THEN 1 ELSE 0 END,
                       updated_at = now()
                   WHERE encounters.source <> %s
                   RETURNING encounter_uid, external_id, patient_age,
                             patient_sex, encounter_type, place_of_service,
                             intake_text, transcript, signed_note,
                             provider_specialty, submitted_em_code,
                             submitted_icd10, version, source""",
                (
                    self.context.practice_id, resource_id, bundle.encounter_id,
                    bundle.patient_age, bundle.patient_sex, bundle.encounter_type,
                    bundle.place_of_service, bundle.intake_text, bundle.transcript,
                    bundle.signed_note, bundle.provider_specialty,
                    bundle.submitted_em_code, bundle.submitted_icd10,
                    effective_source,
                    ingested_by_tenant_id, ingested_by_user_id,
                    source is not None,
                    source is not None,
                    SYNTHETIC_DEMO_SOURCE,
                ),
            ).fetchone()
        if row is None:  # INSERT/UPDATE always returns exactly one scoped row.
            raise PersistenceUnavailable("encounter write could not be verified")
        return self._bundle(row)

    def healthcheck(self) -> None:
        with self.context.connection() as conn:
            conn.execute(
                "SELECT 1 FROM encounters WHERE practice_id = %s LIMIT 1",
                (self.context.practice_id,),
            )


class PostgresAccessAuditStore:
    """Append-only audit attempts and completions; runtime has no update/delete."""

    def __init__(self, context: PostgresContext) -> None:
        self.context = context

    @staticmethod
    def _insert_attempt(conn, event: AccessAuditEvent) -> None:
        # An explicit conflict target requires SELECT on its columns. Omit it
        # so the runtime role remains append-only; the primary key is the only
        # unique constraint on this table.
        conn.execute(
            """INSERT INTO access_audit_attempts (
                   practice_id, event_id, occurred_at, actor_tenant_id,
                   actor_user_id, actor_roles, action, route, method, source_ip
               ) VALUES (
                   %s, %s::uuid, %s::timestamptz, NULLIF(%s, '')::uuid,
                   NULLIF(%s, '')::uuid, %s, %s, %s, %s, %s
               ) ON CONFLICT DO NOTHING""",
            (
                event.practice_id, event.event_id, event.occurred_at,
                event.actor_tenant_id, event.actor_user_id,
                list(event.actor_roles), event.action, event.route,
                event.method, event.source_ip,
            ),
        )

    @staticmethod
    def _insert_completion(conn, event: AccessAuditEvent) -> None:
        conn.execute(
            """INSERT INTO access_audit_completions (
                   practice_id, event_id, completed_at, status, outcome,
                   resource_id
               ) VALUES (%s, %s::uuid, %s::timestamptz, %s, %s, NULLIF(%s, ''))""",
            (
                event.practice_id, event.event_id, event.occurred_at,
                event.status, event.outcome, event.resource_id,
            ),
        )

    def append(self, event: AccessAuditEvent) -> None:
        if event.practice_id != self.context.practice_id:
            raise ValueError("audit event practice does not match configured practice")
        with self.context.connection() as conn:
            self._insert_attempt(conn, event)
            if event.outcome != "attempted":
                self._insert_completion(conn, event)

    def healthcheck(self) -> None:
        with self.context.connection() as conn:
            row = conn.execute(
                """SELECT has_table_privilege(
                       current_user, 'access_audit_attempts', 'INSERT'
                   ) AS attempts,
                   has_table_privilege(
                       current_user, 'access_audit_completions', 'INSERT'
                   ) AS completions"""
            ).fetchone()
        if not row or not row["attempts"] or not row["completions"]:
            raise PersistenceUnavailable("access audit insert privilege is unavailable")


class PostgresFeedbackStore:
    """Normalized, idempotent feedback with a cross-replica revision."""

    def __init__(self, context: PostgresContext) -> None:
        self.context = context

    def _encounter_uid(self, conn, encounter_id: str, version: int) -> str:
        row = conn.execute(
            """SELECT encounter_uid, version FROM encounters
                WHERE practice_id = %s AND external_id = %s
                FOR SHARE""",
            (self.context.practice_id, encounter_id),
        ).fetchone()
        if row is None:
            raise UnknownEncounter(encounter_id)
        if int(row["version"]) != version:
            raise StaleEncounter(encounter_id)
        return str(row["encounter_uid"])

    def _append(
        self, conn, feedback: EncounterFeedback, *,
        import_source_sha256: bytes | None = None,
        import_line: int | None = None,
    ) -> bool:
        validate_feedback_shape(feedback)
        review_id = str(UUID(feedback.review_id))
        coder_tenant_id = str(UUID(feedback.coder_tenant_id))
        coder_user_id = str(UUID(feedback.coder_id))
        payload_hash = feedback_payload_digest(feedback)
        existing = conn.execute(
            """SELECT payload_hash FROM coder_feedback
                WHERE practice_id = %s AND review_id = %s::uuid""",
            (self.context.practice_id, review_id),
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] != payload_hash:
                raise FeedbackConflict(
                    "review ID conflicts with an existing review"
                )
            return False
        reviewer = conn.execute(
            """SELECT cosmaneura_feedback_reviewer_allowed(
                       %s::uuid, %s::uuid
                   ) AS allowed""",
            (coder_tenant_id, coder_user_id),
        ).fetchone()
        if reviewer is None or not reviewer["allowed"]:
            raise ReviewerNotAuthorized("reviewer membership is not active")
        encounter_uid = self._encounter_uid(
            conn, feedback.encounter_id, feedback.encounter_version,
        )
        validate_feedback(feedback)
        row = conn.execute(
            """INSERT INTO coder_feedback (
                   practice_id, review_id, encounter_uid, coder_tenant_id,
                   coder_user_id, engine_version, submitted_em, engine_em,
                   final_em, encounter_version, time_spent_seconds,
                   overall_note, submitted_at,
                   payload_hash, import_source_sha256, import_line
               ) VALUES (
                   %s, %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s, %s, %s,
                   %s, %s, %s, %s, to_timestamp(%s), %s, %s, %s
               ) ON CONFLICT (practice_id, review_id) DO NOTHING
               RETURNING feedback_id""",
            (
                self.context.practice_id, review_id, encounter_uid,
                coder_tenant_id, coder_user_id, feedback.engine_version,
                feedback.submitted_em, feedback.engine_em, feedback.final_em,
                feedback.encounter_version, feedback.time_spent_seconds,
                feedback.overall_note, feedback.ts,
                payload_hash, import_source_sha256, import_line,
            ),
        ).fetchone()
        if row is None:
            # A concurrent request may have inserted the same review after the
            # initial lookup. Compare the canonical payload after it commits.
            existing = conn.execute(
                """SELECT payload_hash FROM coder_feedback
                    WHERE practice_id = %s AND review_id = %s::uuid""",
                (self.context.practice_id, review_id),
            ).fetchone()
            if existing is None or existing["payload_hash"] != payload_hash:
                raise FeedbackConflict(
                    "review ID conflicts with an existing review"
                )
            return False
        feedback_id = row["feedback_id"]
        for ordinal, decision in enumerate(feedback.decisions):
            conn.execute(
                """INSERT INTO coder_feedback_decisions (
                       practice_id, feedback_id, ordinal, kind, engine_code,
                       action, final_code, reason_code, note
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    self.context.practice_id, feedback_id, ordinal,
                    decision.kind, decision.engine_code, decision.action,
                    decision.final_code, decision.reason_code, decision.note,
                ),
            )
        return True

    def append(self, feedback: EncounterFeedback) -> bool:
        with self.context.connection() as conn:
            return self._append(conn, feedback)

    def exact_replay(self, feedback: EncounterFeedback) -> bool:
        """Classify an existing ID without rechecking mutable domain state."""
        validate_feedback_shape(feedback)
        review_id = str(UUID(feedback.review_id))
        payload_hash = feedback_payload_digest(feedback)
        with self.context.connection() as conn:
            existing = conn.execute(
                """SELECT payload_hash FROM coder_feedback
                    WHERE practice_id = %s AND review_id = %s::uuid""",
                (self.context.practice_id, review_id),
            ).fetchone()
        if existing is None:
            return False
        if existing["payload_hash"] != payload_hash:
            raise FeedbackConflict("review ID conflicts with an existing review")
        return True

    def import_batch(
        self,
        rows: list[tuple[EncounterFeedback, bytes, int]],
    ) -> tuple[int, int]:
        """Import a fully prevalidated legacy batch in one transaction."""
        created = replayed = 0
        with self.context.connection() as conn:
            for feedback, source_digest, line_number in rows:
                if self._append(
                    conn, feedback, import_source_sha256=source_digest,
                    import_line=line_number,
                ):
                    created += 1
                else:
                    replayed += 1
        return created, replayed

    def append_with_audit(
        self, feedback: EncounterFeedback, completion: AccessAuditEvent,
    ) -> bool:
        if (
            completion.practice_id != self.context.practice_id
            or completion.actor_user_id != feedback.coder_id
            or completion.actor_tenant_id != feedback.coder_tenant_id
            or completion.outcome != "allowed"
        ):
            raise ValueError("feedback audit identity does not match the review")
        with self.context.connection() as conn:
            created = self._append(conn, feedback)
            PostgresAccessAuditStore._insert_completion(conn, completion)
            return created

    def load(self) -> list[EncounterFeedback]:
        with self.context.connection() as conn:
            headers = conn.execute(
                """SELECT cf.feedback_id, cf.review_id, e.external_id,
                          cf.coder_tenant_id, cf.coder_user_id,
                          cf.engine_version, cf.submitted_em, cf.engine_em,
                          cf.final_em, cf.encounter_version,
                          cf.time_spent_seconds, cf.overall_note,
                          extract(epoch FROM cf.submitted_at) AS ts
                     FROM coder_feedback cf
                     JOIN encounters e
                       ON e.practice_id = cf.practice_id
                      AND e.encounter_uid = cf.encounter_uid
                    WHERE cf.practice_id = %s
                    ORDER BY cf.feedback_id""",
                (self.context.practice_id,),
            ).fetchall()
            ids = [row["feedback_id"] for row in headers]
            decisions: dict[int, list[CodeDecision]] = {}
            if ids:
                for row in conn.execute(
                    """SELECT feedback_id, kind, engine_code, action,
                              final_code, reason_code, note
                         FROM coder_feedback_decisions
                        WHERE practice_id = %s AND feedback_id = ANY(%s)
                        ORDER BY feedback_id, ordinal""",
                    (self.context.practice_id, ids),
                ).fetchall():
                    decisions.setdefault(row["feedback_id"], []).append(
                        CodeDecision(
                            kind=row["kind"], engine_code=row["engine_code"],
                            action=row["action"], final_code=row["final_code"],
                            reason_code=row["reason_code"], note=row["note"],
                        )
                    )
        return [
            EncounterFeedback(
                encounter_id=row["external_id"],
                coder_id=str(row["coder_user_id"]),
                engine_version=row["engine_version"],
                submitted_em=row["submitted_em"], engine_em=row["engine_em"],
                final_em=row["final_em"],
                decisions=decisions.get(row["feedback_id"], []),
                time_spent_seconds=row["time_spent_seconds"],
                overall_note=row["overall_note"], ts=float(row["ts"]),
                review_id=str(row["review_id"]),
                coder_tenant_id=str(row["coder_tenant_id"]),
                encounter_version=int(row["encounter_version"]),
            )
            for row in headers
        ]

    def revision(self) -> int:
        with self.context.connection() as conn:
            row = conn.execute(
                """SELECT COALESCE(max(feedback_id), 0) AS revision
                     FROM coder_feedback WHERE practice_id = %s""",
                (self.context.practice_id,),
            ).fetchone()
        return int(row["revision"])

    def count(self) -> int:
        with self.context.connection() as conn:
            row = conn.execute(
                "SELECT count(*) AS count FROM coder_feedback WHERE practice_id = %s",
                (self.context.practice_id,),
            ).fetchone()
        return int(row["count"])

    def healthcheck(self) -> None:
        self.revision()


@dataclass(frozen=True)
class PostgresServices:
    context: PostgresContext
    memberships: PostgresMembershipStore
    encounters: PostgresEncounterStore
    feedback: PostgresFeedbackStore
    audit: PostgresAccessAuditStore


def postgres_services(settings: Settings) -> PostgresServices:
    context = PostgresContext(
        settings.database_url, practice_id=settings.practice_id,
    )
    return PostgresServices(
        context=context,
        memberships=PostgresMembershipStore(
            context, tenant_id=settings.entra_tenant_id,
        ),
        encounters=PostgresEncounterStore(context),
        feedback=PostgresFeedbackStore(context),
        audit=PostgresAccessAuditStore(context),
    )


__all__ = [
    "ATHENA_PREVIEW_SYNTHETIC_SOURCE", "MemoryEncounterStore",
    "PersistenceUnavailable", "PostgresAccessAuditStore",
    "PostgresContext", "PostgresEncounterStore", "PostgresFeedbackStore",
    "PostgresMembershipStore", "PostgresServices",
    "READ_ONLY_SYNTHETIC_ENCOUNTER_SOURCES", "StoredEncounter",
    "SYNTHETIC_DEMO_SOURCE", "ReviewerNotAuthorized", "StaleEncounter",
    "UnknownEncounter",
    "postgres_services",
]
