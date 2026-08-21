"""Central runtime settings: parsing, validation, and secret handling."""
from pathlib import Path

import pytest

from cosmaneura_billing.settings import Settings, SettingsError, get_settings


def test_defaults_preserve_local_runtime_behavior():
    settings = Settings.from_env({})

    assert settings.environment == "development"
    assert settings.log_level == "INFO"
    assert settings.azure_openai_endpoint == ""
    assert settings.azure_openai_deployment == ""
    assert settings.azure_openai_model == ""
    assert settings.azure_openai_api_version == "2025-04-01-preview"
    assert settings.openai_api_key == ""
    assert settings.database_url == ""
    assert settings.state_store == "local"
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
    assert settings.telemetry_enabled is False
    assert settings.application_insights_connection_string == ""
    assert settings.azure_client_id == ""
    assert settings.azure_openai_enabled is False
    assert settings.azure_openai_synthetic_only is False
    assert settings.patient_messaging_enabled is False
    assert settings.patient_messaging_operationally_activated is False
    assert settings.chase_scheduler_enabled is False
    assert settings.chase_transport == "record_only"
    assert settings.acs_email_connection_string == ""
    assert settings.acs_email_endpoint == ""
    assert settings.acs_email_sender_address == ""
    assert settings.acs_email_reply_to_address == ""
    assert settings.practice_timezone == "UTC"
    assert settings.contact_snapshot_max_age_hours == 24
    assert settings.chase_worker_batch_size == 25
    assert settings.chase_worker_lease_seconds == 300
    assert settings.chase_gate_hmac_key == ""


def test_from_env_parses_paths_numbers_booleans_and_platform_fields(tmp_path):
    settings = Settings.from_env({
        "COSMANEURA_ENV": "STAGING",
        "COSMANEURA_AUTH_MODE": "container_apps",
        "COSMANEURA_ENTRA_TENANT_ID": "11111111-1111-4111-8111-111111111111",
        "COSMANEURA_LOG_LEVEL": "debug",
        "COSMANEURA_TELEMETRY_ENABLED": "true",
        "APPLICATIONINSIGHTS_CONNECTION_STRING": (
            "InstrumentationKey=22222222-2222-4222-8222-222222222222;"
            "IngestionEndpoint=https://example.applicationinsights.azure.com/"
        ),
        "AZURE_CLIENT_ID": "33333333-3333-4333-8333-333333333333",
        "COSMANEURA_AZURE_OPENAI_ENABLED": "true",
        "COSMANEURA_AZURE_OPENAI_SYNTHETIC_ONLY": "true",
        "AZURE_OPENAI_ENDPOINT": "https://cab-openai.openai.azure.com",
        "AZURE_OPENAI_DEPLOYMENT": "cab-gpt",
        "AZURE_OPENAI_MODEL": "gpt-5.6-sol",
        "AZURE_OPENAI_API_VERSION": "2025-04-01-preview",
        "COSMANEURA_FRONTEND_DIST": str(tmp_path / "frontend"),
        "COSMANEURA_MIGRATIONS_DIR": str(tmp_path / "migrations"),
        "CPT_AUTO_FETCH": "no",
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "ICD10_CCSR_CSV": str(tmp_path / "ccsr.csv"),
        "CPT_DATA_DIR": str(tmp_path / "cpt"),
        "COSMANEURA_FEEDBACK_PATH": str(tmp_path / "feedback.jsonl"),
        "COSMANEURA_PREF_MIN": "4",
        "COSMANEURA_PREF_GLOBAL_USERS": "5",
        "COSMANEURA_PREFS": "off",
        "COSMANEURA_STATE_STORE": "POSTGRES",
        "COSMANEURA_CHASE_STORE": "POSTGRES",
        "DATABASE_URL": "postgresql://cosmaneura_app@primary.invalid/db",
        "COSMANEURA_PRACTICE_ID": "practice-a",
        "COSMANEURA_PRACTICE_NAME": "Practice A",
        "COSMANEURA_UPLOAD_URL": "https://upload.cosmaneura.dev/cards",
        "COSMANEURA_CHASE_APPROVAL": "per_message",
        "COSMANEURA_DIRECT_CONTRACT_LIST": str(tmp_path / "direct.csv"),
        "COSMANEURA_CHASE_GATE_HMAC_KEY": "g" * 32,
        "COSMANEURA_PATIENT_MESSAGING_ENABLED": "true",
        "COSMANEURA_PATIENT_MESSAGING_OPERATIONALLY_ACTIVATED": "true",
        "COSMANEURA_CHASE_SCHEDULER_ENABLED": "true",
        "COSMANEURA_CHASE_TRANSPORT": "ACS_EMAIL",
        "COSMANEURA_ACS_EMAIL_ENDPOINT": (
            "https://pilot.communication.azure.com"
        ),
        "COSMANEURA_ACS_EMAIL_SENDER_ADDRESS": "DoNotReply@pilot.example",
        "COSMANEURA_ACS_EMAIL_REPLY_TO_ADDRESS": "billing@pilot.example",
        "COSMANEURA_PRACTICE_TIMEZONE": "America/New_York",
        "COSMANEURA_CONTACT_SNAPSHOT_MAX_AGE_HOURS": "48",
        "COSMANEURA_CHASE_WORKER_BATCH_SIZE": "40",
        "COSMANEURA_CHASE_WORKER_LEASE_SECONDS": "600",
    })

    assert settings.environment == "staging"
    assert settings.log_level == "DEBUG"
    assert settings.frontend_dist == str(tmp_path / "frontend")
    assert settings.migrations_dir == str(tmp_path / "migrations")
    assert settings.telemetry_enabled is True
    assert settings.azure_client_id == "33333333-3333-4333-8333-333333333333"
    assert settings.azure_openai_enabled is True
    assert settings.azure_openai_synthetic_only is True
    assert settings.azure_openai_endpoint == "https://cab-openai.openai.azure.com"
    assert settings.azure_openai_deployment == "cab-gpt"
    assert settings.azure_openai_model == "gpt-5.6-sol"
    assert settings.azure_openai_api_version == "2025-04-01-preview"
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
    assert settings.state_store == "postgres"
    assert settings.chase_store == "postgres"
    assert settings.chase_dsn == "postgresql://cosmaneura_app@primary.invalid/db"
    assert settings.practice_id == "practice-a"
    assert settings.practice_name == "Practice A"
    assert settings.chase_approval == "per_message"
    assert settings.direct_contract_list == str(tmp_path / "direct.csv")
    assert settings.chase_gate_hmac_key == "g" * 32
    assert settings.patient_messaging_enabled is True
    assert settings.patient_messaging_operationally_activated is True
    assert settings.chase_scheduler_enabled is True
    assert settings.chase_transport == "acs_email"
    assert settings.acs_email_connection_string == ""
    assert settings.acs_email_endpoint == "https://pilot.communication.azure.com"
    assert settings.practice_timezone == "America/New_York"
    assert settings.contact_snapshot_max_age_hours == 48
    assert settings.chase_worker_batch_size == 40
    assert settings.chase_worker_lease_seconds == 600


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
    ("COSMANEURA_STATE_STORE", "filesystem"),
    ("COSMANEURA_CHASE_STORE", "filesystem"),
    ("COSMANEURA_CHASE_APPROVAL", "always"),
    ("COSMANEURA_CHASE_TRANSPORT", "sms"),
    ("COSMANEURA_PATIENT_MESSAGING_ENABLED", "maybe"),
    ("COSMANEURA_PATIENT_MESSAGING_OPERATIONALLY_ACTIVATED", "maybe"),
    ("COSMANEURA_CHASE_SCHEDULER_ENABLED", "maybe"),
    ("COSMANEURA_AZURE_OPENAI_ENABLED", "maybe"),
    ("COSMANEURA_AZURE_OPENAI_SYNTHETIC_ONLY", "maybe"),
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
        "OPENAI_API_KEY": "openai-secret",
        "DATABASE_URL": "postgresql://user:db-password@example/db",
        "COSMANEURA_CHASE_DATABASE_URL": "postgresql://user:other-password@example/db",
        "AMA_TOKEN_URL": "https://gateway.example/oauth2/token",
        "AMA_BASE_URL": "https://gateway.example",
        "AMA_CONSUMER_KEY": "ama-key-secret",
        "AMA_CONSUMER_SECRET": "ama-consumer-secret",
        "COSMANEURA_UPLOAD_URL": "https://upload.example/secret-link-token",
        "COSMANEURA_ACS_EMAIL_CONNECTION_STRING": (
            "endpoint=https://pilot.communication.azure.com;"
            "accesskey=acs-secret-key="
        ),
        "COSMANEURA_ACS_EMAIL_SENDER_ADDRESS": "sender-secret@pilot.example",
        "COSMANEURA_ACS_EMAIL_REPLY_TO_ADDRESS": "reply-secret@pilot.example",
        "COSMANEURA_CHASE_GATE_HMAC_KEY": "gate-secret-that-is-at-least-32-bytes",
        "APPLICATIONINSIGHTS_CONNECTION_STRING": (
            "InstrumentationKey=22222222-2222-4222-8222-222222222222;"
            "IngestionEndpoint=https://example.applicationinsights.azure.com/"
        ),
    }
    settings = Settings.from_env({
        **secrets,
        "COSMANEURA_ACS_EMAIL_ENDPOINT": (
            "https://pilot.communication.azure.com"
        ),
    })

    rendered = repr(settings)
    credentials_rendered = repr(settings.ama_credentials)
    non_secret_settings = {"AMA_TOKEN_URL", "AMA_BASE_URL"}
    for key, value in secrets.items():
        if key in non_secret_settings:
            continue
        assert value not in rendered
        assert value not in credentials_rendered
    assert "<redacted>" in rendered


def test_azure_monitor_telemetry_requires_deployed_uami_configuration():
    connection = (
        "InstrumentationKey=22222222-2222-4222-8222-222222222222;"
        "IngestionEndpoint=https://example.applicationinsights.azure.com/"
    )
    with pytest.raises(SettingsError, match="staging or production"):
        Settings(
            telemetry_enabled=True,
            application_insights_connection_string=connection,
            azure_client_id="33333333-3333-4333-8333-333333333333",
        )
    with pytest.raises(SettingsError, match="AZURE_CLIENT_ID"):
        _production_messaging_settings(
            telemetry_enabled=True,
            application_insights_connection_string=connection,
            azure_client_id="patient-Jane",
        )


def test_azure_monitor_connection_error_never_echoes_configuration():
    unsafe = "InstrumentationKey=synthetic-secret;IngestionEndpoint=http://bad"
    with pytest.raises(SettingsError, match="malformed") as exc_info:
        _production_messaging_settings(
            telemetry_enabled=True,
            application_insights_connection_string=unsafe,
            azure_client_id="33333333-3333-4333-8333-333333333333",
        )
    assert unsafe not in str(exc_info.value)


def test_get_settings_reads_current_environment_without_global_cache(monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_ID", "33333333-3333-4333-8333-333333333333")
    monkeypatch.setenv(
        "AZURE_OPENAI_ENDPOINT", "https://cab-openai.openai.azure.com",
    )
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "cab-gpt")
    monkeypatch.setenv("AZURE_OPENAI_MODEL", "gpt-4.1-mini")
    assert get_settings().azure_openai_model == "gpt-4.1-mini"
    monkeypatch.setenv("AZURE_OPENAI_MODEL", "gpt-5-mini")
    assert get_settings().azure_openai_model == "gpt-5-mini"


def test_model_configuration_rejects_free_form_labels_without_echoing_them():
    unsafe = "gpt-4.1-mini-jane-patient"
    with pytest.raises(SettingsError, match="supported model ID shape") as exc_info:
        Settings(
            azure_client_id="33333333-3333-4333-8333-333333333333",
            azure_openai_endpoint="https://cab-openai.openai.azure.com",
            azure_openai_deployment="cab-gpt",
            azure_openai_model=unsafe,
        )
    assert unsafe not in str(exc_info.value)


def test_enabled_azure_openai_settings_are_complete_and_managed_identity_only():
    preconfigured = Settings(
        azure_openai_endpoint="https://cab-openai.openai.azure.com",
    )
    assert preconfigured.azure_openai_enabled is False

    with pytest.raises(SettingsError, match="configured together") as exc_info:
        Settings(
            azure_openai_enabled=True,
            azure_openai_endpoint="https://cab-openai.openai.azure.com",
        )
    assert "AZURE_OPENAI_DEPLOYMENT" in str(exc_info.value)
    assert "AZURE_OPENAI_MODEL" in str(exc_info.value)

    with pytest.raises(SettingsError, match="managed identity"):
        Settings(
            azure_openai_enabled=True,
            azure_openai_endpoint="https://cab-openai.openai.azure.com",
            azure_openai_deployment="cab-gpt",
            azure_openai_model="gpt-4.1-mini",
        )

    with pytest.raises(SettingsError, match="must be a boolean"):
        Settings(azure_openai_enabled="false")


def test_synthetic_only_inference_requires_primary_openai_enablement():
    with pytest.raises(
        SettingsError,
        match="COSMANEURA_AZURE_OPENAI_SYNTHETIC_ONLY requires",
    ):
        Settings(azure_openai_synthetic_only=True)


def test_language_extraction_requires_primary_openai_enablement():
    with pytest.raises(
        SettingsError,
        match="COSMANEURA_AZURE_LANGUAGE_ENABLED requires",
    ):
        Settings(
            azure_language_enabled=True,
            azure_language_endpoint=(
                "https://cosmaneura-language.cognitiveservices.azure.com"
            ),
            azure_client_id="33333333-3333-4333-8333-333333333333",
        )


@pytest.mark.parametrize(
    "name,value",
    [
        ("AZURE_OPENAI_ENDPOINT", "https://patient-jane.example.invalid"),
        ("AZURE_OPENAI_DEPLOYMENT", "patient Jane"),
        ("AZURE_OPENAI_API_VERSION", "latest"),
    ],
)
def test_azure_openai_fields_reject_unsafe_values_without_echoing_them(name, value):
    env = {
        "AZURE_CLIENT_ID": "33333333-3333-4333-8333-333333333333",
        "AZURE_OPENAI_ENDPOINT": "https://cab-openai.openai.azure.com",
        "AZURE_OPENAI_DEPLOYMENT": "cab-gpt",
        "AZURE_OPENAI_MODEL": "gpt-4.1-mini",
        name: value,
    }
    with pytest.raises(SettingsError) as exc_info:
        Settings.from_env(env)
    assert name in str(exc_info.value)
    assert value not in str(exc_info.value)


def test_deployed_runtime_rejects_direct_openai_api_key_without_echoing_it():
    secret = "direct-openai-secret-must-not-leak"
    with pytest.raises(SettingsError, match="development-only") as exc_info:
        _production_messaging_settings(openai_api_key=secret)
    assert secret not in str(exc_info.value)


@pytest.mark.parametrize(
    "name",
    ["AZURE_OPENAI_API_KEY", "AZURE_OPENAI_AD_TOKEN"],
)
def test_enabled_azure_openai_rejects_direct_credentials_without_echo(name):
    secret = "ambient-direct-credential-must-not-leak"
    with pytest.raises(SettingsError, match="managed identity") as exc_info:
        Settings.from_env({
            "COSMANEURA_AZURE_OPENAI_ENABLED": "true",
            name: secret,
        })
    assert secret not in str(exc_info.value)


def test_deployed_environments_require_complete_authentication():
    with pytest.raises(SettingsError, match="authentication cannot be disabled"):
        Settings(environment="production")
    with pytest.raises(SettingsError, match="tenant UUID"):
        Settings(
            environment="production",
            auth_mode="container_apps",
            practice_id="practice-a",
        )


def test_deployed_environments_require_postgres_and_runtime_credentials():
    base = {
        "environment": "production",
        "auth_mode": "container_apps",
        "entra_tenant_id": "11111111-1111-4111-8111-111111111111",
        "practice_id": "practice-a",
    }
    with pytest.raises(SettingsError, match="STATE_STORE=postgres"):
        Settings(**base)
    with pytest.raises(SettingsError, match="least-privilege runtime role"):
        Settings(
            **base,
            state_store="postgres",
            chase_store="postgres",
            database_url="postgresql://postgres@example.invalid/db",
            database_runtime_role="cosmaneura_app",
        )


def test_acs_email_policy_metadata_is_all_or_none_without_echoing_secrets():
    secret = (
        "endpoint=https://pilot.communication.azure.com;"
        "accesskey=connection-secret="
    )
    with pytest.raises(SettingsError, match="configured together") as exc_info:
        Settings.from_env({
            "COSMANEURA_ACS_EMAIL_CONNECTION_STRING": secret,
            "COSMANEURA_ACS_EMAIL_ENDPOINT": (
                "https://pilot.communication.azure.com"
            ),
            "COSMANEURA_ACS_EMAIL_SENDER_ADDRESS": "sender@pilot.example",
        })

    message = str(exc_info.value)
    assert "COSMANEURA_ACS_EMAIL_REPLY_TO_ADDRESS" in message
    assert secret not in message


@pytest.mark.parametrize(
    "name,value",
    [
        ("COSMANEURA_CONTACT_SNAPSHOT_MAX_AGE_HOURS", "0"),
        ("COSMANEURA_CONTACT_SNAPSHOT_MAX_AGE_HOURS", "169"),
        ("COSMANEURA_CHASE_WORKER_BATCH_SIZE", "0"),
        ("COSMANEURA_CHASE_WORKER_BATCH_SIZE", "501"),
        ("COSMANEURA_CHASE_WORKER_LEASE_SECONDS", "29"),
        ("COSMANEURA_CHASE_WORKER_LEASE_SECONDS", "3601"),
    ],
)
def test_chase_worker_numeric_controls_are_bounded(name, value):
    with pytest.raises(SettingsError) as exc_info:
        Settings.from_env({name: value})

    assert name in str(exc_info.value)


def test_practice_timezone_must_resolve_as_iana_name_without_echoing_input():
    invalid = "Not/A-Real-Patient-Timezone"
    with pytest.raises(SettingsError, match="IANA timezone") as exc_info:
        Settings.from_env({"COSMANEURA_PRACTICE_TIMEZONE": invalid})

    assert invalid not in str(exc_info.value)


@pytest.mark.parametrize(
    "name,value",
    [
        ("COSMANEURA_ACS_EMAIL_SENDER_ADDRESS", "sender\n@pilot.example"),
        ("COSMANEURA_ACS_EMAIL_REPLY_TO_ADDRESS", "not-an-email"),
        (
            "COSMANEURA_ACS_EMAIL_CONNECTION_STRING",
            "endpoint=http://pilot.invalid;accesskey=do-not-echo",
        ),
        (
            "COSMANEURA_ACS_EMAIL_CONNECTION_STRING",
            "endpoint=https://[invalid;accesskey=do-not-echo",
        ),
        (
            "COSMANEURA_ACS_EMAIL_ENDPOINT",
            "http://pilot.communication.azure.com",
        ),
        (
            "COSMANEURA_ACS_EMAIL_ENDPOINT",
            "https://[invalid",
        ),
    ],
)
def test_acs_email_fields_reject_invalid_values_without_echoing_them(name, value):
    env = {
        "COSMANEURA_ACS_EMAIL_CONNECTION_STRING": (
            "endpoint=https://pilot.communication.azure.com;accesskey=fake-key="
        ),
        "COSMANEURA_ACS_EMAIL_ENDPOINT": (
            "https://pilot.communication.azure.com"
        ),
        "COSMANEURA_ACS_EMAIL_SENDER_ADDRESS": "sender@pilot.example",
        "COSMANEURA_ACS_EMAIL_REPLY_TO_ADDRESS": "billing@pilot.example",
        name: value,
    }
    with pytest.raises(SettingsError) as exc_info:
        Settings.from_env(env)

    assert name in str(exc_info.value)
    assert value not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def _production_messaging_settings(**overrides):
    values = {
        "environment": "production",
        "auth_mode": "container_apps",
        "entra_tenant_id": "11111111-1111-4111-8111-111111111111",
        "practice_id": "practice-a",
        "state_store": "postgres",
        "chase_store": "postgres",
        "database_runtime_role": "cosmaneura_app",
        "database_url": "postgresql://cosmaneura_app@primary.invalid/db",
        "acs_email_endpoint": "https://pilot.communication.azure.com",
        "acs_email_sender_address": "sender@pilot.example",
        "acs_email_reply_to_address": "billing@pilot.example",
        "upload_url": "https://upload.cosmaneura.dev/cards",
        "chase_gate_hmac_key": "synthetic-gate-key-that-is-32-bytes",
    }
    values.update(overrides)
    return Settings(**values)


def test_production_patient_messaging_is_disabled_by_default():
    settings = _production_messaging_settings()

    assert settings.patient_messaging_enabled is False
    assert settings.patient_messaging_operationally_activated is False
    assert settings.chase_transport == "record_only"


def test_deployed_api_rejects_the_worker_only_acs_connection_secret():
    secret = (
        "endpoint=https://pilot.communication.azure.com;"
        "accesskey=must-not-enter-api="
    )

    with pytest.raises(SettingsError, match="worker-only") as exc_info:
        _production_messaging_settings(acs_email_connection_string=secret)

    assert secret not in str(exc_info.value)
    assert "must-not-enter-api" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_real_patient_messaging_requires_staging_or_production():
    with pytest.raises(SettingsError, match="staging or production"):
        Settings(
            patient_messaging_enabled=True,
            patient_messaging_operationally_activated=True,
            chase_transport="acs_email",
            acs_email_endpoint="https://pilot.communication.azure.com",
            acs_email_sender_address="sender@pilot.example",
            acs_email_reply_to_address="billing@pilot.example",
        )


def test_real_patient_messaging_requires_exact_transport_and_activation():
    with pytest.raises(SettingsError, match="TRANSPORT=acs_email"):
        _production_messaging_settings(
            patient_messaging_enabled=True,
            patient_messaging_operationally_activated=True,
        )
    with pytest.raises(SettingsError, match="explicit operational activation"):
        _production_messaging_settings(
            patient_messaging_enabled=True,
            chase_transport="acs_email",
        )


def test_real_patient_messaging_requires_complete_acs_configuration():
    with pytest.raises(SettingsError, match="complete ACS email settings"):
        _production_messaging_settings(
            patient_messaging_enabled=True,
            patient_messaging_operationally_activated=True,
            chase_transport="acs_email",
            acs_email_endpoint="",
            acs_email_sender_address="",
            acs_email_reply_to_address="",
        )


def test_real_patient_messaging_requires_the_durable_scheduler():
    with pytest.raises(SettingsError, match="durable chase scheduler"):
        _production_messaging_settings(
            patient_messaging_enabled=True,
            patient_messaging_operationally_activated=True,
            chase_transport="acs_email",
        )


@pytest.mark.parametrize("upload_url", [
    "http://upload.pilot.example/cards",
    "https://example.invalid/cards",
    "https://upload.pilot.example/cards",
    "https://upload.pilot.example/cards?token=do-not-store",
    "https://user:secret@upload.pilot.example/cards",
    "https://upload.pilot.example/",
])
def test_real_patient_messaging_requires_a_non_tokenized_secure_upload_route(
    upload_url,
):
    with pytest.raises(SettingsError, match="secure upload route") as exc_info:
        _production_messaging_settings(
            patient_messaging_enabled=True,
            patient_messaging_operationally_activated=True,
            chase_scheduler_enabled=True,
            chase_transport="acs_email",
            upload_url=upload_url,
        )

    assert upload_url not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_real_patient_messaging_requires_a_secret_gate_fingerprint_key():
    with pytest.raises(SettingsError, match="32-byte chase gate key") as exc_info:
        _production_messaging_settings(
            patient_messaging_enabled=True,
            patient_messaging_operationally_activated=True,
            chase_scheduler_enabled=True,
            chase_transport="acs_email",
            chase_gate_hmac_key="too-short-secret",
        )

    assert "too-short-secret" not in str(exc_info.value)


def test_operational_activation_cannot_be_armed_without_messaging_enabled():
    with pytest.raises(SettingsError, match="MESSAGING_ENABLED=true"):
        _production_messaging_settings(
            patient_messaging_operationally_activated=True,
        )


def test_complete_production_email_configuration_can_be_activated():
    settings = _production_messaging_settings(
        patient_messaging_enabled=True,
        patient_messaging_operationally_activated=True,
        chase_scheduler_enabled=True,
        chase_transport="acs_email",
        practice_timezone="America/New_York",
    )

    assert settings.patient_messaging_enabled is True
    assert settings.chase_scheduler_enabled is True
    assert settings.chase_transport == "acs_email"


@pytest.mark.parametrize(
    "origin", ["*", "file:///tmp", "https://host/", "https://host/path"],
)
def test_cors_requires_exact_http_origins(origin):
    with pytest.raises(SettingsError, match="exact HTTP"):
        Settings(cors_origins=(origin,))
