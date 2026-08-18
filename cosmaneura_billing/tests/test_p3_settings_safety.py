"""Deployment-store configuration fails closed before serving requests."""
import pytest

from cosmaneura_billing.api import create_app
from cosmaneura_billing.settings import Settings, SettingsError


TENANT_ID = "11111111-1111-4111-8111-111111111111"
ADMIN_ID = "22222222-2222-4222-8222-222222222222"


def _production(**overrides):
    values = {
        "environment": "production",
        "auth_mode": "container_apps",
        "entra_tenant_id": TENANT_ID,
        "practice_id": "practice-a",
        "state_store": "postgres",
        "chase_store": "postgres",
        "database_runtime_role": "cosmaneura_app",
        "database_url": "postgresql://cosmaneura_app@primary.invalid/db",
    }
    values.update(overrides)
    return Settings(**values)


def test_deployed_state_and_chase_share_the_runtime_database():
    settings = _production(bootstrap_admin_oid=ADMIN_ID)

    assert settings.state_store == "postgres"
    assert settings.chase_store == "postgres"
    assert settings.chase_dsn == settings.database_url


def test_deployed_app_rejects_injected_non_postgres_stores():
    with pytest.raises(ValueError, match="cannot override required Postgres stores"):
        create_app(settings=_production(), feedback_store=object())


def test_postgres_state_rejects_a_split_chase_transaction_boundary():
    with pytest.raises(SettingsError, match="same DATABASE_URL"):
        Settings(
            auth_mode="container_apps",
            entra_tenant_id=TENANT_ID,
            practice_id="practice-a",
            state_store="postgres",
            chase_store="postgres",
            database_url="postgresql://app@state.invalid/db",
            chase_database_url="postgresql://app@chase.invalid/db",
        )


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployed_database_url_identity_must_match_runtime_role(environment):
    with pytest.raises(SettingsError, match="least-privilege runtime role"):
        _production(
            environment=environment,
            database_url="postgresql://cosmaneura_admin@primary.invalid/db",
        )


def test_bootstrap_object_id_is_validated_without_echoing_input():
    invalid = "not-an-entra-object-id"

    with pytest.raises(SettingsError, match="BOOTSTRAP_ADMIN_OID") as exc_info:
        _production(bootstrap_admin_oid=invalid)

    assert invalid not in str(exc_info.value)


def test_runtime_role_rejects_identifier_injection_without_echoing_input():
    invalid = 'app"; ALTER ROLE postgres SUPERUSER; --'

    with pytest.raises(SettingsError, match="safe PostgreSQL role name") as exc_info:
        _production(
            database_runtime_role=invalid,
            database_url=f"postgresql://{invalid}@primary.invalid/db",
        )

    assert invalid not in str(exc_info.value)
