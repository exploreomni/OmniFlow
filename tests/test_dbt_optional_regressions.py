"""Regression coverage for optional static/deployment checks; no live writes."""
import json
import os
import subprocess
from types import SimpleNamespace
from unittest import mock

import pytest

from omniflow import cli
from omniflow.breaking_hold import PENDING_RULE, evaluate_breaking_hold
from omniflow.config import BreakingChangeHoldSettings, DbtExposureSettings, DbtImpactSettings, load_config
from omniflow.dbt_impact import COVERAGE_RULE, ORPHANED_COLUMN_RULE, evaluate_dbt_impact
from omniflow.diagnostics import capture_private_reports
from omniflow.discovery import ModelContext
from omniflow.exceptions import SecurityPolicyError
from omniflow.exposures import run_dbt_exposure_enrichment
from omniflow.revision_data import pull_request_changed_files

VIEW = "sql_table_name: orders\ndimensions:\n  id: {}\n  total: {}\n"


@pytest.fixture(autouse=True)
def isolated_environment():
    with mock.patch.dict(os.environ, {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                     "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}, clear=True):
        yield


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def init(root, files):
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    for name, value in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    return commit(root)


def commit(root):
    git(root, "add", ".")
    git(root, "commit", "-m", "fixture")
    return git(root, "rev-parse", "HEAD")


@pytest.mark.parametrize("name", ["orders.sql", "café.sql", "order details.sql"])
@pytest.mark.parametrize("quote_paths", ["true", "false"])
def test_real_git_pending_hold_preserves_filenames(tmp_path, monkeypatch, name, quote_paths):
    path = f"models/{name}"
    base = init(tmp_path, {path: "select id from source"})
    (tmp_path / path).write_text("select id, total from source")
    head = commit(tmp_path)
    git(tmp_path, "config", "core.quotePath", quote_paths)
    monkeypatch.chdir(tmp_path)
    kwargs = {"diff_result": {"changes": [{"risk": "breaking"}]}, "changed_files": ["omni/orders.view"],
              "settings": BreakingChangeHoldSettings(enabled=True)}
    assert evaluate_breaking_hold(**kwargs, last_sync_sha=base)[0]["rule"] == PENDING_RULE
    assert evaluate_breaking_hold(**kwargs, last_sync_sha=head) == []


def evaluate(root, paths, **overrides):
    with mock.patch("omniflow.dbt_impact._sql_changes", return_value=({"orders": {"id"}}, {})):
        return evaluate_dbt_impact(changed_files=["models/orders.sql"], dbt_paths=["models"],
                                  settings=DbtImpactSettings(enabled=True, **overrides),
                                  omni_yaml_paths=paths, base_ref="main", repo_root=root)


@pytest.mark.parametrize("name", ["relationships.view", "order_relationships.view.yaml", "customer_relationship.view.yml"])
def test_relationship_named_views_are_indexed(tmp_path, name):
    (tmp_path / "omni").mkdir()
    (tmp_path / "omni" / name).write_text(VIEW)
    (tmp_path / "omni/relationships.yaml").write_text("[]")
    report, issues = evaluate(tmp_path, ["omni"])
    assert report["omni_views_indexed"] == 1
    assert report["omni_fields_indexed"] == 2
    assert [item["rule"] for item in issues] == [ORPHANED_COLUMN_RULE]


@pytest.mark.parametrize("strict", [True, False])
def test_one_missing_root_is_incomplete_even_with_valid_other_root(tmp_path, strict):
    (tmp_path / "omni").mkdir()
    (tmp_path / "omni/users.view").write_text("sql_table_name: users\ndimensions:\n  id: {}")
    report, issues = evaluate(tmp_path, ["omni", "missing"], fail_on_incomplete_coverage=strict)
    assert report["coverage_complete"] is False
    assert report["root_coverage"][1]["status"] == "missing"
    assert issues[0]["rule"] == COVERAGE_RULE
    assert issues[0]["severity"] == ("error" if strict else "warning")


def test_metadata_or_ancillary_yaml_does_not_prove_no_dependencies(tmp_path):
    (tmp_path / "omni").mkdir()
    for name, payload in [("model.yaml", "label: Model"), ("other.yml", "unrelated: value")]:
        (tmp_path / "omni" / name).write_text(payload)
        report, issues = evaluate(tmp_path, ["omni"])
        assert report["coverage_complete"] is False
        assert issues[0]["rule"] == COVERAGE_RULE


def test_local_root_cannot_traverse_internal_symlink(tmp_path):
    (tmp_path / "real/nested").mkdir(parents=True)
    (tmp_path / "real/nested/orders.view").write_text(VIEW)
    (tmp_path / "alias").symlink_to(tmp_path / "real", target_is_directory=True)
    with pytest.raises(SecurityPolicyError):
        evaluate(tmp_path, ["alias/nested"])


def pr_fixture(tmp_path, monkeypatch, head_view):
    policy = ("checks:\n  dbt_impact:\n    enabled: true\n  semantic_lint:\n    enabled: false\n"
              "contracts:\n  enabled: false\nreporting:\n  diagnostics:\n    mode: minimal\n")
    registry = {"version": 1, "models": [{"base_url": "https://omni.example", "model_id": "fixture",
                                         "model_path": "omni", "base_branch": "main"}]}
    base = init(tmp_path, {".omniflow.yml": policy, ".omni/flow.json": json.dumps(registry),
                          "models/orders.sql": "select id, total from source",
                          "omni/orders.view.yaml": VIEW, "omni/model.yaml": "label: Model"})
    git(tmp_path, "switch", "-c", "candidate")
    (tmp_path / "models/orders.sql").write_text("select total from source")
    if head_view is None:
        (tmp_path / "omni/orders.view.yaml").unlink()
    elif head_view:
        (tmp_path / "omni/orders.view.yaml").write_text(head_view)
    # The candidate's policy downgrade must never authorize itself.
    (tmp_path / ".omniflow.yml").write_text("checks:\n  dbt_impact:\n    enabled: false\n")
    head = commit(tmp_path)
    git(tmp_path, "checkout", "--detach", base)
    event = {"number": 1, "pull_request": {"number": 1, "body": "",
        "base": {"sha": base, "ref": "main", "repo": {"full_name": "example/fixture"}},
        "head": {"sha": head, "ref": "candidate", "repo": {"full_name": "example/fixture"}}}}
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event))
    for key, value in {"GITHUB_EVENT_NAME": "pull_request_target", "GITHUB_EVENT_PATH": str(event_path),
                       "GITHUB_BASE_REF": "main", "GITHUB_HEAD_REF": "candidate",
                       "GITHUB_REPOSITORY": "example/fixture"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)
    return base, head


@pytest.mark.parametrize("head_view,expected_code", [(VIEW + "# comment\n", 1),
                                                   ("sql_table_name: orders\ndimensions:\n  total: {}\n", 0),
                                                   (None, 0)])
def test_mixed_pr_uses_candidate_refs_once_without_executing_head(tmp_path, monkeypatch, head_view, expected_code):
    base, _ = pr_fixture(tmp_path, monkeypatch, head_view)
    changed = pull_request_changed_files()
    healthy = {"model_id": "fixture", "issues": [], "validation_complete": True, "policy_decision": "pass", "exit_code": 0}
    with mock.patch("omniflow.cli._run_context", return_value=(healthy, 0)) as context, \
         mock.patch("omniflow.cli.evaluate_dbt_impact", wraps=evaluate_dbt_impact) as impact, \
         capture_private_reports() as reports:
        assert cli.main(["run", "--auto"], changed_files=changed) == expected_code
    assert context.call_count == impact.call_count == 1
    check = next(item for item in reports[-1]["model_reports"] if item.get("validator") == "dbt_impact")
    assert check["base_omni_fields_indexed"] == 2
    assert check["omni_fields_indexed"] == (2 if expected_code else 0 if head_view is None else 1)
    assert check["coverage_complete"] is True
    assert git(tmp_path, "rev-parse", "HEAD") == base
    assert "OMNI_API_KEY" not in os.environ


@pytest.mark.parametrize("payload", [[{"error": "synthetic failure"}], [{"name": "x", "depends_on": [{}]}],
                                    [{"name": "x", "depends_on": [""]}], [{"name": "x"}], [{"exposure": None}]])
@pytest.mark.parametrize("strict", [True, False])
def test_unsupported_exposures_cannot_be_complete(payload, strict):
    client = SimpleNamespace(get_dbt_exposures=lambda *a, **k: payload)
    report, code = run_dbt_exposure_enrichment(client=client, model_id="fixture", branch_id=None,
        settings=DbtExposureSettings(enabled=True, fail_on_unavailable=strict))
    assert report["coverage_complete"] is False
    assert report["execution_status"] == "unavailable"
    assert code == int(strict)


@pytest.mark.parametrize("payload,coverage", [([], "available"),
    ([{"name": "dashboard", "depends_on": []}], "available"),
    ([{"dashboard_identifier": "fixture", "exposure": None}], "partial"),
    ([{"name": "dashboard", "depends_on": [{"unique_id": "model.orders"}]}], "available")])
def test_supported_exposure_controls(payload, coverage):
    report, code = run_dbt_exposure_enrichment(
        client=SimpleNamespace(get_dbt_exposures=lambda *a, **k: payload), model_id="fixture", branch_id=None,
        settings=DbtExposureSettings(enabled=True, fail_on_unavailable=True))
    assert report["summary"]["coverage_status"] == coverage
    assert code == 0


@pytest.mark.parametrize("branch_id", [None, "branch-fixture"])
@pytest.mark.parametrize("enabled,complete,decision,status", [(True, True, "pass", "passed"),
    (True, False, "advisory", "incomplete"), (False, False, "advisory", "not_run")])
def test_sync_preserves_incomplete_and_shared_scope_outputs(tmp_path, monkeypatch, branch_id, enabled, complete, decision, status):
    monkeypatch.chdir(tmp_path)
    policy = tmp_path / ".omniflow.yml"
    policy.write_text("deployment:\n  dbt_sync:\n    enabled: true\n")
    config = load_config(policy)
    config.semantic_lint.enabled = config.contracts.enabled = False
    config.dbt_sync.post_sync_validation = enabled
    context = ModelContext(base_url="https://omni.example", model_id="fixture", model_path="omni", base_branch="main")
    client = mock.Mock()
    client.get_model_metadata.return_value = {"model_id": "fixture", "connection_id": "connection-fixture"}
    client.list_refresh_affected_model_ids.return_value = ["fixture"]
    refresh = {"status": "completed", "issues": [], "raw_query_results_stored": False}
    validation = {"issues": [], "validation_complete": complete, "policy_decision": decision, "exit_code": 0}
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    args = SimpleNamespace(config=str(policy), auto=True, base_url=None, model_id=None, model_path=None,
        branch_id=None, branch_name=None, base_branch=None, refresh_mode=None, user_id=None, include_personal_folders=None)
    with mock.patch("omniflow.cli.load_config", return_value=config), \
         mock.patch("omniflow.cli.discover_deployment_contexts", return_value=[context]), \
         mock.patch("omniflow.cli.validate_dbt_sync_environment", return_value="main"), \
         mock.patch("omniflow.cli.require_sync_api_key", return_value="fixture-token"), \
         mock.patch("omniflow.cli._client_and_branch_for_context", return_value=(client, branch_id)), \
         mock.patch("omniflow.cli.run_dbt_sync", return_value=(refresh, 0)), \
         mock.patch("omniflow.cli._run_context", return_value=(validation, 0)), capture_private_reports() as reports:
        assert cli.cmd_dbt_sync(args) == 0
    report = reports[-1]
    assert report["policy_decision"] == decision
    assert report["validation_complete"] is complete
    assert report["model_reports"][0]["post_sync_validation_status"] == status
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert outputs["dbt-sync-complete"] == "true"
    assert outputs["dbt-sync-state-eligible"] == ("true" if branch_id is None else "false")
    assert outputs["post-sync-validation-complete"] == str(complete).lower()
