"""Standalone, fail-closed configuration for the scheduled chase worker."""

import pytest

from cosmaneura_billing.chase.models import Channel
from cosmaneura_billing.chase.policy import ApprovalMode
from cosmaneura_billing.chase.worker_config import ChaseWorkerConfig
from cosmaneura_billing.settings import SettingsError


WORKER_URL = (
    "postgresql://cosmaneura_chase_worker:synthetic-worker-secret@"
    "db.invalid/cab?sslmode=require"
)
ACS_CONNECTION = (
    "endpoint=https://pilot.communication.azure.com;"
    "accesskey=synthetic-acs-secret="
)


def _record_only_env(**changes: str) -> dict[str, str]:
    values = {
        "COSMANEURA_ENV": "test",
        "COSMANEURA_PRACTICE_ID": "practice-a",
        "COSMANEURA_PRACTICE_NAME": "Practice A",
        "COSMANEURA_CHASE_WORKER_DATABASE_URL": WORKER_URL,
        "COSMANEURA_CHASE_SCHEDULER_ENABLED": "true",
        "COSMANEURA_CHASE_TRANSPORT": "record_only",
        "COSMANEURA_PRACTICE_TIMEZONE": "America/New_York",
        "COSMANEURA_CHASE_APPROVAL": "per_message",
    }
    values.update(changes)
    return values


def _real_email_env(**changes: str) -> dict[str, str]:
    values = _record_only_env(
        COSMANEURA_ENV="staging",
        COSMANEURA_CHASE_TRANSPORT="acs_email",
        COSMANEURA_PATIENT_MESSAGING_ENABLED="true",
        COSMANEURA_PATIENT_MESSAGING_OPERATIONALLY_ACTIVATED="true",
        COSMANEURA_ACS_EMAIL_CONNECTION_STRING=ACS_CONNECTION,
        COSMANEURA_ACS_EMAIL_SENDER_ADDRESS="DoNotReply@pilot.example",
        COSMANEURA_ACS_EMAIL_REPLY_TO_ADDRESS="billing@pilot.example",
        COSMANEURA_UPLOAD_URL="https://upload.cosmaneura.dev/insurance",
        COSMANEURA_CHASE_GATE_HMAC_KEY="g" * 32,
    )
    values.update(changes)
    return values


def test_inert_defaults_never_fall_back_to_the_api_database_url():
    api_url = "postgresql://cosmaneura_app:api-secret@db.invalid/cab"

    config = ChaseWorkerConfig.from_env({"DATABASE_URL": api_url})

    assert config.chase_scheduler_enabled is False
    assert config.worker_database_url == ""
    assert api_url not in repr(config)
    policy = config.build_chase_policy()
    delivery = config.build_delivery_configuration()
    assert policy.practice_name == "your care team"
    assert delivery.provider == "record_only"
    assert delivery.scheduler_enabled is False
    assert delivery.operational_enabled is False


def test_record_only_scheduler_uses_only_worker_url_and_still_schedules_staff_tasks():
    api_url = "postgresql://cosmaneura_app:api-secret@db.invalid/cab"
    config = ChaseWorkerConfig.from_env({
        **_record_only_env(),
        "DATABASE_URL": api_url,
        "COSMANEURA_UPLOAD_URL": "https://not-deployed.invalid/insurance",
        "COSMANEURA_DIRECT_CONTRACT_LIST": "/synthetic/direct-contract.csv",
        "COSMANEURA_CONTACT_SNAPSHOT_MAX_AGE_HOURS": "48",
        "COSMANEURA_CHASE_WORKER_BATCH_SIZE": "40",
        "COSMANEURA_CHASE_WORKER_LEASE_SECONDS": "600",
        "COSMANEURA_LOG_LEVEL": "warning",
    })

    assert config.worker_database_url == WORKER_URL
    assert config.worker_database_url != api_url
    assert config.patient_messaging_enabled is False
    assert config.real_messaging_enabled is False
    assert config.chase_worker_batch_size == 40
    assert config.chase_worker_lease_seconds == 600
    assert config.log_level == "WARNING"

    policy = config.build_chase_policy()
    delivery = config.build_delivery_configuration()
    assert policy.practice_name == "Practice A"
    assert policy.approval_mode is ApprovalMode.PER_MESSAGE
    assert any(step.channel is Channel.PHONE for step in policy.steps)
    assert delivery.provider == "record_only"
    assert delivery.enabled_channels == ()
    assert delivery.contact_snapshot_max_age_hours == 48
    assert delivery.scheduler_enabled is True
    assert delivery.operational_enabled is False


def test_deployed_record_only_scheduler_requires_keyed_gate_evidence():
    deployed = _record_only_env(COSMANEURA_ENV="production")

    with pytest.raises(SettingsError, match="32-byte chase gate key"):
        ChaseWorkerConfig.from_env(deployed)

    config = ChaseWorkerConfig.from_env({
        **deployed,
        "COSMANEURA_CHASE_GATE_HMAC_KEY": "synthetic-gate-key-that-is-32-bytes",
    })
    assert config.chase_transport == "record_only"


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://cosmaneura_app:url-secret@db.invalid/cab",
        "postgresql://[malformed-url-secret/cab",
        (
            "postgresql://cosmaneura_chase_worker:url-secret@db.invalid/cab"
            "?user=postgres"
        ),
        (
            "postgresql://cosmaneura_chase_worker:url-secret@db.invalid/cab"
            "?sslmode=require&service=owner"
        ),
    ],
)
def test_worker_url_requires_exact_effective_role_without_secret_leak(url):
    with pytest.raises(
        SettingsError, match="least-privilege chase worker role",
    ) as exc_info:
        ChaseWorkerConfig.from_env({
            "COSMANEURA_CHASE_WORKER_DATABASE_URL": url,
        })

    assert url not in str(exc_info.value)
    assert "url-secret" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


@pytest.mark.parametrize(
    "role",
    ["", "postgres", "pg_read_all_data", "9worker", "w" * 64],
)
def test_worker_role_rejects_unsafe_or_privileged_names_without_echo(role):
    with pytest.raises(SettingsError, match="CHASE_WORKER_DB_ROLE") as exc_info:
        ChaseWorkerConfig(chase_worker_database_role=role)

    if role:
        assert role not in str(exc_info.value)


def test_scheduler_requires_its_own_url_and_practice_scope():
    with pytest.raises(SettingsError, match="WORKER_DATABASE_URL"):
        ChaseWorkerConfig(chase_scheduler_enabled=True, practice_id="practice-a")

    with pytest.raises(SettingsError, match="PRACTICE_ID"):
        ChaseWorkerConfig(
            chase_scheduler_enabled=True,
            chase_worker_database_url=WORKER_URL,
        )


def test_real_email_builds_exact_policy_and_email_only_delivery_configuration():
    config = ChaseWorkerConfig.from_env(_real_email_env(
        COSMANEURA_DIRECT_CONTRACT_LIST="/synthetic/direct-contract.csv",
        COSMANEURA_CONTACT_SNAPSHOT_MAX_AGE_HOURS="72",
    ))

    policy = config.build_chase_policy()
    delivery = config.build_delivery_configuration()

    assert policy.practice_name == "Practice A"
    assert policy.upload_url == "https://upload.cosmaneura.dev/insurance"
    assert policy.approval_mode is ApprovalMode.PER_MESSAGE
    assert delivery.provider == "azure_communication_services_email"
    assert delivery.provider_resource == "https://pilot.communication.azure.com"
    assert delivery.enabled_channels == (Channel.EMAIL,)
    assert delivery.sender_address == "DoNotReply@pilot.example"
    assert delivery.reply_to_address == "billing@pilot.example"
    assert delivery.contact_snapshot_max_age_hours == 72
    assert delivery.scheduler_enabled is True
    assert delivery.operational_enabled is True
    assert config.real_messaging_enabled is True


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"COSMANEURA_ENV": "test"}, "staging or production"),
        (
            {"COSMANEURA_PATIENT_MESSAGING_OPERATIONALLY_ACTIVATED": "false"},
            "explicit operational activation",
        ),
        ({"COSMANEURA_CHASE_SCHEDULER_ENABLED": "false"}, "durable chase scheduler"),
        ({"COSMANEURA_CHASE_TRANSPORT": "record_only"}, "TRANSPORT=acs_email"),
    ],
)
def test_real_messaging_requires_environment_scheduler_transport_and_both_latches(
    changes, match,
):
    with pytest.raises(SettingsError, match=match):
        ChaseWorkerConfig.from_env(_real_email_env(**changes))


@pytest.mark.parametrize(
    "changes,match",
    [
        (
            {"COSMANEURA_UPLOAD_URL": "http://upload.example/insurance"},
            "deployable secure upload route",
        ),
        ({"COSMANEURA_CHASE_GATE_HMAC_KEY": "too-short"}, "32-byte chase gate key"),
    ],
)
def test_real_messaging_requires_secure_upload_route_and_gate_key(changes, match):
    with pytest.raises(SettingsError, match=match):
        ChaseWorkerConfig.from_env(_real_email_env(**changes))


def test_acs_transport_requires_complete_configuration_even_before_activation():
    secret = "endpoint=https://pilot.communication.azure.com;accesskey=secret="

    with pytest.raises(SettingsError, match="configured together") as exc_info:
        ChaseWorkerConfig.from_env({
            "COSMANEURA_CHASE_TRANSPORT": "acs_email",
            "COSMANEURA_ACS_EMAIL_CONNECTION_STRING": secret,
        })

    assert secret not in str(exc_info.value)
    assert "secret=" not in str(exc_info.value)


def test_repr_redacts_every_worker_secret():
    secrets = {
        "COSMANEURA_CHASE_WORKER_DATABASE_URL": WORKER_URL,
        "COSMANEURA_ACS_EMAIL_CONNECTION_STRING": ACS_CONNECTION,
        "COSMANEURA_ACS_EMAIL_SENDER_ADDRESS": "DoNotReply@pilot.example",
        "COSMANEURA_ACS_EMAIL_REPLY_TO_ADDRESS": "billing@pilot.example",
        "COSMANEURA_UPLOAD_URL": "https://upload.cosmaneura.dev/insurance",
        "COSMANEURA_DIRECT_CONTRACT_LIST": "/secret/direct-contract.csv",
        "COSMANEURA_CHASE_GATE_HMAC_KEY": "gate-secret-that-is-at-least-32-bytes",
    }
    config = ChaseWorkerConfig.from_env({
        **secrets,
        "COSMANEURA_CHASE_TRANSPORT": "acs_email",
    })

    rendered = repr(config)
    for secret in secrets.values():
        assert secret not in rendered
    assert rendered.count("<redacted>") == len(secrets)


@pytest.mark.parametrize(
    "name,value",
    [
        ("COSMANEURA_CHASE_SCHEDULER_ENABLED", "maybe"),
        ("COSMANEURA_CONTACT_SNAPSHOT_MAX_AGE_HOURS", "0"),
        ("COSMANEURA_CHASE_WORKER_BATCH_SIZE", "501"),
        ("COSMANEURA_CHASE_WORKER_LEASE_SECONDS", "29"),
        ("COSMANEURA_CHASE_APPROVAL", "always"),
        ("COSMANEURA_PRACTICE_TIMEZONE", "Not/A_Zone"),
        ("COSMANEURA_LOG_LEVEL", "verbose"),
    ],
)
def test_invalid_worker_values_have_specific_value_safe_errors(name, value):
    with pytest.raises(SettingsError) as exc_info:
        ChaseWorkerConfig.from_env({name: value})

    assert name in str(exc_info.value)
    assert value not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
