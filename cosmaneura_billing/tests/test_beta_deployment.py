"""Local compiled-template and source checks; no Azure account or deployment."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from test_p4_azure_infra import _resource, _parameter_default

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / 'infra/app.bicep').read_text()
COMMS = (ROOT / 'infra/communications.bicep').read_text()


def test_disabled_examples_cover_every_beta_latch_without_credentials():
    params = json.loads((ROOT / 'infra/app.parameters.example.json').read_text())['parameters']
    for key in ('provisionCommunications', 'enableCommunications', 'enableCommunicationsScheduler',
                'enableCommunicationsMessaging', 'activateCommunicationsMessaging',
                'communicationsExternalIngress', 'enableBillingHeader',
                'enableProviderHandoff', 'communicationsTestMode'):
        assert _parameter_default(APP, key) == 'false'
        assert params[key]['value'] is False
    assert params['athenaReviewMode']['value'] == 'disabled'
    assert params['communicationsEnabledChannels']['value'] == 'sms'
    assert params['modelDataPolicyJson']['value'] == ''
    for key in ('twilioAccountSid', 'twilioFromNumber', 'athenaReviewClientId',
                'directContractRosterSecretName', 'directContractRosterSecretVersion',
                'twilioMessagingServiceSid', 'communicationsTestRecipients'):
        assert params[key]['value'] == ''
    assert 'AuthToken string' not in APP and 'athenaReviewClientSecret string' not in APP


def test_operational_model_policy_is_api_only_and_external_gate_stays_closed_when_empty():
    api = _resource(APP, 'application')
    assert "@secure()\n@maxLength(8192)" in APP
    assert "param modelDataPolicyJson string = ''" in APP
    assert 'externalIngress && !validatedAzureOpenAISyntheticOnly && empty(modelDataPolicyJson)' in APP
    assert "name: 'COSMANEURA_MODEL_DATA_POLICY_JSON', value: modelDataPolicyJson" in api
    for body in (COMMS, _resource(APP, 'migrationJob'), _resource(APP, 'chaseWorkerJob')):
        assert 'COSMANEURA_MODEL_DATA_POLICY_JSON' not in body


def test_public_sender_and_new_secret_names_fail_closed_before_provisioning():
    assert '!startsWith(twilioAccountSid, \'AC\')' in APP and '!empty(twilioAccountNonHex)' in APP
    assert '!empty(twilioSenderNonDigits)' in APP and "startsWith(twilioFromNumber, '+0')" in APP
    assert 'accountSid: validatedTwilioAccountSid' in APP
    assert 'fromNumber: validatedTwilioFromNumber' in APP
    assert 'betaSecretNameConflict' in APP and 'athenaReservedSecretNames' in APP
    assert "fail('Beta credential names" in APP
    assert "fail('Twilio credential must have a dedicated" in COMMS


def test_only_api_receives_review_and_athena_credentials_and_header_flag():
    api = _resource(APP, 'application')
    worker, callback = _resource(COMMS, 'worker'), _resource(COMMS, 'callback')
    assert "name: 'COSMANEURA_REVIEW_CONTEXT_SIGNING_KEY', secretRef: 'review-signing-key'" in api
    assert "name: 'COSMANEURA_BILLING_HEADER_ENABLED', value: string(enableBillingHeader)" in api
    assert "name: 'COSMANEURA_PROVIDER_HANDOFF_ENABLED', value: string(validatedProviderHandoff)" in api
    assert "name: 'COSMANEURA_ATHENA_CLIENT_SECRET', secretRef: 'athena-review-client-secret'" in api
    assert "athenaReviewCredentialsRequired ? [" in api
    assert 'COSMANEURA_TWILIO_AUTH_TOKEN' not in api and 'worker-database-url' not in api
    for source in (COMMS, worker, callback):
        for prohibited in ('COSMANEURA_ATHENA_', 'COSMANEURA_REVIEW_CONTEXT_SIGNING_KEY',
                           'COSMANEURA_ENTRA_', "name: 'DATABASE_URL'", 'COSMANEURA_AZURE_OPENAI'):
            assert prohibited not in source
    for symbol, scope in (('reviewSigningReader', 'reviewSigningSecret'),
                          ('reviewAthenaReader', 'reviewAthenaSecret'), ('apiRosterReader', 'rosterSecret')):
        assert f'scope: {scope}' in _resource(APP, symbol)


def test_callback_has_no_dispatch_authority_or_outbound_transport():
    callback = _resource(COMMS, 'callback')
    assert 'webhooks:create_app' in callback and "'--no-access-log'" in callback
    assert 'communications.worker' not in callback
    assert "secrets: serviceSecrets[1]" in callback
    for name in ('COSMANEURA_CHASE_SCHEDULER_ENABLED', 'COSMANEURA_PATIENT_MESSAGING_ENABLED',
                 'COSMANEURA_PATIENT_MESSAGING_OPERATIONALLY_ACTIVATED'):
        assert f"name: '{name}', value: 'false'" in callback
    assert 'COSMANEURA_CHASE_GATE_HMAC_KEY' not in callback
    assert 'volumeMounts' not in callback
    assert 'external: externalIngress' in callback and 'allowInsecure: false' in callback
    assert 'applicationAuth' not in COMMS
    for identity_index in (0, 1):
        assert f"'${{identities[{identity_index}].id}}': {{}}" in COMMS


def test_worker_is_one_bounded_nonretrying_job_with_legacy_lane_separate():
    worker = _resource(COMMS, 'worker')
    assert "triggerType: enableScheduler ? 'Schedule' : 'Manual'" in worker
    assert 'replicaRetryLimit: 0' in worker and 'replicaTimeout: 600' in worker
    assert 'parallelism: 1' in worker and 'ingress:' not in worker
    assert "'cosmaneura_billing.chase.communications.worker'" in worker
    assert 'enableChaseScheduler || enablePatientMessaging || activatePatientMessaging' in APP
    assert 'enableCommunicationsMessaging != activateCommunicationsMessaging' in APP
    assert '!validatedCommunicationsScheduler || !communicationsExternalIngress' in APP
    assert "'cosmaneura_billing.chase.worker'" in _resource(APP, 'chaseWorkerJob')


def test_pinned_roster_mount_does_not_mask_application_cache_or_other_state():
    for source in (_resource(APP, 'application'), _resource(COMMS, 'worker')):
        assert "mountPath: '/var/lib/cosmaneura/roster'" in source
        assert "mountPath: '/var/lib/cosmaneura'" not in source
        assert "storageType: 'Secret'" in source
        assert "path: 'direct_contract_patients.json'" in source
    assert "'/var/lib/cosmaneura/roster/direct_contract_patients.json'" in APP
    assert 'rosterSecretVersion: validatedRosterSecretVersion' in APP
    assert '${validatedRosterSecretName}/${validatedRosterSecretVersion}' in APP
    assert '${validatedRosterSecretName}/${validatedRosterVersion}' in COMMS
    assert 'length(directContractRosterSecretVersion) != 32' in APP


@pytest.fixture(scope='module')
def compiled(tmp_path_factory):
    compiler = shutil.which('bicep') or str(Path.home() / '.azure/bin/bicep')
    if not Path(compiler).is_file():
        pytest.skip('Local Bicep compiler is unavailable; source tests still run')
    cache = tmp_path_factory.mktemp('bicep')
    environment = {k: v for k, v in os.environ.items() if k in {'PATH', 'HOME', 'LANG', 'TMPDIR'}}
    environment['DOTNET_BUNDLE_EXTRACT_BASE_DIR'] = str(cache)
    result = subprocess.run([compiler, 'build', str(ROOT / 'infra/app.bicep'), '--no-restore', '--stdout'],
        capture_output=True, text=True, env=environment, timeout=60)
    assert result.returncode == 0, result.stderr
    assert 'Warning' not in result.stderr
    return json.loads(result.stdout)


def test_compiled_addon_is_conditional_and_digest_bound(compiled):
    module = next(r for r in compiled['resources'] if r['type'] == 'Microsoft.Resources/deployments'
                  and r['name'] == 'communications-runtime')
    assert module['condition'] == "[parameters('provisionCommunications')]"
    api = next(r for r in compiled['resources'] if r['type'] == 'Microsoft.App/containerApps')
    image = module['properties']['parameters']['image']['value']
    assert image == api['properties']['template']['containers'][0]['image']
    assert 'validatedImageDigest' in image and 'imageTag' not in image
    template = module['properties']['template']
    runtime = [r for r in template['resources'] if r['type'] in {'Microsoft.App/jobs', 'Microsoft.App/containerApps'}]
    assert len(runtime) == 2
    assert all(r['properties']['template']['containers'][0]['image'] == "[parameters('image')]" for r in runtime)
    readers = [r for r in template['resources'] if r['type'] == 'Microsoft.Authorization/roleAssignments'
               and r['properties']['roleDefinitionId'] == "[variables('secretReaderRole')]"]
    assert len(readers) == 4
    assert all('Microsoft.KeyVault/vaults/secrets' in r['scope'] for r in readers)
    assert 'authConfigs' not in json.dumps(template)


def test_compiled_api_has_required_signer_and_unchanged_auth_gate(compiled):
    api = next(r for r in compiled['resources'] if r['type'] == 'Microsoft.App/containerApps')
    environments = api['properties']['template']['containers'][0]['env']
    assert 'COSMANEURA_REVIEW_CONTEXT_SIGNING_KEY' in str(environments)
    auth = next(r for r in compiled['resources'] if r['type'].endswith('/authConfigs'))
    assert auth['properties']['platform']['enabled'] is True
    assert auth['properties']['globalValidation']['unauthenticatedClientAction'] == 'Return401'


def test_compiled_configured_athena_requires_environment_and_keeps_credentials_api_only(compiled):
    mode = compiled['parameters']['athenaReviewMode']
    assert mode['defaultValue'] == 'disabled' and mode['allowedValues'] == ['disabled', 'preview', 'configured']
    assert compiled['parameters']['athenaReviewCredentialEnvironment']['defaultValue'] == ''
    guard = compiled['variables']['validatedAthenaReviewCredentialEnvironment']
    assert "fail(" in guard and "'configured'" in guard and "'production'" in guard
    required = compiled['variables']['athenaReviewCredentialsRequired']
    assert "'preview'" in required and "'configured'" in required
    api = next(r for r in compiled['resources'] if r['type'] == 'Microsoft.App/containerApps')
    env = str(api['properties']['template']['containers'][0]['env'])
    assert 'COSMANEURA_ATHENA_CREDENTIAL_ENVIRONMENT' in env and 'validatedAthenaReviewCredentialEnvironment' in env
    assert 'athenaReviewCredentialsRequired' in env and 'athena-review-client-secret' in env
    for parameter, variable in (('athenaDeploymentId', 'COSMANEURA_ATHENA_DEPLOYMENT_ID'),
                                ('athenaStorageId', 'COSMANEURA_ATHENA_STORAGE_ID')):
        assert compiled['parameters'][parameter]['defaultValue'] == ''
        assert compiled['parameters'][parameter]['maxLength'] == 128
        assert variable in env and parameter in env


def test_compiled_texting_parameters_bind_api_worker_and_callback_identically(compiled):
    module = next(r for r in compiled['resources'] if r['type'] == 'Microsoft.Resources/deployments'
                  and r['name'] == 'communications-runtime')
    addon = module['properties']['template']
    values = module['properties']['parameters']
    assert values['communicationsEnabledChannels']['value'] == "[parameters('communicationsEnabledChannels')]"
    assert values['twilioMessagingServiceSid']['value'] == "[variables('validatedTwilioMessagingServiceSid')]"
    for template in (compiled, addon):
        channels = template['parameters']['communicationsEnabledChannels']
        assert channels['type'] == 'string' and channels['defaultValue'] == 'sms'
        assert channels['allowedValues'] == ['sms']
        for invalid in ('', 'voice', 'SMS', 'sms,sms', 'sms,', 'sms, automated_voice'):
            assert invalid not in channels['allowedValues']
        service = template['parameters']['twilioMessagingServiceSid']
        assert service['type'] == 'string' and service['defaultValue'] == '' and service['maxLength'] == 34
    # Both entrypoints retain the same fail-closed format guard, including a
    # case-sensitive array membership check for the exact MG prefix.
    for name in ('validatedTwilioMessagingServiceSid', 'twilioMessagingServiceNonHex'):
        assert compiled['variables'][name] == addon['variables'][name]
    validation = addon['variables']['validatedTwilioMessagingServiceSid']
    assert "not(empty(parameters('twilioMessagingServiceSid')))" in validation
    assert "not(equals(length(parameters('twilioMessagingServiceSid')), 34))" in validation
    assert "not(contains(createArray('MG'), take(parameters('twilioMessagingServiceSid'), 2)))" in validation
    assert "not(empty(variables('twilioMessagingServiceNonHex')))" in validation
    assert "fail('twilioMessagingServiceSid must be empty or MG followed by 32 hexadecimal characters')" in validation
    assert addon['variables']['twilioMessagingServiceNonHex'].startswith(
        "[reduce(split('0,1,2,3,4,5,6,7,8,9,a,b,c,d,e,f', ','), toLower(skip(parameters('twilioMessagingServiceSid'), 2))")
    expected = {
        'COSMANEURA_COMMUNICATIONS_ENABLED_CHANNELS': "[parameters('communicationsEnabledChannels')]",
        'COSMANEURA_TWILIO_MESSAGING_SERVICE_SID': "[variables('validatedTwilioMessagingServiceSid')]",
    }
    common = {item['name']: item.get('value') for item in addon['variables']['commonEnv']}
    assert {name: common[name] for name in expected} == expected
    assert common['COSMANEURA_TWILIO_FROM_NUMBER'] == "[parameters('fromNumber')]"
    for resource in addon['resources']:
        if resource['type'] in {'Microsoft.App/jobs', 'Microsoft.App/containerApps'}:
            environment = resource['properties']['template']['containers'][0]['env']
            assert environment.startswith("[concat(variables('commonEnv'),")
            assert not any(name in environment for name in expected)  # no later override
    api = next(r for r in compiled['resources'] if r['type'] == 'Microsoft.App/containerApps')
    environment = api['properties']['template']['containers'][0]['env']
    for name, value in expected.items():
        assert environment.count(name) == 1
        assert f"createObject('name', '{name}', 'value', {value[1:-1]})" in environment
    assert "createObject('name', 'COSMANEURA_TWILIO_FROM_NUMBER', 'value', variables('validatedTwilioFromNumber'))" in environment


def test_compiled_test_scope_is_secure_across_module_and_only_reaches_sending_roles(compiled):
    module = next(r for r in compiled['resources'] if r['type'] == 'Microsoft.Resources/deployments'
                  and r['name'] == 'communications-runtime')
    addon = module['properties']['template']
    for template in (compiled, addon):
        parameter = template['parameters']['communicationsTestRecipients']
        assert parameter['type'].casefold() == 'securestring' and parameter['defaultValue'] == ''
        assert template['parameters']['communicationsTestMode']['defaultValue'] is False
        # No recipient value, hash, or secret reference is exported.
        assert 'communicationsTestRecipients' not in json.dumps(template['outputs'])
        assert 'communications-test-recipients' not in json.dumps(template['outputs'])
    values = module['properties']['parameters']
    assert values['communicationsTestRecipients']['value'] == "[parameters('communicationsTestRecipients')]"
    assert values['communicationsTestMode']['value'] == "[variables('validatedCommunicationsTestMode')]"
    api = next(r for r in compiled['resources'] if r['type'] == 'Microsoft.App/containerApps')
    worker = next(r for r in addon['resources'] if r['type'] == 'Microsoft.App/jobs')
    callback = next(r for r in addon['resources'] if r['type'] == 'Microsoft.App/containerApps')
    for resource in (api, worker):
        secrets = json.dumps(resource['properties']['configuration'])
        assert "if(empty(parameters('communicationsTestRecipients')), createArray()," in secrets
        assert "createObject('name', 'communications-test-recipients', 'value', parameters('communicationsTestRecipients'))" in secrets
        env = resource['properties']['template']['containers'][0]['env']
        assert 'communicationsTestRecipientEnv' in env
        assert "parameters('communicationsTestRecipients')" not in env
    # Callback validates the same mode/channel but never gets outgoing scope.
    assert callback['properties']['configuration']['secrets'] == "[variables('serviceSecrets')[1]]"
    service_secrets = next(value for value in addon['variables']['copy'] if value['name'] == 'serviceSecrets')
    assert 'communicationsTestRecipients' not in json.dumps(service_secrets)
    callback_env = callback['properties']['template']['containers'][0]['env']
    assert "createObject('name', 'COSMANEURA_COMMUNICATIONS_TEST_RECIPIENTS', 'value', '')" in callback_env
    assert 'communicationsTestRecipientEnv' not in callback_env
    assert 'communications-test-recipients' not in callback_env


def test_compiled_empty_test_scope_has_no_dangling_secret_and_mode_constraints_match(compiled):
    module = next(r for r in compiled['resources'] if r['type'] == 'Microsoft.Resources/deployments'
                  and r['name'] == 'communications-runtime')
    addon = module['properties']['template']
    expected_env = ("[if(empty(parameters('communicationsTestRecipients')), "
                    "createObject('name', 'COSMANEURA_COMMUNICATIONS_TEST_RECIPIENTS', 'value', ''), "
                    "createObject('name', 'COSMANEURA_COMMUNICATIONS_TEST_RECIPIENTS', 'secretRef', 'communications-test-recipients'))]")
    for template in (compiled, addon):
        assert template['variables']['communicationsTestRecipientEnv'] == expected_env
        guard = template['variables']['validatedCommunicationsTestMode']
        assert "and(not(parameters('communicationsTestMode')), not(empty(parameters('communicationsTestRecipients'))))" in guard
        assert "and(parameters('communicationsTestMode'), not(equals(parameters('communicationsEnabledChannels'), 'sms')))" in guard
        assert "fail('Test recipients require test mode, and test mode permits SMS only')" in guard
        # No recipient value is interpolated into failure text.
        assert 'format(' not in guard
    assert compiled['variables']['validatedCommunicationsTestMode'] == addon['variables']['validatedCommunicationsTestMode']
    common = {item['name']: item for item in addon['variables']['commonEnv']}
    assert common['COSMANEURA_COMMUNICATIONS_TEST_MODE']['value'] == "[string(variables('validatedCommunicationsTestMode'))]"
    assert 'COSMANEURA_COMMUNICATIONS_TEST_RECIPIENTS' not in common


def test_compiled_provider_handoff_requires_header_without_new_authority(compiled):
    assert compiled['parameters']['enableProviderHandoff']['defaultValue'] is False
    guard = compiled['variables']['validatedProviderHandoff']
    assert "and(parameters('enableProviderHandoff'), not(parameters('enableBillingHeader')))" in guard
    assert "fail('Provider handoff requires enableBillingHeader; operational admission remains separate')" in guard
    api = next(r for r in compiled['resources'] if r['type'] == 'Microsoft.App/containerApps')
    env = api['properties']['template']['containers'][0]['env']
    assert env.count('COSMANEURA_PROVIDER_HANDOFF_ENABLED') == 1
    assert "createObject('name', 'COSMANEURA_PROVIDER_HANDOFF_ENABLED', 'value', string(variables('validatedProviderHandoff')))" in env
    for resource in compiled['resources']:
        if resource is not api and resource['type'] != 'Microsoft.Resources/deployments':
            assert 'COSMANEURA_PROVIDER_HANDOFF_ENABLED' not in json.dumps(resource)


def test_core_bridge_is_opt_in_separate_and_same_digest(compiled):
    params = json.loads((ROOT / 'infra/app.parameters.example.json').read_text())['parameters']
    for key in ('provisionCoreBridge', 'coreBridgeExternalIngress', 'enableCoreBridgeInference',
                'enableCoreBridgeAsyncGeneration', 'enableCoreBridgeGenerationContinuation'):
        assert compiled['parameters'][key]['defaultValue'] is False
        assert params[key]['value'] is False
    for key in ('coreBridgePolicyJson', 'coreBridgeModelDataPolicyJson'):
        assert compiled['parameters'][key]['type'].lower() == 'securestring'
        assert compiled['parameters'][key]['defaultValue'] == ''
    module = next(r for r in compiled['resources'] if r['type'] == 'Microsoft.Resources/deployments'
                  and r['name'] == 'core-workload-runtime')
    assert module['condition'] == "[variables('validatedCoreProvisioning')]"
    assert 'provisionCoreBridge' in compiled['variables']['validatedCoreProvisioning']
    api = next(r for r in compiled['resources'] if r['type'] == 'Microsoft.App/containerApps')
    assert module['properties']['parameters']['image']['value'] == api['properties']['template']['containers'][0]['image']
    assert 'COSMANEURA_CORE_BRIDGE_POLICY_JSON' not in json.dumps(api)
    assert 'coreBridgeDatabaseSecretName' not in json.dumps(api)
    addon = module['properties']['template']
    app = next(r for r in addon['resources'] if r['type'] == 'Microsoft.App/containerApps')
    container = app['properties']['template']['containers'][0]
    assert 'cosmaneura_billing.core_bridge:app' in container['args']
    assert '--no-access-log' in container['args']
    assert 'authConfigs' not in json.dumps(addon)
    assert app['identity']['type'] == 'UserAssigned'
    assert 'core-identity-' in module['properties']['parameters']['identityName']['value']


def test_core_bridge_credentials_and_authorities_do_not_cross_services(compiled):
    module = next(r for r in compiled['resources'] if r['type'] == 'Microsoft.Resources/deployments'
                  and r['name'] == 'core-workload-runtime')
    addon = module['properties']['template']
    app = next(r for r in addon['resources'] if r['type'] == 'Microsoft.App/containerApps')
    secrets = app['properties']['configuration']['secrets']
    assert {s['name'] for s in secrets} == {'bridge-database-url', 'review-signing-key'}
    assert all('identity' in s and 'keyVaultUrl' in s and 'value' not in s for s in secrets)
    readers = [r for r in addon['resources'] if r['type'] == 'Microsoft.Authorization/roleAssignments'
               and r['properties']['roleDefinitionId'] == "[variables('secretReaderRole')]"]
    assert len(readers) == 2
    assert all('Microsoft.KeyVault/vaults/secrets' in r['scope'] for r in readers)
    env = app['properties']['template']['containers'][0]['env']
    for name in ('COSMANEURA_TWILIO_AUTH_TOKEN', 'COSMANEURA_ATHENA_CLIENT_SECRET',
                 'COSMANEURA_CHASE_WORKER_DATABASE_URL', 'COSMANEURA_DB_RUNTIME_PASSWORD'):
        assert name not in env
    for name in ('COSMANEURA_COMMUNICATIONS_ENABLED', 'COSMANEURA_CHASE_SCHEDULER_ENABLED',
                 'COSMANEURA_PATIENT_MESSAGING_ENABLED', 'COSMANEURA_PATIENT_MESSAGING_OPERATIONALLY_ACTIVATED',
                 'COSMANEURA_AZURE_LANGUAGE_ENABLED', 'COSMANEURA_AZURE_LANGUAGE_SHADOW_ENABLED'):
        assert f"createObject('name', '{name}', 'value', 'false')" in env
    assert 'provider' not in json.dumps(addon.get('outputs', {})).lower()
    assert 'validatedCoreDatabaseRole' in module['properties']['parameters']['databaseRole']['value']
    assert 'postgresAppLogin' in compiled['variables']['validatedCoreDatabaseRole']
    assert 'betaSecretNames' in compiled['variables']['validatedCoreDatabaseSecret']


def test_core_bridge_policy_model_and_network_boundaries_are_explicit(compiled):
    addon = next(r for r in compiled['resources'] if r['type'] == 'Microsoft.Resources/deployments'
                 and r['name'] == 'core-workload-runtime')['properties']['template']
    assert addon['parameters']['externalIngress']['defaultValue'] is False
    assert addon['parameters']['enableInference']['defaultValue'] is False
    assert 'empty(parameters(\'policyJson\'))' in addon['variables']['validatedIngress']
    for field in ('policyJson', 'modelDataPolicyJson', 'deploymentId', 'storageId'):
        assert f"empty(parameters('{field}'))" in addon['variables']['validatedInference']
    app = next(r for r in addon['resources'] if r['type'] == 'Microsoft.App/containerApps')
    environment = app['properties']['template']['containers'][0]['env']
    assert "'COSMANEURA_CORE_BRIDGE_POLICY_JSON', 'value', if(empty(parameters('policyJson')), ''," in environment
    assert '.managedIdentityClientId' in environment and '.clientId' in environment
    assert "fail('Core bridge policy managed identity must match this dedicated service')" in environment
    assert addon['variables']['healthPath'] == "[if(empty(parameters('policyJson')), '/api/live', '/api/ready')]"
    app = next(r for r in addon['resources'] if r['type'] == 'Microsoft.App/containerApps')
    assert app['properties']['configuration']['ingress']['external'] == "[variables('validatedIngress')]"
    probes = app['properties']['template']['containers'][0]['probes']
    assert [p['httpGet']['path'] for p in probes] == ["[variables('healthPath')]", "[variables('healthPath')]", '/api/live']
    reader = next(r for r in addon['resources'] if r['type'] == 'Microsoft.Authorization/roleAssignments'
                  and r['properties']['roleDefinitionId'].endswith("'5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')]"))
    assert reader['condition'] == "[variables('validatedInference')]"


def test_core_async_flags_reach_only_the_dedicated_receiver(compiled):
    module = next(r for r in compiled['resources'] if r['type'] == 'Microsoft.Resources/deployments'
                  and r['name'] == 'core-workload-runtime')
    addon = module['properties']['template']
    values = module['properties']['parameters']
    assert values['enableAsyncGeneration']['value'] == "[parameters('enableCoreBridgeAsyncGeneration')]"
    assert values['enableGenerationContinuation']['value'] == "[variables('validatedCoreGenerationContinuation')]"
    for key in ('enableAsyncGeneration', 'enableGenerationContinuation'):
        assert addon['parameters'][key]['defaultValue'] is False
    app = next(r for r in addon['resources'] if r['type'] == 'Microsoft.App/containerApps')
    container = app['properties']['template']['containers'][0]
    env = container['env']
    for name, value in (
        ('COSMANEURA_BILLING_HEADER_ASYNC_ENABLED', "parameters('enableAsyncGeneration')"),
        ('COSMANEURA_BILLING_HEADER_CONTINUATION_ENABLED', "variables('validatedGenerationContinuation')"),
    ):
        assert env.count(name) == 1
        assert f"createObject('name', '{name}', 'value', string({value}))" in env
        for other in compiled['resources']:
            if other is not module:
                assert name not in json.dumps(other)
    assert container['resources'] == {
        'cpu': "[if(parameters('enableAsyncGeneration'), json('1.0'), json('0.5'))]",
        'memory': "[if(parameters('enableAsyncGeneration'), '2Gi', '1Gi')]",
    }


def test_core_async_relationships_fail_closed_without_enabling_other_authority(compiled):
    addon = next(r for r in compiled['resources'] if r['type'] == 'Microsoft.Resources/deployments'
                 and r['name'] == 'core-workload-runtime')['properties']['template']
    for template, continuation, asynchronous, guard in (
        (compiled, 'enableCoreBridgeGenerationContinuation', 'enableCoreBridgeAsyncGeneration',
         'validatedCoreGenerationContinuation'),
        (addon, 'enableGenerationContinuation', 'enableAsyncGeneration', 'validatedGenerationContinuation'),
    ):
        assert template['variables'][guard] == (
            f"[if(and(parameters('{continuation}'), not(parameters('{asynchronous}'))), "
            "fail('Core bridge continuation requires async generation'), "
            f"parameters('{continuation}'))]"
        )
    provisioning = compiled['variables']['validatedCoreProvisioning']
    assert "parameters('enableCoreBridgeAsyncGeneration')" in provisioning
    assert "parameters('enableCoreBridgeGenerationContinuation')" in provisioning
    assert "not(parameters('provisionCoreBridge'))" in provisioning and 'fail(' in provisioning
    for guard in ('validatedInference', 'validatedIngress', 'validatedPolicy'):
        expression = addon['variables'].get(guard, '')
        assert 'enableAsyncGeneration' not in expression and 'enableGenerationContinuation' not in expression
    # The existing model permission still depends only on the separate inference choice.
    model = next(r for r in addon['resources'] if r['type'] == 'Microsoft.Authorization/roleAssignments'
                 and r['properties']['roleDefinitionId'].endswith("'5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')]"))
    assert model['condition'] == "[variables('validatedInference')]"
