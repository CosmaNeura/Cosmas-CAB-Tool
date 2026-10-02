"""Private operator registration/revocation using the migration-owner boundary."""
import argparse
import json
from pathlib import Path
from .provider_admission import connection, grant
from .billing_header import uuid
from .settings import Settings


def register(settings, kind, value):
    """No public route calls this function. Runtime logins cannot register."""
    import psycopg
    from psycopg.types.json import Jsonb
    document = connection(value) if kind=='connection' else grant(value) if kind=='grant' else None
    if document is None: raise ValueError('invalid registration kind')
    tenant = document['tenantId'] if kind=='connection' else document['actorTenantId']
    if tenant != settings.entra_tenant_id: raise ValueError('registration tenant mismatch')
    with psycopg.connect(settings.database_url) as db:
        _owner(db,settings)
        if kind=='connection':
            db.execute('INSERT INTO provider_source_connections(practice_id,connection_id,document) VALUES (%s,%s,%s)',
                       (settings.practice_id,document['connectionId'],Jsonb(document)))
        else:
            source=db.execute('SELECT document,revoked_at FROM provider_source_connections WHERE practice_id=%s AND connection_id=%s FOR SHARE',
                              (settings.practice_id,document['connectionId'])).fetchone()
            if (source is None or source[1] is not None or source[0]['version']!=document['connectionVersion']
                    or source[0]['tenantId']!=tenant): raise ValueError('source connection unavailable')
            allowed=db.execute('SELECT cosmaneura_billing_header_editor_allowed(%s::uuid,%s::uuid,%s)',
                (tenant,document['actorUserId'],document['actorRole'])).fetchone()
            if not allowed or allowed[0] is not True: raise ValueError('intended member unavailable')
            db.execute('INSERT INTO provider_source_admissions(practice_id,operation_id,connection_id,document) VALUES (%s,%s,%s,%s)',
                       (settings.practice_id,document['operationId'],document['connectionId'],Jsonb(document)))


def register_core(settings, kind, value):
    """Owner-only Core connection/projection registration, never an HTTP call."""
    import psycopg
    from psycopg import sql
    from psycopg.types.json import Jsonb
    from uuid import uuid4
    from .auth import CoreWorkloadPolicy
    from .billing_header import closed, text, timestamp
    from .migrations import _prepare_bound_login
    with psycopg.connect(settings.database_url) as db:
        _owner(db,settings)
        if kind=='core-connection':
            closed(value,('connection','corePracticeId','actorIssuer','workload','notBefore','expiresAt'))
            c=connection(value['connection']); w=CoreWorkloadPolicy(**value['workload'])
            text(value['corePracticeId'],128); text(value['actorIssuer'],256)
            if (c['tenantId']!=settings.entra_tenant_id or c['sourceMode']!='core_workload_verified'
                or timestamp(value['notBefore'])>=timestamp(value['expiresAt'])
                or not settings.database_runtime_password): raise ValueError('invalid Core connection')
            # Never turn an existing ordinary API login into the Core bridge.
            if db.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(settings.database_runtime_role,)).fetchone():
                raise ValueError('Core bridge requires a dedicated new login')
            _,ident=_prepare_bound_login(db,role=settings.database_runtime_role,
                password_value=settings.database_runtime_password,practice_id=settings.practice_id,
                description='Core bridge runtime')
            db.execute(sql.SQL('GRANT SELECT ON practices,users,practice_memberships,encounters,coder_feedback,coder_feedback_decisions,provider_handoff_heads,provider_handoff_operations,provider_source_connections,provider_source_admissions,provider_source_bindings,provider_source_acceptances TO {}').format(ident))
            db.execute(sql.SQL('GRANT SELECT,INSERT ON billing_headers,billing_header_revisions,billing_header_operations,billing_header_signoffs TO {}').format(ident))
            db.execute(sql.SQL('GRANT INSERT ON access_audit_attempts,access_audit_completions TO {}').format(ident))
            db.execute(sql.SQL('GRANT EXECUTE ON FUNCTION cosmaneura_practice_allowed(text),cosmaneura_billing_header_editor_allowed(uuid,uuid,text),cosmaneura_commit_provider_handoff(jsonb),cosmaneura_provider_model_source(uuid,uuid,text,text),cosmaneura_resolve_core_actor(uuid,uuid,text,text,text,text,text),cosmaneura_prepare_core_handoff(jsonb),cosmaneura_core_operation(uuid,uuid),cosmaneura_core_lock_encounter(uuid,text) TO {}').format(ident))
            db.execute('INSERT INTO provider_source_connections(practice_id,connection_id,document) VALUES(%s,%s,%s)',
                (settings.practice_id,c['connectionId'],Jsonb(c)))
            db.execute('INSERT INTO core_workload_connections(practice_id,connection_id,version,runtime_role,core_practice_id,actor_issuer,workload_tenant,workload_caller,workload_audience,workload_role,not_before,expires_at,review_ref) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (settings.practice_id,c['connectionId'],c['version'],settings.database_runtime_role,value['corePracticeId'],value['actorIssuer'],w.tenant_id,w.caller_id,w.audience,w.role,value['notBefore'],value['expiresAt'],c['reviewRef']))
            return {'connectionId':c['connectionId']}
        if kind!='core-actor': raise ValueError('invalid Core registration kind')
        closed(value,('connectionId','issuer','subject')); uuid(value['connectionId'])
        text(value['issuer'],256); text(value['subject'],256)
        user,projection=str(uuid4()),str(uuid4())
        db.execute("INSERT INTO users(tenant_id,user_id,identity_kind) VALUES(%s,%s,'core')",(settings.entra_tenant_id,user))
        db.execute("INSERT INTO practice_memberships(practice_id,tenant_id,user_id,roles) VALUES(%s,%s,%s,ARRAY['Provider']::text[])",(settings.practice_id,settings.entra_tenant_id,user))
        db.execute('INSERT INTO core_actor_projections(practice_id,projection_id,connection_id,issuer,subject,tenant_id,user_id) VALUES(%s,%s,%s,%s,%s,%s,%s)',
            (settings.practice_id,projection,value['connectionId'],value['issuer'],value['subject'],settings.entra_tenant_id,user))
        return {'projectionId':projection,'internalUserId':user}


def revoke(settings,kind,identifier):
    import psycopg
    from psycopg import sql
    uuid(identifier)
    if kind not in {'connection','grant','core-connection','core-actor'}: raise ValueError('invalid revocation kind')
    table,column={'connection':('provider_source_connections','connection_id'),'grant':('provider_source_admissions','operation_id'), 'core-connection':('core_workload_connections','connection_id'),'core-actor':('core_actor_projections','projection_id')}[kind]
    with psycopg.connect(settings.database_url) as db:
        _owner(db,settings)
        db.execute(sql.SQL('UPDATE {} SET revoked_at=clock_timestamp() WHERE practice_id=%s AND {}=%s AND revoked_at IS NULL').format(
            sql.Identifier(table),sql.Identifier(column)),(settings.practice_id,identifier))


def _owner(db,settings):
    owner=db.execute("SELECT current_user=pg_get_userbyid(relowner) FROM pg_class WHERE oid='public.provider_source_connections'::regclass").fetchone()
    if not owner or owner[0] is not True: raise PermissionError('registration requires migration owner')
    db.execute("SELECT set_config('cosmaneura.practice_id',%s,true)",(settings.practice_id,))
    practice=db.execute('SELECT 1 FROM practices WHERE practice_id=%s AND entra_tenant_id=%s::uuid',
                        (settings.practice_id,settings.entra_tenant_id)).fetchone()
    if practice is None: raise ValueError('configured practice unavailable')


def parse_manifest(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value: raise ValueError("duplicate registration key")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid JSON constant")))


def main():
    parser=argparse.ArgumentParser(description='Register or revoke a privately reviewed completed-note admission')
    parser.add_argument('action',choices=('register-connection','register-grant','revoke-connection','revoke-grant','register-core-connection','register-core-actor','revoke-core-connection','revoke-core-actor'))
    parser.add_argument('input',help='private JSON manifest path for registration; UUID for revocation')
    args=parser.parse_args()
    try:
        config=Settings.from_env(); action,kind=args.action.split('-',1)
        if action=='register':
            path=Path(args.input)
            if path.stat().st_size>16384: raise ValueError('registration manifest too large')
            raw=path.read_bytes()
            if len(raw)>16384: raise ValueError('registration manifest too large')
            (register_core if kind.startswith('core-') else register)(config,kind,parse_manifest(raw))
        else: revoke(config,kind,args.input)
    except Exception:
        print('source registration failed'); return 1
    print('source registration updated'); return 0


if __name__=='__main__': raise SystemExit(main())
