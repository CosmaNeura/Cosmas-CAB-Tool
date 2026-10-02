"""Private migration-owner registration; no runtime or HTTP code imports this CLI."""
import argparse
import json
from pathlib import Path

from .athena_review import text, uid
from .athena_staff_contract import AthenaStaffProfile, AthenaStaffScope
from .settings import Settings


def _revision(value):
    if type(value) is not int or not 1 <= value <= 2**31 - 1:
        raise ValueError("invalid registration revision")
    return value


def _manifest(settings, action, value):
    common = {"schemaVersion", "serverPracticeId", "tenantId", "reviewRef"}
    keys = {"register-connection": {"environment", "vendorPracticeId", "connectionRevision"},
        "register-binding": {"caseId", "patientRef", "sourcePatientId", "connectionRevision", "bindingRevision"},
        "revoke-connection": {"connectionRevision"},
        "revoke-binding": {"caseId", "bindingRevision"},
        "register-read-admission": {"admissionId","admissionRevision","caseId","scopeSha256","runtimeRole","deploymentId","storageId","notBefore","expiresAt"},
        "revoke-read-admission": {"caseId","admissionId"}}
    if (action not in keys or type(value) is not dict or set(value) != common | keys[action]
            or value["schemaVersion"] != "athena-staff-registration.v1"
            or value["serverPracticeId"] != settings.practice_id
            or value["tenantId"] != settings.entra_tenant_id):
        raise ValueError("invalid registration manifest")
    text(value["serverPracticeId"]); uid(value["tenantId"]); text(value["reviewRef"], 160)
    for key in ("connectionRevision", "bindingRevision"):
        if key in value: _revision(value[key])
    if "caseId" in value: text(value["caseId"])
    if "patientRef" in value: text(value["patientRef"])
    if action == "register-read-admission":
        from .athena_admission import registration
        registration(value)
    if "admissionId" in value: uid(value["admissionId"])
    return value


def _owner(db, settings, *, registering):
    owner = db.execute("""SELECT bool_and(current_user=pg_get_userbyid(relowner))
        FROM pg_class WHERE oid IN ('public.athena_staff_connections'::regclass,
                                   'public.athena_staff_case_bindings'::regclass)""").fetchone()
    if not owner or owner[0] is not True:
        raise PermissionError("registration requires migration owner")
    db.execute("SELECT set_config('cosmaneura.practice_id',%s,true)", (settings.practice_id,))
    db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
               ("athena-staff-binding:" + settings.practice_id,))
    found = db.execute("SELECT 1 FROM practices WHERE practice_id=%s AND entra_tenant_id=%s::uuid AND (active OR %s) FOR SHARE",
                       (settings.practice_id, settings.entra_tenant_id, not registering)).fetchone()
    if not found: raise ValueError("configured practice unavailable")


def apply(settings, action, value):
    """Apply one reviewed manifest atomically. Duplicate revisions are conflicts."""
    import psycopg
    document = _manifest(settings, action, value)
    with psycopg.connect(settings.database_url, connect_timeout=3) as db:
        _owner(db, settings, registering=action.startswith("register-"))
        p, tenant, ref = settings.practice_id, settings.entra_tenant_id, document["reviewRef"]
        if action == "register-connection":
            profile = AthenaStaffProfile(p, document["environment"], document["vendorPracticeId"],
                                         document["connectionRevision"])
            db.execute("""INSERT INTO athena_staff_connections(practice_id,tenant_id,connection_revision,
                environment,vendor_practice_id,profile_sha256,review_ref) VALUES(%s,%s::uuid,%s,%s,%s,%s,%s)""",
                (p, tenant, profile.connection_revision, profile.environment,
                 profile.vendor_practice_id, profile.fingerprint, ref))
        elif action == "register-binding":
            row = db.execute("""SELECT connection_revision,environment,vendor_practice_id,revoked_at
                FROM athena_staff_connections WHERE practice_id=%s ORDER BY connection_revision DESC LIMIT 1 FOR SHARE""", (p,)).fetchone()
            if not row or row[0] != document["connectionRevision"] or row[3] is not None:
                raise ValueError("connection unavailable")
            profile = AthenaStaffProfile(p, row[1], row[2], row[0])
            scope = AthenaStaffScope(profile, document["caseId"], document["patientRef"],
                                      document["sourcePatientId"], document["bindingRevision"])
            db.execute("""INSERT INTO athena_staff_case_bindings(practice_id,case_id,binding_revision,
                connection_revision,patient_ref,source_patient_id,scope_sha256,review_ref)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""", (p, scope.case_id, scope.binding_revision,
                    profile.connection_revision, scope.patient_ref, scope.source_patient_id, scope.fingerprint, ref))
        elif action == "register-read-admission":
            from psycopg.types.json import Jsonb
            db.execute("""INSERT INTO athena_staff_read_admissions(practice_id,case_id,admission_id,admission_revision,document)
                VALUES(%s,%s,%s::uuid,%s,%s)""",(p,document["caseId"],document["admissionId"],document["admissionRevision"],Jsonb(document)))
        elif action == "revoke-read-admission":
            result=db.execute("""UPDATE athena_staff_read_admissions SET revocation_ref=%s
                WHERE practice_id=%s AND case_id=%s AND admission_id=%s::uuid AND revoked_at IS NULL""",
                (ref,p,document["caseId"],document["admissionId"]))
            if result.rowcount!=1:raise ValueError("registration unavailable")
        else:
            if action == "revoke-connection":
                result = db.execute("""UPDATE athena_staff_connections SET revocation_ref=%s
                    WHERE practice_id=%s AND connection_revision=%s AND revoked_at IS NULL""",
                    (ref, p, document["connectionRevision"]))
            else:
                result = db.execute("""UPDATE athena_staff_case_bindings SET revocation_ref=%s
                    WHERE practice_id=%s AND case_id=%s AND binding_revision=%s AND revoked_at IS NULL""",
                    (ref, p, document["caseId"], document["bindingRevision"]))
            if result.rowcount != 1: raise ValueError("registration unavailable")


def parse_manifest(raw):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out: raise ValueError("duplicate registration key")
            out[key] = value
        return out
    def reject(_): raise ValueError("invalid JSON constant")
    return json.loads(raw, object_pairs_hook=unique, parse_constant=reject)


def main():
    parser = argparse.ArgumentParser(description="Register/revoke an operator-reviewed Athena identity binding")
    parser.add_argument("action", choices=("register-connection", "register-binding", "revoke-connection", "revoke-binding", "register-read-admission", "revoke-read-admission"))
    parser.add_argument("manifest", help="private JSON manifest path; do not put source IDs or secrets in CLI arguments")
    args = parser.parse_args()
    try:
        path = Path(args.manifest)
        if path.stat().st_size > 16384: raise ValueError("registration manifest too large")
        raw = path.read_bytes()
        if len(raw) > 16384: raise ValueError("registration manifest too large")
        apply(Settings.from_env(), args.action, parse_manifest(raw))
    except Exception:
        print("Athena identity registration failed"); return 1
    print("Athena identity registration updated"); return 0


if __name__ == "__main__": raise SystemExit(main())
