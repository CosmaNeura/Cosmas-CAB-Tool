"""One-shot, fail-closed Athena sandbox sync job.

The deterministic mock can exercise the durable, source-agnostic import and
checkpoint boundary.  This module still performs no OAuth exchange or Athena
network call: a live configuration can be staged, but execution returns the
static ``ATHENA_LIVE_NOT_IMPLEMENTED`` code until the vendor contract is
approved and implemented.
"""
from __future__ import annotations

import json
import os
import re
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
    MAX_PAGES,
    NORMALIZED_SCHEMA_VERSION,
    AthenaClient,
    AthenaClientError,
    AthenaPage,
    MockAthenaClient,
    validate_athena_https_url,
)
from .persistence import ATHENA_PREVIEW_SYNTHETIC_SOURCE
from .settings import SettingsError, _database_url_uses_role, _parse_bool
from .source_import import (
    MemorySourceImportStore,
    PostgresSourceImportStore,
    SourceImportError,
    SourceImportStore,
)


_MODES = frozenset({"disabled", "mock", "live"})
ATHENA_PREVIEW_SOURCE_SYSTEM = "athena_preview"
ATHENA_PREVIEW_ENCOUNTER_FEED = "encounters"
ATHENA_IMPORT_DATABASE_ROLE = "cosmaneura_athena_import"


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
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
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
    import_database_role: str = ATHENA_IMPORT_DATABASE_ROLE
    import_database_url: str = field(
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

        _safe_config_text(
            self.import_database_role,
            setting="COSMANEURA_ATHENA_IMPORT_DB_ROLE",
            maximum=63,
        )
        if (
            re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_.-]{0,62}",
                self.import_database_role,
            )
            is None
            or self.import_database_role.casefold().startswith("pg_")
            or self.import_database_role.casefold()
            in {"postgres", "azure_pg_admin"}
        ):
            raise SettingsError(
                "COSMANEURA_ATHENA_IMPORT_DB_ROLE must be a safe PostgreSQL role"
            )
        if self.import_database_url and not _database_url_uses_role(
            self.import_database_url,
            role=self.import_database_role,
        ):
            raise SettingsError(
                "COSMANEURA_ATHENA_IMPORT_DATABASE_URL must use the dedicated "
                "Athena import role"
            )
        if self.mode == "live" and not self.import_database_url:
            raise SettingsError(
                "COSMANEURA_ATHENA_MODE=live requires the dedicated import "
                "database URL"
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
            import_database_role=(
                env.get("COSMANEURA_ATHENA_IMPORT_DB_ROLE")
                or ATHENA_IMPORT_DATABASE_ROLE
            ).strip(),
            # Do not trim or normalize database credentials.
            import_database_url=env.get(
                "COSMANEURA_ATHENA_IMPORT_DATABASE_URL",
                "",
            ),
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
    IMPORT_ERROR = "import_error"
    LIVE_NOT_IMPLEMENTED = "live_not_implemented"


_STATUS_CODES = {
    AthenaSyncStatus.COMPLETED: "ATHENA_MOCK_COMPLETE",
    AthenaSyncStatus.DISABLED: "ATHENA_SYNC_DISABLED",
    AthenaSyncStatus.CONFIGURATION_ERROR: "ATHENA_CONFIGURATION_ERROR",
    AthenaSyncStatus.ADAPTER_ERROR: "ATHENA_ADAPTER_ERROR",
    AthenaSyncStatus.IMPORT_ERROR: "ATHENA_IMPORT_ERROR",
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


def _run_mock_pages(
    *,
    client: AthenaClient,
    store: SourceImportStore,
) -> AthenaSyncResult:
    """Import bounded mock pages, committing each page with its continuation."""

    if getattr(client, "mode", None) != "athena_mock":
        return AthenaSyncResult(AthenaSyncStatus.ADAPTER_ERROR)

    state = store.load_checkpoint(
        source_system=ATHENA_PREVIEW_SOURCE_SYSTEM,
        feed=ATHENA_PREVIEW_ENCOUNTER_FEED,
    )
    if (
        state is not None
        and state.mapping_version != NORMALIZED_SCHEMA_VERSION
    ):
        return AthenaSyncResult(AthenaSyncStatus.IMPORT_ERROR)
    cursor = state.next_cursor if state is not None else None
    seen_cursors: set[str] = (
        set(state.cursor_history)
        if state is not None and state.next_cursor is not None
        else set()
    )
    expected_checkpoint_version = state.version if state is not None else None
    fetched = 0
    imported = 0
    replayed = 0

    for page_count in range(1, MAX_PAGES + 1):
        try:
            page = client.read_page(cursor)
        except AthenaClientError:
            return AthenaSyncResult(
                status=AthenaSyncStatus.ADAPTER_ERROR,
                page_count=page_count - 1,
                fetched_count=fetched,
                replayed_count=replayed,
                imported_count=imported,
            )
        if not isinstance(page, AthenaPage):
            return AthenaSyncResult(
                status=AthenaSyncStatus.ADAPTER_ERROR,
                page_count=page_count - 1,
                fetched_count=fetched,
                replayed_count=replayed,
                imported_count=imported,
            )
        # A repeated continuation is an adapter failure. Validate it before
        # entering the page transaction so neither its records nor its
        # checkpoint can become the last committed boundary.
        if page.next_cursor is not None and (
            page.next_cursor in seen_cursors or page.next_cursor == cursor
        ):
            return AthenaSyncResult(
                status=AthenaSyncStatus.ADAPTER_ERROR,
                page_count=page_count - 1,
                fetched_count=fetched,
                replayed_count=replayed,
                imported_count=imported,
            )
        try:
            outcome = store.import_encounter_page(
                source_system=ATHENA_PREVIEW_SOURCE_SYSTEM,
                feed=ATHENA_PREVIEW_ENCOUNTER_FEED,
                provenance=ATHENA_PREVIEW_SYNTHETIC_SOURCE,
                mapping_version=NORMALIZED_SCHEMA_VERSION,
                records=page.records,
                checkpoint=page.checkpoint,
                next_cursor=page.next_cursor,
                expected_checkpoint_version=expected_checkpoint_version,
            )
        except (SourceImportError, RuntimeError):
            return AthenaSyncResult(
                status=AthenaSyncStatus.IMPORT_ERROR,
                page_count=page_count - 1,
                fetched_count=fetched,
                replayed_count=replayed,
                imported_count=imported,
            )
        fetched += len(page.records)
        imported += outcome.imported_count
        replayed += outcome.replayed_count
        expected_checkpoint_version = outcome.checkpoint.version
        if page.next_cursor is None:
            return AthenaSyncResult(
                status=AthenaSyncStatus.COMPLETED,
                page_count=page_count,
                fetched_count=fetched,
                replayed_count=replayed,
                imported_count=imported,
            )
        seen_cursors.add(page.next_cursor)
        cursor = page.next_cursor
    return AthenaSyncResult(
        status=AthenaSyncStatus.ADAPTER_ERROR,
        page_count=MAX_PAGES,
        fetched_count=fetched,
        replayed_count=replayed,
        imported_count=imported,
    )


def run_once(
    config: AthenaSyncConfig,
    *,
    import_store: SourceImportStore | None = None,
) -> AthenaSyncResult:
    """Run the mock import path; keep live OAuth and network fail-closed."""

    if not isinstance(config, AthenaSyncConfig):
        return AthenaSyncResult(AthenaSyncStatus.CONFIGURATION_ERROR)
    if config.enabled is not True:
        return AthenaSyncResult(AthenaSyncStatus.DISABLED)
    if config.mode != "mock":
        return AthenaSyncResult(AthenaSyncStatus.LIVE_NOT_IMPLEMENTED)

    try:
        selected_client = MockAthenaClient(
            server_practice_id=config.target_practice_id,
        )
        selected_store = import_store or (
            PostgresSourceImportStore(
                config.import_database_url,
                practice_id=config.target_practice_id,
            )
            if config.import_database_url
            else MemorySourceImportStore(
                practice_id=config.target_practice_id,
            )
        )
        return _run_mock_pages(client=selected_client, store=selected_store)
    except AthenaClientError:
        return AthenaSyncResult(AthenaSyncStatus.ADAPTER_ERROR)
    except (SourceImportError, RuntimeError):
        return AthenaSyncResult(AthenaSyncStatus.IMPORT_ERROR)


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
