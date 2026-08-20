"""Fail-closed one-shot Athena sync skeleton."""
from __future__ import annotations

import io
import json
import socket
import urllib.request

import pytest

from cosmaneura_billing.athena_sync import (
    AthenaSyncConfig,
    AthenaSyncStatus,
    main,
    run_once,
)
from cosmaneura_billing.settings import SettingsError


def _live_env(**changes: str) -> dict[str, str]:
    values = {
        "COSMANEURA_ATHENA_SYNC_ENABLED": "true",
        "COSMANEURA_ATHENA_MODE": "live",
        "COSMANEURA_ATHENA_BASE_URL": (
            "https://reviewed-api.invalid/base"
        ),
        "COSMANEURA_ATHENA_TOKEN_URL": (
            "https://reviewed-auth.invalid/token"
        ),
        "COSMANEURA_ATHENA_CLIENT_ID": "synthetic-client-id",
        "COSMANEURA_ATHENA_SCOPES": (
            "fixture/read-one,fixture/read-two"
        ),
        "COSMANEURA_ATHENA_CLIENT_SECRET": "synthetic-client-secret",
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
    assert run_once(config).status is AthenaSyncStatus.DISABLED


def test_mock_requires_explicit_enable_and_reads_four_without_importing():
    disabled = AthenaSyncConfig(
        enabled=False,
        mode="mock",
        practice_id="server-practice",
    )
    assert run_once(disabled).status is AthenaSyncStatus.DISABLED

    result = run_once(AthenaSyncConfig(
        enabled=True,
        mode="mock",
        practice_id="server-practice",
    ))
    assert result.status is AthenaSyncStatus.COMPLETED
    assert result.code == "ATHENA_MOCK_COMPLETE"
    assert result.page_count == 2
    assert result.fetched_count == 4
    assert result.replayed_count == 0
    assert result.imported_count == 0
    assert result.exit_code == 0


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
        "imported_count": 0,
        "page_count": 2,
        "replayed_count": 0,
        "status": "completed",
    }
    assert "sensitive-practice-identifier" not in output
    assert "SYN-RC" not in output
    assert "cursor" not in output


def test_enabled_sync_cannot_use_disabled_mode_or_empty_practice():
    with pytest.raises(SettingsError, match="MODE=mock or live"):
        AthenaSyncConfig(enabled=True, mode="disabled", practice_id="practice-a")
    with pytest.raises(SettingsError, match="PRACTICE_ID"):
        AthenaSyncConfig(enabled=True, mode="mock", practice_id="")


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
    assert config.scopes == (
        "fixture/read-one",
        "fixture/read-two",
    )
    assert "synthetic-client-secret" not in rendered
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
            "unique explicit scopes",
        ),
    ],
)
def test_live_configuration_rejects_unsafe_metadata(changes, match):
    with pytest.raises(SettingsError, match=match):
        AthenaSyncConfig.from_env(_live_env(**changes))


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
