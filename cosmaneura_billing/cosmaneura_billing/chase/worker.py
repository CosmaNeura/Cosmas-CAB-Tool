"""One-shot, fail-closed worker for durable chase scheduling and ACS email.

The API process never imports or constructs this service.  A scheduled job runs
``python -m cosmaneura_billing.chase.worker --once`` with the dedicated worker
database login.  Every invocation is bounded; there is deliberately no daemon
or retry loop here.

Provider submission is the only external side effect.  A delivery is claimed
before that call, and any uncertain provider result is left in ``submitting`` so
the repository can quarantine it as ambiguous after its lease expires.  It is
never returned to the send lane again.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import logging
import sys
from typing import Any, Callable, Optional, Sequence
from uuid import UUID

from ..direct_contract import DirectContractList, check_direct_contract
from ..settings import SettingsError
from .acs_email import ACSEmailTransport, EmailSubmissionError
from .models import Channel
from .policy import ChasePolicy
from .policy_fingerprint import DeliveryConfiguration, policy_fingerprint
from .scheduler_store import (
    DispatchContext,
    PostgresChaseSchedulerRepository,
    SchedulerConflict,
    SchedulerLeaseExpired,
)
from .worker_config import ChaseWorkerConfig


Clock = Callable[[], datetime]


class WorkerStatus(str, Enum):
    """PHI-free terminal status for one bounded invocation."""

    COMPLETED = "completed"
    CONFIGURATION_ERROR = "configuration_error"
    DATABASE_UNCERTAIN = "database_uncertain"
    TRANSPORT_UNCERTAIN = "transport_uncertain"


@dataclass(frozen=True)
class WorkerRunResult:
    """Counts and status only; no patient or message data may escape a run."""

    status: WorkerStatus
    quarantined: int = 0
    ambiguous: int = 0
    scheduled: int = 0
    reserved: int = 0
    claimed: int = 0
    accepted: int = 0
    permanent_failures: int = 0

    @property
    def exit_code(self) -> int:
        if self.status is WorkerStatus.COMPLETED:
            return 0
        if self.status is WorkerStatus.CONFIGURATION_ERROR:
            return 2
        return 1


class WorkerConfigurationError(ValueError):
    """The scheduled job cannot safely start with the supplied configuration."""


@dataclass
class _RunCounts:
    quarantined: int = 0
    ambiguous: int = 0
    scheduled: int = 0
    reserved: int = 0
    claimed: int = 0
    accepted: int = 0
    permanent_failures: int = 0

    def result(self, status: WorkerStatus) -> WorkerRunResult:
        return WorkerRunResult(
            status=status,
            quarantined=self.quarantined,
            ambiguous=self.ambiguous,
            scheduled=self.scheduled,
            reserved=self.reserved,
            claimed=self.claimed,
            accepted=self.accepted,
            permanent_failures=self.permanent_failures,
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WorkerConfigurationError("worker clock must return an aware datetime")
    try:
        offset = value.utcoffset()
    except (ValueError, OverflowError):
        offset = None
    if offset is None:
        raise WorkerConfigurationError("worker clock must return an aware datetime")
    return value.astimezone(timezone.utc)


def _safe_delivery_id(value: str) -> str:
    """Return only a canonical random identifier suitable for a log field."""
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError):
        return ""
    return str(parsed) if parsed.version == 4 else ""


def _valid_uuid(value: str) -> bool:
    try:
        UUID(value)
    except (TypeError, ValueError, AttributeError):
        return False
    return True


class ChaseWorker:
    """Coordinate one bounded scheduling and delivery pass.

    Repository, transport, roster, clock, and logger are injectable so the
    safety boundary can be tested without a database, network, or patient data.
    """

    def __init__(
        self,
        config: ChaseWorkerConfig,
        *,
        repository: Any | None = None,
        transport: Any | None = None,
        roster: DirectContractList | None = None,
        clock: Clock | None = None,
        logger: logging.Logger | Any | None = None,
    ) -> None:
        if not isinstance(config, ChaseWorkerConfig):
            raise WorkerConfigurationError("worker configuration is required")
        if config.chase_scheduler_enabled is not True:
            raise WorkerConfigurationError("durable chase scheduler is disabled")
        if not config.worker_database_url or not config.practice_id:
            raise WorkerConfigurationError("durable chase worker configuration is incomplete")

        self._config = config
        self._policy: ChasePolicy = config.build_chase_policy()
        self._delivery: DeliveryConfiguration = (
            config.build_delivery_configuration()
        )
        try:
            self._policy_fingerprint = policy_fingerprint(
                self._policy,
                practice_timezone=config.practice_timezone,
                delivery=self._delivery,
            )
        except Exception:
            invalid_policy = True
        else:
            invalid_policy = False
        if invalid_policy:
            raise WorkerConfigurationError(
                "executable chase policy configuration is invalid"
            ) from None

        if repository is None:
            try:
                repository = PostgresChaseSchedulerRepository(
                    config.worker_database_url,
                    practice_id=config.practice_id,
                )
            except Exception:
                repository_failed = True
            else:
                repository_failed = False
            if repository_failed:
                raise WorkerConfigurationError(
                    "chase worker repository could not be initialized"
                ) from None
        self._repository = repository

        if roster is None:
            roster = DirectContractList(
                path=config.direct_contract_list,
                fingerprint_key=config.chase_gate_hmac_key,
            )
        self._roster = roster
        self._clock = clock or _utc_now
        self._logger = logger or logging.getLogger(
            "cosmaneura_billing.chase.worker"
        )

        if self._delivery.operational_enabled:
            if self._delivery.provider != "azure_communication_services_email":
                raise WorkerConfigurationError(
                    "operational chase delivery provider is unsupported"
                )
            if transport is None:
                try:
                    transport = ACSEmailTransport.from_connection_string(
                        connection_string=config.acs_email_connection_string,
                        sender_address=config.acs_email_sender_address,
                        reply_to_address=config.acs_email_reply_to_address,
                    )
                except Exception:
                    transport_failed = True
                else:
                    transport_failed = False
                if transport_failed:
                    raise WorkerConfigurationError(
                        "ACS email transport could not be initialized"
                    ) from None
            if not callable(transport):
                raise WorkerConfigurationError(
                    "operational chase transport must be callable"
                )
        self._transport = transport

    def __repr__(self) -> str:
        return (
            "ChaseWorker(config='<redacted>', repository='<redacted>', "
            "transport='<redacted>', roster='<redacted>')"
        )

    def _now(self) -> datetime:
        return _aware_utc(self._clock())

    def _log(
        self,
        level: str,
        event: str,
        status: str,
        *,
        delivery_id: str = "",
        count: Optional[int] = None,
    ) -> None:
        """Log only fixed codes, counts, and a validated random delivery UUID."""
        method = getattr(self._logger, level, None)
        if not callable(method):
            return
        safe_id = _safe_delivery_id(delivery_id)
        fields = [f"event={event}", f"status={status}"]
        if safe_id:
            fields.append(f"delivery_id={safe_id}")
        if count is not None:
            fields.append(f"count={count}")
        try:
            method("chase_worker " + " ".join(fields))
        except Exception:
            # Observability cannot be allowed to change delivery state.  A
            # deployment health check detects a broken logging sink separately.
            return

    def _database_uncertain(
        self, counts: _RunCounts, *, event: str,
    ) -> WorkerRunResult:
        self._log("error", event, WorkerStatus.DATABASE_UNCERTAIN.value)
        return counts.result(WorkerStatus.DATABASE_UNCERTAIN)

    def _finalize_permanent_failure(
        self,
        context: DispatchContext,
        *,
        error_code: str,
        counts: _RunCounts,
    ) -> bool:
        """Record a known pre-submit failure.  ``False`` means DB uncertainty."""
        try:
            self._repository.finalize_delivery(
                delivery_id=context.delivery_id,
                lease_token=context.lease_token,
                outcome="permanent_failure",
                as_of=self._now(),
                error_code=error_code,
            )
        except Exception:
            finalized = False
        else:
            finalized = True
        if not finalized:
            self._log(
                "error",
                "delivery_finalization",
                WorkerStatus.DATABASE_UNCERTAIN.value,
                delivery_id=context.delivery_id,
            )
            return False
        counts.permanent_failures += 1
        self._log(
            "warning", "delivery_rejected", error_code,
            delivery_id=context.delivery_id,
        )
        return True

    def _gate_error(self, context: DispatchContext, *, checked_at: datetime) -> str:
        """Re-read the live roster and return a fixed rejection code, if any."""
        try:
            check = check_direct_contract(
                context.patient_ref,
                roster=self._roster,
                now=lambda: checked_at.isoformat(timespec="seconds"),
            )
        except Exception:
            return "direct_contract_gate_unavailable"
        if check.gate_fingerprint != context.gate_fingerprint:
            return "direct_contract_gate_changed"
        if check.blocked:
            # F9 routes a direct-contract match or unavailable roster to named
            # human review.  Only the exact persisted release may authorize the
            # worker after the live fingerprint has been re-confirmed.
            if context.gate_disposition != "released":
                return "direct_contract_gate_release_required"
            if not _valid_uuid(context.review_release_id):
                return "direct_contract_gate_release_invalid"
            return ""
        if (
            context.gate_disposition != "clear"
            or context.review_release_id
        ):
            return "direct_contract_gate_authorization_invalid"
        return ""

    def _render(self, context: DispatchContext) -> tuple[str, str]:
        """Render only the exact locally approved email template, without a name."""
        if context.policy_fingerprint != self._policy_fingerprint:
            raise ValueError("policy identity mismatch")
        if (
            type(context.policy_step_ordinal) is not int
            or not 0 <= context.policy_step_ordinal < len(self._policy.steps)
        ):
            raise ValueError("policy step identity mismatch")
        step = self._policy.steps[context.policy_step_ordinal]
        if (
            step.channel is not Channel.EMAIL
            or context.channel is not Channel.EMAIL
            or step.channel is not context.channel
            or step.template_id != context.template_id
        ):
            raise ValueError("template identity mismatch")
        template = self._policy.template(context.template_id)
        if template.channel is not context.channel:
            raise ValueError("template identity mismatch")
        rendered = template.render(
            practice=self._policy.practice_name,
            upload_url=self._policy.upload_url,
            patient_first_name="",
        )
        return rendered["subject"], rendered["body"]

    def _dispatch_one(
        self, context: DispatchContext, counts: _RunCounts,
    ) -> Optional[WorkerStatus]:
        """Dispatch one claim; return a terminal uncertain status, if any."""
        permanent_error = ""
        uncertain_transport = False
        transport_started = False
        subject = ""
        body = ""
        try:
            with self._repository.guard_submission(context) as guarded:
                if guarded is not context:
                    permanent_error = "submission_authorization_changed"
                else:
                    permanent_error = self._gate_error(
                        context, checked_at=self._now(),
                    )
                if not permanent_error:
                    try:
                        subject, body = self._render(context)
                    except Exception:
                        permanent_error = "approved_message_validation_failed"
                if not permanent_error and self._transport is None:
                    permanent_error = "email_transport_unavailable"
                if not permanent_error:
                    transport_started = True
                    try:
                        result = self._transport(
                            channel=Channel.EMAIL,
                            recipient=context.recipient,
                            subject=subject,
                            body=body,
                            operation_id=context.provider_operation_id,
                        )
                    except ValueError:
                        permanent_error = "email_message_validation_failed"
                    except EmailSubmissionError:
                        uncertain_transport = True
                    except Exception:
                        uncertain_transport = True
                    else:
                        uncertain_transport = not (
                            type(result) is str and result == "accepted"
                        )
        except SchedulerLeaseExpired:
            guard_lease_expired = True
            guard_conflict = False
            guard_database_uncertain = False
        except SchedulerConflict:
            guard_lease_expired = False
            guard_conflict = True
            guard_database_uncertain = False
        except Exception:
            guard_lease_expired = False
            guard_conflict = False
            guard_database_uncertain = True
        else:
            guard_lease_expired = False
            guard_conflict = False
            guard_database_uncertain = False
        finally:
            # Do not retain rendered content longer than the provider call.
            subject = ""
            body = ""

        if guard_lease_expired:
            # The row may represent a request made by an earlier process.  It
            # must reach the ambiguous lane; converting it to a known failure
            # would erase exactly the uncertainty the lease protects.
            self._log(
                "error",
                "submission_lease_expired",
                WorkerStatus.TRANSPORT_UNCERTAIN.value,
                delivery_id=context.delivery_id,
            )
            return WorkerStatus.TRANSPORT_UNCERTAIN
        if guard_database_uncertain:
            # Do not try to prove a new state through a second connection.  The
            # claimed row remains submitting and expires into reconciliation.
            self._log(
                "error",
                "submission_guard",
                WorkerStatus.DATABASE_UNCERTAIN.value,
                delivery_id=context.delivery_id,
            )
            return WorkerStatus.DATABASE_UNCERTAIN
        if guard_conflict:
            if transport_started:
                self._log(
                    "error",
                    "submission_guard",
                    WorkerStatus.TRANSPORT_UNCERTAIN.value,
                    delivery_id=context.delivery_id,
                )
                return WorkerStatus.TRANSPORT_UNCERTAIN
            permanent_error = "submission_authorization_changed"

        if permanent_error:
            if not self._finalize_permanent_failure(
                context,
                error_code=permanent_error,
                counts=counts,
            ):
                return WorkerStatus.DATABASE_UNCERTAIN
            return None
        if uncertain_transport:
            # Do not finalize or retry.  Lease expiry moves this exact operation
            # to the ambiguous reconciliation lane.
            self._log(
                "error",
                "delivery_submission",
                WorkerStatus.TRANSPORT_UNCERTAIN.value,
                delivery_id=context.delivery_id,
            )
            return WorkerStatus.TRANSPORT_UNCERTAIN

        try:
            self._repository.finalize_delivery(
                delivery_id=context.delivery_id,
                lease_token=context.lease_token,
                outcome="accepted",
                as_of=self._now(),
                provider_message_id="",
                error_code="",
            )
        except Exception:
            accepted_finalized = False
        else:
            accepted_finalized = True
        if not accepted_finalized:
            self._log(
                "error",
                "delivery_finalization",
                WorkerStatus.DATABASE_UNCERTAIN.value,
                delivery_id=context.delivery_id,
            )
            return WorkerStatus.DATABASE_UNCERTAIN
        counts.accepted += 1
        self._log(
            "info", "delivery_finalized", "accepted",
            delivery_id=context.delivery_id,
        )
        return None

    def run_once(self) -> WorkerRunResult:
        """Run one bounded pass; operational uncertainty is returned, not retried."""
        counts = _RunCounts()
        self._log("info", "run_started", "running")
        try:
            as_of = self._now()
        except Exception:
            self._log(
                "error", "clock_preflight", WorkerStatus.CONFIGURATION_ERROR.value,
            )
            return counts.result(WorkerStatus.CONFIGURATION_ERROR)

        try:
            quarantined = self._repository.quarantine_expired_submissions(
                as_of=as_of,
                limit=self._config.chase_worker_batch_size,
            )
            counts.quarantined = len(quarantined)
        except Exception:
            return self._database_uncertain(counts, event="lease_quarantine")

        try:
            ambiguous = self._repository.list_ambiguous(limit=1)
            counts.ambiguous = len(ambiguous)
        except Exception:
            return self._database_uncertain(
                counts, event="ambiguous_delivery_check",
            )
        has_ambiguity = bool(counts.quarantined or counts.ambiguous)
        if has_ambiguity:
            delivery_id = ""
            if ambiguous:
                delivery_id = getattr(ambiguous[0], "delivery_id", "")
            self._log(
                "error",
                "ambiguous_delivery_pending",
                WorkerStatus.TRANSPORT_UNCERTAIN.value,
                delivery_id=delivery_id,
                count=max(counts.quarantined, counts.ambiguous),
            )
            return counts.result(WorkerStatus.TRANSPORT_UNCERTAIN)
        try:
            counts.scheduled = self._repository.schedule_unscheduled(
                self._policy,
                self._delivery,
                practice_timezone=self._config.practice_timezone,
                as_of=as_of,
                limit=self._config.chase_worker_batch_size,
            )
        except Exception:
            return self._database_uncertain(
                counts, event="cadence_initialization",
            )

        try:
            reservations = self._repository.reserve_due(
                self._policy,
                self._delivery,
                practice_timezone=self._config.practice_timezone,
                as_of=as_of,
                limit=self._config.chase_worker_batch_size,
            )
            counts.reserved = len(reservations)
        except Exception:
            return self._database_uncertain(counts, event="due_reservation")

        # Record-only and latch-off invocations stop here.  The reservation
        # transaction may still have created staff phone tasks, but no transport
        # claim is possible or attempted.
        if not self._delivery.operational_enabled:
            self._log("info", "run_finished", WorkerStatus.COMPLETED.value)
            return counts.result(WorkerStatus.COMPLETED)

        for _ in range(self._config.chase_worker_batch_size):
            try:
                context = self._repository.claim_delivery(
                    expected_policy_fingerprint=self._policy_fingerprint,
                    contact_max_age_hours=(
                        self._delivery.contact_snapshot_max_age_hours
                    ),
                    as_of=self._now(),
                    lease_seconds=self._config.chase_worker_lease_seconds,
                )
            except Exception:
                return self._database_uncertain(counts, event="delivery_claim")
            if context is None:
                break
            if not isinstance(context, DispatchContext):
                return self._database_uncertain(
                    counts, event="delivery_claim_contract",
                )
            counts.claimed += 1
            try:
                status = self._dispatch_one(context, counts)
            except WorkerConfigurationError:
                self._log(
                    "error",
                    "clock_preflight",
                    WorkerStatus.CONFIGURATION_ERROR.value,
                )
                return counts.result(WorkerStatus.CONFIGURATION_ERROR)
            except Exception:
                # The claim has committed but its side-effect state is no longer
                # provable.  Leave it leased for ambiguous quarantine; never
                # retry it from this invocation.
                self._log(
                    "error",
                    "delivery_processing",
                    WorkerStatus.TRANSPORT_UNCERTAIN.value,
                    delivery_id=context.delivery_id,
                )
                return counts.result(WorkerStatus.TRANSPORT_UNCERTAIN)
            if status is not None:
                return counts.result(status)

        self._log("info", "run_finished", WorkerStatus.COMPLETED.value)
        return counts.result(WorkerStatus.COMPLETED)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one bounded worker pass.  No continuous/daemon mode exists."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    logger = logging.getLogger("cosmaneura_billing.chase.worker")
    if arguments != ["--once"]:
        logger.error(
            "chase_worker event=invocation status=configuration_error"
        )
        return WorkerRunResult(WorkerStatus.CONFIGURATION_ERROR).exit_code

    try:
        config = ChaseWorkerConfig.from_env()
        logging.basicConfig(
            level=getattr(logging, config.log_level),
            format="%(levelname)s %(name)s %(message)s",
        )
        worker = ChaseWorker(config, logger=logger)
    except (SettingsError, WorkerConfigurationError):
        configuration_failed = True
    except Exception:
        configuration_failed = True
    else:
        configuration_failed = False
    if configuration_failed:
        logger.error(
            "chase_worker event=initialization status=configuration_error"
        )
        return WorkerRunResult(WorkerStatus.CONFIGURATION_ERROR).exit_code
    return worker.run_once().exit_code


if __name__ == "__main__":  # pragma: no cover - exercised through ``main``
    raise SystemExit(main())


__all__ = [
    "ChaseWorker",
    "WorkerConfigurationError",
    "WorkerRunResult",
    "WorkerStatus",
    "main",
]
