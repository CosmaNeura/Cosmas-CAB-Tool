"""Fail-closed configuration for the dedicated chase worker database login."""

import pytest

from cosmaneura_billing.settings import Settings, SettingsError


def test_chase_worker_database_defaults_are_inert_and_distinct():
    settings = Settings.from_env({})

    assert settings.chase_worker_database_role == "cosmaneura_chase_worker"
    assert settings.chase_worker_database_password == ""
    assert settings.chase_worker_database_url == ""
    assert settings.chase_worker_database_role != settings.database_runtime_role


def test_chase_worker_database_settings_parse_and_redact_every_secret():
    password = "synthetic-worker-password"
    worker_url = (
        "postgresql://cosmaneura_chase_worker:worker-url-secret@db.invalid/cab"
    )

    settings = Settings.from_env({
        "COSMANEURA_CHASE_WORKER_DB_ROLE": "cosmaneura_chase_worker",
        "COSMANEURA_CHASE_WORKER_DB_PASSWORD": password,
        "COSMANEURA_CHASE_WORKER_DATABASE_URL": worker_url,
    })

    assert settings.chase_worker_database_role == "cosmaneura_chase_worker"
    assert settings.chase_worker_database_password == password
    assert settings.chase_worker_database_url == worker_url
    rendered = repr(settings)
    assert password not in rendered
    assert worker_url not in rendered
    assert "worker-url-secret" not in rendered


@pytest.mark.parametrize(
    "role",
    [
        "",
        'worker"; ALTER ROLE postgres SUPERUSER; --',
        "pg_read_all_data",
        "9worker",
        "w" * 64,
    ],
)
def test_chase_worker_role_rejects_unsafe_identifiers_without_echoing_them(role):
    with pytest.raises(SettingsError, match="CHASE_WORKER_DB_ROLE") as exc_info:
        Settings(
            chase_worker_database_role=role,
            chase_worker_database_password="synthetic-password",
        )

    if role:
        assert role not in str(exc_info.value)


def test_chase_worker_and_api_roles_must_be_distinct():
    with pytest.raises(SettingsError, match="must be distinct"):
        Settings(
            database_runtime_role="cosmaneura_same_role",
            chase_worker_database_role="COSMANEURA_SAME_ROLE",
        )


@pytest.mark.parametrize(
    "worker_url",
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
def test_chase_worker_url_requires_exact_worker_identity_without_echoing_secret(
    worker_url,
):
    with pytest.raises(
        SettingsError, match="least-privilege chase worker role",
    ) as exc_info:
        Settings(chase_worker_database_url=worker_url)

    assert worker_url not in str(exc_info.value)
    assert "secret" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_chase_worker_url_allows_only_a_secure_tls_query():
    settings = Settings(
        chase_worker_database_url=(
            "postgresql://cosmaneura_chase_worker:synthetic@db.invalid/cab"
            "?sslmode=require"
        ),
    )

    assert settings.chase_worker_database_url.endswith("?sslmode=require")
