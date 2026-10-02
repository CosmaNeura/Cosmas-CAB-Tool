"""Athena-only configuration contract, without communications dependencies."""
import pytest
from cosmaneura_billing.settings import Settings, SettingsError


def environment(**changes):
    return {"COSMANEURA_ENV": "test", "COSMANEURA_AUTH_MODE": "container_apps",
        "COSMANEURA_ENTRA_TENANT_ID": "11111111-1111-4111-8111-111111111111",
        "COSMANEURA_PRACTICE_ID": "synthetic-beta", "COSMANEURA_STATE_STORE": "postgres",
        "COSMANEURA_CHASE_STORE": "postgres", "DATABASE_URL": "postgresql://cosmaneura_app@db.invalid/cab",
        **changes}


def test_athena_preview_credentials_are_secret_and_mock_is_local_only():
    env = environment(COSMANEURA_ATHENA_REVIEW_MODE="preview",
        COSMANEURA_ATHENA_CLIENT_ID="synthetic-client", COSMANEURA_ATHENA_CLIENT_SECRET="synthetic-secret")
    config = Settings.from_env(env)
    assert config.athena_client_secret == "synthetic-secret"
    assert "synthetic-secret" not in repr(config) and "synthetic-client" not in repr(config)
    with pytest.raises(SettingsError):
        Settings.from_env(environment(COSMANEURA_ENV="staging", COSMANEURA_ATHENA_REVIEW_MODE="mock"))


@pytest.mark.parametrize("mode", ["mock", "preview", "configured"])
def test_durable_athena_requires_the_same_worklist_authority_in_local_environments(mode):
    env = environment(COSMANEURA_COMMUNICATIONS_ENABLED="false", COSMANEURA_CHASE_TRANSPORT="record_only",
        COSMANEURA_ATHENA_REVIEW_MODE=mode, COSMANEURA_ATHENA_CLIENT_ID="synthetic-client",
        COSMANEURA_ATHENA_CLIENT_SECRET="synthetic-secret", COSMANEURA_CHASE_STORE="memory",
        COSMANEURA_ATHENA_CREDENTIAL_ENVIRONMENT="preview")
    with pytest.raises(SettingsError, match="shared PostgreSQL worklist"):
        Settings.from_env(env)


@pytest.mark.parametrize("credential_environment", ["preview", "production"])
def test_configured_athena_requires_explicit_environment_and_durable_storage(credential_environment):
    env = environment(COSMANEURA_COMMUNICATIONS_ENABLED="false", COSMANEURA_CHASE_TRANSPORT="record_only",
        COSMANEURA_ATHENA_REVIEW_MODE="configured", COSMANEURA_ATHENA_CLIENT_ID="synthetic-client",
        COSMANEURA_ATHENA_CLIENT_SECRET="synthetic-secret",
        COSMANEURA_ATHENA_CREDENTIAL_ENVIRONMENT=credential_environment)
    config = Settings.from_env(env)
    assert config.athena_review_mode == "configured"
    assert config.athena_credential_environment == credential_environment
    for change in ({"COSMANEURA_STATE_STORE": "local"}, {"COSMANEURA_ATHENA_CLIENT_ID": ""},
                   {"COSMANEURA_ATHENA_CLIENT_SECRET": ""}, {"COSMANEURA_ATHENA_CREDENTIAL_ENVIRONMENT": ""}):
        with pytest.raises(SettingsError, match="Configured Athena review"):
            Settings.from_env({**env, **change})
    with pytest.raises(SettingsError, match="CREDENTIAL_ENVIRONMENT"):
        Settings.from_env({**env, "COSMANEURA_ATHENA_CREDENTIAL_ENVIRONMENT": "other"})


def test_athena_admission_runtime_identifiers_are_explicit_and_bounded():
    defaults = Settings.from_env({})
    assert defaults.athena_deployment_id == defaults.athena_storage_id == ""
    settings = Settings.from_env({"COSMANEURA_ATHENA_DEPLOYMENT_ID": "cab:beta-1",
                                  "COSMANEURA_ATHENA_STORAGE_ID": "store.1"})
    assert (settings.athena_deployment_id, settings.athena_storage_id) == ("cab:beta-1", "store.1")
    for name in ("COSMANEURA_ATHENA_DEPLOYMENT_ID", "COSMANEURA_ATHENA_STORAGE_ID"):
        for value in (" padded", "bad/identifier", "nonascii-é", "a" * 129):
            with pytest.raises(SettingsError, match="deployment and storage IDs"):
                Settings.from_env({name: value})


def test_athena_defaults_and_unknown_mode_fail_closed():
    defaults = Settings.from_env({})
    assert defaults.athena_review_mode == "disabled"
    assert defaults.athena_client_id == defaults.athena_client_secret == ""
    with pytest.raises(SettingsError, match="ATHENA_REVIEW_MODE"):
        Settings.from_env({"COSMANEURA_ATHENA_REVIEW_MODE": "automatic"})
