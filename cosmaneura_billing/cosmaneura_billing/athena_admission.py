"""Server-owned read/storage authority. No model/outreach authority or network."""
from copy import deepcopy
from datetime import datetime,timezone
import re
from uuid import UUID

PROOF_KEYS=frozenset({'schemaVersion','admissionId','admissionRevision','scopeSha256','grantSha256','expiresAt'})
REGISTRATION_KEYS=frozenset({'schemaVersion','serverPracticeId','tenantId','reviewRef','admissionId',
    'admissionRevision','caseId','scopeSha256','runtimeRole','deploymentId','storageId','notBefore','expiresAt'})
_SEAL=object()

class AthenaAdmissionError(ValueError):
    def __init__(self): super().__init__('Athena read/storage admission unavailable')

def instant(value):
    if not isinstance(value,str) or not re.fullmatch(
        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)',value) or value.endswith('-00:00'):
        raise AthenaAdmissionError()
    try:return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(timezone.utc)
    except ValueError:raise AthenaAdmissionError() from None

def registration(value):
    try:
        if type(value) is not dict or set(value)!=REGISTRATION_KEYS or value['schemaVersion']!='athena-staff-registration.v1':raise AthenaAdmissionError()
        for key in ('admissionId','tenantId'):
            if str(UUID(value[key]))!=value[key]:raise AthenaAdmissionError()
        if type(value['admissionRevision']) is not int or not 1<=value['admissionRevision']<=2**31-1:raise AthenaAdmissionError()
        if not isinstance(value['scopeSha256'],str) or not re.fullmatch('[a-f0-9]{64}',value['scopeSha256']):raise AthenaAdmissionError()
        for key in ('runtimeRole','deploymentId','storageId'):
            if not isinstance(value[key],str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}',value[key]):raise AthenaAdmissionError()
        for key in ('serverPracticeId','caseId','reviewRef'):
            s=value[key]
            if not isinstance(s,str) or not 1<=len(s)<=128 or s!=s.strip() or any(ord(c)<32 or ord(c)==127 or 0xD800<=ord(c)<=0xDFFF for c in s):raise AthenaAdmissionError()
        if instant(value['notBefore'])>=instant(value['expiresAt']):raise AthenaAdmissionError()
        return deepcopy(value)
    except (KeyError,TypeError,ValueError):raise AthenaAdmissionError() from None

def _proof(value,scope):
    try:
        if type(value) is not dict or set(value)!=PROOF_KEYS or value['schemaVersion']!='athena-read-storage-admission.v1':raise AthenaAdmissionError()
        if str(UUID(value['admissionId']))!=value['admissionId'] or value['scopeSha256']!=scope.fingerprint:raise AthenaAdmissionError()
        if type(value['admissionRevision']) is not int or value['admissionRevision']<1:raise AthenaAdmissionError()
        if not re.fullmatch('[a-f0-9]{64}',value['grantSha256']):raise AthenaAdmissionError()
        instant(value['expiresAt']);return deepcopy(value)
    except (KeyError,TypeError,ValueError):raise AthenaAdmissionError() from None

class AthenaReadLease:
    """Usable only inside the resolving DB transaction, never from JSON/flags."""
    def __init__(self,seal,scope,proof,check):
        if seal is not _SEAL:raise AthenaAdmissionError()
        self._scope,self._proof,self._check,self._active=scope.fingerprint,proof,check,True

    @property
    def public(self):return deepcopy(self._proof)

    def assert_active(self,scope):
        if not self._active or scope.fingerprint!=self._scope or self._check()!=self._proof:raise AthenaAdmissionError()

    def close(self):self._active=False

def resolve_lease(conn,scope,actor,deployment_id,storage_id):
    if scope.profile.environment!='production' or not deployment_id or not storage_id:return None
    if conn.closed or conn.autocommit or conn.info.transaction_status.name!='INTRANS':raise AthenaAdmissionError()
    transaction=conn.execute('SELECT pg_current_xact_id()::text AS xid').fetchone()['xid']
    def read():
        if conn.closed or conn.autocommit or conn.info.transaction_status.name!='INTRANS':raise AthenaAdmissionError()
        if conn.execute('SELECT pg_current_xact_id()::text AS xid').fetchone()['xid']!=transaction:raise AthenaAdmissionError()
        row=conn.execute('SELECT public.cosmaneura_athena_read_admission(%s::uuid,%s::uuid,%s,%s,%s,%s) AS admission',
            (actor.tenant_id,actor.user_id,actor.role,scope.case_id,deployment_id,storage_id)).fetchone()
        value=row['admission'] if row else None
        if value=={'error':'read_not_admitted'}:return None
        return _proof(value,scope)
    value=read()
    return AthenaReadLease(_SEAL,scope,value,read) if value is not None else None
