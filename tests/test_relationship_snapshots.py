"""Exercise real snapshot ingestion, not only pre-parsed graph construction."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from omniflow import cli
from omniflow.config import _to_config
from omniflow.discovery import ModelContext


def model_payload(relationship_file, cardinality):
    return {
        "files": {
            "model": "connection: synthetic\n",
            "orders.view": "dimensions:\n  id:\n    sql: ${TABLE}.id\n",
            "users.view": "dimensions:\n  id:\n    sql: ${TABLE}.id\n",
            relationship_file: (
                "- name: orders_users\n  join_from_view: orders\n  join_to_view: users\n"
                "  on_sql: ${orders.user_id} = ${users.id}\n  relationship_type: " + cardinality + "\n"
            ),
        },
        "viewNames": {"orders": "orders.view", "users": "users.view"},
    }


@pytest.mark.parametrize("api_name,changed", [
    ("joins.relationships", True), ("joins.relationships.yaml", True),
    ("joins.relationships.yml", True), ("relationships", True),
    ("joins.relationships", False),
])
def test_complete_pr_pipeline_does_not_omit_relationships(tmp_path, monkeypatch, api_name, changed):
    monkeypatch.chdir(tmp_path)
    for name in ("GITHUB_BASE_REF", "GITHUB_REPOSITORY", "GITHUB_SERVER_URL", "OMNIFLOW_ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)

    def git(*args):
        return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()

    git("init", "-b", "main")
    git("config", "user.name", "Synthetic fixture")
    git("config", "user.email", "synthetic@example.invalid")
    (tmp_path / "omni").mkdir()
    git_name = api_name if api_name.endswith((".yaml", ".yml")) else api_name + ".yaml"
    revisions = {}
    for side in ("base", "head"):
        cardinality = "one_to_many" if changed and side == "head" else "many_to_one"
        for name, text in model_payload(git_name, cardinality)["files"].items():
            (tmp_path / "omni" / name).write_text(text)
        git("add", "omni")
        git("commit", "--allow-empty", "-m", side)
        revisions[side] = {"sha": git("rev-parse", "HEAD"), "repo": {"full_name": "synthetic/repo"}}
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": revisions}))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    context = ModelContext(base_url="https://synthetic.example", model_id="model", model_path="omni",
                           branch_id="branch", branch_name="synthetic")
    config = _to_config({"checks": {"content_validation": {"fail_on_new_only": True},
                                     "model_validation": {"fail_on_new_only": True}},
                         "reporting": {"diagnostics": {"mode": "minimal"}}}, None)

    def yaml(*args, branch_id=None, **kwargs):
        return model_payload(api_name, "one_to_many" if changed and branch_id else "many_to_one")

    client = SimpleNamespace(get_model_yaml=yaml, validate_content=lambda *a, **k: {"content": []},
                             validate_model=lambda *a, **k: [], list_content=lambda **k: [],
                             search_content_references=lambda *a, **k: {"content": [{"document_id": "doc-1"}]})
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(cli, "discover_contexts", lambda **k: [context])
    monkeypatch.setattr(cli, "_client_and_branch_for_context", lambda *a, **k: (client, "branch"))
    assert cli.main(["run", "--auto"]) == int(changed)
    report = json.loads((tmp_path / ".omniflow/public/report.json").read_text())
    assert report["validation_complete"] is True
    assert report["policy_decision"] == ("fail" if changed else "pass")
    proof = report["model_reports"][0]["candidate_verification"]
    assert proof["status"] == ("verified" if changed else "scope_empty")
    if not changed:
        assert proof["changed_file_count"] == 0
