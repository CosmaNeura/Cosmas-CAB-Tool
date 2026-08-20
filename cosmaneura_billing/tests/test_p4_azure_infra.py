"""Static deployment invariants for the isolated P4 runtime.

The Bicep compiler runs separately in CI.  These tests pin the security and
activation boundaries in source form so a future template refactor cannot
silently give the API a delivery credential or leave a disabled cron running.
"""
from __future__ import annotations

import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
FOUNDATION_PATH = ROOT / "infra" / "foundation.bicep"
APP_PATH = ROOT / "infra" / "app.bicep"
FOUNDATION = FOUNDATION_PATH.read_text(encoding="utf-8")
APP = APP_PATH.read_text(encoding="utf-8")


def _resource(source: str, symbol: str) -> str:
    """Return one brace-balanced Bicep resource declaration."""
    start = source.index(f"resource {symbol} ")
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        character = source[index]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unterminated Bicep resource: {symbol}")


def _parameter_default(source: str, name: str) -> str:
    match = re.search(
        rf"^param {re.escape(name)} [^=\n]+(?:= ([^\n]+))?$",
        source,
        flags=re.MULTILINE,
    )
    assert match is not None, f"missing parameter {name}"
    return (match.group(1) or "").strip()


def test_worker_identity_is_distinct_and_owns_only_its_job_image_pull():
    identity = _resource(FOUNDATION, "chaseWorkerIdentity")
    acr_pull = _resource(FOUNDATION, "chaseWorkerAcrPull")
    job = _resource(APP, "chaseWorkerJob")

    assert "chaseWorkerIdentityName" in identity
    assert "output chaseWorkerIdentityName string" in FOUNDATION
    assert "principalId: chaseWorkerIdentity.properties.principalId" in acr_pull
    assert "workloadIdentity.properties.principalId" not in acr_pull
    assert "migrationIdentity.properties.principalId" not in acr_pull
    assert "'${chaseWorkerIdentity.id}': {}" in job
    assert "identity: chaseWorkerIdentity.id" in job
    assert "'${identity.id}': {}" not in job
    assert "'${migrationIdentity.id}': {}" not in job


def test_expensive_readiness_is_edge_protected_but_internal_probes_remain():
    auth = _resource(APP, "applicationAuth")
    application = _resource(APP, "application")

    assert "'/api/live'" in auth
    assert "'/api/ready'" not in auth
    assert application.count("path: '/api/ready'") == 2
    assert application.count("path: '/api/live'") == 1


def test_every_runtime_lane_deploys_one_validated_manifest_digest():
    application = _resource(APP, "application")
    migration = _resource(APP, "migrationJob")
    worker = _resource(APP, "chaseWorkerJob")

    assert "@minLength(71)\n@maxLength(71)\nparam imageDigest string" in APP
    assert "startsWith(normalizedImageDigest, 'sha256:')" in APP
    assert "empty(imageDigestNonHex)" in APP
    assert "cosmaneura-billing@${validatedImageDigest}" in APP
    assert "cosmaneura-billing:${imageTag}" not in APP
    for runtime in (application, migration, worker):
        assert "image: image" in runtime
    assert "output deployedImageDigest string = validatedImageDigest" in APP


def test_key_vault_permissions_are_secret_scoped_and_partitioned():
    combined = FOUNDATION + "\n" + APP
    assert re.search(r"^\s*scope:\s*keyVault\s*$", combined, re.MULTILINE) is None

    expected_scopes = {
        "runtimeDatabaseSecretReader": "databaseUrlSecret",
        "migrationDatabaseSecretReader": "migrationDatabaseUrlSecret",
        "migrationRuntimePasswordSecretReader": "runtimeDatabasePasswordSecret",
        "migrationChaseWorkerPasswordSecretReader": (
            "chaseWorkerDatabasePasswordSecret"
        ),
        "chaseWorkerDatabaseSecretReader": "chaseWorkerDatabaseUrlSecret",
        "apiChaseGateSecretReader": "chaseGateHmacKeySecret",
        "chaseWorkerGateSecretReader": "chaseGateHmacKeySecret",
        "chaseWorkerAcsEmailSecretReader": "acsEmailConnectionStringSecret",
    }
    for symbol, scope in expected_scopes.items():
        block = _resource(FOUNDATION, symbol)
        assert f"scope: {scope}" in block

    entra_reader = _resource(APP, "entraClientSecretReader")
    assert "scope: entraClientSecretVault" in entra_reader

    application = _resource(APP, "application")
    worker = _resource(APP, "chaseWorkerJob")
    migration = _resource(APP, "migrationJob")
    assert "chase-worker-database-url" not in application
    assert "COSMANEURA_ACS_EMAIL_CONNECTION_STRING" not in application
    assert "migration-database-url" not in worker
    assert "database-runtime-password" not in worker
    assert "microsoft-provider-authentication-secret" not in worker
    assert "chase-worker-database-url" not in migration
    assert "acs-email-connection-string" not in migration


def test_worker_database_principal_is_separate_and_provisioned_by_migration():
    assert "@secure()\n@description('Password for the least-privilege chase worker" in FOUNDATION
    assert "postgresChaseWorkerLoginConflicts" in FOUNDATION
    assert "toLower(postgresAdminLogin)" in FOUNDATION
    assert "toLower(postgresAppLogin)" in FOUNDATION
    assert (
        "postgresql://${validatedPostgresChaseWorkerLogin}:"
        "${encodedChaseWorkerPassword}" in FOUNDATION
    )
    assert "name: 'chase-worker-database-url'" in FOUNDATION
    assert "name: 'chase-worker-database-password'" in FOUNDATION
    assert "chaseGateHmacKey placeholder must be replaced" in FOUNDATION

    migration = _resource(APP, "migrationJob")
    assert "COSMANEURA_CHASE_WORKER_DB_ROLE" in migration
    assert "COSMANEURA_CHASE_WORKER_DB_PASSWORD" in migration
    assert "secretRef: 'chase-worker-database-password'" in migration


def test_worker_job_is_bounded_and_redeployably_disabled_by_default():
    job = _resource(APP, "chaseWorkerJob")
    declaration = job.split("{", 1)[0]
    assert "= if" not in declaration
    assert _parameter_default(APP, "enableChaseScheduler") == "false"
    assert "triggerType: validatedSchedulerEnabled ? 'Schedule' : 'Manual'" in job
    assert "cronExpression: chaseWorkerCronExpression" in job
    assert "replicaTimeout: 600" in job
    assert "replicaRetryLimit: 0" in job
    assert "parallelism: 1" in job
    assert "replicaCompletionCount: 1" in job
    assert "'cosmaneura_billing.chase.worker'" in job
    assert "'--once'" in job
    assert "name: 'COSMANEURA_CHASE_SCHEDULER_ENABLED'" in job
    assert "value: schedulerEnabledValue" in job
    assert "ingress:" not in job


def test_shared_network_writes_are_serialized():
    postgres_subnet = _resource(FOUNDATION, "postgresSubnet")
    private_endpoints_subnet = _resource(FOUNDATION, "privateEndpointsSubnet")
    key_vault_private_endpoint = _resource(
        FOUNDATION,
        "keyVaultPrivateEndpoint",
    )

    assert "dependsOn: [\n    containerAppsSubnet\n  ]" in postgres_subnet
    assert "dependsOn: [\n    postgresSubnet\n  ]" in private_endpoints_subnet
    assert (
        "dependsOn: [\n    acrPrivateEndpoint\n  ]"
        in key_vault_private_endpoint
    )


def test_real_messaging_is_double_latched_and_inert_by_default():
    assert _parameter_default(APP, "enablePatientMessaging") == "false"
    assert _parameter_default(APP, "activatePatientMessaging") == "false"
    assert "enablePatientMessaging != activatePatientMessaging" in APP
    assert "enablePatientMessaging && !enableChaseScheduler" in APP
    assert "? 'acs_email' : 'record_only'" in APP

    application = _resource(APP, "application")
    worker = _resource(APP, "chaseWorkerJob")
    for name in (
        "COSMANEURA_CHASE_SCHEDULER_ENABLED",
        "COSMANEURA_PATIENT_MESSAGING_ENABLED",
        "COSMANEURA_PATIENT_MESSAGING_OPERATIONALLY_ACTIVATED",
        "COSMANEURA_CHASE_TRANSPORT",
        "COSMANEURA_CHASE_GATE_HMAC_KEY",
        "COSMANEURA_PRACTICE_TIMEZONE",
        "COSMANEURA_CONTACT_SNAPSHOT_MAX_AGE_HOURS",
    ):
        assert name in application
        assert name in worker

    assert "COSMANEURA_ACS_EMAIL_CONNECTION_STRING" not in application
    assert "COSMANEURA_ACS_EMAIL_CONNECTION_STRING" in worker
    for name in (
        "COSMANEURA_ACS_EMAIL_ENDPOINT",
        "COSMANEURA_ACS_EMAIL_SENDER_ADDRESS",
        "COSMANEURA_ACS_EMAIL_REPLY_TO_ADDRESS",
    ):
        assert name in application
        assert name in worker


def test_parameter_examples_are_safe_complete_and_default_off():
    foundation = json.loads(
        (ROOT / "infra" / "foundation.parameters.example.json").read_text(
            encoding="utf-8"
        )
    )["parameters"]
    app = json.loads(
        (ROOT / "infra" / "app.parameters.example.json").read_text(
            encoding="utf-8"
        )
    )["parameters"]

    assert foundation["postgresAppLogin"]["value"] != foundation[
        "postgresChaseWorkerLogin"
    ]["value"]
    assert "REPLACE" in foundation["postgresChaseWorkerPassword"]["value"]
    assert len(foundation["chaseGateHmacKey"]["value"].encode("utf-8")) >= 32
    assert foundation["acsEmailConnectionString"]["value"] == ""
    assert app["postgresChaseWorkerLogin"]["value"] == foundation[
        "postgresChaseWorkerLogin"
    ]["value"]
    assert app["enableChaseScheduler"]["value"] is False
    assert app["enablePatientMessaging"]["value"] is False
    assert app["activatePatientMessaging"]["value"] is False
    assert app["acsEmailEndpoint"]["value"] == ""
    assert app["acsEmailSenderAddress"]["value"] == ""
    assert app["acsEmailReplyToAddress"]["value"] == ""


def test_ci_compiles_both_bicep_templates_without_deploying():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "az bicep install --version v0.46.1" in workflow
    assert (
        "az bicep build --file infra/foundation.bicep --stdout" in workflow
    )
    assert "az bicep build --file infra/app.bicep --stdout" in workflow
