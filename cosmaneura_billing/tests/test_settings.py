"""Central runtime settings: parsing, validation, and secret handling."""
from pathlib import Path

import pytest

from cosmaneura_billing.settings import Settings, SettingsError, get_settings


def test_defaults_preserve_local_runtime_behavior():
    settings = Settings.from_env({})

    assert settings.environment == "development"
    assert settings.log_level == "INFO"
    assert settings.anthropic_api_key == ""
    assert settings.anthropic_model == "claude-opus-5"
    assert settings.anthropic_effort == "high"
    assert settings.openai_api_key == ""
    assert settings.database_url == ""
    assert settings.chase_dsn == ""
    assert settings.chase_store == "memory"
    assert settings.icd10_ccsr_csv is None
    assert settings.cpt_data_dir is None
    assert settings.cpt_auto_fetch is True
    assert settings.cpt_cache_dir == Path.home() / ".cache" / "cosmaneura" / "cpt"
    assert settings.ama_credentials is None
    assert settings.feedback_path == "coder_feedback.jsonl"
    assert settings.preference_min_count == 3
    assert settings.preference_global_min_users == 3
    assert settings.preferences_enabled is True
    assert settings.auth_mode == "disabled"
    assert settings.cors_origins == (
        "http://127.0.0.1:5173", "http://localhost:5173",
    )
    assert settings.frontend_dist is None
    assert settings.migrations_dir is None


def test_from_env_parses_paths_numbers_booleans_and_platform_fields(tmp_path):
    settings = Settings.from_env({
        "COSMANEURA_ENV": "STAGING",
        "COSMANEURA_AUTH_MODE": "container_apps",
        "COSMANEURA_ENTRA_TENANT_ID": "11111111-1111-4111-8111-111111111111",
        "COSMANEURA_LOG_LEVEL": "debug",
        "COSMANEURA_FRONTEND_DIST": str(tmp_path / "frontend"),
        "COSMANEURA_MIGRATIONS_DIR": str(tmp_path / "migrations"),
        "ANTHROPIC_MODEL": "test-model",
        "ANTHROPIC_EFFORT": "medium",
        "CPT_AUTO_FETCH": "no",
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "ICD10_CCSR_CSV": str(tmp_path / "ccsr.csv"),
        "CPT_DATA_DIR": str(tmp_path / "cpt"),
        "COSMANEURA_FEEDBACK_PATH": str(tmp_path / "feedback.jsonl"),
        "COSMANEURA_PREF_MIN": "4",
        "COSMANEURA_PREF_GLOBAL_USERS": "5",
        "COSMANEURA_PREFS": "off",
        "COSMANEURA_CHASE_STORE": "POSTGRES",
        "COSMANEURA_CHASE_DATABASE_URL": "postgresql://chase.invalid/db",
        "DATABASE_URL": "postgresql://primary.invalid/db",
        "COSMANEURA_PRACTICE_ID": "practice-a",
        "COSMANEURA_PRACTICE_NAME": "Practice A",
        "COSMANEURA_UPLOAD_URL": "https://practice.invalid/upload",
        "COSMANEURA_CHASE_APPROVAL": "per_message",
        "COSMANEURA_DIRECT_CONTRACT_LIST": str(tmp_path / "direct.csv"),
    })

    assert settings.environment == "staging"
    assert settings.log_level == "DEBUG"
    assert settings.frontend_dist == str(tmp_path / "frontend")
    assert settings.migrations_dir == str(tmp_path / "migrations")
    assert settings.anthropic_model == "test-model"
    assert settings.anthropic_effort == "medium"
    assert settings.cpt_auto_fetch is False
    assert settings.cpt_cache_dir == tmp_path / "cache" / "cosmaneura" / "cpt"
    assert settings.icd10_ccsr_csv == str(tmp_path / "ccsr.csv")
    assert settings.cpt_data_dir == str(tmp_path / "cpt")
    assert settings.feedback_path == str(tmp_path / "feedback.jsonl")
    assert settings.preference_min_count == 4
    assert settings.preference_global_min_users == 5
    assert settings.preferences_enabled is False
    assert settings.auth_mode == "container_apps"
    assert settings.entra_tenant_id == "11111111-1111-4111-8111-111111111111"
    assert settings.chase_store == "postgres"
    assert settings.chase_dsn == "postgresql://chase.invalid/db"
    assert settings.practice_id == "practice-a"
    assert settings.practice_name == "Practice A"
    assert settings.chase_approval == "per_message"
    assert settings.direct_contract_list == str(tmp_path / "direct.csv")


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_boolean_true_spellings(value):
    assert Settings.from_env({"COSMANEURA_PREFS": value}).preferences_enabled


@pytest.mark.parametrize("name,value", [
    ("CPT_AUTO_FETCH", "sometimes"),
    ("COSMANEURA_PREFS", "maybe"),
    ("COSMANEURA_PREF_MIN", "zero-ish"),
    ("COSMANEURA_PREF_GLOBAL_USERS", "0"),
    ("COSMANEURA_LOG_LEVEL", "verbose"),
    ("COSMANEURA_ENV", "invalid-env"),
    ("COSMANEURA_CHASE_STORE", "filesystem"),
    ("COSMANEURA_CHASE_APPROVAL", "always"),
])
def test_invalid_values_raise_safe_specific_errors(name, value):
    with pytest.raises(SettingsError) as exc_info:
        Settings.from_env({name: value})
    assert name in str(exc_info.value)
    assert value not in str(exc_info.value)


def test_ama_credentials_are_all_or_none_and_error_does_not_echo_secret():
    secret = "consumer-secret-must-not-leak"
    with pytest.raises(SettingsError) as exc_info:
        Settings.from_env({
            "AMA_TOKEN_URL": "https://gateway.example/oauth2/token",
            "AMA_CONSUMER_SECRET": secret,
        })

    message = str(exc_info.value)
    assert "AMA_BASE_URL" in message
    assert "AMA_CONSUMER_KEY" in message
    assert secret not in message


def test_complete_ama_credentials_resolve_with_configurable_paths():
    settings = Settings.from_env({
        "AMA_TOKEN_URL": "https://gateway.example/oauth2/token",
        "AMA_BASE_URL": "https://gateway.example",
        "AMA_CONSUMER_KEY": "key-value",
        "AMA_CONSUMER_SECRET": "secret-value",
        "AMA_RELEASES_PATH": "/custom/releases",
        "AMA_FILE_PATH": "/custom/files/{release}",
    })

    assert settings.ama_configured is True
    assert settings.ama_credentials is not None
    assert settings.ama_credentials.base_url == "https://gateway.example"
    assert settings.ama_releases_path == "/custom/releases"
    assert settings.ama_file_path == "/custom/files/{release}"


def test_repr_redacts_every_credential_and_database_dsn():
    secrets = {
        "ANTHROPIC_API_KEY": "anthropic-secret",
        "OPENAI_API_KEY": "openai-secret",
        "DATABASE_URL": "postgresql://user:db-password@example/db",
        "COSMANEURA_CHASE_DATABASE_URL": "postgresql://user:other-password@example/db",
        "AMA_TOKEN_URL": "https://gateway.example/oauth2/token",
        "AMA_BASE_URL": "https://gateway.example",
        "AMA_CONSUMER_KEY": "ama-key-secret",
        "AMA_CONSUMER_SECRET": "ama-consumer-secret",
        "COSMANEURA_UPLOAD_URL": "https://upload.example/secret-link-token",
    }
    settings = Settings.from_env(secrets)

    rendered = repr(settings)
    credentials_rendered = repr(settings.ama_credentials)
    for value in secrets.values():
        if value.startswith("https://gateway.example"):
            continue
        assert value not in rendered
        assert value not in credentials_rendered
    assert "<redacted>" in rendered


def test_get_settings_reads_current_environment_without_global_cache(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "first-model")
    assert get_settings().anthropic_model == "first-model"
    monkeypatch.setenv("ANTHROPIC_MODEL", "second-model")
    assert get_settings().anthropic_model == "second-model"


def test_deployed_environments_require_complete_authentication():
    with pytest.raises(SettingsError, match="authentication cannot be disabled"):
        Settings(environment="production")
    with pytest.raises(SettingsError, match="tenant UUID"):
        Settings(
            environment="production",
            auth_mode="container_apps",
            practice_id="practice-a",
        )


@pytest.mark.parametrize(
    "origin", ["*", "file:///tmp", "https://host/", "https://host/path"],
)
def test_cors_requires_exact_http_origins(origin):
    with pytest.raises(SettingsError, match="exact HTTP"):
        Settings(cors_origins=(origin,))
