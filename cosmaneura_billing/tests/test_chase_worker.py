"""Fail-closed orchestration for the bounded durable chase worker."""

from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import UUID

import pytest

from cosmaneura_billing.chase.acs_email import EmailSubmissionError
from cosmaneura_billing.chase.models import Channel
from cosmaneura_billing.chase.policy_fingerprint import policy_fingerprint
from cosmaneura_billing.chase.scheduler_store import (
    AmbiguousDelivery,
    DispatchContext,
    SchedulerConflict,
    SchedulerLeaseExpired,
)
from cosmaneura_billing.chase.worker import (
    ChaseWorker,
    WorkerConfigurationError,
    WorkerStatus,
    main,
)
from cosmaneura_billing.chase.worker_config import ChaseWorkerConfig
from cosmaneura_billing.direct_contract import (
    DirectContractList,
    check_direct_contract,
)


NOW = datetime(2026, 8, 18, 15, 30, tzinfo=timezone.utc)
WORKER_URL = (
    "postgresql://cosmaneura_chase_worker:synthetic-worker-secret@"
    "db.invalid/cab?sslmode=require"
)
ACS_CONNECTION = (
    "endpoint=https://pilot.communication.azure.com;"
    "accesskey=synthetic-acs-secret="
)
PATIENT_REF = "synthetic-patient-reference"
RECIPIENT = "synthetic-patient@example.test"


def _email_config(**changes) -> ChaseWorkerConfig:
    values = {
        "environment": "staging",
        "practice_id": "practice-synthetic",
        "practice_name": "Synthetic Practice",
        "chase_worker_database_url": WORKER_URL,
        "chase_scheduler_enabled": True,
        "patient_messaging_enabled": True,
        "patient_messaging_operationally_activated": True,
        "chase_transport": "acs_email",
        "acs_email_connection_string": ACS_CONNECTION,
        "acs_email_sender_address": "sender@pilot.example",
        "acs_email_reply_to_address": "billing@pilot.example",
        "upload_url": "https://upload.cosmaneura.dev/insurance",
        "direct_contract_list": "/synthetic/direct-contract.csv",
        "chase_gate_hmac_key": "g" * 32,
        "practice_timezone": "America/New_York",
        "chase_worker_batch_size": 3,
        "chase_worker_lease_seconds": 300,
    }
    values.update(changes)
    return ChaseWorkerConfig(**values)


def _record_only_config(**changes) -> ChaseWorkerConfig:
    values = {
        "environment": "test",
        "practice_id": "practice-synthetic",
        "practice_name": "Synthetic Practice",
        "chase_worker_database_url": WORKER_URL,
        "chase_scheduler_enabled": True,
        "chase_transport": "record_only",
        "practice_timezone": "America/New_York",
        "chase_worker_batch_size": 3,
    }
    values.update(changes)
    return ChaseWorkerConfig(**values)


def _fingerprint(config: ChaseWorkerConfig) -> str:
    return policy_fingerprint(
        config.build_chase_policy(),
        practice_timezone=config.practice_timezone,
        delivery=config.build_delivery_configuration(),
    )


def _gate_fingerprint(roster: DirectContractList) -> str:
    return check_direct_contract(
        PATIENT_REF,
        roster=roster,
        now=lambda: NOW.isoformat(),
    ).gate_fingerprint


def _dispatch(
    config: ChaseWorkerConfig,
    roster: DirectContractList,
    *,
    delivery_id: str = "10000000-0000-4000-8000-000000000001",
    operation_id: str = "20000000-0000-4000-8000-000000000001",
    gate_fingerprint: str = "",
    template_id: str = "email.first",
    policy_step_ordinal: int = 0,
    gate_disposition: str = "clear",
    review_release_id: str = "",
) -> DispatchContext:
    return DispatchContext(
        delivery_id=delivery_id,
        lease_token="30000000-0000-4000-8000-000000000001",
        provider_operation_id=operation_id,
        policy_revision_id="40000000-0000-4000-8000-000000000001",
        policy_fingerprint=_fingerprint(config),
        policy_step_ordinal=policy_step_ordinal,
        channel=Channel.EMAIL,
        template_id=template_id,
        recipient=RECIPIENT,
        patient_ref=PATIENT_REF,
        gate_fingerprint=gate_fingerprint or _gate_fingerprint(roster),
        gate_disposition=gate_disposition,
        review_release_id=review_release_id,
    )


class CapturingLogger:
    def __init__(self):
        self.messages: list[str] = []

    def _capture(self, message, *args, **kwargs):
        assert not args
        assert not kwargs
        self.messages.append(str(message))

    debug = _capture
    info = _capture
    warning = _capture
    error = _capture
    critical = _capture


class FakeRepository:
    def __init__(
        self, *, claims=(), quarantined=(), ambiguous=(), fail_at: str = "",
    ):
        self.claims = list(claims)
        self.quarantined = list(quarantined)
        self.ambiguous = list(ambiguous)
        self.fail_at = fail_at
        self.calls: list[tuple[str, object]] = []
        self.finalizations: list[dict] = []
        self.guard_active = False

    def _fail(self, name: str):
        if self.fail_at == name:
            raise RuntimeError(
                "synthetic raw database detail with synthetic-patient-reference"
            )

    def quarantine_expired_submissions(self, **kwargs):
        self._fail("quarantine")
        self.calls.append(("quarantine", kwargs))
        return self.quarantined

    def list_ambiguous(self, **kwargs):
        self._fail("ambiguous")
        self.calls.append(("ambiguous", kwargs))
        return self.ambiguous

    def schedule_unscheduled(self, policy, delivery, **kwargs):
        self._fail("schedule")
        self.calls.append(("schedule", (policy, delivery, kwargs)))
        return 2

    def reserve_due(self, policy, delivery, **kwargs):
        self._fail("reserve")
        self.calls.append(("reserve", (policy, delivery, kwargs)))
        return [object(), object()]

    def claim_delivery(self, **kwargs):
        self._fail("claim")
        self.calls.append(("claim", kwargs))
        return self.claims.pop(0) if self.claims else None

    @contextmanager
    def guard_submission(self, context):
        self._fail("guard")
        self.calls.append(("guard_enter", context.delivery_id))
        if self.fail_at == "guard_conflict":
            raise SchedulerConflict("synthetic authorization changed")
        if self.fail_at == "lease_expired":
            raise SchedulerLeaseExpired("synthetic lease expired")
        try:
            self.guard_active = True
            yield context
        finally:
            self.guard_active = False
            self.calls.append(("guard_exit", context.delivery_id))

    def finalize_delivery(self, **kwargs):
        self._fail("finalize")
        self.calls.append(("finalize", kwargs))
        self.finalizations.append(kwargs)
        return object()


class FakeTransport:
    def __init__(self, outcome="accepted", *, guarded_by=None):
        self.outcome = outcome
        self.guarded_by = guarded_by
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        if self.guarded_by is not None:
            assert self.guarded_by.guard_active is True
        self.calls.append(kwargs)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def test_success_uses_exact_template_stable_operation_and_finalizes_once():
    config = _email_config()
    roster = DirectContractList(
        refs=[], version="roster-v1", fingerprint_key="g" * 32,
    )
    context = _dispatch(config, roster)
    repository = FakeRepository(claims=[context])
    transport = FakeTransport(guarded_by=repository)
    logger = CapturingLogger()

    result = ChaseWorker(
        config,
        repository=repository,
        transport=transport,
        roster=roster,
        clock=lambda: NOW,
        logger=logger,
    ).run_once()

    assert result.status is WorkerStatus.COMPLETED
    assert result.exit_code == 0
    assert result.quarantined == 0
    assert result.ambiguous == 0
    assert result.scheduled == 2
    assert result.reserved == 2
    assert result.claimed == 1
    assert result.accepted == 1
    assert result.permanent_failures == 0
    assert len(transport.calls) == 1
    submitted = transport.calls[0]
    assert submitted["channel"] is Channel.EMAIL
    assert submitted["recipient"] == RECIPIENT
    assert submitted["operation_id"] == context.provider_operation_id
    assert submitted["subject"] == "Synthetic Practice: we need your insurance card"
    assert "Hi there," in submitted["body"]
    assert PATIENT_REF not in submitted["subject"]
    assert PATIENT_REF not in submitted["body"]

    assert repository.finalizations == [{
        "delivery_id": context.delivery_id,
        "lease_token": context.lease_token,
        "outcome": "accepted",
        "as_of": NOW,
        "provider_message_id": "",
        "error_code": "",
    }]
    claim = [value for name, value in repository.calls if name == "claim"][0]
    assert claim["expected_policy_fingerprint"] == context.policy_fingerprint
    assert claim["contact_max_age_hours"] == 24
    assert claim["lease_seconds"] == 300

    log_text = "\n".join(logger.messages)
    assert PATIENT_REF not in log_text
    assert RECIPIENT not in log_text
    assert submitted["subject"] not in log_text
    assert submitted["body"] not in log_text
    assert WORKER_URL not in log_text
    assert ACS_CONNECTION not in log_text
    assert str(UUID(context.delivery_id)) in log_text


def test_record_only_runs_db_staff_task_lane_without_claiming_or_sending():
    config = _record_only_config()
    repository = FakeRepository()
    transport = FakeTransport(
        AssertionError("record-only worker must never invoke a transport")
    )

    result = ChaseWorker(
        config,
        repository=repository,
        transport=transport,
        roster=DirectContractList(refs=[]),
        clock=lambda: NOW,
        logger=CapturingLogger(),
    ).run_once()

    assert result.status is WorkerStatus.COMPLETED
    assert result.reserved == 2
    assert not transport.calls
    assert "claim" not in [name for name, _ in repository.calls]
    reservation = [value for name, value in repository.calls if name == "reserve"][0]
    policy, delivery, kwargs = reservation
    assert any(step.channel is Channel.PHONE for step in policy.steps)
    assert delivery.provider == "record_only"
    assert delivery.operational_enabled is False
    assert kwargs["limit"] == config.chase_worker_batch_size


def test_live_gate_fingerprint_change_is_permanent_failure_before_io():
    config = _email_config()
    old_roster = DirectContractList(
        refs=[], version="roster-v1", fingerprint_key="g" * 32,
    )
    live_roster = DirectContractList(
        refs=[], version="roster-v2", fingerprint_key="g" * 32,
    )
    context = _dispatch(config, old_roster)
    repository = FakeRepository(claims=[context])
    transport = FakeTransport()

    result = ChaseWorker(
        config,
        repository=repository,
        transport=transport,
        roster=live_roster,
        clock=lambda: NOW,
        logger=CapturingLogger(),
    ).run_once()

    assert result.status is WorkerStatus.COMPLETED
    assert result.permanent_failures == 1
    assert result.accepted == 0
    assert not transport.calls
    assert repository.finalizations[0]["outcome"] == "permanent_failure"
    assert repository.finalizations[0]["error_code"] == (
        "direct_contract_gate_changed"
    )


@pytest.mark.parametrize("available", [True, False])
def test_exact_human_release_allows_live_blocked_gate_after_recheck(available):
    config = _email_config()
    if available:
        roster = DirectContractList(
            refs=[PATIENT_REF], version="roster-v1", fingerprint_key="g" * 32,
        )
    else:
        roster = DirectContractList(
            path="/synthetic/unavailable-direct-contract-roster.csv",
            fingerprint_key="g" * 32,
        )
    release_id = "60000000-0000-4000-8000-000000000001"
    context = _dispatch(
        config,
        roster,
        gate_disposition="released",
        review_release_id=release_id,
    )
    repository = FakeRepository(claims=[context])
    transport = FakeTransport()

    result = ChaseWorker(
        config,
        repository=repository,
        transport=transport,
        roster=roster,
        clock=lambda: NOW,
        logger=CapturingLogger(),
    ).run_once()

    assert result.status is WorkerStatus.COMPLETED
    assert len(transport.calls) == 1
    assert repository.finalizations[0]["outcome"] == "accepted"


@pytest.mark.parametrize(
    "gate_disposition,review_release_id,error_code",
    [
        ("clear", "", "direct_contract_gate_release_required"),
        ("released", "", "direct_contract_gate_release_invalid"),
        ("released", "not-a-uuid", "direct_contract_gate_release_invalid"),
    ],
)
def test_blocked_gate_without_exact_release_is_rejected_before_io(
    gate_disposition, review_release_id, error_code,
):
    config = _email_config()
    roster = DirectContractList(
        refs=[PATIENT_REF], version="roster-v1", fingerprint_key="g" * 32,
    )
    context = _dispatch(
        config,
        roster,
        gate_disposition=gate_disposition,
        review_release_id=review_release_id,
    )
    repository = FakeRepository(claims=[context])
    transport = FakeTransport()

    result = ChaseWorker(
        config,
        repository=repository,
        transport=transport,
        roster=roster,
        clock=lambda: NOW,
        logger=CapturingLogger(),
    ).run_once()

    assert result.status is WorkerStatus.COMPLETED
    assert not transport.calls
    assert repository.finalizations[0]["error_code"] == error_code


def test_clear_gate_rejects_mismatched_release_evidence_before_io():
    config = _email_config()
    roster = DirectContractList(
        refs=[], version="roster-v1", fingerprint_key="g" * 32,
    )
    context = _dispatch(
        config,
        roster,
        gate_disposition="released",
        review_release_id="60000000-0000-4000-8000-000000000001",
    )
    repository = FakeRepository(claims=[context])
    transport = FakeTransport()

    result = ChaseWorker(
        config,
        repository=repository,
        transport=transport,
        roster=roster,
        clock=lambda: NOW,
        logger=CapturingLogger(),
    ).run_once()

    assert result.status is WorkerStatus.COMPLETED
    assert not transport.calls
    assert repository.finalizations[0]["error_code"] == (
        "direct_contract_gate_authorization_invalid"
    )


def test_invalid_exact_template_is_known_failure_and_never_reaches_transport():
    config = _email_config()
    roster = DirectContractList(
        refs=[], version="roster-v1", fingerprint_key="g" * 32,
    )
    context = _dispatch(config, roster, template_id="email.not-approved")
    repository = FakeRepository(claims=[context])
    transport = FakeTransport()

    result = ChaseWorker(
        config,
        repository=repository,
        transport=transport,
        roster=roster,
        clock=lambda: NOW,
        logger=CapturingLogger(),
    ).run_once()

    assert result.status is WorkerStatus.COMPLETED
    assert not transport.calls
    assert repository.finalizations[0]["outcome"] == "permanent_failure"
    assert repository.finalizations[0]["error_code"] == (
        "approved_message_validation_failed"
    )


def test_known_transport_validation_failure_is_finalized_without_touch():
    config = _email_config()
    roster = DirectContractList(
        refs=[], version="roster-v1", fingerprint_key="g" * 32,
    )
    context = _dispatch(config, roster)
    repository = FakeRepository(claims=[context])
    transport = FakeTransport(ValueError("synthetic recipient rejected"))

    result = ChaseWorker(
        config,
        repository=repository,
        transport=transport,
        roster=roster,
        clock=lambda: NOW,
        logger=CapturingLogger(),
    ).run_once()

    assert result.status is WorkerStatus.COMPLETED
    assert result.permanent_failures == 1
    assert repository.finalizations[0]["outcome"] == "permanent_failure"
    assert repository.finalizations[0]["error_code"] == (
        "email_message_validation_failed"
    )


@pytest.mark.parametrize(
    "transport_error",
    [
        EmailSubmissionError("synthetic uncertain ACS result"),
        RuntimeError("synthetic unknown SDK result"),
    ],
)
def test_uncertain_provider_result_stays_submitting_and_stops_run(transport_error):
    config = _email_config()
    roster = DirectContractList(
        refs=[], version="roster-v1", fingerprint_key="g" * 32,
    )
    first = _dispatch(config, roster)
    second = _dispatch(
        config,
        roster,
        delivery_id="10000000-0000-4000-8000-000000000002",
        operation_id="20000000-0000-4000-8000-000000000002",
    )
    repository = FakeRepository(claims=[first, second])
    logger = CapturingLogger()

    result = ChaseWorker(
        config,
        repository=repository,
        transport=FakeTransport(transport_error),
        roster=roster,
        clock=lambda: NOW,
        logger=logger,
    ).run_once()

    assert result.status is WorkerStatus.TRANSPORT_UNCERTAIN
    assert result.exit_code != 0
    assert result.claimed == 1
    assert not repository.finalizations
    assert len(repository.claims) == 1
    log_text = "\n".join(logger.messages)
    assert str(transport_error) not in log_text
    assert PATIENT_REF not in log_text
    assert RECIPIENT not in log_text


@pytest.mark.parametrize(
    "fail_at",
    [
        "quarantine", "ambiguous", "schedule", "reserve", "claim", "guard",
        "finalize",
    ],
)
def test_database_uncertainty_is_nonzero_and_raw_error_is_never_logged(fail_at):
    config = _email_config()
    roster = DirectContractList(
        refs=[], version="roster-v1", fingerprint_key="g" * 32,
    )
    context = _dispatch(config, roster)
    repository = FakeRepository(claims=[context], fail_at=fail_at)
    logger = CapturingLogger()

    result = ChaseWorker(
        config,
        repository=repository,
        transport=FakeTransport(),
        roster=roster,
        clock=lambda: NOW,
        logger=logger,
    ).run_once()

    assert result.status is WorkerStatus.DATABASE_UNCERTAIN
    assert result.exit_code != 0
    log_text = "\n".join(logger.messages)
    assert "synthetic raw database detail" not in log_text
    assert PATIENT_REF not in log_text


def test_changed_authorization_guard_finalizes_without_transport_io():
    config = _email_config()
    roster = DirectContractList(
        refs=[], version="roster-v1", fingerprint_key="g" * 32,
    )
    context = _dispatch(config, roster)
    repository = FakeRepository(claims=[context], fail_at="guard_conflict")
    transport = FakeTransport()

    result = ChaseWorker(
        config,
        repository=repository,
        transport=transport,
        roster=roster,
        clock=lambda: NOW,
        logger=CapturingLogger(),
    ).run_once()

    assert result.status is WorkerStatus.COMPLETED
    assert result.permanent_failures == 1
    assert not transport.calls
    assert repository.finalizations[0]["outcome"] == "permanent_failure"
    assert repository.finalizations[0]["error_code"] == (
        "submission_authorization_changed"
    )


def test_expired_submission_guard_stays_submitting_for_ambiguity():
    config = _email_config()
    roster = DirectContractList(
        refs=[], version="roster-v1", fingerprint_key="g" * 32,
    )
    context = _dispatch(config, roster)
    repository = FakeRepository(claims=[context], fail_at="lease_expired")
    transport = FakeTransport()

    result = ChaseWorker(
        config,
        repository=repository,
        transport=transport,
        roster=roster,
        clock=lambda: NOW,
        logger=CapturingLogger(),
    ).run_once()

    assert result.status is WorkerStatus.TRANSPORT_UNCERTAIN
    assert result.exit_code != 0
    assert not transport.calls
    assert not repository.finalizations


def test_existing_or_newly_quarantined_ambiguity_stops_before_new_work():
    config = _email_config()
    roster = DirectContractList(
        refs=[], version="roster-v1", fingerprint_key="g" * 32,
    )
    context = _dispatch(config, roster)
    ambiguous = AmbiguousDelivery(
        delivery_id="50000000-0000-4000-8000-000000000001",
        provider_operation_id="70000000-0000-4000-8000-000000000001",
        policy_fingerprint=context.policy_fingerprint,
        ambiguous_at=NOW,
        error_code="lease_expired_unreconciled",
    )
    repository = FakeRepository(
        claims=[context],
        quarantined=[ambiguous.delivery_id],
        ambiguous=[ambiguous],
    )
    transport = FakeTransport()

    result = ChaseWorker(
        config,
        repository=repository,
        transport=transport,
        roster=roster,
        clock=lambda: NOW,
        logger=CapturingLogger(),
    ).run_once()

    assert result.status is WorkerStatus.TRANSPORT_UNCERTAIN
    assert result.exit_code != 0
    assert result.quarantined == 1
    assert result.ambiguous == 1
    assert not transport.calls
    assert [name for name, _ in repository.calls] == [
        "quarantine", "ambiguous",
    ]


def test_batch_bound_limits_claims_and_provider_calls():
    config = _email_config(chase_worker_batch_size=1)
    roster = DirectContractList(
        refs=[], version="roster-v1", fingerprint_key="g" * 32,
    )
    claims = [
        _dispatch(config, roster),
        _dispatch(
            config,
            roster,
            delivery_id="10000000-0000-4000-8000-000000000002",
            operation_id="20000000-0000-4000-8000-000000000002",
        ),
    ]
    repository = FakeRepository(claims=claims)
    transport = FakeTransport()

    result = ChaseWorker(
        config,
        repository=repository,
        transport=transport,
        roster=roster,
        clock=lambda: NOW,
        logger=CapturingLogger(),
    ).run_once()

    assert result.status is WorkerStatus.COMPLETED
    assert result.claimed == 1
    assert len(transport.calls) == 1
    assert len(repository.claims) == 1


def test_disabled_scheduler_and_non_once_cli_invocation_fail_closed():
    with pytest.raises(WorkerConfigurationError, match="disabled"):
        ChaseWorker(
            ChaseWorkerConfig(),
            repository=FakeRepository(),
            roster=DirectContractList(refs=[]),
            clock=lambda: NOW,
            logger=CapturingLogger(),
        )

    assert main([]) != 0
    assert main(["--daemon"]) != 0


def test_operational_worker_rejects_a_non_callable_transport():
    with pytest.raises(WorkerConfigurationError, match="must be callable"):
        ChaseWorker(
            _email_config(),
            repository=FakeRepository(),
            transport=object(),
            roster=DirectContractList(refs=[]),
            clock=lambda: NOW,
            logger=CapturingLogger(),
        )


def test_worker_repr_and_result_never_expose_injected_objects_or_secrets():
    config = _record_only_config()
    worker = ChaseWorker(
        config,
        repository=FakeRepository(),
        transport=FakeTransport(),
        roster=DirectContractList(
            refs=[PATIENT_REF], version="synthetic-secret-version",
        ),
        clock=lambda: NOW,
        logger=CapturingLogger(),
    )

    rendered = repr(worker)
    assert WORKER_URL not in rendered
    assert PATIENT_REF not in rendered
    assert "synthetic-secret-version" not in rendered
    assert rendered.count("<redacted>") == 4
    result = worker.run_once()
    assert PATIENT_REF not in repr(result)
    assert RECIPIENT not in repr(result)
