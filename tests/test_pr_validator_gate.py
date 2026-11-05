"""Exercise the PR gate through the CLI, a real Git history, and synthetic Omni HTTP responses."""

import json
import subprocess
from pathlib import Path

import pytest

from omniflow import cli
from omniflow.omni_client import OmniClient

REPO = "example/analytics"
MODEL = "test-model"
BRANCH = "test-branch"
VIEW = "label: Orders\ndimensions:\n  id:\n    primary_key: true\n"
CHANGED_VIEW = VIEW.replace("label: Orders", "label: Reviewed orders")
LEGACY = "label: Legacy\ndimensions:\n  amount:\n    sql: ${TABLE}.amount\n"
BASE = {"orders.view": VIEW, "legacy.view": LEGACY}
CODES = {"OF-MODEL-VALIDATION-ISSUE", "OF-CONTENT-DASHBOARD-FILTER-VALIDATION-ISSUE"}


@pytest.fixture
def run_pr(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("OMNI_BASE_URL", "OMNI_MODEL_ID", "OMNI_BRANCH_ID", "OMNI_BRANCH_NAME", "OMNI_USER_ID",
                "OMNIFLOW_ENVIRONMENT", "OMNIFLOW_CHANGED_FILES", "OMNIFLOW_GITHUB_TOKEN", "GITHUB_ACTIONS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OMNI_API_KEY", "synthetic-credential")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    monkeypatch.setenv("GITHUB_HEAD_REF", "test/proposal")
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, stderr=subprocess.DEVNULL, text=True).strip()

    def run(head, branch, *, comparison=None, wrapper=".yaml", base_api=None, existing=False,
            referenced=True, unreadable=None):
        git("init", "-b", "main")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.invalid")
        git("remote", "add", "origin", f"https://github.com/{REPO}.git")
        Path(".omni").mkdir()
        Path(".omni/flow.json").write_text(json.dumps({"version": 1, "models": [{
            "base_url": "https://fixture.omni.example", "model_id": MODEL, "model_path": "omni",
            "base_branch": "main", "git_provider": "github", "web_url": f"https://github.com/{REPO}",
        }]}))
        config = {"omni": {"include_personal_folders": False}, "contracts": {"enabled": False},
                  "checks": {"content_validation": {"enabled": True, "fail_on_new_only": True},
                             "model_validation": {"enabled": True, "fail_on_new_only": True},
                             "semantic_lint": {"enabled": False}}, "reporting": {"diagnostics": {"mode": "minimal"}}}
        if comparison:
            config["validation"] = {"source_verification": comparison}
        Path(".omniflow.yml").write_text(json.dumps(config))
        Path("omni").mkdir()
        for name, value in BASE.items():
            Path(f"omni/{name}.yaml").write_text(value)
        git("add", ".")
        git("commit", "-m", "Trusted base")
        base_sha = git("rev-parse", "HEAD")
        git("switch", "-c", "test/proposal")
        for name in BASE:
            Path(f"omni/{name}.yaml").unlink()
        for name, value in head.items():
            Path(f"omni/{name}{wrapper if name == 'orders.view' else '.yaml'}").write_text(value)
        git("add", "omni")
        git("commit", "-m", "PR change")
        head_sha = git("rev-parse", "HEAD")
        git("switch", "main")
        event = {"number": 1, "pull_request": {"number": 1, "body": "",
                 "base": {"ref": "main", "sha": base_sha, "repo": {"full_name": REPO}},
                 "head": {"ref": "test/proposal", "sha": head_sha, "repo": {"full_name": REPO}}}}
        event_path = tmp_path / ".git/event.json"
        event_path.write_text(json.dumps(event))
        monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
        monkeypatch.setenv("GITHUB_SHA", base_sha)

        def request(_client, method, path, *, params=None, **_kwargs):
            assert method == "GET"
            params = params or {}
            if path == "/api/v1/models":
                return {"records": [{"id": BRANCH, "modelKind": "BRANCH", "baseModelId": MODEL,
                                     "name": "test/proposal"}], "pageInfo": {}}
            if path == "/api/v1/content":
                return {"records": [{"identifier": "dashboard"}], "pageInfo": {}}
            if path.endswith("/drafts"):
                return []
            branch_id = params.get("branchId") or params.get("branch_id")
            assert branch_id in {None, BRANCH} and path.startswith(f"/api/v1/models/{MODEL}/")
            files = branch if branch_id else (base_api if base_api is not None else BASE)
            broken = existing or (referenced and "legacy.view" not in files)
            if path.endswith("/yaml"):
                return {"files": files, "viewNames": {key.removesuffix(".view"): key for key in files}}
            if path.endswith("/validate"):
                if not branch_id and unreadable == "model":
                    return {"malformed": True}
                return [{"message": "Missing legacy.amount", "is_warning": False,
                         "yaml_path": "orders.view/dimensions/id"}] if broken else []
            assert path.endswith("/content-validator")
            doc = {"document_id": "dashboard", "identifier": "dashboard",
                   "dashboard_filter_issues": ["Missing legacy.amount"] if broken else []}
            return {"model_id": MODEL, "branch": {"id": branch_id} if branch_id else None,
                    "content": None if not branch_id and unreadable == "content" else [doc]}

        monkeypatch.setattr(OmniClient, "_request", request)
        code = cli.main(["run", "--auto", "--config", ".omniflow.yml"])
        report = json.loads(Path(".omniflow/public/report.json").read_text())
        assert report["exit_code"] == code
        if code in {0, 1}:
            context = report["model_reports"][0]
            assert context["candidate_verification"]["method"] == "pr_changed_files"
            states = {c["validator"]: c["status"] for c in context["check_states"]}
            assert states["content"] == states["model"] == "completed"
        return code, report
    return run


@pytest.mark.parametrize("wrapper,head_text,unrelated_drift", [
    (".yml", VIEW, False), (".yaml", CHANGED_VIEW.rstrip("\n"), False),
    (".yaml", CHANGED_VIEW, True),
])
def test_pr_gate_accepts_verified_edit_wrapper_or_harmless_formatting(run_pr, wrapper, head_text, unrelated_drift):
    branch_text = VIEW if wrapper == ".yml" else CHANGED_VIEW
    base_api = dict(BASE)
    if unrelated_drift:
        base_api["legacy.view"] += "\n"
    code, report = run_pr({**BASE, "orders.view": head_text}, {**base_api, "orders.view": branch_text},
                          wrapper=wrapper, base_api=base_api)
    assert code == 0 and report["validation_complete"] is True
    assert not [issue for issue in report["issues"] if issue.get("blocking")]


def test_full_model_opt_in_still_checks_untouched_files(run_pr):
    head = {**BASE, "orders.view": CHANGED_VIEW}
    code, report = run_pr(head, {**head, "legacy.view": LEGACY + "\n"}, comparison="full_model")
    assert code == 2 and report["validation_complete"] is False
    assert any(issue.get("candidate_reason") == "content_mismatch" for issue in report["issues"])


@pytest.mark.parametrize("referenced", [True, False])
def test_synced_deletion_uses_both_validators_to_decide(run_pr, referenced):
    head = {"orders.view": VIEW}
    code, report = run_pr(head, head, referenced=referenced)
    assert code == int(referenced) and report["validation_complete"] is True
    blockers = {i["diagnostic_code"] for i in report["issues"] if i.get("blocking") and i.get("state") == "new"}
    assert blockers == (CODES if referenced else set())


@pytest.mark.parametrize("head", [
    {"orders.view": VIEW},
    {**BASE, "new.view": "label: New view\n"},
    {**BASE, "orders.view": VIEW.replace("primary_key: true", "primary_key: true\n    sql: ${TABLE}.changed_column")},
], ids=["unreflected-deletion", "unreflected-addition", "unreflected-sql-edit"])
def test_stale_change_cannot_turn_clean_validator_results_into_a_pass(run_pr, head):
    code, report = run_pr(head, BASE)
    assert code == 2 and report["validation_complete"] is False
    assert any(i.get("candidate_reason") == "pr_change_not_synchronized" and i.get("blocking") for i in report["issues"])


def test_synchronized_new_view_can_pass_after_both_validators_complete(run_pr):
    head = {**BASE, "new.view": "label: New view\n"}
    code, report = run_pr(head, head)
    assert code == 0 and report["validation_complete"] is True


def test_unchanged_baseline_debt_is_informational_after_scoped_verification(run_pr):
    head = {**BASE, "orders.view": CHANGED_VIEW}
    code, report = run_pr(head, head, existing=True)
    assert code == 0 and report["validation_complete"] is True
    assert {i["diagnostic_code"] for i in report["issues"] if i.get("state") == "existing" and not i.get("blocking")} == CODES


@pytest.mark.parametrize("validator", ["content", "model"])
def test_clean_candidate_needs_a_readable_baseline(run_pr, validator):
    head = {**BASE, "orders.view": CHANGED_VIEW}
    code, report = run_pr(head, head, unreadable=validator)
    assert code == 4 and report["validation_complete"] is False
    assert any(i.get("validator") == validator and i.get("blocking") for i in report["issues"])
