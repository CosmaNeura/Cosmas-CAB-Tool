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
from urllib.parse import urlsplit
from uuid import UUID


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
_DEFAULT_ALLOWED_ROLES = ("Admin", "BillingStaff", "Coder", "Provider")
_LOCAL_CORS_ORIGINS = ("http://127.0.0.1:5173", "http://localhost:5173")


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
        if self.chase_store not in _CHASE_STORES:
            raise SettingsError("COSMANEURA_CHASE_STORE must be memory or postgres")
        if self.state_store not in _STATE_STORES:
            raise SettingsError("COSMANEURA_STATE_STORE must be local or postgres")
        if self.chase_approval not in _APPROVAL_MODES:
            raise SettingsError(
                "COSMANEURA_CHASE_APPROVAL must be per_policy or per_message"
            )
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
            database_user = urlsplit(self.database_url).username or ""
            if database_user != self.database_runtime_role:
                raise SettingsError(
                    "DATABASE_URL must use the configured least-privilege runtime role"
                )
        if self.database_runtime_password and not self.database_runtime_role.strip():
            raise SettingsError("COSMANEURA_DB_RUNTIME_ROLE must not be empty")
        if (
            self.state_store == "postgres" or self.database_runtime_password
        ) and not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_.-]{0,62}", self.database_runtime_role,
        ):
            raise SettingsError(
                "COSMANEURA_DB_RUNTIME_ROLE must be a safe PostgreSQL role name"
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
