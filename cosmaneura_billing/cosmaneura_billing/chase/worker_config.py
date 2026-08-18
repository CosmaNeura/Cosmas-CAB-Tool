"""Fail-closed configuration boundary for the dedicated chase worker.

The scheduled job intentionally does not consume the API process's
``DATABASE_URL``.  It receives a separate, least-privilege worker URL and only
the policy/delivery inputs needed to reserve staff tasks or submit an approved
email.  This keeps an API secret from becoming an accidental fallback when the
worker secret is missing.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..settings import (
    SettingsError,
    _canonicalize_acs_email_endpoint,
    _database_url_uses_role,
    _parse_bool,
    _parse_bounded_int,
    _validate_acs_connection_string,
    _validate_email_address,
    _validate_patient_upload_url,
)
from .policy import ApprovalMode, ChasePolicy
from .policy_fingerprint import DeliveryConfiguration


_ENVIRONMENTS = frozenset({"development", "test", "staging", "production"})
_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_APPROVAL_MODES = frozenset(mode.value for mode in ApprovalMode)
_TRANSPORTS = frozenset({"record_only", "acs_email"})
_WORKER_ROLE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,62}\Z")
_RESERVED_WORKER_ROLES = frozenset({"postgres", "azure_pg_admin"})


def _worker_bool(
    env: Mapping[str, str], name: str, default: bool,
) -> bool:
    """Reuse the shared grammar without retaining a rejected raw value."""
    message = ""
    try:
        return _parse_bool(env, name, default)
    except SettingsError as exc:
        message = str(exc)
    raise SettingsError(message) from None


def _worker_bounded_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Reuse shared bounds while dropping ``int``'s raw-value context."""
    message = ""
    try:
        return _parse_bounded_int(env, name, default, minimum, maximum)
    except SettingsError as exc:
        message = str(exc)
    raise SettingsError(message) from None


@dataclass(frozen=True, repr=False)
class ChaseWorkerConfig:
    """Validated configuration owned by one scheduled chase-worker process."""

    environment: str = "development"
    practice_id: str = ""
    practice_name: str = "your care team"
    chase_worker_database_role: str = "cosmaneura_chase_worker"
    chase_worker_database_url: str = field(
        default="", repr=False, metadata={"secret": True},
    )

    chase_scheduler_enabled: bool = False
    patient_messaging_enabled: bool = False
    patient_messaging_operationally_activated: bool = False
    chase_transport: str = "record_only"
    acs_email_connection_string: str = field(
        default="", repr=False, metadata={"secret": True},
    )
    acs_email_endpoint: str = ""
    acs_email_sender_address: str = field(
        default="", repr=False, metadata={"secret": True},
    )
    acs_email_reply_to_address: str = field(
        default="", repr=False, metadata={"secret": True},
    )

    upload_url: str = field(
        default="https://example.invalid/insurance",
        repr=False,
        metadata={"secret": True},
    )
    chase_approval: str = "per_policy"
    direct_contract_list: str = field(
        default="direct_contract_patients.csv",
        repr=False,
        metadata={"secret": True},
    )
    chase_gate_hmac_key: str = field(
        default="", repr=False, metadata={"secret": True},
    )
    practice_timezone: str = "UTC"
    contact_snapshot_max_age_hours: int = 24
    chase_worker_batch_size: int = 25
    chase_worker_lease_seconds: int = 300
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if self.environment not in _ENVIRONMENTS:
            raise SettingsError(
                "COSMANEURA_ENV must be development, test, staging, or production"
            )
        if self.log_level not in _LOG_LEVELS:
            raise SettingsError(
                "COSMANEURA_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL"
            )
        if self.chase_approval not in _APPROVAL_MODES:
            raise SettingsError(
                "COSMANEURA_CHASE_APPROVAL must be per_policy or per_message"
            )
        if self.chase_transport not in _TRANSPORTS:
            raise SettingsError(
                "COSMANEURA_CHASE_TRANSPORT must be record_only or acs_email"
            )
        if not self.practice_name.strip():
            raise SettingsError("COSMANEURA_PRACTICE_NAME must not be empty")
        if not self.direct_contract_list.strip():
            raise SettingsError(
                "COSMANEURA_DIRECT_CONTRACT_LIST must not be empty"
            )
        if not _WORKER_ROLE.fullmatch(self.chase_worker_database_role):
            raise SettingsError(
                "COSMANEURA_CHASE_WORKER_DB_ROLE must be a safe PostgreSQL role name"
            )
        folded_role = self.chase_worker_database_role.casefold()
        if folded_role.startswith("pg_") or folded_role in _RESERVED_WORKER_ROLES:
            raise SettingsError(
                "COSMANEURA_CHASE_WORKER_DB_ROLE must be a safe PostgreSQL role name"
            )
        if self.chase_worker_database_url and not _database_url_uses_role(
            self.chase_worker_database_url,
            role=self.chase_worker_database_role,
        ):
            raise SettingsError(
                "COSMANEURA_CHASE_WORKER_DATABASE_URL must use the configured "
                "least-privilege chase worker role"
            ) from None
        if self.chase_scheduler_enabled:
            if not self.chase_worker_database_url:
                raise SettingsError(
                    "COSMANEURA_CHASE_WORKER_DATABASE_URL is required when the "
                    "durable chase scheduler is enabled"
                )
            if not self.practice_id.strip():
                raise SettingsError(
                    "COSMANEURA_PRACTICE_ID is required when the durable chase "
                    "scheduler is enabled"
                )

        timezone_valid = True
        try:
            ZoneInfo(self.practice_timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            timezone_valid = False
        if not timezone_valid:
            raise SettingsError(
                "COSMANEURA_PRACTICE_TIMEZONE must be a valid IANA timezone"
            ) from None

        for name, value, minimum, maximum in (
            (
                "COSMANEURA_CONTACT_SNAPSHOT_MAX_AGE_HOURS",
                self.contact_snapshot_max_age_hours,
                1,
                168,
            ),
            (
                "COSMANEURA_CHASE_WORKER_BATCH_SIZE",
                self.chase_worker_batch_size,
                1,
                500,
            ),
            (
                "COSMANEURA_CHASE_WORKER_LEASE_SECONDS",
                self.chase_worker_lease_seconds,
                30,
                3_600,
            ),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise SettingsError(
                    f"{name} must be an integer from {minimum} through {maximum}"
                )

        acs_fields = {
            "COSMANEURA_ACS_EMAIL_CONNECTION_STRING": bool(
                self.acs_email_connection_string
            ),
            "COSMANEURA_ACS_EMAIL_ENDPOINT": bool(self.acs_email_endpoint),
            "COSMANEURA_ACS_EMAIL_SENDER_ADDRESS": bool(
                self.acs_email_sender_address
            ),
            "COSMANEURA_ACS_EMAIL_REPLY_TO_ADDRESS": bool(
                self.acs_email_reply_to_address
            ),
        }
        if any(acs_fields.values()) and not all(acs_fields.values()):
            missing = ", ".join(
                name for name, configured in acs_fields.items() if not configured
            )
            raise SettingsError(
                "ACS email settings must be configured together; "
                f"missing: {missing}"
            )
        if self.chase_transport == "acs_email" and not all(acs_fields.values()):
            raise SettingsError(
                "COSMANEURA_CHASE_TRANSPORT=acs_email requires complete ACS "
                "email settings"
            )
        if all(acs_fields.values()):
            connection_endpoint = _validate_acs_connection_string(
                self.acs_email_connection_string
            )
            metadata_endpoint = _canonicalize_acs_email_endpoint(
                self.acs_email_endpoint
            )
            if connection_endpoint != metadata_endpoint:
                raise SettingsError(
                    "COSMANEURA_ACS_EMAIL_CONNECTION_STRING endpoint must match "
                    "COSMANEURA_ACS_EMAIL_ENDPOINT"
                )
            _validate_email_address(
                self.acs_email_sender_address,
                setting="COSMANEURA_ACS_EMAIL_SENDER_ADDRESS",
            )
            _validate_email_address(
                self.acs_email_reply_to_address,
                setting="COSMANEURA_ACS_EMAIL_REPLY_TO_ADDRESS",
            )

        if (
            self.patient_messaging_operationally_activated
            and not self.patient_messaging_enabled
        ):
            raise SettingsError(
                "COSMANEURA_PATIENT_MESSAGING_OPERATIONALLY_ACTIVATED requires "
                "COSMANEURA_PATIENT_MESSAGING_ENABLED=true"
            )
        if (
            self.environment in {"staging", "production"}
            and self.chase_scheduler_enabled
            and len(self.chase_gate_hmac_key.encode("utf-8")) < 32
        ):
            raise SettingsError(
                "a deployed chase scheduler requires a 32-byte chase gate key"
            )
        if self.patient_messaging_enabled:
            if self.environment not in {"staging", "production"}:
                raise SettingsError(
                    "real patient messaging is allowed only in staging or production"
                )
            if not self.chase_scheduler_enabled:
                raise SettingsError(
                    "real patient messaging requires the durable chase scheduler"
                )
            if not self.patient_messaging_operationally_activated:
                raise SettingsError(
                    "real patient messaging requires explicit operational activation"
                )
            if self.chase_transport != "acs_email":
                raise SettingsError(
                    "real patient messaging requires "
                    "COSMANEURA_CHASE_TRANSPORT=acs_email"
                )
            if not all(acs_fields.values()):
                raise SettingsError(
                    "real patient messaging requires complete ACS email settings"
                )
            _validate_patient_upload_url(self.upload_url)

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None,
    ) -> "ChaseWorkerConfig":
        """Resolve the worker environment without reading API-only settings."""
        env = os.environ if environ is None else environ
        return cls(
            environment=(env.get("COSMANEURA_ENV") or "development").strip().lower(),
            practice_id=(env.get("COSMANEURA_PRACTICE_ID") or "").strip(),
            practice_name=(
                env.get("COSMANEURA_PRACTICE_NAME") or "your care team"
            ).strip(),
            chase_worker_database_role=(
                env.get("COSMANEURA_CHASE_WORKER_DB_ROLE")
                or "cosmaneura_chase_worker"
            ).strip(),
            chase_worker_database_url=(
                env.get("COSMANEURA_CHASE_WORKER_DATABASE_URL") or ""
            ).strip(),
            chase_scheduler_enabled=_worker_bool(
                env, "COSMANEURA_CHASE_SCHEDULER_ENABLED", False
            ),
            patient_messaging_enabled=_worker_bool(
                env, "COSMANEURA_PATIENT_MESSAGING_ENABLED", False
            ),
            patient_messaging_operationally_activated=_worker_bool(
                env,
                "COSMANEURA_PATIENT_MESSAGING_OPERATIONALLY_ACTIVATED",
                False,
            ),
            chase_transport=(
                env.get("COSMANEURA_CHASE_TRANSPORT") or "record_only"
            ).strip().lower(),
            acs_email_connection_string=(
                env.get("COSMANEURA_ACS_EMAIL_CONNECTION_STRING") or ""
            ).strip(),
            acs_email_endpoint=(
                env.get("COSMANEURA_ACS_EMAIL_ENDPOINT") or ""
            ).strip(),
            acs_email_sender_address=(
                env.get("COSMANEURA_ACS_EMAIL_SENDER_ADDRESS") or ""
            ).strip(),
            acs_email_reply_to_address=(
                env.get("COSMANEURA_ACS_EMAIL_REPLY_TO_ADDRESS") or ""
            ).strip(),
            upload_url=(
                env.get("COSMANEURA_UPLOAD_URL")
                or "https://example.invalid/insurance"
            ).strip(),
            chase_approval=(
                env.get("COSMANEURA_CHASE_APPROVAL") or "per_policy"
            ).strip().lower(),
            direct_contract_list=(
                env.get("COSMANEURA_DIRECT_CONTRACT_LIST")
                or "direct_contract_patients.csv"
            ).strip(),
            chase_gate_hmac_key=(
                env.get("COSMANEURA_CHASE_GATE_HMAC_KEY") or ""
            ),
            practice_timezone=(
                env.get("COSMANEURA_PRACTICE_TIMEZONE") or "UTC"
            ).strip(),
            contact_snapshot_max_age_hours=_worker_bounded_int(
                env,
                "COSMANEURA_CONTACT_SNAPSHOT_MAX_AGE_HOURS",
                24,
                1,
                168,
            ),
            chase_worker_batch_size=_worker_bounded_int(
                env, "COSMANEURA_CHASE_WORKER_BATCH_SIZE", 25, 1, 500
            ),
            chase_worker_lease_seconds=_worker_bounded_int(
                env, "COSMANEURA_CHASE_WORKER_LEASE_SECONDS", 300, 30, 3_600
            ),
            log_level=(
                env.get("COSMANEURA_LOG_LEVEL") or "INFO"
            ).strip().upper(),
        )

    @property
    def worker_database_url(self) -> str:
        """The only database URL the scheduled worker is permitted to use."""
        return self.chase_worker_database_url

    @property
    def real_messaging_enabled(self) -> bool:
        return bool(
            self.patient_messaging_enabled
            and self.patient_messaging_operationally_activated
        )

    def build_chase_policy(self) -> ChasePolicy:
        """Build the exact deployment policy whose fingerprint is approved."""
        return ChasePolicy(
            practice_name=self.practice_name,
            upload_url=self.upload_url,
            approval_mode=ApprovalMode(self.chase_approval),
        )

    def build_delivery_configuration(self) -> DeliveryConfiguration:
        """Build the exact delivery inputs covered by policy approval."""
        return DeliveryConfiguration.from_settings(self)  # type: ignore[arg-type]

    def __repr__(self) -> str:
        parts: list[str] = []
        for item in fields(self):
            value = getattr(self, item.name)
            if item.metadata.get("secret"):
                rendered = "'<redacted>'" if value else repr(value)
            else:
                rendered = repr(value)
            parts.append(f"{item.name}={rendered}")
        return f"ChaseWorkerConfig({', '.join(parts)})"


__all__ = ["ChaseWorkerConfig"]
