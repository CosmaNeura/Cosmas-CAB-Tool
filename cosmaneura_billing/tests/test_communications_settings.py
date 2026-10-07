"""Runtime activation and separate-service composition; no vendor/database I/O."""
from dataclasses import asdict

import pytest

from cosmaneura_billing.chase.communications import worker
from cosmaneura_billing.chase.policy_fingerprint import DeliveryConfiguration
from cosmaneura_billing.settings import Settings, SettingsError


def environment(**changes):
    return {"COSMANEURA_ENV": "test", "COSMANEURA_AUTH_MODE": "container_apps",
        "COSMANEURA_ENTRA_TENANT_ID": "11111111-1111-4111-8111-111111111111",
        "COSMANEURA_PRACTICE_ID": "synthetic-beta", "COSMANEURA_STATE_STORE": "postgres",
        "COSMANEURA_CHASE_STORE": "postgres", "DATABASE_URL": "postgresql://cosmaneura_app@db.invalid/cab",
        "COSMANEURA_COMMUNICATIONS_ENABLED": "true", "COSMANEURA_CHASE_TRANSPORT": "twilio",
        "COSMANEURA_TWILIO_ACCOUNT_SID": "AC" + "0" * 32,
        "COSMANEURA_TWILIO_FROM_NUMBER": "+12025550199",
        "COSMANEURA_TWILIO_CALLBACK_BASE_URL": "https://callback.example.invalid",
        "COSMANEURA_CHASE_WORKER_DATABASE_URL": "postgresql://cosmaneura_chase_worker@db.invalid/cab",
        "COSMANEURA_TWILIO_AUTH_TOKEN": "synthetic-secret-never-live", **changes}


def test_dark_settings_never_activate_legacy_worker_or_expose_vendor_token():
    config = Settings.from_env(environment())
    assert config.communications_enabled and not config.patient_messaging_enabled
    assert not config.patient_messaging_operationally_activated
    assert "synthetic-secret-never-live" not in repr(config)
    assert "twilio_auth_token" not in asdict(config)
    assert config.twilio_from_number not in repr(config)
    legacy = DeliveryConfiguration.from_settings(config)
    assert legacy.provider == "record_only" and legacy.scheduler_enabled is False


def activated_sms_environment(**changes):
    return environment(**{
        "COSMANEURA_ENV": "staging",
        "COSMANEURA_COMMUNICATIONS_ENABLED_CHANNELS": "sms",
        "COSMANEURA_COMMUNICATIONS_TEST_MODE": "true",
        "COSMANEURA_COMMUNICATIONS_TEST_RECIPIENTS": "+12025550188",
        "COSMANEURA_CHASE_SCHEDULER_ENABLED": "true",
        "COSMANEURA_PATIENT_MESSAGING_ENABLED": "true",
        "COSMANEURA_PATIENT_MESSAGING_OPERATIONALLY_ACTIVATED": "true",
        "COSMANEURA_CHASE_GATE_HMAC_KEY": "synthetic-gate-key-that-is-32-bytes",
        **changes,
    })


@pytest.mark.parametrize("deployed_environment", ["staging", "production"])
def test_activated_sms_composes_without_an_unused_upload_route(
    deployed_environment, monkeypatch,
):
    import psycopg
    from cosmaneura_billing.chase.communications.runtime import dispatcher_from_settings

    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: pytest.fail("constructor database I/O"))
    env = activated_sms_environment(COSMANEURA_ENV=deployed_environment)
    assert "COSMANEURA_UPLOAD_URL" not in env
    config = Settings.from_env(env)
    staff = dispatcher_from_settings(config)
    outbound = worker.configured_dispatcher(environ=env, outbound=True)
    assert staff.adapter is None
    assert outbound.adapter.transport is not None
    assert staff.policy == outbound.policy
    assert staff.policy.scheduler_enabled and staff.policy.patient_messaging_enabled
    assert staff.policy.operationally_activated and staff.policy.test_mode
    assert staff.policy.enabled_channels == ("sms",)
    assert staff.policy.test_recipients == ("+12025550188",)
    assert all(config.upload_url not in template["text"] for template in staff.policy.templates)

    legacy = DeliveryConfiguration.from_settings(config)
    assert legacy.provider == "record_only"
    assert not legacy.scheduler_enabled and not legacy.operational_enabled


@pytest.mark.parametrize("changes", [
    {"COSMANEURA_ENV": "test"},
    {"COSMANEURA_AUTH_MODE": "disabled"},
    {"COSMANEURA_STATE_STORE": "local"},
    {"COSMANEURA_CHASE_STORE": "memory"},
    {"COSMANEURA_COMMUNICATIONS_ENABLED": "false"},
    {"COSMANEURA_CHASE_TRANSPORT": "record_only"},
    {"COSMANEURA_CHASE_SCHEDULER_ENABLED": "false"},
    {"COSMANEURA_PATIENT_MESSAGING_ENABLED": "false"},
    {"COSMANEURA_PATIENT_MESSAGING_OPERATIONALLY_ACTIVATED": "false"},
    {"COSMANEURA_CHASE_GATE_HMAC_KEY": "too-short"},
    {"COSMANEURA_TWILIO_FROM_NUMBER": ""},
])
def test_sms_upload_route_exception_preserves_activation_guards(changes):
    with pytest.raises(SettingsError):
        Settings.from_env(activated_sms_environment(**changes))


@pytest.mark.parametrize("channels", ["automated_voice", "sms,automated_voice"])
def test_voice_enabled_topologies_keep_the_upload_route_requirement(channels):
    with pytest.raises(SettingsError, match="secure upload route"):
        Settings.from_env(activated_sms_environment(
            COSMANEURA_COMMUNICATIONS_ENABLED_CHANNELS=channels,
            COSMANEURA_COMMUNICATIONS_TEST_MODE="false",
            COSMANEURA_COMMUNICATIONS_TEST_RECIPIENTS="",
        ))


@pytest.mark.parametrize("changes", [
    {"COSMANEURA_TWILIO_FROM_NUMBER": ""}, {"COSMANEURA_TWILIO_ACCOUNT_SID": "bad"},
    {"COSMANEURA_TWILIO_CALLBACK_BASE_URL": "http://insecure.invalid"},
    {"COSMANEURA_TWILIO_CALLBACK_BASE_URL": "https://secret:password@example.invalid"},
    {"COSMANEURA_TWILIO_CALLBACK_BASE_URL": "https://callback.example.invalid/path"},
    {"COSMANEURA_TWILIO_CALLBACK_BASE_URL": "https://callback.example.invalid/?token=secret"},
    {"COSMANEURA_TWILIO_CALLBACK_BASE_URL": "https://callback.example.invalid:444"},
    {"COSMANEURA_STATE_STORE": "local"}, {"COSMANEURA_CHASE_STORE": "memory"},
    {"COSMANEURA_COMMUNICATIONS_ENABLED": "false"},
    {"COSMANEURA_PATIENT_MESSAGING_ENABLED": "true"},
])
def test_incomplete_or_unapproved_configuration_fails_closed(changes):
    with pytest.raises(SettingsError) as exc:
        Settings.from_env(environment(**changes))
    assert "synthetic-secret" not in str(exc.value) and "password" not in str(exc.value)


def test_callback_composition_is_signature_only_and_worker_alone_has_transport(monkeypatch):
    import psycopg
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: pytest.fail("constructor database I/O"))
    inbound = worker.configured_dispatcher(environ=environment())
    outbound = worker.configured_dispatcher(environ=environment(), outbound=True)
    assert inbound.adapter.transport is None
    assert outbound.adapter.transport is not None
    assert inbound.store._dsn == environment()["COSMANEURA_CHASE_WORKER_DATABASE_URL"]


@pytest.mark.parametrize("changes", [
    {"COSMANEURA_CHASE_WORKER_DATABASE_URL": ""},
    {"COSMANEURA_CHASE_TRANSPORT": "record_only"}, {"COSMANEURA_TWILIO_AUTH_TOKEN": ""},
])
def test_service_refuses_missing_worker_configuration(changes):
    with pytest.raises((ValueError, RuntimeError)):
        worker.configured_dispatcher(environ=environment(**changes))


def test_worker_failure_output_never_contains_exception_payload(monkeypatch, capsys):
    def fail(**kwargs): raise RuntimeError("patient@example.invalid synthetic-credential")
    monkeypatch.setattr(worker, "configured_dispatcher", fail)
    assert worker.main() == 1
    assert capsys.readouterr().out == '{"status": "communications_worker_unavailable"}\n'


def test_services_never_read_api_identity_database_or_athena_credentials():
    class WorkerOnly(dict):
        def get(self, key, default=None):
            if key in {"DATABASE_URL", "COSMANEURA_ENTRA_TENANT_ID", "COSMANEURA_AUTH_MODE",
                       "COSMANEURA_REVIEW_CONTEXT_SIGNING_KEY", "COSMANEURA_ATHENA_CLIENT_SECRET"}:
                pytest.fail("worker attempted to read API-only configuration")
            return super().get(key, default)
    env = WorkerOnly(environment(COSMANEURA_ENV="staging"))
    service = worker.configured_dispatcher(environ=env)
    assert service.adapter.transport is None
    assert "cosmaneura_chase_worker@" in service.store._dsn


@pytest.mark.parametrize("changes", [
    {"COSMANEURA_CHASE_WORKER_DB_ROLE": "postgres", "COSMANEURA_CHASE_WORKER_DATABASE_URL": "postgresql://postgres@db.invalid/cab"},
    {"COSMANEURA_CHASE_WORKER_DATABASE_URL": "postgresql://cosmaneura_chase_worker@db.invalid/cab?user=postgres"},
    {"COSMANEURA_ENV": "test", "COSMANEURA_PATIENT_MESSAGING_ENABLED": "true"},
    {"COSMANEURA_ENV": "staging", "COSMANEURA_CHASE_SCHEDULER_ENABLED": "true"},
    {"COSMANEURA_PATIENT_MESSAGING_OPERATIONALLY_ACTIVATED": "true"},
])
def test_separate_service_preserves_role_and_activation_guards(changes):
    with pytest.raises(SettingsError):
        worker.configured_dispatcher(environ=environment(**changes))
