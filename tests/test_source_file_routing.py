"""Only genuine Omni source families select a model for automatic validation."""

import json
from types import SimpleNamespace

import pytest

from omniflow.cli import cmd_route
from omniflow.config import _to_config
from omniflow.discovery import discover_contexts, select_model_contexts
from omniflow.exceptions import ConfigError
from omniflow.github.revalidation import _readiness_route


def registry(version, model_path):
    model = {
        "base_url": "https://synthetic.omni.example", "model_id": "model",
        "model_path": model_path, "base_branch": "main",
    }
    if version == 2:
        model.update(environment="test", git_follower=False, web_url="https://github.com/synthetic/repo")
    return {"version": version, "models": [model]}


@pytest.fixture(autouse=True)
def isolated_routing(monkeypatch):
    for name in ("OMNI_BASE_URL", "OMNI_MODEL_ID", "OMNI_BRANCH_ID", "OMNI_BRANCH_NAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    monkeypatch.setenv("GITHUB_HEAD_REF", "feature/test")
    monkeypatch.setattr("omniflow.discovery.github_event_payload", lambda: {
        "pull_request": {"base": {"ref": "main"}, "head": {"ref": "feature/test"}},
    })


def select(version, model_path, paths, *, marker=None, allow_skip=True):
    return select_model_contexts(
        registry(version, model_path), changed_files=paths, branch_name="feature/test",
        marker=marker, allow_skip=allow_skip,
    )


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("model_path", [".", "omni/Snowflake Analytics"])
def test_native_source_families_route_in_root_and_nested_models(version, model_path):
    prefix = "" if model_path == "." else model_path + "/"
    for name in (
        "model", "model.yaml", "relationships", "relationships.yml",
        "SCHEMA/orders.view", "SCHEMA/orders.view.yaml", "SCHEMA/orders.query.view.yml",
        "Sales.topic", "Sales.topic.yaml", "Sales.composite_topic.yml",
        "SCHEMA/joins.relationships.yaml", "雪/Order Details.view.yaml",
        "nested/model.yaml", "nested/relationships",
    ):
        contexts = select(version, model_path, [prefix + name])
        assert [context.model_id for context in contexts] == ["model"], name


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("model_path", [".", "omni/Snowflake Analytics"])
def test_ancillary_changes_inside_model_do_not_select_a_model(version, model_path):
    prefix = "" if model_path == "." else model_path + "/"
    for name in (
        "README.md", "script.py", "query.sql", "data.csv", ".DS_Store",
        "settings.json", "config.yaml", "views/settings.yaml", "topics/settings.yml",
        "orders.view.yaml.yaml",
        "orders.VIEW", "orders.YAML", "Model.yaml",
    ):
        assert select(version, model_path, [prefix + name]) == [], name


@pytest.mark.parametrize("version", [1, 2])
def test_repository_control_files_never_become_root_model_sources(version):
    for path in (
        ".omniflow.yml", ".omniflow.example.yml", ".omni/flow.json",
        ".github/workflows/omniflow.yml", ".omniflow/report.yaml",
        ".github/fixtures/orders.view.yaml", ".omni/model.yaml",
    ):
        assert select(version, ".", [path]) == [], path


@pytest.mark.parametrize("version", [1, 2])
def test_explicit_content_marker_and_local_doctor_still_select_context(version):
    for paths in ([], ["omni/README.md"], ["omni/config.yaml"]):
        contexts = select(version, "omni", paths, marker={"model_id": "model"})
        assert [context.model_id for context in contexts] == ["model"]
        assert len(select(version, "omni", paths, allow_skip=False)) == 1


@pytest.mark.parametrize("version", [1, 2])
def test_removed_or_renamed_source_remains_relevant_without_reading_checkout(version):
    # Immutable Git/REST inventories retain deleted names and both rename sides.
    for paths in (
        ["omni/removed.view.yaml"],
        ["omni/old.topic.yml", "docs/old.md"],
        ["omni/new.view.yaml", "tools/new.yaml"],
        ["omni/README.md", "omni/model.yaml"],
    ):
        assert len(select(version, "omni", paths)) == 1


@pytest.mark.parametrize("version", [1, 2])
def test_actual_source_outside_registered_scope_is_not_ignored(version):
    for path in ("other/orders.view.yaml", "other/model.yaml", "other/relationships"):
        with pytest.raises(ConfigError, match="outside every"):
            select(version, "omni", [path])
    assert select(version, "omni", ["other/views/config.yaml"]) == []


def test_pr_marker_cannot_hide_changed_models_or_unregistered_files(monkeypatch):
    flow = registry(1, "omni/first")
    second = dict(flow["models"][0], model_id="second", model_path="omni/second")
    flow["models"].append(second)
    monkeypatch.setattr("omniflow.discovery.load_flow_metadata", lambda *a, **kw: flow)
    monkeypatch.setattr("omniflow.discovery.load_pr_marker", lambda: {"model_id": "second"})
    changed = ["omni/first/orders.view.yaml", "omni/first/sales.topic.yml"]
    contexts = discover_contexts(auto=True, allow_skip=True, changed_files=changed)
    assert sorted(context.model_id for context in contexts) == ["model", "second"]
    with pytest.raises(ConfigError, match="outside every registered model_path"):
        discover_contexts(auto=True, allow_skip=True, changed_files=[*changed, "other/new.view.yaml"])


def test_missing_registry_only_blocks_recognized_source_families(monkeypatch):
    monkeypatch.setattr("omniflow.discovery.load_flow_metadata", lambda *a, **kw: None)
    monkeypatch.setattr("omniflow.discovery.load_pr_marker", lambda: {})
    for path in ("omni/orders.view.yaml", "omni/model.yaml", "omni/relationships"):
        with pytest.raises(ConfigError, match="trusted .omni/flow.json"):
            discover_contexts(auto=True, allow_skip=True, changed_files=[path])
    for path in ("omni/config.yaml", "omni/views/config.yaml", ".github/workflows/ci.yml"):
        assert discover_contexts(auto=True, allow_skip=True, changed_files=[path]) == []


def test_ancillary_model_files_do_not_suppress_independent_dbt_routing(monkeypatch, capsys):
    changed = ["omni/README.md", "omni/config.yaml", "models/orders.sql"]
    config = _to_config({"checks": {"dbt_impact": {"enabled": True}}}, None)
    monkeypatch.setattr("omniflow.cli.load_config", lambda *a, **kw: config)
    monkeypatch.setattr("omniflow.cli._validate_run_output_layout", lambda *a, **kw: None)
    monkeypatch.setattr("omniflow.cli._purge_restricted_path", lambda *a, **kw: None)
    monkeypatch.setattr("omniflow.discovery.load_flow_metadata", lambda *a, **kw: registry(1, "omni"))
    monkeypatch.setattr("omniflow.discovery.load_pr_marker", lambda: {})
    monkeypatch.setattr("omniflow.discovery.get_changed_files", lambda *a, **kw: list(changed))
    monkeypatch.setattr("omniflow.cli.get_dbt_changed_files", lambda: list(changed))
    args = SimpleNamespace(config=None, auto=True, format="json")
    assert cmd_route(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["should_run"] is True
    assert payload["requires_omni"] is False
    assert payload["model_count"] == 0
    assert changed == ["omni/README.md", "omni/config.yaml", "models/orders.sql"]


def test_readiness_uses_the_same_scoped_classifier_and_keeps_dbt_checks(monkeypatch):
    config = _to_config({"checks": {"dbt_impact": {"enabled": True}}}, None)
    flow = registry(1, "omni")
    monkeypatch.setattr("omniflow.discovery.load_flow_metadata", lambda *a, **kw: flow)
    monkeypatch.setattr("omniflow.github.revalidation.load_flow_metadata", lambda *a, **kw: flow)
    monkeypatch.setattr("omniflow.discovery.load_pr_marker", lambda: {})
    ancillary = ["omni/config.yaml", "omni/nested/settings.yaml", "omni/README.md"]
    assert _readiness_route(config, ancillary) == ("not_applicable", set(), 0)
    assert _readiness_route(config, [*ancillary, "models/orders.sql"]) == ("dbt_impact", set(), 1)
    for native in ("omni/model.yaml", "omni/nested/model.yaml", "omni/nested/relationships"):
        assert _readiness_route(config, [*ancillary, native]) == ("models", {"model"}, 0)
    for outside in ("elsewhere/model.yaml", "elsewhere/nested/model.yaml", "elsewhere/nested/relationships"):
        with pytest.raises(ConfigError, match="outside registered"):
            _readiness_route(config, [outside])
