"""Validated runtime configuration for the billing service.

Importing this module never snapshots the environment.  Applications resolve a
``Settings`` object at their composition boundary and inject it into long-lived
components, which keeps deployment wiring explicit and tests deterministic.

Secret values are excluded from representations and validation errors.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Mapping


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
_APPROVAL_MODES = frozenset({"per_policy", "per_message"})


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

    anthropic_api_key: str = field(default="", repr=False, metadata={"secret": True})
    anthropic_model: str = "claude-opus-5"
    anthropic_effort: str = "high"
    openai_api_key: str = field(default="", repr=False, metadata={"secret": True})

    database_url: str = field(default="", repr=False, metadata={"secret": True})
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
    coder_id: str = "coder_A"

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
        if self.chase_approval not in _APPROVAL_MODES:
            raise SettingsError(
                "COSMANEURA_CHASE_APPROVAL must be per_policy or per_message"
            )
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
        cache_value = env.get("XDG_CACHE_HOME")
        cache_home = (
            Path(cache_value) if cache_value else Path.home() / ".cache"
        )
        return cls(
            environment=(env.get("COSMANEURA_ENV") or "development").strip().lower(),
            log_level=(env.get("COSMANEURA_LOG_LEVEL") or "INFO").strip().upper(),
            frontend_dist=_optional(env, "COSMANEURA_FRONTEND_DIST"),
            migrations_dir=_optional(env, "COSMANEURA_MIGRATIONS_DIR"),
            anthropic_api_key=env.get("ANTHROPIC_API_KEY", ""),
            anthropic_model=env.get("ANTHROPIC_MODEL") or "claude-opus-5",
            anthropic_effort=env.get("ANTHROPIC_EFFORT") or "high",
            openai_api_key=env.get("OPENAI_API_KEY", ""),
            database_url=env.get("DATABASE_URL", ""),
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
            coder_id=env.get("COSMANEURA_CODER_ID") or "coder_A",
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
