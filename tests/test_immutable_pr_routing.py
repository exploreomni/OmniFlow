import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from omniflow import discovery
from omniflow.cli import main
from omniflow.exceptions import ConfigError, GitHubAPIError, SecurityPolicyError
from omniflow.github.revalidation import GitHubRepository
from omniflow.revision_data import pull_request_changed_files


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OMNIFLOW_CHANGED_FILES", raising=False)
    git("init", "-b", "main")
    git("config", "user.name", "Synthetic routing test")
    git("config", "user.email", "synthetic@example.invalid")
    Path(".omni").mkdir()
    Path("omni").mkdir()
    Path(".omniflow.yml").write_text("checks:\n  model_validation:\n    enabled: true\n")
    flow = {"version": 1, "models": [{"base_url": "https://synthetic.omniapp.co", "model_id": "model",
                                     "model_path": "omni", "base_branch": "main"}]}
    Path(".omni/flow.json").write_text(json.dumps(flow))
    Path("omni/orders.view.yaml").write_text("dimensions: {}\n")
    Path("README.md").write_text("Synthetic baseline\n")
    git("add", ".")
    git("commit", "-m", "Synthetic baseline")
    base = git("rev-parse", "HEAD")
    git("switch", "-c", "candidate")
    Path("omni/orders.view.yaml").write_text("dimensions: {id: {type: number}}\n")
    git("commit", "-am", "Synthetic model change")
    head = git("rev-parse", "HEAD")
    git("switch", "main")
    git("update-ref", "refs/remotes/origin/main", base)
    event = {"pull_request": {"body": "", "changed_files": 1,
                             "base": {"ref": "main", "sha": base, "repo": {"full_name": "owner/repo"}},
                             "head": {"ref": "candidate", "sha": head, "repo": {"full_name": "owner/repo"}}}}
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event))
    for name, value in {"GITHUB_EVENT_NAME": "pull_request_target", "GITHUB_EVENT_PATH": str(event_path),
                        "GITHUB_BASE_REF": "main", "GITHUB_HEAD_REF": "candidate",
                        "OMNIFLOW_GITHUB_TOKEN": "synthetic-token"}.items():
        monkeypatch.setenv(name, value)
    discovery.reset_changed_files_cache()
    monkeypatch.setattr("requests.sessions.Session.request", Mock(side_effect=AssertionError("No live network")))
    return event, event_path, flow


def route(capsys):
    assert main(["route", "--auto", "--format", "json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_local_git_replacement_cannot_hide_the_pr_edit(repository):
    event, _, _ = repository
    pr = event["pull_request"]
    git("replace", pr["head"]["sha"], pr["base"]["sha"])
    assert pull_request_changed_files() == ["omni/orders.view.yaml"]


@pytest.mark.parametrize("event_name,version", [("pull_request", 1), ("pull_request_target", 1), ("pull_request_target", 2)])
def test_routing_uses_event_head_not_checkout_current_pr_files_or_environment(repository, monkeypatch, capsys,
                                                                          event_name, version):
    event, event_path, flow = repository
    if version == 2:
        flow["version"] = 2
        flow["models"][0].update(environment="test", git_follower=False, web_url="https://github.com/owner/repo")
        Path(".omni/flow.json").write_text(json.dumps(flow))
        git("commit", "-am", "Synthetic version 2 registration")
        git("update-ref", "refs/remotes/origin/main", git("rev-parse", "HEAD"))
        event["pull_request"]["base"]["sha"] = git("rev-parse", "HEAD")
        event_path.write_text(json.dumps(event))
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.setenv("OMNIFLOW_CHANGED_FILES", "README.md")
    checkout = git("rev-parse", "HEAD")
    assert route(capsys)["should_run"] is True
    assert git("rev-parse", "HEAD") == checkout
    assert checkout != event["pull_request"]["head"]["sha"]


@pytest.mark.parametrize("change,should_run", [("docs", False), ("unicode", True), ("spaces", True),
                                               ("deleted", True), ("renamed", True)])
def test_exact_inventory_preserves_paths_and_change_kinds(repository, monkeypatch, capsys, change, should_run):
    event, event_path, _ = repository
    git("switch", "candidate")
    git("restore", "--source", event["pull_request"]["base"]["sha"], "--", "omni/orders.view.yaml")
    if change == "docs":
        Path("README.md").write_text("Docs-only change\n")
    elif change in {"unicode", "spaces"}:
        path = Path("omni/\u96ea.view.yaml" if change == "unicode" else "omni/Snowflake Analytics/SCHEMA/orders.view.yaml")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("dimensions: {}\n")
    elif change == "deleted":
        Path("omni/orders.view.yaml").unlink()
    else:
        Path("omni/orders.view.yaml").rename("orders.md")
    git("add", "omni", "README.md", *(["orders.md"] if change == "renamed" else []))
    git("commit", "-m", "Synthetic path change")
    event["pull_request"]["head"]["sha"] = git("rev-parse", "HEAD")
    event_path.write_text(json.dumps(event))
    git("switch", "main")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    assert route(capsys)["should_run"] is should_run


def test_pr_diff_uses_merge_base_not_target_only_changes(repository, capsys):
    event, event_path, _ = repository
    git("switch", "candidate")
    git("restore", "--source", event["pull_request"]["base"]["sha"], "--", "omni/orders.view.yaml")
    Path("README.md").write_text("PR docs only\n")
    git("commit", "-am", "Synthetic docs-only PR")
    head = git("rev-parse", "HEAD")
    git("switch", "main")
    Path("omni/base_only.view.yaml").write_text("dimensions: {}\n")
    git("add", "omni")
    git("commit", "-m", "Independent base change")
    base = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", base)
    event["pull_request"]["base"]["sha"] = base
    event["pull_request"]["head"]["sha"] = head
    event_path.write_text(json.dumps(event))
    assert "omni/base_only.view.yaml" in git("diff", "--name-only", base + ".." + head)
    assert pull_request_changed_files() == ["README.md"]
    assert route(capsys)["should_run"] is False


def test_cache_tracks_replaced_event_revisions(repository):
    event, event_path, _ = repository
    head = event["pull_request"]["head"]["sha"]
    event["pull_request"]["head"]["sha"] = event["pull_request"]["base"]["sha"]
    event_path.write_text(json.dumps(event))
    assert discovery.get_changed_files() == []
    event["pull_request"]["head"]["sha"] = head
    event_path.write_text(json.dumps(event))
    assert discovery.get_changed_files() == ["omni/orders.view.yaml"]


@pytest.fixture
def fallback(repository, monkeypatch):
    event, _, _ = repository
    monkeypatch.setattr("omniflow.revision_data.subprocess.run", Mock(side_effect=subprocess.CalledProcessError(1, "git")))
    request = Mock()
    monkeypatch.setattr(GitHubRepository, "request", request)
    return event, request


@pytest.mark.parametrize("record,expected", [
    ({"filename": "omni/orders.view.yaml", "status": "removed"}, ["omni/orders.view.yaml"]),
    ({"filename": "docs/orders.md", "status": "renamed", "previous_filename": "omni/orders.view.yaml"},
     ["docs/orders.md", "omni/orders.view.yaml"]),
])
def test_unavailable_git_uses_exact_rest_comparison(fallback, record, expected):
    event, request = fallback
    request.return_value = {"files": [record]}
    assert pull_request_changed_files() == expected
    pr = event["pull_request"]
    request.assert_called_once_with("GET", f"/compare/{pr['base']['sha']}...{pr['head']['sha']}")


@pytest.mark.parametrize("record", [
    None, {}, {"filename": "README.md"}, {"filename": "README.md", "status": "unknown"},
    {"filename": "README.md", "status": []}, {"filename": "", "status": "modified"},
    {"filename": "   ", "status": "modified"}, {"filename": ".", "status": "modified"},
    {"filename": "./README.md", "status": "modified"}, {"filename": "../secret", "status": "modified"},
    {"filename": "README.md", "status": "renamed"},
    {"filename": "README.md", "status": "renamed", "previous_filename": ""},
])
def test_malformed_rest_file_evidence_blocks(fallback, record):
    _, request = fallback
    request.return_value = {"files": [record]}
    with pytest.raises((ConfigError, SecurityPolicyError)):
        pull_request_changed_files()


@pytest.mark.parametrize("response", [None, {}, {"files": None}, {"files": [
    {"filename": "README.md", "status": "modified"}] * 300}])
def test_incomplete_rest_inventory_blocks(fallback, response):
    _, request = fallback
    request.return_value = response
    with pytest.raises(ConfigError):
        pull_request_changed_files()


def test_github_transport_rejects_duplicate_json_keys(monkeypatch):
    response = Mock(status_code=200)
    response.iter_content.return_value = [b'{"files":[{"filename":"omni/orders.view","status":"modified"}],"files":[]}']
    transport = Mock()
    transport.__enter__ = Mock(return_value=response)
    transport.__exit__ = Mock(return_value=False)
    request = Mock(return_value=transport)
    monkeypatch.setattr("requests.request", request)
    with pytest.raises(GitHubAPIError) as caught:
        GitHubRepository("owner/repo", "synthetic-token").request("GET", "/compare/" + "a" * 40 + "..." + "b" * 40)
    assert caught.value.reason_code == "github_invalid_json_response"
    assert caught.value.diagnostic_context["http_status"] == 200
    assert caught.value.diagnostic_context["retryable"] is False
    assert "omni/orders.view" not in str(caught.value)
    assert "synthetic-token" not in str(caught.value)
    assert request.call_args.kwargs["allow_redirects"] is False
