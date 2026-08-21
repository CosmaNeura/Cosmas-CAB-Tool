"""Static invariants for dormant, private Azure AI Language provisioning."""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "infra" / "app.bicep").read_text(encoding="utf-8")
FOUNDATION = (ROOT / "infra" / "foundation.bicep").read_text(encoding="utf-8")
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


def test_language_account_is_private_f0_and_provisioning_is_default_off():
    account = _resource(APP, "azureLanguage")

    assert "if (provisionAzureLanguage)" in account
    assert "kind: 'TextAnalytics'" in account
    assert "name: 'F0'" in account
    assert "customSubDomainName: azureLanguageName" in account
    assert "disableLocalAuth: true" in account
    assert "publicNetworkAccess: 'Disabled'" in account
    assert "defaultAction: 'Deny'" in account
    assert "param provisionAzureLanguage bool = false" in APP
    assert "param enableAzureLanguageExtraction bool = false" in APP
    assert "param enableAzureLanguageShadow bool = false" in APP
    assert "enableAzureLanguageExtraction requires provisionAzureLanguage" in APP
    assert (
        "enableAzureLanguageExtraction requires enableAzureOpenAIInference"
        in APP
    )
    assert "enableAzureLanguageShadow requires provisionAzureLanguage" in APP
    assert "enableAzureLanguageShadow requires enableAzureOpenAIInference" in APP
    assert "enableAzureLanguageShadow and enableAzureLanguageExtraction are mutually exclusive" in APP


def test_only_api_uami_receives_language_reader_role():
    assignment = _resource(APP, "apiAzureLanguageReader")
    role_id = "7628b7b8-a8b2-4cdc-b46f-e9b35248918e"

    assert "if (provisionAzureLanguage)" in assignment
    assert "scope: azureLanguage" in assignment
    assert "principalId: identity.properties.principalId" in assignment
    assert "principalType: 'ServicePrincipal'" in assignment
    assert role_id in assignment
    assert "migrationIdentity" not in assignment
    assert "chaseWorkerIdentity" not in assignment
    assert APP.count(role_id) == 1
    assert role_id not in FOUNDATION


def test_language_uses_shared_private_subnet_and_private_dns():
    subnet = _resource(APP, "privateEndpointsSubnet")
    endpoint = _resource(APP, "azureLanguagePrivateEndpoint")
    dns = _resource(APP, "azureLanguagePrivateDns")
    dns_link = _resource(APP, "azureLanguageDnsLink")
    dns_group = _resource(APP, "azureLanguagePrivateDnsGroup")

    assert "existing" in subnet.split("{", 1)[0]
    assert "name: 'private-endpoints'" in subnet
    assert "id: privateEndpointsSubnet.id" in endpoint
    assert "privateLinkServiceId: azureLanguage.id" in endpoint
    assert "groupIds: [\n            'account'" in endpoint
    assert "apiAzureLanguageReader\n    azureLanguageDnsLink" in endpoint
    assert any(
        line.strip() == "name: 'privatelink.cognitiveservices.azure.com'"
        for line in dns.splitlines()
    )
    assert "id: vnet.id" in dns_link
    assert "privateDnsZoneId: azureLanguagePrivateDns.id" in dns_group
    assert "must not overlap a foundation deployment" in INFRA_README


def test_language_environment_is_api_only_and_endpoint_is_enable_gated():
    api = _resource(APP, "application")
    worker = _resource(APP, "chaseWorkerJob")
    migration = _resource(APP, "migrationJob")

    assert "name: 'COSMANEURA_AZURE_LANGUAGE_ENABLED'" in api
    assert "value: azureLanguageEnabledValue" in api
    assert "name: 'COSMANEURA_AZURE_LANGUAGE_SHADOW_ENABLED'" in api
    assert "value: azureLanguageShadowEnabledValue" in api
    assert "validatedAzureLanguageExtractionEnabled || validatedAzureLanguageShadowEnabled ? [" in api
    assert "name: 'AZURE_LANGUAGE_ENDPOINT'" in api
    assert "value: azureLanguage!.properties.endpoint" in api
    assert "name: 'AZURE_LANGUAGE_API_VERSION'" in api
    assert "apiAzureLanguageReader" in api
    assert "azureLanguagePrivateDnsGroup" in api
    for name in (
        "COSMANEURA_AZURE_LANGUAGE_ENABLED",
        "COSMANEURA_AZURE_LANGUAGE_SHADOW_ENABLED",
        "AZURE_LANGUAGE_ENDPOINT",
        "AZURE_LANGUAGE_API_VERSION",
    ):
        assert name not in worker
        assert name not in migration
    for prohibited in (
        "AZURE_LANGUAGE_KEY",
        "AZURE_LANGUAGE_AD_TOKEN",
        "AZURE_AI_LANGUAGE_KEY",
    ):
        assert prohibited not in APP


def test_language_examples_and_outputs_preserve_dormant_state():
    parameters = json.loads(
        (ROOT / "infra" / "app.parameters.example.json").read_text(
            encoding="utf-8"
        )
    )["parameters"]

    assert parameters["provisionAzureLanguage"]["value"] is False
    assert parameters["enableAzureLanguageExtraction"]["value"] is False
    assert parameters["enableAzureLanguageShadow"]["value"] is False
    assert parameters["azureLanguageApiVersion"]["value"] == "2023-04-01"
    assert "output azureLanguageProvisioned bool" in APP
    assert "output azureLanguageExtractionEnabled bool" in APP
    assert "output azureLanguageShadowEnabled bool" in APP
    assert "output azureLanguageAccountName string" in APP
    assert "output azureLanguageEndpoint string" in APP
