"""Trusted cross-repo CLI lifecycle with real Git and mocked remote metadata.

The advisory coordinator is mocked explicitly; these tests do not qualify live
Omni content lookup, hosted Actions execution, or business query correctness.
"""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import yaml
from test_omni_repository import DBT_REPO, OMNI_SHA, SOURCE, VIEW, FakeRepository, git

from omniflow import cli
from omniflow.config import OmniRepositorySettings, load_config
from omniflow.content_impact_reporting import current_validation_report
from omniflow.discovery import ModelContext
from omniflow.exceptions import ConfigError

ROOT = Path(__file__).resolve().parents[1]
MODEL_ID = "00000000-0000-4000-8000-000000000001"
BASE_SQL = "select id, amount * .9 as net from raw.orders"


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch):
    with mock.patch.dict(os.environ, {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
    }, clear=True):
        monkeypatch.setattr("requests.request", mock.Mock(side_effect=AssertionError("No live HTTP in workflow test")))
        yield


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    def create(candidate=BASE_SQL + " -- comment only", *, fork=False, extra_files=None):
        monkeypatch.chdir(tmp_path)
        git(tmp_path, "init", "-q", "-b", "main")
        git(tmp_path, "config", "user.email", "test@example.invalid")
        git(tmp_path, "config", "user.name", "Fixture")
        policy = {"reporting": {"diagnostics": {"mode": "minimal"}}, "checks": {"dbt_impact": {
            "enabled": True, "content_impact": {"enabled": True},
            "omni_repository": {"repository": SOURCE, "ref": "release/stable", "model_paths": ["omni"]},
        }}}
        registry = {"version": 1, "models": [{
            "base_url": "https://fixture.omniapp.co", "model_id": MODEL_ID, "model_path": "omni",
            "base_branch": "release/stable", "git_provider": "github", "web_url": f"https://github.com/{SOURCE}",
        }]}
        Path(".omniflow.yml").write_text(yaml.safe_dump(policy))
        Path(".omni").mkdir()
        Path(".omni/flow.json").write_text(json.dumps(registry))
        Path("models").mkdir()
        Path("models/orders.sql").write_text(BASE_SQL)
        git(tmp_path, "add", ".")
        git(tmp_path, "commit", "-qm", "trusted base")
        base = git(tmp_path, "rev-parse", "HEAD")
        git(tmp_path, "checkout", "-qb", "candidate")
        Path("models/orders.sql").write_text(candidate)
        # Checked-out PR configuration must never disable/redirect trusted work.
        hostile = copy.deepcopy(policy)
        hostile["checks"]["dbt_impact"].update(enabled=False, omni_repository=None)
        Path(".omniflow.yml").write_text(yaml.safe_dump(hostile))
        registry["models"][0].update(model_id="00000000-0000-4000-8000-000000000099",
                                      web_url="https://github.com/untrusted/source", base_branch="main")
        Path(".omni/flow.json").write_text(json.dumps(registry))
        for filename, text in (extra_files or {}).items():
            target = Path(filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        git(tmp_path, "add", ".")
        git(tmp_path, "commit", "-qm", "candidate with untrusted policy edit")
        head = git(tmp_path, "rev-parse", "HEAD")
        event = {"number": 91, "repository": {"full_name": DBT_REPO, "private": True}, "pull_request": {
            "number": 91, "state": "open",
            "base": {"sha": base, "ref": "main", "repo": {"full_name": DBT_REPO}},
            "head": {"sha": head, "ref": "candidate", "repo": {"full_name": "untrusted/fork" if fork else DBT_REPO}},
        }}
        event_path = tmp_path / "event.json"
        event_path.write_text(json.dumps(event))
        for key, value in {
            "GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "pull_request_target",
            "GITHUB_EVENT_PATH": str(event_path), "GITHUB_REPOSITORY": DBT_REPO,
            "GITHUB_BASE_REF": "main", "GITHUB_HEAD_REF": "candidate",
            "GITHUB_EVENT_NUMBER": "91", "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_SERVER_URL": "https://github.com", "OMNIFLOW_REPORT_DIR": ".omniflow",
            "OMNIFLOW_GITHUB_TOKEN": "synthetic-dbt-only-token",
            "OMNIFLOW_OMNI_REPO_TOKEN": "synthetic-source-only-token",
        }.items():
            monkeypatch.setenv(key, value)
        source = FakeRepository()
        current_pr = copy.deepcopy(event["pull_request"])
        dbt_api = SimpleNamespace(repository=DBT_REPO)
        dbt_api.request = mock.Mock(side_effect=lambda method, path, **kw: copy.deepcopy(current_pr)
                                   if method == "GET" and path == "/pulls/91" else
                                   pytest.fail("Unexpected workflow-token operation"))
        calls = []

        def factory(repository, token):
            calls.append((repository, token))
            if repository == SOURCE and token == "synthetic-source-only-token":
                return source
            if repository == DBT_REPO and token == "synthetic-dbt-only-token":
                return dbt_api
            pytest.fail("Credential crossed its source boundary")

        monkeypatch.setattr("omniflow.github.revalidation.GitHubRepository", factory)
        monkeypatch.setattr(cli, "_run_context", mock.Mock(side_effect=AssertionError("No native validator for dbt-only route")))
        return SimpleNamespace(root=tmp_path, base=base, head=head, source=source, current_pr=current_pr,
                               calls=calls, event=event, event_path=event_path)
    return create


def report():
    return json.loads(Path(".omniflow/public/report.json").read_text())


def mock_enrichment(monkeypatch, *, wrong_git=False):
    calls = []

    def enrich(**kwargs):
        calls.append(kwargs)
        contexts = kwargs["contexts"]
        assert len(contexts) == 1 and contexts[0].model_id == MODEL_ID
        assert contexts[0].base_branch == "release/stable"
        assert kwargs["source_identity"] == (SOURCE, OMNI_SHA)
        native = SimpleNamespace(get_git_configuration=lambda model_id: {
            "modelPath": "omni", "baseBranch": "release/stable", "gitServiceProvider": "github",
            "webUrl": f"https://github.com/{'untrusted/source' if wrong_git else SOURCE}",
        })
        kwargs["client_verifier"](contexts[0], native)
        static = kwargs["collect_static"](None)
        for scope in ("base", "head"):
            assert kwargs["source_file_reader"](contexts[0], "orders.view.yaml", scope) == VIEW
        fields = {item["field_name"] for item in static["impacts"]}
        return {**static, "status": "complete" if static["coverage_complete"] else "partial", "scope": "published",
                "summary": {"affected_fields": len(fields), "dashboards": 0, "workbooks": 0,
                            "queries": 0, "dashboard_filters": 0, "searches": 0, "unverified_documents": 0}}

    monkeypatch.setattr("omniflow.content_impact.collect_and_enrich_content_impact", enrich)
    return calls


@pytest.mark.parametrize("candidate,expected_exit", [
    (BASE_SQL + " -- comment only", 0),
    (BASE_SQL.replace("as net", "as subtotal"), 1),
])
def test_route_static_enrich_verify_uses_trusted_policy_and_one_source_receipt(
    workflow, monkeypatch, capsys, candidate, expected_exit,
):
    state = workflow(candidate)
    assert cli.main(["route", "--auto", "--format", "github"]) == 0
    route = capsys.readouterr().out
    assert "should_run=true" in route and "requires_omni=false" in route and "external_omni=true" in route
    assert state.calls == [], "Routing must not read either credentialed API"
    assert "OMNI_API_KEY" not in os.environ
    assert cli.main(["run", "--auto"]) == expected_exit
    original = report()
    assert original["exit_code"] == expected_exit and original["validation_complete"] is True
    sealed = current_validation_report(load_config())
    receipt = sealed["model_reports"][0]["dbt_impact_snapshot"]
    assert receipt["omni_commit_sha"] == OMNI_SHA
    assert receipt["dbt_base_sha"] == state.base and receipt["dbt_head_sha"] == state.head
    assert all(repository == SOURCE for repository, _ in state.calls)
    calls = mock_enrichment(monkeypatch)
    assert cli.main(["dbt", "content-impact"]) == 0
    assert len(calls) == 1 and report()["content_impact"]["status"] == "complete"
    assert report()["exit_code"] == expected_exit, "Advisory enrichment must not replace the gate"
    assert cli.main(["dbt", "verify-source"]) == expected_exit
    assert (DBT_REPO, "synthetic-dbt-only-token") in state.calls
    assert current_validation_report(load_config())["exit_code"] == expected_exit


def test_missing_external_token_fails_static_without_native_credentials(workflow, monkeypatch):
    state = workflow()
    monkeypatch.delenv("OMNIFLOW_OMNI_REPO_TOKEN")
    assert cli.main(["route", "--auto"]) == 0
    assert cli.main(["run", "--auto"]) == 2
    assert state.calls == []
    assert report().get("validation_complete") is not True and report()["policy_decision"] == "fail"


def test_fork_fails_route_before_any_source_client(workflow):
    state = workflow(fork=True)
    assert cli.main(["route", "--auto"]) == 5
    assert cli.main(["run", "--auto"]) == 5
    assert state.calls == []


@pytest.mark.parametrize("filename", ["dbt_project.yml", "packages.yml", "tools/config.yaml"])
def test_dbt_and_tooling_yaml_do_not_trigger_mixed_omni_routing(workflow, capsys, filename):
    state = workflow(extra_files={filename: "name: fixture\nversion: '1'\n"})
    assert cli.main(["route", "--auto", "--format", "github"]) == 0
    output = capsys.readouterr().out
    assert "requires_omni=false" in output and "external_omni=true" in output
    assert state.calls == []


def test_local_authored_omni_file_under_dbt_path_is_rejected(workflow):
    state = workflow(extra_files={"models/local/orders.view.yaml": VIEW})
    assert cli.main(["route", "--auto"]) == 2
    assert cli.main(["run", "--auto"]) == 2
    assert state.calls == []
    assert report()["policy_decision"] == "fail"


@pytest.mark.parametrize("moved", ["source", "base", "head"])
def test_final_gate_rejects_changed_external_or_live_pr_revision(workflow, moved):
    state = workflow()
    assert cli.main(["run", "--auto"]) == 0
    if moved == "source":
        state.source.sha = "d" * 40
    else:
        state.current_pr[moved]["sha"] = "d" * 40
    assert cli.main(["dbt", "verify-source"]) == 2
    assert report().get("validation_complete") is not True and report()["exit_code"] == 2


def test_enrichment_rejects_drift_before_coordinator_and_keeps_static_decision(workflow, monkeypatch):
    state = workflow()
    assert cli.main(["run", "--auto"]) == 0
    state.source.sha = "d" * 40
    calls = mock_enrichment(monkeypatch)
    assert cli.main(["dbt", "content-impact"]) == 0
    assert calls == []
    assert report()["exit_code"] == 0
    assert report()["content_impact"]["status"] == "unavailable"
    assert "omni_repository_snapshot_changed" in report()["content_impact"]["coverage_reasons"]


def test_published_native_git_mismatch_is_advisory_unavailable(workflow, monkeypatch):
    workflow()
    assert cli.main(["run", "--auto"]) == 0
    calls = mock_enrichment(monkeypatch, wrong_git=True)
    assert cli.main(["dbt", "content-impact"]) == 0
    assert len(calls) == 1
    assert report()["exit_code"] == 0 and report()["content_impact"]["status"] == "unavailable"
    assert "omni_repository_target_mismatch" in report()["content_impact"]["coverage_reasons"]


def test_source_read_error_without_reason_code_stays_advisory(workflow, monkeypatch):
    state = workflow(BASE_SQL.replace(".9", ".8"))
    assert cli.main(["run", "--auto"]) == 0
    state.source.tree_payload["truncated"] = True
    mock_enrichment(monkeypatch)
    assert cli.main(["dbt", "content-impact"]) == 0
    assert report()["exit_code"] == 0 and report()["content_impact"]["status"] == "unavailable"


@pytest.mark.parametrize("tamper", ["report", "attempt"])
def test_enrichment_and_final_gate_reject_stale_validation_receipt(workflow, monkeypatch, tamper):
    workflow()
    assert cli.main(["run", "--auto"]) == 0
    if tamper == "report":
        path = Path(".omniflow/public/report.json")
        path.write_text(path.read_text() + "\n")
    else:
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    calls = mock_enrichment(monkeypatch)
    assert cli.main(["dbt", "content-impact"]) == 0
    assert calls == []
    assert cli.main(["dbt", "verify-source"]) == 2


def test_action_and_example_keep_new_token_off_fork_and_nonvalidation_paths():
    action = yaml.safe_load((ROOT / "action.yml").read_text())
    steps = action["runs"]["steps"]
    by_id = {step.get("id"): step for step in steps if step.get("id")}
    assert "omni-repository-token" in action["inputs"]
    assert "OMNIFLOW_OMNI_REPO_TOKEN" not in by_id["route"]["env"]
    for step in steps:
        env = step.get("env", {})
        if "OMNIFLOW_OMNI_REPO_TOKEN" not in env:
            continue
        assert "external_omni == 'true'" in str(env["OMNIFLOW_OMNI_REPO_TOKEN"]) + step.get("if", "")
        assert "repair" not in step.get("if", "") and "dbt-sync" not in step.get("if", "")
    assert "OMNI_API_KEY" not in by_id["dbt_impact"]["env"]
    workflow = yaml.safe_load((ROOT / ".github/workflow-examples/omniflow.yml").read_text())
    steps = workflow["jobs"]["omniflow"]["steps"]
    fork = next(step for step in steps if step["name"] == "Route fork pull request without Omni secret")
    trusted = next(step for step in steps if step.get("id") == "omniflow")
    assert "omni-repository-token" not in fork["with"]
    assert "omni-api-key" not in fork["with"]
    assert "github.event.pull_request.head.repo.full_name == github.repository" in trusted["if"]
    assert trusted["with"]["omni-repository-token"] == "${{ secrets.OMNIFLOW_OMNI_REPO_TOKEN }}"


@pytest.mark.parametrize("urls,root", [
    ({"webUrl": f"https://github.com/{SOURCE}.git/"}, False),
    ({"webUrl": f"https://github.com/{SOURCE}/"}, False),
    ({"cloneUrl": f"git@github.com:{SOURCE}.git"}, False),
    ({"cloneUrl": f"git@github.com:{SOURCE}.git"}, True),
])
def test_native_source_url_forms_and_empty_root_path_are_verified(urls, root):
    context = ModelContext(base_url="https://fixture.omniapp.co", model_id=MODEL_ID,
                           model_path="." if root else "omni", base_branch="release/stable",
                           git_provider="github", web_url=f"https://github.com/{SOURCE}")
    external = OmniRepositorySettings(SOURCE, "release/stable", [context.model_path])
    actual = {"modelPath": "" if root else "omni", "baseBranch": "release/stable",
              "gitServiceProvider": "github", **urls}
    client = SimpleNamespace(get_git_configuration=lambda model_id: actual)
    cli._verify_external_impact_context(context, client, external)


@pytest.mark.parametrize("override", [
    {"baseBranch": "Release/stable"},
    {"modelPath": "Omni"},
    {"webUrl": "https://github.com/untrusted/source"},
])
def test_native_source_identity_mismatches_are_not_normalized_away(override):
    context = ModelContext(base_url="https://fixture.omniapp.co", model_id=MODEL_ID, model_path="omni",
                           base_branch="release/stable", git_provider="github", web_url=f"https://github.com/{SOURCE}")
    external = OmniRepositorySettings(SOURCE, "release/stable", ["omni"])
    actual = {"modelPath": "omni", "baseBranch": "release/stable", "gitServiceProvider": "github",
              "webUrl": context.web_url, **override}
    client = SimpleNamespace(get_git_configuration=lambda model_id: actual)
    with pytest.raises(ConfigError) as caught:
        cli._verify_external_impact_context(context, client, external)
    assert caught.value.reason_code == "omni_repository_target_mismatch"


@pytest.mark.parametrize("model_path,registered", [
    ("jaffle", "omni/jaffle"),          # Omni default: bare folder name under omni/
    ("/omni/jaffle", "omni/jaffle"),    # explicit path, reported with a leading slash
    ("/bi/models/sales/", "bi/models/sales"),
    ("omni", "omni"),                   # literal match stays accepted
])
def test_native_source_model_path_forms_map_to_repository_folders(model_path, registered):
    context = ModelContext(base_url="https://fixture.omniapp.co", model_id=MODEL_ID, model_path=registered,
                           base_branch="release/stable", git_provider="github", web_url=f"https://github.com/{SOURCE}")
    external = OmniRepositorySettings(SOURCE, "release/stable", [registered])
    actual = {"modelPath": model_path, "baseBranch": "release/stable", "gitServiceProvider": "github",
              "cloneUrl": f"git@github.com:{SOURCE}.git"}
    client = SimpleNamespace(get_git_configuration=lambda model_id: actual)
    cli._verify_external_impact_context(context, client, external)


@pytest.mark.parametrize("model_path,registered", [
    ("jaffle", "omni/other"),
    ("jaffle", "models/jaffle"),        # the default folder is omni/, nowhere else
    ("/jaffle", "omni/jaffle"),         # an explicit path is not re-rooted under omni/
    ("a/b", "omni/a/b"),
])
def test_native_source_model_path_forms_do_not_widen_to_other_folders(model_path, registered):
    context = ModelContext(base_url="https://fixture.omniapp.co", model_id=MODEL_ID, model_path=registered,
                           base_branch="release/stable", git_provider="github", web_url=f"https://github.com/{SOURCE}")
    external = OmniRepositorySettings(SOURCE, "release/stable", [registered])
    actual = {"modelPath": model_path, "baseBranch": "release/stable", "gitServiceProvider": "github",
              "cloneUrl": f"git@github.com:{SOURCE}.git"}
    client = SimpleNamespace(get_git_configuration=lambda model_id: actual)
    with pytest.raises(ConfigError) as caught:
        cli._verify_external_impact_context(context, client, external)
    assert caught.value.reason_code == "omni_repository_target_mismatch"
