"""Fail-closed one-shot Athena sync skeleton."""
from __future__ import annotations

import io
import inspect
import json
import socket
import urllib.request
from dataclasses import replace

import pytest

from cosmaneura_billing.athena_client import (
    ATHENA_PREVIEW_INTROSPECTION_URL,
    ATHENA_PREVIEW_ORIGIN,
    ATHENA_PREVIEW_PRACTICE_ID,
    ATHENA_PREVIEW_SCOPE,
    ATHENA_PREVIEW_TOKEN_URL,
    AthenaSchemaError,
    MockAthenaClient,
)
from cosmaneura_billing.athena_sync import (
    ATHENA_PREVIEW_ENCOUNTER_FEED,
    ATHENA_PREVIEW_SOURCE_SYSTEM,
    AthenaSyncConfig,
    AthenaSyncStatus,
    _run_mock_pages,
    main,
    run_once,
)
from cosmaneura_billing.settings import SettingsError
from cosmaneura_billing.source_import import MemorySourceImportStore
from cosmaneura_billing.source_import import source_import_external_id


def _live_env(**changes: str) -> dict[str, str]:
    values = {
        "COSMANEURA_ATHENA_SYNC_ENABLED": "true",
        "COSMANEURA_ATHENA_MODE": "live",
        "COSMANEURA_ATHENA_BASE_URL": ATHENA_PREVIEW_ORIGIN,
        "COSMANEURA_ATHENA_TOKEN_URL": ATHENA_PREVIEW_TOKEN_URL,
        "COSMANEURA_ATHENA_CLIENT_ID": "synthetic-client-id",
        "COSMANEURA_ATHENA_SCOPES": ATHENA_PREVIEW_SCOPE,
        "COSMANEURA_ATHENA_CLIENT_SECRET": "synthetic-client-secret",
        "COSMANEURA_ATHENA_IMPORT_DB_ROLE": "cosmaneura_athena_import",
        "COSMANEURA_ATHENA_IMPORT_DATABASE_URL": (
            "postgresql://cosmaneura_athena_import:synthetic-db-secret@"
            "database.invalid/cosmaneura?sslmode=require"
        ),
        "COSMANEURA_PRACTICE_ID": "server-practice",
    }
    values.update(changes)
    return values


def test_defaults_are_inert_and_do_not_fall_back_to_api_settings():
    config = AthenaSyncConfig.from_env({
        "DATABASE_URL": "postgresql://api:secret@database.invalid/app",
        "AZURE_CLIENT_ID": "api-managed-identity",
    })

    assert config.enabled is False
    assert config.mode == "disabled"
    assert config.base_url == ""
    assert config.client_secret == ""
    assert config.import_database_url == ""
    assert run_once(config).status is AthenaSyncStatus.DISABLED


def test_mock_requires_explicit_enable_and_imports_four():
    disabled = AthenaSyncConfig(
        enabled=False,
        mode="mock",
        target_practice_id="server-practice",
    )
    assert run_once(disabled).status is AthenaSyncStatus.DISABLED

    result = run_once(AthenaSyncConfig(
        enabled=True,
        mode="mock",
        target_practice_id="server-practice",
    ))
    assert result.status is AthenaSyncStatus.COMPLETED
    assert result.code == "ATHENA_MOCK_COMPLETE"
    assert result.page_count == 2
    assert result.fetched_count == 4
    assert result.replayed_count == 0
    assert result.imported_count == 4
    assert result.exit_code == 0


def test_mock_configuration_rejects_non_mock_client_before_any_read():
    class NetworkCapableClient:
        mode = "athena_sandbox"

        def read_page(self, cursor=None):
            raise AssertionError("non-mock client must never be invoked")

    assert "client" not in inspect.signature(run_once).parameters
    result = _run_mock_pages(
        client=NetworkCapableClient(),
        store=MemorySourceImportStore(practice_id="server-practice"),
    )

    assert result.status is AthenaSyncStatus.ADAPTER_ERROR
    assert result.fetched_count == 0


def test_mock_cli_output_is_count_and_code_only():
    stream = io.StringIO()
    exit_code = main(
        ["--once"],
        environ={
            "COSMANEURA_ATHENA_SYNC_ENABLED": "true",
            "COSMANEURA_ATHENA_MODE": "mock",
            "COSMANEURA_PRACTICE_ID": "sensitive-practice-identifier",
        },
        stream=stream,
    )

    output = stream.getvalue()
    parsed = json.loads(output)
    assert exit_code == 0
    assert parsed == {
        "code": "ATHENA_MOCK_COMPLETE",
        "fetched_count": 4,
        "imported_count": 4,
        "page_count": 2,
        "replayed_count": 0,
        "status": "completed",
    }
    assert "sensitive-practice-identifier" not in output
    assert "SYN-RC" not in output
    assert "cursor" not in output


def test_mock_sync_replays_exactly_and_resumes_from_durable_cursor():
    config = AthenaSyncConfig(
        enabled=True,
        mode="mock",
        target_practice_id="server-practice",
    )
    store = MemorySourceImportStore(practice_id="server-practice")

    first = run_once(config, import_store=store)
    replay = run_once(config, import_store=store)

    assert first.imported_count == 4
    assert first.replayed_count == 0
    assert replay.imported_count == 0
    assert replay.replayed_count == 4

    resume_store = MemorySourceImportStore(practice_id="server-practice")
    base = MockAthenaClient(server_practice_id="server-practice")

    class FailAfterFirstPage:
        mode = "athena_mock"

        def read_page(self, cursor=None):
            if cursor is not None:
                raise AthenaSchemaError()
            return base.read_page(cursor)

    failed = _run_mock_pages(
        client=FailAfterFirstPage(),
        store=resume_store,
    )
    checkpoint = resume_store.load_checkpoint(
        source_system=ATHENA_PREVIEW_SOURCE_SYSTEM,
        feed=ATHENA_PREVIEW_ENCOUNTER_FEED,
    )
    assert failed.status is AthenaSyncStatus.ADAPTER_ERROR
    assert failed.page_count == 1
    assert failed.fetched_count == 2
    assert failed.imported_count == 2
    assert checkpoint is not None
    assert checkpoint.next_cursor == "mock-cursor-v1-2"

    resumed = run_once(config, import_store=resume_store)
    assert resumed.status is AthenaSyncStatus.COMPLETED
    assert resumed.page_count == 1
    assert resumed.fetched_count == 2
    assert resumed.imported_count == 2


def test_cyclic_cursor_page_is_rejected_before_records_or_checkpoint_commit():
    config = AthenaSyncConfig(
        enabled=True,
        mode="mock",
        target_practice_id="server-practice",
    )
    store = MemorySourceImportStore(practice_id="server-practice")
    base = MockAthenaClient(server_practice_id="server-practice")
    first_page = base.read_page()
    second_page = base.read_page(first_page.next_cursor)

    class CyclicCursorClient:
        mode = "athena_mock"

        def read_page(self, cursor=None):
            if cursor is None:
                return first_page
            return replace(second_page, next_cursor=first_page.next_cursor)

    result = _run_mock_pages(client=CyclicCursorClient(), store=store)

    assert result.status is AthenaSyncStatus.ADAPTER_ERROR
    assert (result.page_count, result.fetched_count, result.imported_count) == (
        1, 2, 2,
    )
    checkpoint = store.load_checkpoint(
        source_system=ATHENA_PREVIEW_SOURCE_SYSTEM,
        feed=ATHENA_PREVIEW_ENCOUNTER_FEED,
    )
    assert checkpoint is not None
    assert checkpoint.checkpoint == first_page.checkpoint
    assert checkpoint.next_cursor == first_page.next_cursor
    second_record_id = source_import_external_id(
        practice_id="server-practice",
        source_system=ATHENA_PREVIEW_SOURCE_SYSTEM,
        feed=ATHENA_PREVIEW_ENCOUNTER_FEED,
        source_record_id=second_page.records[0].source_record_id,
    )
    assert store.get_encounter(second_record_id) is None


def test_resumed_cursor_cycle_is_rejected_before_cycle_page_commit():
    config = AthenaSyncConfig(
        enabled=True,
        mode="mock",
        target_practice_id="server-practice",
    )
    store = MemorySourceImportStore(practice_id="server-practice")
    base = MockAthenaClient(server_practice_id="server-practice")
    first_page = base.read_page()
    store.import_encounter_page(
        source_system=ATHENA_PREVIEW_SOURCE_SYSTEM,
        feed=ATHENA_PREVIEW_ENCOUNTER_FEED,
        provenance="athena_preview_synthetic",
        mapping_version=first_page.records[0].mapping_version,
        records=first_page.records,
        checkpoint=first_page.checkpoint,
        next_cursor=first_page.next_cursor,
    )
    second_page = replace(
        base.read_page(first_page.next_cursor),
        next_cursor="mock-cursor-v1-cycle",
    )
    cycle_page = replace(
        first_page,
        checkpoint="mock-checkpoint-cycle-must-not-commit",
        next_cursor=first_page.next_cursor,
    )

    class ResumedCycleClient:
        mode = "athena_mock"

        def read_page(self, cursor=None):
            if cursor == first_page.next_cursor:
                return second_page
            if cursor == second_page.next_cursor:
                return cycle_page
            raise AssertionError("unexpected cursor")

    result = _run_mock_pages(client=ResumedCycleClient(), store=store)

    assert result.status is AthenaSyncStatus.ADAPTER_ERROR
    assert (result.page_count, result.fetched_count, result.imported_count) == (
        1, 2, 2,
    )
    checkpoint = store.load_checkpoint(
        source_system=ATHENA_PREVIEW_SOURCE_SYSTEM,
        feed=ATHENA_PREVIEW_ENCOUNTER_FEED,
    )
    assert checkpoint is not None
    assert checkpoint.checkpoint == second_page.checkpoint
    assert checkpoint.next_cursor == second_page.next_cursor


def test_second_page_import_failure_reports_only_prior_committed_counts():
    committed_pages = 0

    def fail_second_page() -> None:
        nonlocal committed_pages
        committed_pages += 1
        if committed_pages == 2:
            raise RuntimeError("synthetic second-page failure")

    result = run_once(
        AthenaSyncConfig(
            enabled=True,
            mode="mock",
            target_practice_id="server-practice",
        ),
        import_store=MemorySourceImportStore(
            practice_id="server-practice",
            transaction_check=fail_second_page,
        ),
    )

    assert result.status is AthenaSyncStatus.IMPORT_ERROR
    assert result.as_dict() == {
        "code": "ATHENA_IMPORT_ERROR",
        "fetched_count": 2,
        "imported_count": 2,
        "page_count": 1,
        "replayed_count": 0,
        "status": "import_error",
    }


def test_mock_import_failure_returns_only_static_code_and_zero_counts():
    def fail_before_commit() -> None:
        raise RuntimeError("cursor-and-record-values-must-not-escape")

    result = run_once(
        AthenaSyncConfig(
            enabled=True,
            mode="mock",
            target_practice_id="server-practice",
        ),
        import_store=MemorySourceImportStore(
            practice_id="server-practice",
            transaction_check=fail_before_commit,
        ),
    )

    assert result.status is AthenaSyncStatus.IMPORT_ERROR
    assert result.as_dict() == {
        "code": "ATHENA_IMPORT_ERROR",
        "fetched_count": 0,
        "imported_count": 0,
        "page_count": 0,
        "replayed_count": 0,
        "status": "import_error",
    }
    assert "cursor-and-record-values" not in repr(result)


def test_enabled_sync_cannot_use_disabled_mode_or_empty_practice():
    with pytest.raises(SettingsError, match="MODE=mock or live"):
        AthenaSyncConfig(
            enabled=True,
            mode="disabled",
            target_practice_id="practice-a",
        )
    with pytest.raises(SettingsError, match="PRACTICE_ID"):
        AthenaSyncConfig(enabled=True, mode="mock", target_practice_id="")
    with pytest.raises(SettingsError, match="PRACTICE_ID"):
        AthenaSyncConfig(
            enabled=True,
            mode="mock",
            target_practice_id="invalid-\ud800-practice",
        )


def test_mock_mode_rejects_all_live_credentials():
    with pytest.raises(SettingsError, match="must not receive live credentials"):
        AthenaSyncConfig.from_env({
            "COSMANEURA_ATHENA_MODE": "mock",
            "COSMANEURA_PRACTICE_ID": "server-practice",
            **{
                key: value
                for key, value in _live_env().items()
                if key.startswith("COSMANEURA_ATHENA_")
                and key != "COSMANEURA_ATHENA_MODE"
            },
        })


def test_partial_live_configuration_fails_without_echoing_secret():
    secret = "credential-that-must-not-escape"
    with pytest.raises(SettingsError) as caught:
        AthenaSyncConfig.from_env({
            "COSMANEURA_ATHENA_MODE": "live",
            "COSMANEURA_ATHENA_CLIENT_SECRET": secret,
        })
    assert "must be configured together" in str(caught.value)
    assert secret not in str(caught.value)


def test_complete_live_config_is_validated_redacted_and_execution_refused(
    monkeypatch,
):
    def network_is_a_test_failure(*args, **kwargs):
        raise AssertionError("live sync must not open a socket in this slice")

    monkeypatch.setattr(urllib.request, "urlopen", network_is_a_test_failure)
    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        network_is_a_test_failure,
    )
    monkeypatch.setattr(socket, "create_connection", network_is_a_test_failure)
    config = AthenaSyncConfig.from_env(_live_env())
    rendered = repr(config)
    assert config.mode == "live"
    assert config.scopes == (ATHENA_PREVIEW_SCOPE,)
    assert config.target_practice_id == "server-practice"
    assert config.vendor_practice_id == ATHENA_PREVIEW_PRACTICE_ID
    assert config.introspection_url == ATHENA_PREVIEW_INTROSPECTION_URL
    assert "synthetic-client-secret" not in rendered
    assert "synthetic-db-secret" not in rendered
    assert "<redacted>" in rendered

    result = run_once(config)
    assert result.status is AthenaSyncStatus.LIVE_NOT_IMPLEMENTED
    assert result.code == "ATHENA_LIVE_NOT_IMPLEMENTED"
    assert result.exit_code == 2
    assert result.fetched_count == 0
    assert result.imported_count == 0


@pytest.mark.parametrize(
    "changes,match",
    [
        (
            {"COSMANEURA_ATHENA_BASE_URL": "http://unsafe.example/athena"},
            "BASE_URL",
        ),
        (
            {"COSMANEURA_ATHENA_TOKEN_URL": "https://user:pw@auth.example/token"},
            "TOKEN_URL",
        ),
        (
            {"COSMANEURA_ATHENA_CLIENT_ID": "client id with spaces"},
            "CLIENT_ID",
        ),
        (
            {"COSMANEURA_ATHENA_SCOPES": "same.read,same.read"},
            "exact reviewed Preview scope",
        ),
        (
            {
                "COSMANEURA_ATHENA_BASE_URL": (
                    ATHENA_PREVIEW_ORIGIN + "/unreviewed"
                )
            },
            "fixed Preview origin",
        ),
        (
            {
                "COSMANEURA_ATHENA_TOKEN_URL": (
                    ATHENA_PREVIEW_ORIGIN + "/oauth2/token"
                )
            },
            "fixed Preview token URL",
        ),
    ],
)
def test_live_configuration_rejects_unsafe_metadata(changes, match):
    with pytest.raises(SettingsError, match=match):
        AthenaSyncConfig.from_env(_live_env(**changes))


@pytest.mark.parametrize("role", ["bad:role", "pg_import", "postgres"])
def test_import_database_role_must_be_a_safe_non_system_login(role):
    with pytest.raises(SettingsError, match="IMPORT_DB_ROLE"):
        AthenaSyncConfig(
            enabled=True,
            mode="mock",
            target_practice_id="server-practice",
            import_database_role=role,
        )


def test_invalid_boolean_and_mode_return_static_configuration_output():
    for environ in (
        {"COSMANEURA_ATHENA_SYNC_ENABLED": "sometimes"},
        {"COSMANEURA_ATHENA_MODE": "auto"},
    ):
        stream = io.StringIO()
        exit_code = main([], environ=environ, stream=stream)
        assert exit_code == 2
        assert json.loads(stream.getvalue()) == {
            "code": "ATHENA_CONFIGURATION_ERROR",
            "fetched_count": 0,
            "imported_count": 0,
            "page_count": 0,
            "replayed_count": 0,
            "status": "configuration_error",
        }


def test_unrecognized_cli_arguments_cannot_select_hidden_behavior():
    stream = io.StringIO()
    assert main(["--live"], environ=_live_env(), stream=stream) == 2
    assert json.loads(stream.getvalue())["code"] == "ATHENA_CONFIGURATION_ERROR"


def test_live_cli_never_echoes_credential_or_endpoint():
    stream = io.StringIO()
    exit_code = main([], environ=_live_env(), stream=stream)
    output = stream.getvalue()
    assert exit_code == 2
    assert json.loads(output)["code"] == "ATHENA_LIVE_NOT_IMPLEMENTED"
    assert "synthetic-client-secret" not in output
    assert "athenahealth" not in output
