"""Static Azure invariants for PHI-safe P5 observability and inference secrets."""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FOUNDATION = (ROOT / "infra" / "foundation.bicep").read_text(encoding="utf-8")
APP = (ROOT / "infra" / "app.bicep").read_text(encoding="utf-8")


def _resource(source: str, symbol: str) -> str:
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


def test_app_insights_is_aad_only_unsampled_and_workspace_retention_is_bounded():
    insights = _resource(FOUNDATION, "appInsights")
    workspace = _resource(FOUNDATION, "logAnalytics")

    assert "DisableLocalAuth: true" in insights
    assert "SamplingPercentage: 100" in insights
    assert "IngestionMode: 'LogAnalytics'" in insights
    assert "retentionInDays: 30" in workspace


def test_only_api_identity_can_publish_explicit_numeric_metrics():
    assignment = _resource(FOUNDATION, "apiMetricsPublisher")

    assert "scope: appInsights" in assignment
    assert "principalId: workloadIdentity.properties.principalId" in assignment
    assert "3913510d-42f4-4e42-8a64-420c390055eb" in assignment
    assert "migrationIdentity" not in assignment
    assert "chaseWorkerIdentity" not in assignment

    api = _resource(APP, "application")
    worker = _resource(APP, "chaseWorkerJob")
    migration = _resource(APP, "migrationJob")
    for name in (
        "COSMANEURA_TELEMETRY_ENABLED",
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "AZURE_CLIENT_ID",
        "APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL",
        "APPLICATIONINSIGHTS_SDKSTATS_DISABLED",
        "APPLICATIONINSIGHTS_CONTROLPLANE_DISABLED",
    ):
        assert name in api
        assert name not in worker
        assert name not in migration


def test_private_azure_openai_is_managed_identity_only_and_default_off():
    account = _resource(FOUNDATION, "azureOpenAI")
    deployment = _resource(FOUNDATION, "azureOpenAIDeployment")
    reader = _resource(FOUNDATION, "apiAzureOpenAIUser")
    endpoint = _resource(FOUNDATION, "azureOpenAIPrivateEndpoint")
    dns = _resource(FOUNDATION, "azureOpenAIPrivateDns")
    api = _resource(APP, "application")
    worker = _resource(APP, "chaseWorkerJob")
    migration = _resource(APP, "migrationJob")

    assert "if (provisionAzureOpenAI)" in account
    assert "kind: 'OpenAI'" in account
    assert "disableLocalAuth: true" in account
    assert "publicNetworkAccess: 'Disabled'" in account
    assert "defaultAction: 'Deny'" in account
    assert "name: azureOpenAIModelName" in deployment
    assert "version: azureOpenAIModelVersion" in deployment
    assert "name: azureOpenAIDeploymentSku" in deployment
    assert "capacity: azureOpenAIDeploymentCapacity" in deployment
    assert "versionUpgradeOption: 'NoAutoUpgrade'" in deployment
    assert "scope: azureOpenAI" in reader
    assert "principalId: workloadIdentity.properties.principalId" in reader
    assert "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd" in reader
    assert "groupIds: [\n            'account'" in endpoint
    assert "keyVaultPrivateEndpoint" in endpoint
    assert "privatelink.openai.azure.com" in dns
    assert "enableAzureOpenAIInference bool = false" in APP
    assert "COSMANEURA_AZURE_OPENAI_ENABLED" in api
    assert "AZURE_OPENAI_ENDPOINT" in api
    assert "AZURE_OPENAI_DEPLOYMENT" in api
    assert "AZURE_OPENAI_MODEL" in api
    assert "AZURE_OPENAI_API_VERSION" in api
    for forbidden in (
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT",
        "COSMANEURA_AZURE_OPENAI_ENABLED",
    ):
        assert forbidden not in worker
        assert forbidden not in migration
    assert "anthropic" not in (FOUNDATION + APP).casefold()

    foundation_parameters = json.loads(
        (ROOT / "infra" / "foundation.parameters.example.json").read_text(
            encoding="utf-8",
        ),
    )["parameters"]
    app_parameters = json.loads(
        (ROOT / "infra" / "app.parameters.example.json").read_text(
            encoding="utf-8",
        ),
    )["parameters"]
    assert foundation_parameters["provisionAzureOpenAI"]["value"] is False
    assert foundation_parameters["azureOpenAIDeploymentSku"]["value"] == (
        "DataZoneStandard"
    )
    assert foundation_parameters["azureOpenAIDeploymentCapacity"]["value"] == 200
    assert app_parameters["enableAzureOpenAIInference"]["value"] is False
    assert app_parameters["azureOpenAIDeploymentName"]["value"] == (
        foundation_parameters["azureOpenAIDeploymentName"]["value"]
    )
    assert app_parameters["azureOpenAIModelName"]["value"] == (
        foundation_parameters["azureOpenAIModelName"]["value"]
    )


def test_container_installs_inference_and_metrics_without_auto_instrumentation():
    project = (ROOT / "cosmaneura_billing" / "pyproject.toml").read_text(
        encoding="utf-8",
    )
    dockerfile = (ROOT / "cosmaneura_billing" / "Dockerfile").read_text(
        encoding="utf-8",
    )

    assert "azure-monitor-opentelemetry-exporter" in project
    assert "opentelemetry-sdk" in project
    assert "azure-identity" in project
    assert "openai" in project
    assert "[api,inference,postgres,messaging,telemetry]" in dockerfile
    assert "from openai import AzureOpenAI" in dockerfile
    assert "opentelemetry-instrumentation" not in project
    assert "azure-monitor-opentelemetry\"" not in project
    assert "opentelemetry-instrument" not in dockerfile


def test_ci_runs_fail_closed_log_scanner():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8",
    )
    assert "python -m cosmaneura_billing.log_scanner --self-test" in workflow
