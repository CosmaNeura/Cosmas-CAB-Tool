"""Private local test composition; no HTTP authority or inference replacement."""
from uuid import NAMESPACE_URL, uuid4, uuid5
from cosmaneura_billing.billing_header import HeaderActor
from cosmaneura_billing.provider_admission import admission_for
from cosmaneura_billing.provider_handoff import HandoffRequest, prepare_handoff


def source_for(actor):
    return {'connectionId': str(uuid5(NAMESPACE_URL, 'synthetic-admission:'+actor.tenant_id)), 'version': 1,
            'tenantId': actor.tenant_id, 'sourceMode': 'cab_provider_attested', 'dataClass': 'synthetic',
            'reviewRef': 'local disposable synthetic receiver check'}


def registration(p, actor, source=None, **changes):
    result = admission_for(p, actor, source or source_for(actor),
        not_before='2026-01-01T00:00:00Z', expires_at='2099-01-01T00:00:00Z')
    return {**result, **changes}


def register_memory(store, p, actor):
    source = source_for(actor)
    if source['connectionId'] not in store.admissions.connections:
        store.admissions.register_connection(source)
    store.admissions.register_grant(registration(p, actor, source))


def local_actor(config):
    user = str(uuid5(NAMESPACE_URL, f'cab-local:{config.development_user_id}'))
    return HeaderActor(str(uuid5(NAMESPACE_URL, 'cab-local:development')), user, f'Provider {user[:8]}', 'Provider')


def register_route(client, payload):
    config = client.app.state.settings
    p = prepare_handoff(HandoffRequest.model_validate(payload), config.practice_id)
    register_memory(client.app.state.provider_handoff_store, p, local_actor(config))
