"""Operator-owned completed-note admission. No request creates source authority."""
from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock
from uuid import uuid4

from .billing_header import BillingHeaderError, closed, digest, integer, text, uuid
from .provider_handoff import HandoffError, require

CONNECTION_KEYS = {'connectionId','version','tenantId','sourceMode','dataClass','reviewRef'}
GRANT_KEYS = {'admissionId','connectionId','connectionVersion','operationId','actorTenantId','actorUserId','actorRole',
              'recordKey','sourceRevision','requestSha256','manifestSha256','expectedSource','notBefore','expiresAt'}


def instant(value):
    text(value,40)
    parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None: raise ValueError('admission time requires timezone')
    return parsed


def connection(value):
    closed(value,CONNECTION_KEYS)
    uuid(value['connectionId']); integer(value['version'],1)
    uuid(value['tenantId']); text(value['reviewRef'],160)
    require(value['sourceMode']=='cab_provider_attested')
    require(value['dataClass'] in {'synthetic','deidentified'})
    return deepcopy(value)


def grant(value):
    import re
    closed(value,GRANT_KEYS)
    for key in ('admissionId','connectionId','operationId','actorTenantId','actorUserId'): uuid(value[key])
    integer(value['connectionVersion'],1); integer(value['sourceRevision'],1,9007199254740991)
    require(value['actorRole'] in {'Provider','Admin'})
    require(isinstance(value['recordKey'],str) and re.fullmatch(r'source-encounter-v1:[a-f0-9]{64}',value['recordKey']))
    for key in ('requestSha256','manifestSha256'):
        require(isinstance(value[key],str) and re.fullmatch(r'[a-f0-9]{64}',value[key]))
    require(value['expectedSource'] is None or type(value['expectedSource']) is dict)
    require(instant(value['notBefore']) < instant(value['expiresAt']))
    return deepcopy(value)


def admission_for(prepared, actor, source, *, not_before, expires_at, admission_id=None):
    """Prepare a registration from a privately reviewed request; never an API."""
    c=connection(source)
    return grant({'admissionId':admission_id or str(uuid4()),'connectionId':c['connectionId'],
        'connectionVersion':c['version'],'operationId':prepared.operation_id,'actorTenantId':actor.tenant_id,
        'actorUserId':actor.user_id,'actorRole':actor.role,'recordKey':prepared.record_key,
        'sourceRevision':prepared.source_revision,'requestSha256':digest(prepared.intent),
        'manifestSha256':prepared.manifest_sha256,'expectedSource':prepared.expected_source,
        'notBefore':not_before,'expiresAt':expires_at})


def verify_admission(source, registration, prepared, actor, now):
    try:
        c,g=connection(source),grant(registration)
        require(c['connectionId']==g['connectionId'] and c['version']==g['connectionVersion']
            and c['tenantId']==g['actorTenantId']==actor.tenant_id
            and g['actorUserId']==actor.user_id and g['actorRole']==actor.role
            and g['operationId']==prepared.operation_id and g['recordKey']==prepared.record_key
            and g['sourceRevision']==prepared.source_revision and g['requestSha256']==digest(prepared.intent)
            and g['manifestSha256']==prepared.manifest_sha256
            and digest(g['expectedSource'])==digest(prepared.expected_source)
            and instant(g['notBefore']) <= now < instant(g['expiresAt']))
        return {'connection':c,'admission':g,'requestSha256':g['requestSha256']}
    except (ValueError,TypeError,KeyError,HandoffError,BillingHeaderError):
        raise HandoffError('operational_handoff_not_approved') from None


class MemoryAdmissionRegistry:
    """Local test/operator composition, empty by default; no HTTP mutators."""
    def __init__(self,practice_id,clock=lambda:datetime.now(timezone.utc)):
        self.practice_id,self.clock=practice_id,clock
        self.connections,self.grants,self.bindings,self.acceptances={},{},{},{}
        self.lock=RLock()

    def register_connection(self,value):
        c=connection(value)
        with self.lock:
            require(c['connectionId'] not in self.connections,'handoff_conflict')
            self.connections[c['connectionId']]={'document':c,'revoked':False}

    def register_grant(self,value):
        g=grant(value)
        with self.lock:
            require(g['connectionId'] in self.connections and g['operationId'] not in self.grants,'handoff_conflict')
            self.grants[g['operationId']]={'document':g,'revoked':False}

    def revoke(self,kind,identifier):
        with self.lock:
            rows=self.connections if kind=='connection' else self.grants if kind=='grant' else None
            require(rows is not None and identifier in rows)
            rows[identifier]['revoked']=True

    def admit(self,prepared,actor,has_head):
        g=self.grants.get(prepared.operation_id)
        c=self.connections.get(g['document']['connectionId']) if g else None
        require(g is not None and c is not None and not g['revoked'] and not c['revoked'],
                'operational_handoff_not_approved')
        proof=verify_admission(c['document'],g['document'],prepared,actor,self.clock())
        bound=self.bindings.get(prepared.record_key)
        require((not has_head or bound is not None) and (bound is None or bound==c['document']['connectionId']),
                'operational_handoff_not_approved')
        return proof

    def record(self,prepared,proof):
        require(self.clock() < instant(proof['admission']['expiresAt']), 'operational_handoff_not_approved')
        self.bindings[prepared.record_key]=proof['connection']['connectionId']
        self.acceptances[prepared.operation_id]=deepcopy(proof)
