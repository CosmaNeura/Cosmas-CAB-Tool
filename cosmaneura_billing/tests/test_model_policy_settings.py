"""A configured invalid policy must fail startup instead of opening legacy input."""
import pytest
from cosmaneura_billing.settings import Settings, SettingsError


def test_empty_model_policy_is_default():
    assert Settings.from_env({}).model_data_policy_json == ""


@pytest.mark.parametrize("value", [" ", "private-config-invalid", "{}", "x" * 8193])
def test_invalid_configured_policy_aborts_settings_without_echoing_content(value):
    with pytest.raises(SettingsError, match="^COSMANEURA_MODEL_DATA_POLICY_JSON is invalid$"):
        Settings.from_env({"COSMANEURA_MODEL_DATA_POLICY_JSON": value})
