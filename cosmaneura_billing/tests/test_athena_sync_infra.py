"""Static invariants for the dormant Athena sandbox sync lane."""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "infra" / "app.bicep").read_text(encoding="utf-8")
PARAMETERS = json.loads(
    (ROOT / "infra" / "app.parameters.example.json").read_text(
        encoding="utf-8"
    )
)["parameters"]
INFRA_README = (ROOT / "infra" / "README.md").read_text(encoding="utf-8")


def _resource(source: str, symbol: str) -> str:
    """Return one brace-balanced Bicep resource declaration."""
    start = source.index(f"resource {symbol} ")
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unterminated Bicep resource: {symbol}")


def test_athena_sync_has_separate_default_off_provision_and_enable_latches():
    assert "param provisionAthenaSync bool = false" in APP
    assert "param enableAthenaSync bool = false" in APP
    assert "enableAthenaSync requires provisionAthenaSync" in APP
    assert "enableAthenaSync requires explicit HTTPS base/token URLs" in APP
    assert "output athenaSyncProvisioned bool" in APP
    assert "output athenaSyncEnabled bool" in APP

    assert PARAMETERS["provisionAthenaSync"]["value"] is False
    assert PARAMETERS["enableAthenaSync"]["value"] is False
    for name in (
        "athenaBaseUrl",
        "athenaTokenUrl",
        "athenaClientId",
        "athenaScopes",
    ):
        assert PARAMETERS[name]["value"] == ""


def test_athena_sync_uses_its_own_conditional_identity_and_acr_pull():
    identity = _resource(APP, "athenaSyncIdentity")
    acr_pull = _resource(APP, "athenaSyncAcrPull")
    job = _resource(APP, "athenaSyncJob")

    assert "if (provisionAthenaSync)" in identity
    assert "athenaSyncIdentityName" in identity
    assert "if (provisionAthenaSync)" in acr_pull
    assert "scope: registry" in acr_pull
    assert "principalId: athenaSyncIdentity!.properties.principalId" in acr_pull
    assert "7f951dda-4ed3-4680-a7ca-43fe172d538d" in acr_pull
    assert "'${athenaSyncIdentity!.id}': {}" in job
    assert "identity: athenaSyncIdentity!.id" in job
    assert "'${identity.id}': {}" not in job
    assert "migrationIdentity" not in job
    assert "chaseWorkerIdentity" not in job


def test_athena_client_secret_is_operator_owned_and_exactly_scoped():
    secret = _resource(APP, "athenaClientSecretVault")
    reader = _resource(APP, "athenaClientSecretReader")
    job = _resource(APP, "athenaSyncJob")

    assert "existing = if (validatedAthenaSyncEnabled)" in secret
    assert "name: athenaClientSecretKeyVaultSecretName" in secret
    assert "scope: athenaClientSecretVault" in reader
    assert "if (validatedAthenaSyncEnabled)" in reader
    assert "principalId: athenaSyncIdentity!.properties.principalId" in reader
    assert "4633458b-17de-408a-b874-0445c86b69e6" in reader
    assert "@secure()\nparam athena" not in APP
    assert "athenaClientSecret string" not in APP
    assert "keyVaultUrl:" in job
    assert "secretRef: 'athena-client-secret'" in job

    for symbol in ("application", "migrationJob", "chaseWorkerJob"):
        lane = _resource(APP, symbol)
        assert "COSMANEURA_ATHENA_" not in lane
        assert "athena-client-secret" not in lane


def test_athena_job_is_no_ingress_manual_bounded_and_digest_pinned():
    job = _resource(APP, "athenaSyncJob")

    assert "if (provisionAthenaSync)" in job
    assert "var athenaSyncJobName = '${containerNamePrefix}-ath-${suffix}'" in APP
    assert "triggerType: 'Manual'" in job
    assert "manualTriggerConfig:" in job
    assert "replicaTimeout: 900" in job
    assert "replicaRetryLimit: 0" in job
    assert "parallelism: 1" in job
    assert "replicaCompletionCount: 1" in job
    assert "scheduleTriggerConfig" not in job
    assert "cronExpression" not in job
    assert "ingress:" not in job
    assert "image: image" in job
    assert "'cosmaneura_billing.athena_sync'" in job
    assert "'--once'" in job
    assert "name: 'COSMANEURA_ATHENA_SYNC_ENABLED'" in job
    assert "value: athenaSyncEnabledValue" in job
    assert "name: 'COSMANEURA_ATHENA_MODE'" in job
    assert "value: athenaSyncMode" in job
    assert "validatedAthenaSyncEnabled ? 'live' : 'disabled'" in APP
    assert "name: 'COSMANEURA_PRACTICE_ID'" in job


def test_athena_live_configuration_is_injected_only_after_enablement():
    job = _resource(APP, "athenaSyncJob")

    assert "secrets: validatedAthenaSyncEnabled ? [" in job
    assert "], validatedAthenaSyncEnabled ? [" in job
    for name in (
        "COSMANEURA_ATHENA_BASE_URL",
        "COSMANEURA_ATHENA_TOKEN_URL",
        "COSMANEURA_ATHENA_CLIENT_ID",
        "COSMANEURA_ATHENA_SCOPES",
        "COSMANEURA_ATHENA_CLIENT_SECRET",
    ):
        assert name in job
    assert "AZURE_CLIENT_ID" in job
    assert "no scheduler" in INFRA_README.lower()
    assert "synthetic/read-only" in INFRA_README
