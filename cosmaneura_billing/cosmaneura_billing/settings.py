"""Validated runtime configuration for the billing service.

Importing this module never snapshots the environment.  Applications resolve a
``Settings`` object at their composition boundary and inject it into long-lived
components, which keeps deployment wiring explicit and tests deterministic.

Secret values are excluded from representations and validation errors.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Mapping
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .runtime import safe_llm_model_label


class SettingsError(ValueError):
    """A runtime setting is missing, incomplete, or malformed."""


AMA_CREDENTIAL_ENV_VARS = (
    "AMA_TOKEN_URL",
    "AMA_BASE_URL",
    "AMA_CONSUMER_KEY",
    "AMA_CONSUMER_SECRET",
)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_ENVIRONMENTS = frozenset({"development", "test", "staging", "production"})
_CHASE_STORES = frozenset({"memory", "postgres"})
_STATE_STORES = frozenset({"local", "postgres"})
_APPROVAL_MODES = frozenset({"per_policy", "per_message"})
_AUTH_MODES = frozenset({"disabled", "container_apps"})
_CHASE_TRANSPORTS = frozenset({"record_only", "acs_email"})
_DEFAULT_ALLOWED_ROLES = ("Admin", "BillingStaff", "Coder", "Provider")
_LOCAL_CORS_ORIGINS = ("http://127.0.0.1:5173", "http://localhost:5173")
_EMAIL = re.compile(
    r"(?=.{3,254}\Z)(?![^@]*\.\.)"
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)


def _parse_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise SettingsError(
        f"{name} must be a boolean (1/0, true/false, yes/no, or on/off)"
    )


def _parse_positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise SettingsError(f"{name} must be a positive integer")
    return value


def _parse_bounded_int(
    env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int,
) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise SettingsError(
            f"{name} must be an integer from {minimum} through {maximum}"
        ) from exc
    if not minimum <= value <= maximum:
        raise SettingsError(
            f"{name} must be an integer from {minimum} through {maximum}"
        )
    return value


def _validate_email_address(value: str, *, setting: str) -> None:
    if not isinstance(value, str) or not _EMAIL.fullmatch(value):
        raise SettingsError(f"{setting} must be one valid email address")


def _canonicalize_acs_email_endpoint(
    value: str,
    *,
    setting: str = "COSMANEURA_ACS_EMAIL_ENDPOINT",
) -> str:
    """Validate ACS endpoint metadata and return its approval-safe identity."""
    if not isinstance(value, str) or any(
        char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value
    ):
        raise SettingsError(f"{setting} must be a valid ACS HTTPS endpoint")
    malformed = False
    try:
        endpoint = urlsplit(value)
        endpoint_port = endpoint.port
        hostname = (endpoint.hostname or "").lower()
    except (TypeError, ValueError):
        malformed = True
        endpoint = None
        endpoint_port = None
        hostname = ""
    if malformed:
        raise SettingsError(
            f"{setting} must be a valid ACS HTTPS endpoint"
        ) from None
    assert endpoint is not None
    if (
        endpoint.scheme.lower() != "https"
        or not hostname
        or not hostname.endswith(".communication.azure.com")
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint_port is not None
        or endpoint.path not in {"", "/"}
        or endpoint.query
        or endpoint.fragment
    ):
        raise SettingsError(f"{setting} must be a valid ACS HTTPS endpoint")
    return f"https://{hostname}"


def _validate_acs_connection_string(value: str) -> str:
    """Validate the secret shape and return only its canonical ACS endpoint."""
    if not isinstance(value, str) or any(
        ord(char) < 32 or ord(char) == 127 for char in value
    ):
        raise SettingsError(
            "COSMANEURA_ACS_EMAIL_CONNECTION_STRING is malformed"
        )
    parts: dict[str, str] = {}
    for segment in value.split(";"):
        if not segment or "=" not in segment:
            raise SettingsError(
                "COSMANEURA_ACS_EMAIL_CONNECTION_STRING is malformed"
            )
        key, part_value = segment.split("=", 1)
        key = key.strip().lower()
        if (
            key in parts
            or key not in {"endpoint", "accesskey"}
            or not part_value
            or any(char.isspace() for char in part_value)
        ):
            raise SettingsError(
                "COSMANEURA_ACS_EMAIL_CONNECTION_STRING is malformed"
            )
        parts[key] = part_value
    if set(parts) != {"endpoint", "accesskey"}:
        raise SettingsError(
            "COSMANEURA_ACS_EMAIL_CONNECTION_STRING is malformed"
        )
    return _canonicalize_acs_email_endpoint(
        parts["endpoint"],
        setting="COSMANEURA_ACS_EMAIL_CONNECTION_STRING",
    )


def _validate_patient_upload_url(value: str) -> None:
    """Require a public-looking HTTPS route without a bearer-like query token."""
    malformed = False
    try:
        parsed = urlsplit(value)
        port = parsed.port
        hostname = (parsed.hostname or "").lower()
    except (TypeError, ValueError):
        malformed = True
        parsed = None
        port = None
        hostname = ""
    if malformed:
        raise SettingsError(
            "COSMANEURA_UPLOAD_URL is not a deployable secure upload route"
        ) from None
    assert parsed is not None
    if (
        parsed.scheme != "https"
        or not hostname
        or hostname == "localhost"
        or hostname.endswith((".invalid", ".example", ".localhost", ".local"))
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.path in {"", "/"}
        or parsed.query
        or parsed.fragment
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise SettingsError(
            "COSMANEURA_UPLOAD_URL is not a deployable secure upload route"
        )


def _validate_application_insights_connection_string(value: str) -> None:
    """Validate routing metadata without reflecting it in an error."""

    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4_096
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise SettingsError(
            "APPLICATIONINSIGHTS_CONNECTION_STRING is malformed"
        )
    parts: dict[str, str] = {}
    for segment in value.split(";"):
        if not segment:
            continue
        if "=" not in segment:
            raise SettingsError(
                "APPLICATIONINSIGHTS_CONNECTION_STRING is malformed"
            )
        key, part_value = segment.split("=", 1)
        normalized = key.strip().casefold()
        if not normalized or normalized in parts or not part_value.strip():
            raise SettingsError(
                "APPLICATIONINSIGHTS_CONNECTION_STRING is malformed"
            )
        parts[normalized] = part_value.strip()
    try:
        UUID(parts.get("instrumentationkey", ""))
        ingestion = urlsplit(parts.get("ingestionendpoint", ""))
    except (TypeError, ValueError, AttributeError):
        raise SettingsError(
            "APPLICATIONINSIGHTS_CONNECTION_STRING is malformed"
        ) from None
    if (
        ingestion.scheme != "https"
        or not ingestion.hostname
        or ingestion.username is not None
        or ingestion.password is not None
        or ingestion.query
        or ingestion.fragment
    ):
        raise SettingsError(
            "APPLICATIONINSIGHTS_CONNECTION_STRING is malformed"
        )


def _database_url_uses_role(value: str, *, role: str) -> bool:
    """Validate a PostgreSQL URI without allowing libpq identity overrides.

    libpq accepts connection keywords in a URI query string, including
    ``user`` and ``service``. Looking only at ``urlsplit(value).username`` can
    therefore approve a least-privilege name while psycopg connects as a more
    powerful account. Deployment URLs need only TLS mode, so the query surface
    is deliberately restricted to one secure ``sslmode`` value.
    """
    malformed = False
    try:
        parsed = urlsplit(value)
        parsed.port  # force validation of a malformed port without retaining it
        query = parse_qsl(
            parsed.query, keep_blank_values=True, strict_parsing=True,
        )
    except (TypeError, ValueError):
        malformed = True
        parsed = None
        query = []
    if malformed:
        return False
    assert parsed is not None
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or parsed.username != role
        or parsed.hostname is None
        or parsed.fragment
        or not parsed.path
        or parsed.path == "/"
    ):
        return False
    if not query:
        return True
    return (
        len(query) == 1
        and query[0][0] == "sslmode"
        and query[0][1] in {"require", "verify-ca", "verify-full"}
    )


def _optional(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    return value if value and value.strip() else None


def _parse_csv(
    env: Mapping[str, str], name: str, default: tuple[str, ...],
) -> tuple[str, ...]:
    raw = env.get(name)
    if raw is None:
        return default
    values = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    return values


@dataclass(frozen=True, repr=False)
class AMACredentials:
    """Complete credentials for the AMA CPT Zip API."""

    token_url: str
    base_url: str
    consumer_key: str = field(repr=False, metadata={"secret": True})
    consumer_secret: str = field(repr=False, metadata={"secret": True})

    def __repr__(self) -> str:
        return (
            "AMACredentials("
            f"token_url={self.token_url!r}, base_url={self.base_url!r}, "
            "consumer_key='<redacted>', consumer_secret='<redacted>')"
        )


@dataclass(frozen=True, repr=False)
class Settings:
    """Resolved process configuration with conservative local defaults."""

    environment: str = "development"
    log_level: str = "INFO"
    frontend_dist: str | None = None
    migrations_dir: str | None = None
    telemetry_enabled: bool = False
    application_insights_connection_string: str = field(
        default="", repr=False, metadata={"secret": True}
    )
    azure_client_id: str = ""

    auth_mode: str = "disabled"
    entra_tenant_id: str = ""
    allowed_roles: tuple[str, ...] = _DEFAULT_ALLOWED_ROLES
    development_user_id: str = "local-developer"
    development_roles: tuple[str, ...] = ("Admin",)
    cors_origins: tuple[str, ...] = ()
    max_request_bytes: int = 1_048_576
    inference_requests_per_minute: int = 10
    general_requests_per_minute: int = 60
    auth_failures_per_minute: int = 30
    access_audit_path: str = "access_audit.jsonl"

    anthropic_api_key: str = field(default="", repr=False, metadata={"secret": True})
    anthropic_model: str = "claude-opus-5"
    anthropic_effort: str = "high"
    openai_api_key: str = field(default="", repr=False, metadata={"secret": True})

    database_url: str = field(default="", repr=False, metadata={"secret": True})
    state_store: str = "local"
    database_runtime_role: str = "cosmaneura_app"
    database_runtime_password: str = field(
        default="", repr=False, metadata={"secret": True}
    )
    chase_worker_database_role: str = "cosmaneura_chase_worker"
    chase_worker_database_password: str = field(
        default="", repr=False, metadata={"secret": True}
    )
    chase_worker_database_url: str = field(
        default="", repr=False, metadata={"secret": True}
    )
    bootstrap_admin_oid: str = ""
    chase_database_url: str = field(
        default="", repr=False, metadata={"secret": True}
    )
    chase_store: str = "memory"
    practice_id: str = ""
    practice_name: str = "your care team"
    upload_url: str = field(
        default="https://example.invalid/insurance",
        repr=False,
        metadata={"secret": True},
    )
    chase_approval: str = "per_policy"
    direct_contract_list: str = "direct_contract_patients.csv"
    chase_gate_hmac_key: str = field(
        default="", repr=False, metadata={"secret": True}
    )

    patient_messaging_enabled: bool = False
    patient_messaging_operationally_activated: bool = False
    chase_scheduler_enabled: bool = False
    chase_transport: str = "record_only"
    acs_email_connection_string: str = field(
        default="", repr=False, metadata={"secret": True}
    )
    acs_email_endpoint: str = ""
    acs_email_sender_address: str = field(
        default="", repr=False, metadata={"secret": True}
    )
    acs_email_reply_to_address: str = field(
        default="", repr=False, metadata={"secret": True}
    )
    practice_timezone: str = "UTC"
    contact_snapshot_max_age_hours: int = 24
    chase_worker_batch_size: int = 25
    chase_worker_lease_seconds: int = 300

    icd10_ccsr_csv: str | None = None
    cpt_data_dir: str | None = None
    cpt_auto_fetch: bool = True
    xdg_cache_home: Path = field(default_factory=lambda: Path.home() / ".cache")

    ama_token_url: str | None = None
    ama_base_url: str | None = None
    ama_consumer_key: str | None = field(
        default=None, repr=False, metadata={"secret": True}
    )
    ama_consumer_secret: str | None = field(
        default=None, repr=False, metadata={"secret": True}
    )
    ama_releases_path: str = "/cpt-zip/1.0.0/releases"
    ama_file_path: str = "/cpt-zip/1.0.0/files?release={release}"

    feedback_path: str = "coder_feedback.jsonl"
    preference_min_count: int = 3
    preference_global_min_users: int = 3
    preferences_enabled: bool = True

    def __post_init__(self) -> None:
        present = {
            "AMA_TOKEN_URL": bool(self.ama_token_url),
            "AMA_BASE_URL": bool(self.ama_base_url),
            "AMA_CONSUMER_KEY": bool(self.ama_consumer_key),
            "AMA_CONSUMER_SECRET": bool(self.ama_consumer_secret),
        }
        if any(present.values()) and not all(present.values()):
            missing = ", ".join(
                name for name, configured in present.items() if not configured
            )
            raise SettingsError(
                "AMA CPT credentials must be configured together; "
                f"missing: {missing}"
            )
        if self.environment not in _ENVIRONMENTS:
            raise SettingsError(
                "COSMANEURA_ENV must be development, test, staging, or production"
            )
        if self.log_level not in _LOG_LEVELS:
            raise SettingsError(
                "COSMANEURA_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL"
            )
        if safe_llm_model_label(self.anthropic_model) == "invalid_model_label":
            raise SettingsError("ANTHROPIC_MODEL must be a supported model ID shape")
        if self.anthropic_effort not in {"low", "medium", "high", "max"}:
            raise SettingsError("ANTHROPIC_EFFORT must be low, medium, high, or max")
        if self.telemetry_enabled:
            if self.environment not in {"staging", "production"}:
                raise SettingsError(
                    "Azure Monitor telemetry is allowed only in staging or production"
                )
            _validate_application_insights_connection_string(
                self.application_insights_connection_string,
            )
            try:
                client_id = UUID(self.azure_client_id)
            except (TypeError, ValueError, AttributeError):
                raise SettingsError(
                    "AZURE_CLIENT_ID must identify the API managed identity"
                ) from None
            if str(client_id) != self.azure_client_id.casefold():
                raise SettingsError(
                    "AZURE_CLIENT_ID must identify the API managed identity"
                )
        if self.chase_store not in _CHASE_STORES:
            raise SettingsError("COSMANEURA_CHASE_STORE must be memory or postgres")
        if self.state_store not in _STATE_STORES:
            raise SettingsError("COSMANEURA_STATE_STORE must be local or postgres")
        if self.chase_approval not in _APPROVAL_MODES:
            raise SettingsError(
                "COSMANEURA_CHASE_APPROVAL must be per_policy or per_message"
            )
        if self.chase_transport not in _CHASE_TRANSPORTS:
            raise SettingsError(
                "COSMANEURA_CHASE_TRANSPORT must be record_only or acs_email"
            )
        try:
            ZoneInfo(self.practice_timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise SettingsError(
                "COSMANEURA_PRACTICE_TIMEZONE must be a valid IANA timezone"
            ) from exc
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
        acs_metadata_fields = {
            "COSMANEURA_ACS_EMAIL_ENDPOINT": bool(self.acs_email_endpoint),
            "COSMANEURA_ACS_EMAIL_SENDER_ADDRESS": bool(
                self.acs_email_sender_address
            ),
            "COSMANEURA_ACS_EMAIL_REPLY_TO_ADDRESS": bool(
                self.acs_email_reply_to_address
            ),
        }
        if any(acs_metadata_fields.values()) and not all(
            acs_metadata_fields.values()
        ):
            missing = ", ".join(
                name
                for name, configured in acs_metadata_fields.items()
                if not configured
            )
            raise SettingsError(
                "ACS email policy metadata must be configured together; "
                f"missing: {missing}"
            )
        if all(acs_metadata_fields.values()):
            _canonicalize_acs_email_endpoint(self.acs_email_endpoint)
            _validate_email_address(
                self.acs_email_sender_address,
                setting="COSMANEURA_ACS_EMAIL_SENDER_ADDRESS",
            )
            _validate_email_address(
                self.acs_email_reply_to_address,
                setting="COSMANEURA_ACS_EMAIL_REPLY_TO_ADDRESS",
            )
        if self.environment in {"staging", "production"} and (
            self.acs_email_connection_string
        ):
            raise SettingsError(
                "COSMANEURA_ACS_EMAIL_CONNECTION_STRING is worker-only and "
                "must not be configured in the deployed API"
            )
        if self.acs_email_connection_string:
            _validate_acs_connection_string(self.acs_email_connection_string)
        if self.chase_transport == "acs_email" and not all(
            acs_metadata_fields.values()
        ):
            raise SettingsError(
                "COSMANEURA_CHASE_TRANSPORT=acs_email requires complete ACS "
                "email settings metadata"
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
            if self.state_store != "postgres" or self.chase_store != "postgres":
                raise SettingsError(
                    "real patient messaging requires durable PostgreSQL stores"
                )
            if self.chase_transport != "acs_email":
                raise SettingsError(
                    "real patient messaging requires "
                    "COSMANEURA_CHASE_TRANSPORT=acs_email"
                )
            if not all(acs_metadata_fields.values()):
                raise SettingsError(
                    "real patient messaging requires complete ACS email "
                    "settings metadata"
                )
            if not self.patient_messaging_operationally_activated:
                raise SettingsError(
                    "real patient messaging requires explicit operational activation"
                )
            if not self.chase_scheduler_enabled:
                raise SettingsError(
                    "real patient messaging requires the durable chase scheduler"
                )
            _validate_patient_upload_url(self.upload_url)
        if self.auth_mode not in _AUTH_MODES:
            raise SettingsError(
                "COSMANEURA_AUTH_MODE must be disabled or container_apps"
            )
        if self.environment in {"staging", "production"} and self.auth_mode == "disabled":
            raise SettingsError(
                "authentication cannot be disabled in staging or production"
            )
        if self.auth_mode == "container_apps":
            try:
                UUID(self.entra_tenant_id)
            except (TypeError, ValueError, AttributeError) as exc:
                raise SettingsError(
                    "COSMANEURA_ENTRA_TENANT_ID must be a tenant UUID"
                ) from exc
            if not self.practice_id.strip():
                raise SettingsError(
                    "COSMANEURA_PRACTICE_ID is required for authenticated access"
                )
        if self.state_store == "postgres":
            if self.auth_mode != "container_apps":
                raise SettingsError(
                    "Postgres state requires COSMANEURA_AUTH_MODE=container_apps"
                )
            if not self.database_url.strip():
                raise SettingsError(
                    "DATABASE_URL is required when COSMANEURA_STATE_STORE=postgres"
                )
            if not self.practice_id.strip():
                raise SettingsError(
                    "COSMANEURA_PRACTICE_ID is required for Postgres state"
                )
            try:
                UUID(self.entra_tenant_id)
            except (TypeError, ValueError, AttributeError) as exc:
                raise SettingsError(
                    "COSMANEURA_ENTRA_TENANT_ID is required for Postgres state"
                ) from exc
            if self.chase_database_url and self.chase_database_url != self.database_url:
                raise SettingsError(
                    "Postgres state and chase must use the same DATABASE_URL "
                    "for atomic audit transactions"
                )
        if self.environment in {"staging", "production"}:
            if self.state_store != "postgres":
                raise SettingsError(
                    "staging and production require COSMANEURA_STATE_STORE=postgres"
                )
            if self.chase_store != "postgres":
                raise SettingsError(
                    "staging and production require COSMANEURA_CHASE_STORE=postgres"
                )
            if not self.database_runtime_role.strip():
                raise SettingsError("COSMANEURA_DB_RUNTIME_ROLE must not be empty")
            if not _database_url_uses_role(
                self.database_url, role=self.database_runtime_role,
            ):
                raise SettingsError(
                    "DATABASE_URL must use the configured least-privilege runtime role"
                )
        if self.database_runtime_password and not self.database_runtime_role.strip():
            raise SettingsError("COSMANEURA_DB_RUNTIME_ROLE must not be empty")
        if (
            self.chase_worker_database_password
            and not self.chase_worker_database_role.strip()
        ):
            raise SettingsError("COSMANEURA_CHASE_WORKER_DB_ROLE must not be empty")
        if (
            self.state_store == "postgres" or self.database_runtime_password
        ) and not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_.-]{0,62}", self.database_runtime_role,
        ):
            raise SettingsError(
                "COSMANEURA_DB_RUNTIME_ROLE must be a safe PostgreSQL role name"
            )
        if (
            self.state_store == "postgres" or self.database_runtime_password
        ) and (
            self.database_runtime_role.casefold().startswith("pg_")
            or self.database_runtime_role.casefold()
            in {"postgres", "azure_pg_admin"}
        ):
            raise SettingsError(
                "COSMANEURA_DB_RUNTIME_ROLE must be a safe PostgreSQL role name"
            )
        if not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_.-]{0,62}",
            self.chase_worker_database_role,
        ):
            raise SettingsError(
                "COSMANEURA_CHASE_WORKER_DB_ROLE must be a safe PostgreSQL role name"
            )
        if (
            self.chase_worker_database_role.casefold().startswith("pg_")
            or self.chase_worker_database_role.casefold()
            in {"postgres", "azure_pg_admin"}
        ):
            raise SettingsError(
                "COSMANEURA_CHASE_WORKER_DB_ROLE must be a safe PostgreSQL role name"
            )
        if (
            self.chase_worker_database_role.casefold()
            == self.database_runtime_role.casefold()
        ):
            raise SettingsError(
                "the API runtime role and chase worker role must be distinct"
            )
        if self.chase_worker_database_url:
            if not _database_url_uses_role(
                self.chase_worker_database_url,
                role=self.chase_worker_database_role,
            ):
                raise SettingsError(
                    "COSMANEURA_CHASE_WORKER_DATABASE_URL must use the configured "
                    "least-privilege chase worker role"
                )
        if self.bootstrap_admin_oid:
            try:
                UUID(self.bootstrap_admin_oid)
            except (TypeError, ValueError, AttributeError) as exc:
                raise SettingsError(
                    "COSMANEURA_BOOTSTRAP_ADMIN_OID must be a UUID"
                ) from exc
        if not self.allowed_roles or any(not role.strip() for role in self.allowed_roles):
            raise SettingsError("COSMANEURA_ALLOWED_ROLES must not be empty")
        if not set(self.allowed_roles).issubset(_DEFAULT_ALLOWED_ROLES):
            raise SettingsError(
                "COSMANEURA_ALLOWED_ROLES contains an unknown application role"
            )
        if not self.development_user_id.strip() or not self.development_roles:
            raise SettingsError("development authentication identity must not be empty")
        if not set(self.development_roles).issubset(_DEFAULT_ALLOWED_ROLES):
            raise SettingsError("COSMANEURA_DEV_ROLES contains an unknown application role")
        for origin in self.cors_origins:
            parsed = urlsplit(origin)
            if (
                origin == "*"
                or parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise SettingsError(
                    "COSMANEURA_CORS_ORIGINS must contain exact HTTP(S) origins"
                )
        for name, value in (
            ("COSMANEURA_MAX_REQUEST_BYTES", self.max_request_bytes),
            ("COSMANEURA_INFERENCE_REQUESTS_PER_MINUTE", self.inference_requests_per_minute),
            ("COSMANEURA_GENERAL_REQUESTS_PER_MINUTE", self.general_requests_per_minute),
            ("COSMANEURA_AUTH_FAILURES_PER_MINUTE", self.auth_failures_per_minute),
        ):
            if value < 1:
                raise SettingsError(f"{name} must be a positive integer")
        if not self.access_audit_path.strip():
            raise SettingsError("COSMANEURA_ACCESS_AUDIT_PATH must not be empty")
        if self.preference_min_count < 1:
            raise SettingsError("COSMANEURA_PREF_MIN must be a positive integer")
        if self.preference_global_min_users < 1:
            raise SettingsError(
                "COSMANEURA_PREF_GLOBAL_USERS must be a positive integer"
            )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        """Parse and validate settings from ``environ`` (the process by default)."""
        env = os.environ if environ is None else environ
        environment = (env.get("COSMANEURA_ENV") or "development").strip().lower()
        cache_value = env.get("XDG_CACHE_HOME")
        cache_home = (
            Path(cache_value) if cache_value else Path.home() / ".cache"
        )
        return cls(
            environment=environment,
            log_level=(env.get("COSMANEURA_LOG_LEVEL") or "INFO").strip().upper(),
            frontend_dist=_optional(env, "COSMANEURA_FRONTEND_DIST"),
            migrations_dir=_optional(env, "COSMANEURA_MIGRATIONS_DIR"),
            telemetry_enabled=_parse_bool(
                env, "COSMANEURA_TELEMETRY_ENABLED", False,
            ),
            application_insights_connection_string=(
                env.get("APPLICATIONINSIGHTS_CONNECTION_STRING") or ""
            ).strip(),
            azure_client_id=(env.get("AZURE_CLIENT_ID") or "").strip(),
            auth_mode=(env.get("COSMANEURA_AUTH_MODE") or "disabled").strip().lower(),
            entra_tenant_id=env.get("COSMANEURA_ENTRA_TENANT_ID", "").strip(),
            allowed_roles=_parse_csv(
                env, "COSMANEURA_ALLOWED_ROLES", _DEFAULT_ALLOWED_ROLES
            ),
            development_user_id=(
                env.get("COSMANEURA_DEV_USER_ID") or "local-developer"
            ),
            development_roles=_parse_csv(
                env, "COSMANEURA_DEV_ROLES", ("Admin",)
            ),
            cors_origins=_parse_csv(
                env,
                "COSMANEURA_CORS_ORIGINS",
                _LOCAL_CORS_ORIGINS if environment in {"development", "test"} else (),
            ),
            max_request_bytes=_parse_positive_int(
                env, "COSMANEURA_MAX_REQUEST_BYTES", 1_048_576
            ),
            inference_requests_per_minute=_parse_positive_int(
                env, "COSMANEURA_INFERENCE_REQUESTS_PER_MINUTE", 10
            ),
            general_requests_per_minute=_parse_positive_int(
                env, "COSMANEURA_GENERAL_REQUESTS_PER_MINUTE", 60
            ),
            auth_failures_per_minute=_parse_positive_int(
                env, "COSMANEURA_AUTH_FAILURES_PER_MINUTE", 30
            ),
            access_audit_path=(
                env.get("COSMANEURA_ACCESS_AUDIT_PATH") or "access_audit.jsonl"
            ),
            anthropic_api_key=env.get("ANTHROPIC_API_KEY", ""),
            anthropic_model=env.get("ANTHROPIC_MODEL") or "claude-opus-5",
            anthropic_effort=env.get("ANTHROPIC_EFFORT") or "high",
            openai_api_key=env.get("OPENAI_API_KEY", ""),
            database_url=env.get("DATABASE_URL", ""),
            state_store=(
                env.get("COSMANEURA_STATE_STORE") or "local"
            ).strip().lower(),
            database_runtime_role=(
                env.get("COSMANEURA_DB_RUNTIME_ROLE") or "cosmaneura_app"
            ).strip(),
            database_runtime_password=env.get(
                "COSMANEURA_DB_RUNTIME_PASSWORD", ""
            ),
            chase_worker_database_role=(
                env.get("COSMANEURA_CHASE_WORKER_DB_ROLE")
                or "cosmaneura_chase_worker"
            ).strip(),
            chase_worker_database_password=env.get(
                "COSMANEURA_CHASE_WORKER_DB_PASSWORD", ""
            ),
            chase_worker_database_url=env.get(
                "COSMANEURA_CHASE_WORKER_DATABASE_URL", ""
            ),
            bootstrap_admin_oid=(
                env.get("COSMANEURA_BOOTSTRAP_ADMIN_OID") or ""
            ).strip(),
            chase_database_url=env.get("COSMANEURA_CHASE_DATABASE_URL", ""),
            chase_store=(env.get("COSMANEURA_CHASE_STORE") or "memory").strip().lower(),
            practice_id=env.get("COSMANEURA_PRACTICE_ID") or "",
            practice_name=env.get("COSMANEURA_PRACTICE_NAME") or "your care team",
            upload_url=(
                env.get("COSMANEURA_UPLOAD_URL")
                or "https://example.invalid/insurance"
            ),
            chase_approval=(
                env.get("COSMANEURA_CHASE_APPROVAL") or "per_policy"
            ).strip().lower(),
            direct_contract_list=(
                env.get("COSMANEURA_DIRECT_CONTRACT_LIST")
                or "direct_contract_patients.csv"
            ),
            chase_gate_hmac_key=(
                env.get("COSMANEURA_CHASE_GATE_HMAC_KEY") or ""
            ),
            patient_messaging_enabled=_parse_bool(
                env, "COSMANEURA_PATIENT_MESSAGING_ENABLED", False
            ),
            patient_messaging_operationally_activated=_parse_bool(
                env,
                "COSMANEURA_PATIENT_MESSAGING_OPERATIONALLY_ACTIVATED",
                False,
            ),
            chase_scheduler_enabled=_parse_bool(
                env, "COSMANEURA_CHASE_SCHEDULER_ENABLED", False
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
            practice_timezone=(
                env.get("COSMANEURA_PRACTICE_TIMEZONE") or "UTC"
            ).strip(),
            contact_snapshot_max_age_hours=_parse_bounded_int(
                env,
                "COSMANEURA_CONTACT_SNAPSHOT_MAX_AGE_HOURS",
                24,
                1,
                168,
            ),
            chase_worker_batch_size=_parse_bounded_int(
                env, "COSMANEURA_CHASE_WORKER_BATCH_SIZE", 25, 1, 500
            ),
            chase_worker_lease_seconds=_parse_bounded_int(
                env, "COSMANEURA_CHASE_WORKER_LEASE_SECONDS", 300, 30, 3_600
            ),
            icd10_ccsr_csv=_optional(env, "ICD10_CCSR_CSV"),
            cpt_data_dir=_optional(env, "CPT_DATA_DIR"),
            cpt_auto_fetch=_parse_bool(env, "CPT_AUTO_FETCH", True),
            xdg_cache_home=cache_home,
            ama_token_url=_optional(env, "AMA_TOKEN_URL"),
            ama_base_url=_optional(env, "AMA_BASE_URL"),
            ama_consumer_key=_optional(env, "AMA_CONSUMER_KEY"),
            ama_consumer_secret=_optional(env, "AMA_CONSUMER_SECRET"),
            ama_releases_path=(
                env.get("AMA_RELEASES_PATH") or "/cpt-zip/1.0.0/releases"
            ),
            ama_file_path=(
                env.get("AMA_FILE_PATH")
                or "/cpt-zip/1.0.0/files?release={release}"
            ),
            feedback_path=(
                env.get("COSMANEURA_FEEDBACK_PATH") or "coder_feedback.jsonl"
            ),
            preference_min_count=_parse_positive_int(
                env, "COSMANEURA_PREF_MIN", 3
            ),
            preference_global_min_users=_parse_positive_int(
                env, "COSMANEURA_PREF_GLOBAL_USERS", 3
            ),
            preferences_enabled=_parse_bool(env, "COSMANEURA_PREFS", True),
        )

    @property
    def cpt_cache_dir(self) -> Path:
        return self.xdg_cache_home / "cosmaneura" / "cpt"

    @property
    def ama_configured(self) -> bool:
        return bool(self.ama_token_url)

    @property
    def ama_credentials(self) -> AMACredentials | None:
        if not self.ama_configured:
            return None
        return AMACredentials(
            token_url=self.ama_token_url or "",
            base_url=self.ama_base_url or "",
            consumer_key=self.ama_consumer_key or "",
            consumer_secret=self.ama_consumer_secret or "",
        )

    @property
    def chase_dsn(self) -> str:
        return self.chase_database_url or self.database_url

    def __repr__(self) -> str:
        parts: list[str] = []
        for item in fields(self):
            value = getattr(self, item.name)
            if item.metadata.get("secret"):
                rendered = "'<redacted>'" if value else repr(value)
            else:
                rendered = repr(value)
            parts.append(f"{item.name}={rendered}")
        return f"Settings({', '.join(parts)})"


def get_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Resolve current settings without import-time or global caching."""
    return Settings.from_env(environ)
