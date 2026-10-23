"""External source policy is opt-in, literal, bounded and unambiguous."""
import copy
import os
from unittest.mock import patch

import pytest
import yaml

from omniflow.config import load_config
from omniflow.exceptions import OmniFlowError


@pytest.fixture
def policy(tmp_path):
    with patch.dict(os.environ, {}, clear=True):
        def load(external=None, **overrides):
            impact = {"enabled": True, **overrides.pop("impact", {})}
            if external is not None:
                impact["omni_repository"] = external
            raw = {"checks": {"dbt_impact": impact}, **overrides}
            path = tmp_path / ".omniflow.yml"
            path.write_text(yaml.safe_dump(raw))
            return load_config(str(path))
        yield load


SOURCE = {"repository": "fixture/omni", "ref": "release/stable", "model_paths": ["omni/sales"]}


def test_single_repository_default_and_cross_repo_opt_in(policy):
    assert policy().dbt_impact.omni_repository is None
    external = policy(SOURCE).dbt_impact.omni_repository
    assert external.repository == "fixture/omni"
    assert external.ref == "release/stable"
    assert external.model_paths == ["omni/sales"]
    assert external.allow_repository_diagnostics is False
    assert policy({**SOURCE, "model_paths": ["."]}).dbt_impact.omni_repository.model_paths == ["."]


@pytest.mark.parametrize("key,value", [
    ("repository", "https://github.com/fixture/omni"), ("repository", "../omni"),
    ("repository", "${SOURCE_REPO}"), ("ref", "refs/pull/12/head"),
    ("ref", "a" * 40), ("ref", "main..other"), ("ref", "release//stable"),
    ("model_paths", []), ("model_paths", ["omni", "omni/sales"]),
    ("model_paths", [".", "omni"]), ("model_paths", ["../omni"]),
    ("model_paths", ["omni/${ROOT}"]), ("model_paths", ["omni/../sales"]),
    ("model_paths", ["omni//sales"]), ("model_paths", ["omni", "omni"]),
    ("allow_repository_diagnostics", "${DISCLOSURE}"), ("unexpected", True),
])
def test_unsafe_or_mutable_source_policy_is_rejected(policy, key, value):
    source = copy.deepcopy(SOURCE)
    source[key] = value
    with pytest.raises(OmniFlowError):
        policy(source)


@pytest.mark.parametrize("overrides", [
    {"impact": {"enabled": False}}, {"impact": {"omni_yaml_paths": ["local/omni"]}},
    {"deployment": {"breaking_change_hold": {"enabled": True}}},
    {"deployment": {"dbt_sync": {"enabled": True}}},
])
def test_unsupported_mixed_source_and_deployment_modes_are_explicit(policy, overrides):
    with pytest.raises(OmniFlowError) as error:
        policy(SOURCE, **overrides)
    assert error.value.reason_code == "omni_repository_configuration_invalid"
