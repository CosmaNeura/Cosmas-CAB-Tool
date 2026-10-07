"""Owned PostgreSQL + spawned fake Azure adapter; authored sources only."""
from dataclasses import replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import base64
import json
import os
import time

import psycopg
from psycopg.conninfo import make_conninfo
from cosmaneura_billing.auth import AuthPrincipal
from cosmaneura_billing.billing_header import HeaderActor, source_snapshot
from cosmaneura_billing.corpus.demo import SAMPLE_VISIT
from cosmaneura_billing.llm import AzureOpenAILLM
from cosmaneura_billing.migrations import _provision_runtime_role
from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.persistence import PostgresContext, PostgresEncounterStore
from cosmaneura_billing.settings import Settings
from core_bridge_fixture import AuthoredCoreResponses


class Responses(AuthoredCoreResponses):
    def __init__(self, path):
        super().__init__()
        self.path = Path(path)

    def create(self, **request):
        with (self.path / 'calls.txt').open('a') as f:
            f.write('attempt\n')
        control = json.loads((self.path / 'control.json').read_text())
        if control.get('exit'):
            os._exit(7)  # Deliberate owned-child crash; never production behavior.
        time.sleep(control.get('delay', 0))
        return super().create(**request)


def engine(settings, *, feedback_store=None, path):
    result = BillingOrchestrator(settings=settings, llm=AzureOpenAILLM(settings=settings,
        client=Responses(path), max_retries=0))
    unsupported = json.loads((Path(path) / 'control.json').read_text()).get('unsupported')
    if unsupported:
        setattr(result, unsupported, object())
    return result


def prepare(dsn, path):
    path = Path(path); path.mkdir(exist_ok=True)
    (path / 'control.json').write_text('{"delay": 0}')
    practice, tenant, user = 'async-' + uuid4().hex, str(uuid4()), str(uuid4())
    role = 'async_' + uuid4().hex[:16]
    owner = PostgresContext(dsn, practice_id=practice)
    settings = Settings(environment='test', auth_mode='container_apps', entra_tenant_id=tenant,
        practice_id=practice, state_store='postgres', chase_store='postgres',
        database_url=make_conninfo(dsn, user=role), database_runtime_role=role,
        database_runtime_password='disposable-synthetic-test-only', billing_header_enabled=True,
        billing_header_async_enabled=True, allowed_roles=('Provider', 'Coder', 'Admin'),
        review_context_signing_key='synthetic-header-generation-signing-key-only',
        azure_openai_enabled=True, azure_openai_synthetic_only=False, azure_client_id=str(uuid4()),
        azure_openai_endpoint='https://authored-async.openai.azure.com', azure_openai_deployment='authored',
        azure_openai_model='gpt-5.6-sol', azure_language_enabled=False,
        azure_language_shadow_enabled=False, cpt_auto_fetch=False, telemetry_enabled=False)
    with psycopg.connect(dsn) as db:
        assert db.info.host == '127.0.0.1' and db.info.port == 32800
        assert db.info.dbname.startswith('cab_header_async_20261007_')
        db.execute('INSERT INTO practices(practice_id,entra_tenant_id) VALUES(%s,%s)',(practice,tenant))
        db.execute('INSERT INTO users(tenant_id,user_id) VALUES(%s,%s)',(tenant,user))
        db.execute('INSERT INTO practice_memberships(practice_id,tenant_id,user_id,roles) VALUES(%s,%s,%s,%s)',
                   (practice,tenant,user,['Provider']))
        admin = str(uuid4())
        db.execute('INSERT INTO users(tenant_id,user_id) VALUES(%s,%s)',(tenant,admin))
        db.execute('INSERT INTO practice_memberships(practice_id,tenant_id,user_id,roles) VALUES(%s,%s,%s,%s)',
                   (practice,tenant,admin,['Admin']))
        _provision_runtime_role(db, settings)
    stored = PostgresEncounterStore(owner).upsert(SAMPLE_VISIT, source='synthetic')
    principal = AuthPrincipal(user, tenant, practice, frozenset({'Provider'}))
    actor = HeaderActor(tenant, user, 'Synthetic provider', 'Provider')
    claims = {'auth_typ':'aad','claims':[{'typ':'oid','val':user},{'typ':'tid','val':tenant},{'typ':'roles','val':'Provider'}]}
    auth = {'X-MS-CLIENT-PRINCIPAL':base64.b64encode(json.dumps(claims).encode()).decode()}
    return SimpleNamespace(settings=settings, owner=owner, stored=stored, actor=actor, principal=principal,
        factory=partial(engine,path=str(path)), path=path, auth=auth,
        target={'encounterId':stored.bundle.encounter_id,'expectedSource':source_snapshot(stored)})
