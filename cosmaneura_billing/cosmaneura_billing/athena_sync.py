"""One-shot, fail-closed skeleton for the future Athena sandbox sync job.

This slice intentionally supports only the deterministic mock client.  It does
not perform an OAuth exchange, call Athena, or write imported records.  A live
configuration can be validated and staged, but execution returns a static
``ATHENA_LIVE_NOT_IMPLEMENTED`` code until the approved sandbox contract and
the persistence acceptance are implemented.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Mapping, Sequence, TextIO

from .athena_client import (
    ATHENA_PREVIEW_INTROSPECTION_URL,
    ATHENA_PREVIEW_ORIGIN,
    ATHENA_PREVIEW_PRACTICE_ID,
    ATHENA_PREVIEW_SCOPE,
    ATHENA_PREVIEW_TOKEN_URL,
    AthenaClientError,
    MockAthenaClient,
    read_all_pages,
    validate_athena_https_url,
)
from .settings import SettingsError, _parse_bool


_MODES = frozenset({"disabled", "mock", "live"})


def _parse_scopes(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = env.get("COSMANEURA_ATHENA_SCOPES", "")
    if not raw.strip():
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _safe_config_text(
    value: str,
    *,
    setting: str,
    maximum: int,
    allow_space: bool = False,
) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or (not allow_space and any(char.isspace() for char in value))
    ):
        raise SettingsError(f"{setting} is malformed") from None


def _validate_config_url(value: str, *, setting: str) -> None:
    try:
        validate_athena_https_url(value)
        parsed = urllib.parse.urlsplit(value)
    except AthenaClientError:
        raise SettingsError(f"{setting} must be an explicit HTTPS URL") from None
    if parsed.query or parsed.fragment:
        raise SettingsError(f"{setting} must be an explicit HTTPS URL") from None


@dataclass(frozen=True, repr=False)
class AthenaSyncConfig:
    """Standalone job configuration; no API/database setting is inherited."""

    enabled: bool = False
    mode: str = "disabled"
    base_url: str = ""
    token_url: str = ""
    client_id: str = ""
    scopes: tuple[str, ...] = ()
    client_secret: str = field(
        default="",
        repr=False,
        metadata={"secret": True},
    )
    target_practice_id: str = ""

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise SettingsError(
                "COSMANEURA_ATHENA_SYNC_ENABLED must be a boolean"
            )
        if self.mode not in _MODES:
            raise SettingsError(
                "COSMANEURA_ATHENA_MODE must be disabled, mock, or live"
            )
        if self.enabled and self.mode == "disabled":
            raise SettingsError(
                "enabled Athena sync requires COSMANEURA_ATHENA_MODE=mock or live"
            )
        if self.enabled:
            _safe_config_text(
                self.target_practice_id,
                setting="COSMANEURA_PRACTICE_ID",
                maximum=256,
                allow_space=True,
            )

        live_fields = {
            "COSMANEURA_ATHENA_BASE_URL": bool(self.base_url),
            "COSMANEURA_ATHENA_TOKEN_URL": bool(self.token_url),
            "COSMANEURA_ATHENA_CLIENT_ID": bool(self.client_id),
            "COSMANEURA_ATHENA_SCOPES": bool(self.scopes),
            "COSMANEURA_ATHENA_CLIENT_SECRET": bool(self.client_secret),
        }
        if any(live_fields.values()) and not all(live_fields.values()):
            missing = ", ".join(
                name for name, configured in live_fields.items() if not configured
            )
            raise SettingsError(
                "Athena live settings must be configured together; "
                f"missing: {missing}"
            )
        if self.mode == "live" and not all(live_fields.values()):
            raise SettingsError(
                "COSMANEURA_ATHENA_MODE=live requires complete Athena live settings"
            )
        if self.mode == "mock" and any(live_fields.values()):
            raise SettingsError(
                "COSMANEURA_ATHENA_MODE=mock must not receive live credentials"
            )

        if all(live_fields.values()):
            _validate_config_url(
                self.base_url,
                setting="COSMANEURA_ATHENA_BASE_URL",
            )
            _validate_config_url(
                self.token_url,
                setting="COSMANEURA_ATHENA_TOKEN_URL",
            )
            if self.base_url != ATHENA_PREVIEW_ORIGIN:
                raise SettingsError(
                    "COSMANEURA_ATHENA_BASE_URL must be the fixed Preview origin"
                ) from None
            if self.token_url != ATHENA_PREVIEW_TOKEN_URL:
                raise SettingsError(
                    "COSMANEURA_ATHENA_TOKEN_URL must be the fixed Preview token URL"
                ) from None
            _safe_config_text(
                self.client_id,
                setting="COSMANEURA_ATHENA_CLIENT_ID",
                maximum=512,
            )
            if self.scopes != (ATHENA_PREVIEW_SCOPE,):
                raise SettingsError(
                    "COSMANEURA_ATHENA_SCOPES must be the exact reviewed Preview scope"
                )
            if (
                not isinstance(self.client_secret, str)
                or not self.client_secret
                or len(self.client_secret) > 4_096
                or any(
                    ord(char) < 32 or ord(char) == 127
                    for char in self.client_secret
                )
            ):
                raise SettingsError(
                    "COSMANEURA_ATHENA_CLIENT_SECRET is malformed"
                ) from None

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "AthenaSyncConfig":
        env = os.environ if environ is None else environ
        return cls(
            enabled=_parse_bool(
                env,
                "COSMANEURA_ATHENA_SYNC_ENABLED",
                False,
            ),
            mode=(env.get("COSMANEURA_ATHENA_MODE") or "disabled")
            .strip()
            .lower(),
            base_url=(env.get("COSMANEURA_ATHENA_BASE_URL") or "").strip(),
            token_url=(env.get("COSMANEURA_ATHENA_TOKEN_URL") or "").strip(),
            client_id=(env.get("COSMANEURA_ATHENA_CLIENT_ID") or "").strip(),
            scopes=_parse_scopes(env),
            # Do not trim or normalize credentials.
            client_secret=env.get("COSMANEURA_ATHENA_CLIENT_SECRET", ""),
            target_practice_id=(
                env.get("COSMANEURA_PRACTICE_ID") or ""
            ).strip(),
        )

    @property
    def vendor_practice_id(self) -> str:
        """Fixed Athena public Preview practice, never an environment value."""

        return ATHENA_PREVIEW_PRACTICE_ID

    @property
    def introspection_url(self) -> str:
        """Verified Preview metadata; no introspection call exists yet."""

        return ATHENA_PREVIEW_INTROSPECTION_URL

    def __repr__(self) -> str:
        parts: list[str] = []
        for item in fields(self):
            value = getattr(self, item.name)
            if item.metadata.get("secret"):
                rendered = "'<redacted>'" if value else repr(value)
            else:
                rendered = repr(value)
            parts.append(f"{item.name}={rendered}")
        return f"AthenaSyncConfig({', '.join(parts)})"


class AthenaSyncStatus(str, Enum):
    COMPLETED = "completed"
    DISABLED = "disabled"
    CONFIGURATION_ERROR = "configuration_error"
    ADAPTER_ERROR = "adapter_error"
    LIVE_NOT_IMPLEMENTED = "live_not_implemented"


_STATUS_CODES = {
    AthenaSyncStatus.COMPLETED: "ATHENA_MOCK_COMPLETE",
    AthenaSyncStatus.DISABLED: "ATHENA_SYNC_DISABLED",
    AthenaSyncStatus.CONFIGURATION_ERROR: "ATHENA_CONFIGURATION_ERROR",
    AthenaSyncStatus.ADAPTER_ERROR: "ATHENA_ADAPTER_ERROR",
    AthenaSyncStatus.LIVE_NOT_IMPLEMENTED: "ATHENA_LIVE_NOT_IMPLEMENTED",
}


@dataclass(frozen=True)
class AthenaSyncResult:
    """Identifier-free operational result for one bounded invocation."""

    status: AthenaSyncStatus
    page_count: int = 0
    fetched_count: int = 0
    replayed_count: int = 0
    imported_count: int = 0

    @property
    def code(self) -> str:
        return _STATUS_CODES[self.status]

    @property
    def exit_code(self) -> int:
        if self.status is AthenaSyncStatus.COMPLETED:
            return 0
        if self.status in {
            AthenaSyncStatus.DISABLED,
            AthenaSyncStatus.CONFIGURATION_ERROR,
            AthenaSyncStatus.LIVE_NOT_IMPLEMENTED,
        }:
            return 2
        return 1

    def as_dict(self) -> dict[str, str | int]:
        return {
            "code": self.code,
            "fetched_count": self.fetched_count,
            "imported_count": self.imported_count,
            "page_count": self.page_count,
            "replayed_count": self.replayed_count,
            "status": self.status.value,
        }


def run_once(config: AthenaSyncConfig) -> AthenaSyncResult:
    """Run only the deterministic mock; no network or persistence exists."""

    if not isinstance(config, AthenaSyncConfig):
        return AthenaSyncResult(AthenaSyncStatus.CONFIGURATION_ERROR)
    if config.enabled is not True:
        return AthenaSyncResult(AthenaSyncStatus.DISABLED)
    if config.mode != "mock":
        return AthenaSyncResult(AthenaSyncStatus.LIVE_NOT_IMPLEMENTED)

    try:
        client = MockAthenaClient(server_practice_id=config.target_practice_id)
        result = read_all_pages(client)
    except AthenaClientError:
        return AthenaSyncResult(AthenaSyncStatus.ADAPTER_ERROR)
    return AthenaSyncResult(
        status=AthenaSyncStatus.COMPLETED,
        page_count=result.page_count,
        fetched_count=len(result.records),
        replayed_count=result.replay_count,
        # Persistence is intentionally absent in this slice.
        imported_count=0,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stream: TextIO | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    output = stream or sys.stdout
    if args not in ([], ["--once"]):
        result = AthenaSyncResult(AthenaSyncStatus.CONFIGURATION_ERROR)
    else:
        try:
            config = AthenaSyncConfig.from_env(environ)
        except (SettingsError, ValueError, TypeError):
            result = AthenaSyncResult(AthenaSyncStatus.CONFIGURATION_ERROR)
        else:
            result = run_once(config)
    print(json.dumps(result.as_dict(), sort_keys=True), file=output)
    return result.exit_code


if __name__ == "__main__":  # pragma: no cover - exercised through main
    raise SystemExit(main())
