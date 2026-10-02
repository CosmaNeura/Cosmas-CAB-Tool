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


def revoke(settings,kind,identifier):
    import psycopg
    from psycopg import sql
    uuid(identifier)
    if kind not in {'connection','grant'}: raise ValueError('invalid revocation kind')
    table,column=('provider_source_connections','connection_id') if kind=='connection' else ('provider_source_admissions','operation_id')
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
    parser.add_argument('action',choices=('register-connection','register-grant','revoke-connection','revoke-grant'))
    parser.add_argument('input',help='private JSON manifest path for registration; UUID for revocation')
    args=parser.parse_args()
    try:
        config=Settings.from_env(); action,kind=args.action.split('-',1)
        if action=='register':
            path=Path(args.input)
            if path.stat().st_size>16384: raise ValueError('registration manifest too large')
            raw=path.read_bytes()
            if len(raw)>16384: raise ValueError('registration manifest too large')
            register(config,kind,parse_manifest(raw))
        else: revoke(config,kind,args.input)
    except Exception:
        print('source registration failed'); return 1
    print('source registration updated'); return 0


if __name__=='__main__': raise SystemExit(main())
