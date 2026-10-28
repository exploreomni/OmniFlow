"""Omni Git path aliases must not turn malformed API metadata into identity."""

from types import SimpleNamespace

import pytest

from omniflow import cli
from omniflow.config import OmniRepositorySettings
from omniflow.discovery import ModelContext
from omniflow.exceptions import ConfigError

REPOSITORY = "fixture/models"


@pytest.fixture(autouse=True)
def isolated_pr_context(monkeypatch):
    monkeypatch.setattr(cli, "is_pull_request_event", lambda: False)


def configuration(actual_path, registered_path):
    context = ModelContext(
        base_url="https://fixture.omniapp.co", model_id="synthetic-model", model_path=registered_path,
        base_branch="main", git_provider="github", git_follower=False,
        web_url=f"https://github.com/{REPOSITORY}",
    )
    actual = {
        "modelPath": actual_path, "baseBranch": "main", "gitServiceProvider": "github",
        "gitFollower": False, "webUrl": context.web_url,
    }
    external = OmniRepositorySettings(REPOSITORY, "main", [registered_path])
    client = SimpleNamespace(get_git_configuration=lambda model_id: actual)
    return context, actual, external, client


@pytest.mark.parametrize("actual_path,registered_path", [
    (None, "."), ("", "."), ("/", "."), (".", "."),
    ("jaffle", "omni/jaffle"), ("jaffle/", "omni/jaffle"),
    ("jaffle", "jaffle"), ("omni", "omni"),
    ("Snowflake Analytics", "omni/Snowflake Analytics"),
    ("/omni/Snowflake Analytics/", "omni/Snowflake Analytics"),
    ("/bi/models/sales/", "bi/models/sales"),
    ("bi/models/sales/", "bi/models/sales"),
])
def test_supported_path_forms_work_for_external_target_and_doctor(actual_path, registered_path):
    context, actual, external, client = configuration(actual_path, registered_path)
    assert registered_path in cli._omni_git_model_paths(actual_path)
    cli._verify_external_impact_context(context, client, external)
    cli._verify_target_git_configuration(context, actual)
    assert cli._git_configuration_issues(context, actual) == []


def assert_path_mismatch(actual_path, registered_path):
    context, actual, external, client = configuration(actual_path, registered_path)
    with pytest.raises(ConfigError) as external_error:
        cli._verify_external_impact_context(context, client, external)
    assert external_error.value.reason_code == "omni_repository_target_mismatch"
    with pytest.raises(ConfigError) as target_error:
        cli._verify_target_git_configuration(context, actual)
    assert target_error.value.reason_code == "git_configuration_mismatch"
    issues = cli._git_configuration_issues(context, actual)
    assert len(issues) == 1
    assert issues[0].startswith("model_path expected ")


@pytest.mark.parametrize("actual_path", [[], {}, ["jaffle"], {"path": "jaffle"}, 123, True, 3.14])
def test_malformed_paths_produce_configuration_diagnostics_without_crash_or_coercion(actual_path):
    assert cli._omni_git_model_paths(actual_path) == set()
    # Even an identically spelled repository folder cannot authenticate a
    # non-string response value through doctor string coercion.
    assert_path_mismatch(actual_path, str(actual_path))


@pytest.mark.parametrize("actual_path,registered_path", [
    ("jaffle", "omni/other"), ("jaffle", "models/jaffle"),
    ("/jaffle", "omni/jaffle"), ("a/b", "omni/a/b"),
    ("a/b/", "omni/a/b"), ("Sales", "omni/sales"),
    ("/bi/sales", "omni/bi/sales"),
])
def test_mismatches_and_undocumented_nested_defaults_are_not_widened(actual_path, registered_path):
    assert_path_mismatch(actual_path, registered_path)
