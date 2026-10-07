"""Durable one-attempt generation with bounded, supervised child processes.

Only normalized server identity/configuration crosses the private spawn pipe. No
Request, JWT, clinical result, or credential is put in process args or logs.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone, timedelta
import multiprocessing
import os
import signal
import threading
import time
from uuid import uuid4

from . import billing_header as domain
from .persistence import PostgresAccessAuditStore, PostgresContext, postgres_services

_ACTIVE = ContextVar('cab_header_generation', default=None)


def check_generation_request():
    active = _ACTIVE.get()
    if active is not None:
        active[0].call(active[1], 'check')


class GenerationStore:
    def __init__(self, context):
        self.context = context

    def call(self, command, action, *, conn=None, attempt=None, **extra):
        with (nullcontext(conn) if conn is not None else self.context.connection()) as db:
            db.execute("SET LOCAL statement_timeout='5s'")
            db.execute("SET LOCAL lock_timeout='2s'")
            if attempt is not None:
                PostgresAccessAuditStore._insert_attempt(db, attempt)
            try:
                row = db.execute('SELECT public.cosmaneura_header_generation(%s) AS result',
                    (self.context.Jsonb({**command, 'action': action, **extra}),)).fetchone()
            except self.context.psycopg.Error as exc:
                code = {'generation source changed':'source_changed',
                        'generation operation conflict':'operation_conflict',
                        'generation capacity unavailable':'generation_capacity',
                        'generation not authorized':'not_authorized',
                        'generation no longer running':'generation_unknown'}.get(exc.diag.message_primary)
                if code: raise domain.BillingHeaderError(code) from None
                raise
            if attempt is not None and row['result'].get('new') is not True:
                # This internal reservation did not start work. Close it in the
                # same transaction; the original job owns its own compute audit.
                PostgresAccessAuditStore._insert_completion(db, replace(attempt,
                    occurred_at=domain.utc_now(), status=200, outcome='allowed'))
            return row['result']

    def healthcheck(self):
        with self.context.connection() as db:
            row = db.execute("SELECT has_function_privilege(current_user, 'public.cosmaneura_header_generation(jsonb)', 'EXECUTE') AS ok").fetchone()
            domain.require(row and row['ok'], 'generation_unavailable')


def command_for(stored, actor, operation):
    domain.uuid(operation)
    source = domain.source_snapshot(stored)
    return {'tenant': actor.tenant_id, 'user': actor.user_id, 'role': actor.role,
            'encounter': stored.bundle.encounter_id, 'source': source,
            'sourceSha': domain.digest(source), 'operation': operation}


@dataclass(frozen=True, repr=False)
class Work:
    settings: object
    principal: object
    command: dict
    audit: object
    core: object = None
    factory: object = None


def _worker(work):
    """One owned child, one pipeline; all exceptions stay metadata-only."""
    from . import api
    from .billing_header_store import PostgresBillingHeaderStore
    from .billing_header_runtime import generated_header
    from .model_admission import parse_policy
    services = postgres_services(work.settings)
    jobs = GenerationStore(services.context)
    terminal = 'failed'
    engine = None
    lease = None
    token = _ACTIVE.set((jobs, work.command))
    try:
        remaining = float(jobs.call(work.command, 'check')['remaining'])
        # The child also owns a deadline: parent loss must not leave an orphan
        # able to disclose/commit later. SIGALRM exit leaves durable unknown.
        signal.signal(signal.SIGALRM, lambda *_: os._exit(124))
        signal.setitimer(signal.ITIMER_REAL, max(.001, remaining))
        headers = PostgresBillingHeaderStore(services.context)
        if work.core is not None:
            from .core_bridge import worker_lease, CoreHeaderStore
            lease = worker_lease(work.core, services)
            headers = CoreHeaderStore(services.context)
        with (lease.use() if lease is not None else nullcontext()):
            stored = services.encounters.get(work.command['encounter'])
            domain.check_source(stored, work.command['source'])
            check_generation_request()
            with api.inference_scope_for(work.settings, parse_policy(work.settings.model_data_policy_json),
                    services.encounters, work.principal, stored) as permit:
                engine = (work.factory or api.get_orchestrator)(work.settings, feedback_store=services.feedback)
                # Secondary/shadow extraction uses threads that do not retain
                # this job's disclosure guard. This receiver supports one lane.
                domain.require(engine.secondary_coding is None and engine.ta4h is None
                    and engine.shadow_runner is None and engine.ta4h_mode == 'off', 'generation_unavailable')
                jobs.call(work.command, 'start')
                terminal = 'unknown'  # From here an external attempt may have happened.
                result = engine.process(stored.bundle, user_id=work.principal.user_id,
                    input_provenance=stored.source, **({'input_scope': permit} if permit else {}))
                check_generation_request()
                generated = generated_header(stored, result, engine_version=api._ENGINE_VERSION)
                if permit:
                    generated['generation']['modelInputProof'] = permit.proof
                def before(db, actual):
                    domain.check_source(actual, work.command['source'])
                    jobs.call(work.command, 'check', conn=db)
                    if permit:
                        permit.assert_persistable(db, actual)
                def after(db, actual, view, created):
                    jobs.call(work.command, 'finish', conn=db, status='succeeded',
                              header=view['header']['headerId'], created=created)
                headers.create(work.command['encounter'], expected_source=work.command['source'],
                    generated=generated, actor=domain.actor_for_principal(work.principal, work.command['role']),
                    operation_id=work.command['operation'], audit_completion=replace(work.audit,
                        occurred_at=domain.utc_now(), status=200, outcome='allowed'),
                    before_insert=before, after_create=after)
                terminal = 'succeeded'
    except BaseException:
        pass
    finally:
        _ACTIVE.reset(token)
        try:
            state = jobs.call(work.command, 'finish', status=terminal)
            terminal = state['status']
        except Exception:
            terminal = 'unknown'  # Persistent deadline also projects unknown, without retry.
        if work.core is not None:
            try:
                from .core_bridge import complete_generation
                complete_generation(work.core, terminal)
            except Exception:
                pass  # Core's original action expires; completion never grants new work.
        if engine is not None:
            try:
                engine.shutdown()
            except Exception:
                pass


class GenerationManager:
    """No queue. Durable practice bound and independent local process bound."""
    def __init__(self, settings, *, factory=None):
        domain.require(os.name == 'posix' and settings.state_store == 'postgres' and settings.billing_header_enabled,
                       'generation_unavailable')
        domain.require(factory is None or settings.environment in {'test', 'development'}, 'generation_unavailable')
        self.settings, self.factory = settings, factory
        self.store = GenerationStore(PostgresContext(settings.database_url, practice_id=settings.practice_id))
        self._lock, self._stop = threading.RLock(), threading.Event()
        self._children = {}
        self._monitor = None

    def start(self):
        self.store.healthcheck()
        domain.require(not self._children and (self._monitor is None or not self._monitor.is_alive()), 'generation_unavailable')
        self._stop.clear()
        self._monitor = threading.Thread(target=self._watch, name='cab-generation-supervisor', daemon=False)
        self._monitor.start()

    def submit(self, stored, actor, principal, operation, audit, *, core=None):
        cmd = command_for(stored, actor, operation)
        # Read-only duplicate lookup before reserving capacity; the DB claim still
        # serializes different operation IDs and replicas on the exact source.
        prior = self.store.call(cmd, 'status')
        if 'job' in prior:
            return prior
        with self._lock:
            domain.require(not self._stop.is_set() and len(self._children) < 2, 'generation_capacity')
            deadline = None
            if core is not None:
                deadline = datetime.fromtimestamp(core['workload'].expires_at, timezone.utc)
                if core['generationExpiresAt'] is not None:
                    deadline = min(deadline, domain.timestamp(core['generationExpiresAt']))
            job, owner = str(uuid4()), str(uuid4())
            audit = replace(audit, event_id=str(uuid4()), occurred_at=domain.utc_now(), status=0, outcome='attempted')
            cmd.update(job=job, owner=owner, deadline=deadline.isoformat() if deadline else None, audit=audit.event_id)
            claimed = self.store.call(cmd, 'claim', attempt=audit)
            if not claimed.get('new'):
                return claimed
            work = Work(self.settings, principal, cmd, audit, core, self.factory)
            process = multiprocessing.get_context('spawn').Process(target=_worker, args=(work,), name='cab-header-generation')
            try:
                process.start()
            except BaseException:
                self.store.call(cmd, 'finish', status='failed')
                raise domain.BillingHeaderError('generation_unavailable') from None
            self._children[job] = (process, work, time.monotonic() + float(claimed["remaining"]))
            return claimed

    def _retire(self, job, entry, *, stop=False):
        process, work, deadline = entry
        if stop or time.monotonic() >= deadline:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=2)
        if process.is_alive():
            return
        process.join(timeout=0)
        try:
            state = self.store.call(work.command, 'finish', status='unknown')
            # A killed child cannot send completion; the original Core action expires.
        except Exception:
            pass
        process.close()
        self._children.pop(job, None)

    def _watch(self):
        while not self._stop.wait(.2):
            with self._lock:
                for job, entry in list(self._children.items()):
                    self._retire(job, entry)

    def shutdown(self):
        self._stop.set()
        if self._monitor is not None:
            self._monitor.join(timeout=12)
        with self._lock:
            for job, entry in list(self._children.items()):
                self._retire(job, entry, stop=True)


def response(state, operation, store, encounter, source):
    status = state['status']
    view = store.latest(encounter, expected_source=source) if status == 'succeeded' else None
    domain.require(status != 'succeeded' or view is not None, 'store_invalid')
    # Polling reports current header (including later human revisions), never a
    # stale generation snapshot. 'created' is false for all status responses.
    return {'generation': {'operationId': operation, 'status': status}, 'view': view, 'created': False}
