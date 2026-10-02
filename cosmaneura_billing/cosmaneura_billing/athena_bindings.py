"""Read-only, transaction-scoped access to operator-owned Athena case bindings.

This registry supplies identity data, never vendor/data-handling permission.
There is no network, patient search, implicit mapping or public write path.
"""
from contextlib import contextmanager

from .athena_review import ReviewActor, text
from .athena_staff_contract import AthenaStaffProfile, AthenaStaffScope, AthenaStaffScopeError


ERRORS = frozenset({"not_authorized", "not_found", "connection_missing",
    "connection_revoked", "binding_missing", "binding_revoked", "binding_stale",
    "binding_invalid"})
SCOPE_KEYS = frozenset({"tenantId", "serverPracticeId", "environment", "vendorPracticeId",
    "connectionRevision", "profileSha256", "caseId", "patientRef", "sourcePatientId",
    "bindingRevision", "scopeSha256"})


class AthenaBindingError(ValueError):
    def __init__(self, code="binding_invalid"):
        self.code = code if code in ERRORS else "binding_invalid"
        super().__init__(self.code)


def decode_scope(value, *, practice_id, case_id, tenant_id):
    """Validate the complete server result and recompute both identity digests."""
    if (type(value) is dict and set(value) == {"error"}
            and type(value["error"]) is str and value["error"] in ERRORS):
        raise AthenaBindingError(value["error"])
    try:
        if (type(value) is not dict or set(value) != SCOPE_KEYS
                or value["serverPracticeId"] != practice_id or value["caseId"] != case_id
                or value["tenantId"] != tenant_id):
            raise AthenaBindingError()
        profile = AthenaStaffProfile(practice_id, value["environment"],
            value["vendorPracticeId"], value["connectionRevision"])
        scope = AthenaStaffScope(profile, case_id, value["patientRef"],
            value["sourcePatientId"], value["bindingRevision"])
        if profile.fingerprint != value["profileSha256"] or scope.fingerprint != value["scopeSha256"]:
            raise AthenaBindingError()
        return scope
    except (AthenaStaffScopeError, TypeError, KeyError):
        raise AthenaBindingError() from None


class PostgresAthenaBindingStore:
    """Each scope is valid only for the transaction in which it was resolved."""
    durable = True

    def __init__(self, context):
        self.context, self.practice_id = context, text(context.practice_id)

    def in_transaction(self, conn, case_id, actor):
        """Use an existing practice-scoped transaction; never commit it here.

        The SQL reader retains membership, case, revision and revocation locks
        until the caller commits/rolls back. Reusing the returned value after
        that point requires a fresh resolution, including before an import.
        Resolve before taking case/registry row locks: the shared practice
        advisory lock must precede those locks in every consuming workflow.
        """
        if type(actor) is not ReviewActor:
            raise AthenaBindingError("not_authorized")
        if getattr(conn, "autocommit", True) is not False:
            raise AthenaBindingError()
        actor.__post_init__()
        text(case_id)
        row = conn.execute(
            "SELECT public.cosmaneura_athena_staff_scope(%s::uuid,%s::uuid,%s,%s) AS scope",
            (actor.tenant_id, actor.user_id, actor.role, case_id)).fetchone()
        return decode_scope(row["scope"] if row else None, practice_id=self.practice_id,
                            case_id=case_id, tenant_id=actor.tenant_id)

    @contextmanager
    def scope(self, case_id, actor):
        with self.context.connection() as conn:
            yield self.in_transaction(conn, case_id, actor)

    def healthcheck(self):
        with self.context.connection() as conn:
            row = conn.execute("""SELECT
                has_function_privilege(current_user,
                  'public.cosmaneura_athena_staff_scope(uuid,uuid,text,text)','EXECUTE')
                AND NOT has_table_privilege(current_user,'public.athena_staff_connections','SELECT,INSERT,UPDATE,DELETE,TRUNCATE')
                AND NOT has_table_privilege(current_user,'public.athena_staff_case_bindings','SELECT,INSERT,UPDATE,DELETE,TRUNCATE') AS ok""").fetchone()
            if not row or row["ok"] is not True:
                raise AthenaBindingError()
