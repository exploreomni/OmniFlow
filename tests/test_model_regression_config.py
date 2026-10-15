import os
from unittest.mock import patch

import pytest

from omniflow.config import ModelValidationSettings, load_config
from omniflow.exceptions import ConfigError


@pytest.fixture(autouse=True)
def isolated_environment():
    with patch.dict(os.environ, {}, clear=True):
        yield


@pytest.mark.parametrize("value", [True, False])
def test_explicit_model_regression_policy_is_independent_of_warning_policy(tmp_path, value):
    path = tmp_path / "custom-policy.yml"
    path.write_text(
        "checks:\n  model_validation:\n"
        f"    fail_on_new_only: {str(value).lower()}\n    fail_on_warnings: true\n"
    )
    config = load_config(path)
    assert config.model_validation.fail_on_new_only is value
    assert config.model_validation.fail_on_warnings is True
    assert config.content_validation.fail_on_new_only is False


def test_omitted_custom_policy_settings_remain_strict(tmp_path):
    path = tmp_path / "custom-policy.yml"
    path.write_text("checks:\n  model_validation:\n    enabled: true\n")
    config = load_config(path)
    assert config.model_validation.fail_on_new_only is False
    assert config.content_validation.fail_on_new_only is False
    assert config.model_validation.fail_on_warnings is False
    assert ModelValidationSettings().fail_on_new_only is False


@pytest.mark.parametrize("model_value", [None, False, True])
@pytest.mark.parametrize("content_override", ["true", "false"])
def test_content_environment_override_never_changes_model_policy(tmp_path, monkeypatch, model_value, content_override):
    path = tmp_path / "custom-policy.yml"
    policy = "checks:\n  model_validation:\n    enabled: true\n"
    if model_value is not None:
        policy += f"    fail_on_new_only: {str(model_value).lower()}\n"
    path.write_text(policy)
    monkeypatch.setenv("OMNI_FAIL_ON_NEW_ONLY", content_override)
    config = load_config(path)
    assert config.content_validation.fail_on_new_only is (content_override == "true")
    assert config.model_validation.fail_on_new_only is (model_value is True)


@pytest.mark.parametrize("value", ["unexpected", "[]", "{}"])
def test_invalid_model_regression_policy_is_rejected(tmp_path, value):
    path = tmp_path / "custom-policy.yml"
    path.write_text(f"checks:\n  model_validation:\n    fail_on_new_only: {value}\n")
    with pytest.raises(ConfigError, match="model_validation.fail_on_new_only"):
        load_config(path)


def test_misspelled_model_regression_key_is_rejected(tmp_path):
    path = tmp_path / "custom-policy.yml"
    path.write_text("checks:\n  model_validation:\n    fail_on_new_ony: true\n")
    with pytest.raises(ConfigError, match="unsupported key"):
        load_config(path)
