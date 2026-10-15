"""A small end-to-end matrix: trusted target -> candidate -> checks -> public report."""

import json
import subprocess
from types import SimpleNamespace
from unittest import mock

import pytest

from omniflow.cli import _client_and_branch_for_context, _run_context, cmd_run
from omniflow.config import _to_config
from omniflow.diagnostics import capture_private_reports
from omniflow.discovery import ModelContext
from omniflow.exceptions import ConfigError, OmniAuthError, SecurityPolicyError
from omniflow.reporting.markdown_report import render_markdown_report

MODEL_TEXT = "label: Orders\ndimensions:\n  id:\n    sql: ${TABLE}.id\n    primary_key: true\n"


@pytest.fixture
def promotion(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("OMNI_BASE_URL", "OMNI_MODEL_ID", "OMNI_BRANCH_ID", "OMNI_BRANCH_NAME",
                 "GITHUB_EVENT_NAME", "GITHUB_BASE_REF", "OMNIFLOW_CHANGED_FILES"):
        monkeypatch.delenv(name, raising=False)

    def git(*args):
        return subprocess.check_output(["git", *args], text=True).strip()

    git("init", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (tmp_path / "omni/shared").mkdir(parents=True)
    (tmp_path / "omni/shared/orders.view").write_text(MODEL_TEXT)
    (tmp_path / ".omni").mkdir()
    targets = [dict(environment=environment, base_branch=base, model_id=model,
                    base_url=f"https://{environment}.omni.example", model_path="omni/shared",
                    git_follower=follower, web_url="https://github.com/test/models")
               for environment, base, model, follower in (
                   ("development", "develop", "dev-model", False), ("production", "main", "prod-model", True))]
    (tmp_path / ".omni/flow.json").write_text(json.dumps({"version": 2, "models": targets}))
    (tmp_path / ".omniflow.yml").write_text("checks: {}\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "trusted base")
    base_sha = git("rev-parse", "HEAD")
    git("branch", "develop")
    git("checkout", "-b", "release/test")
    head_text = MODEL_TEXT.replace("Orders", "Reviewed Orders")
    (tmp_path / "omni/shared/orders.view").write_text(head_text)
    git("commit", "-am", "candidate")
    head_sha = git("rev-parse", "HEAD")
    git("checkout", "main")
    event = {"number": 1, "pull_request": {
        "base": {"ref": "main", "sha": base_sha, "repo": {"full_name": "test/models"}},
        "head": {"ref": "release/test", "sha": head_sha, "repo": {"full_name": "test/models"}},
    }}
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event))
    for name, value in {
        "GITHUB_EVENT_NAME": "pull_request_target", "GITHUB_BASE_REF": "main",
        "GITHUB_HEAD_REF": "release/test", "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_REPOSITORY": "test/models", "GITHUB_SERVER_URL": "https://github.com",
        "OMNIFLOW_ENVIRONMENT": "production", "OMNI_API_KEY": "production-test-credential",
    }.items():
        monkeypatch.setenv(name, value)
    return tmp_path, targets, event, head_text, event_path


class TargetClient:
    def __init__(self, model_id, base, follower, head_text):
        self.model_id, self.base, self.follower, self.head_text = model_id, base, follower, head_text
        self.calls = []
        self.stale = False
        self.disappear = False
        self.change_during_validation = False
        self.view_names = {"orders": "orders.view"}

    def get_git_configuration(self, model_id):
        assert model_id == self.model_id
        return dict(baseBranch=self.base, modelPath="omni/shared", gitFollower=self.follower,
                    branchPerPullRequest=True, gitServiceProvider="github", webUrl="https://github.com/test/models")

    def list_models(self, **kwargs):
        assert kwargs["base_model_id"] == self.model_id
        return [] if self.disappear else [dict(id="candidate-id", modelKind="BRANCH",
                                              baseModelId=self.model_id, name=kwargs["name"])]

    def get_model_yaml(self, model_id, branch_id=None, **kwargs):
        assert model_id == self.model_id
        self.calls.append(("yaml", branch_id))
        text = MODEL_TEXT if branch_id is None or self.stale else self.head_text
        return {"files": {"orders.view": text}, "viewNames": self.view_names}

    def validate_content(self, model_id, branch_id=None, **kwargs):
        assert model_id == self.model_id
        self.calls.append(("content", branch_id))
        return {"model_id": model_id, "branch": {"id": branch_id} if branch_id else None, "content": []}

    def validate_model(self, model_id, branch_id=None):
        assert model_id == self.model_id
        self.calls.append(("model", branch_id))
        if self.change_during_validation:
            self.stale = True
        return []

    def list_content(self, **kwargs):
        return []

    def search_content_references(self, model_id, branch_id=None, **kwargs):
        return self.validate_content(model_id, branch_id=branch_id)


def run_args():
    return SimpleNamespace(config=None, auto=True, skip_reason=None)


@pytest.mark.parametrize("scenario", ["existing", "comment_only", "new_model", "new_content", "stale_head", "stale_base", "base_drift"])
def test_regression_pr_verifies_both_git_snapshots_even_without_environment(promotion, monkeypatch, scenario):
    root, _, event, head_text, event_path = promotion
    if scenario == "comment_only":
        head_text = MODEL_TEXT + "# Comment-only proposed change\n"
        subprocess.run(["git", "checkout", "release/test"], check=True, capture_output=True)
        (root / "omni/shared/orders.view").write_text(head_text)
        subprocess.run(["git", "commit", "-am", "Comment-only fixture"], check=True, capture_output=True)
        event["pull_request"]["head"]["sha"] = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        event_path.write_text(json.dumps(event))
        subprocess.run(["git", "checkout", "main"], check=True, capture_output=True)
    client = TargetClient("prod-model", "main", True, head_text)
    context = ModelContext(base_url="https://omni.example", model_id="prod-model", model_path="omni/shared",
                           branch_id="candidate-id", branch_name="release/test")
    config = _to_config({"contracts": {"enabled": False}, "checks": {
        "semantic_lint": {"enabled": False}, "content_validation": {"fail_on_new_only": True},
        "model_validation": {"fail_on_new_only": True},
    }}, None)
    original_yaml = client.get_model_yaml
    base_reads = []

    def yaml_snapshot(model_id, branch_id=None, **kwargs):
        result = original_yaml(model_id, branch_id, **kwargs)
        if branch_id is None:
            base_reads.append(True)
        if ((scenario == "stale_head" and branch_id) or (scenario == "stale_base" and not branch_id)
                or (scenario == "base_drift" and not branch_id and len(base_reads) > 1)):
            result["files"]["orders.view"] += "# unsynchronized change\n"
        return result

    def model(model_id, branch_id=None):
        rows = [{"message": "Existing model error", "yaml_path": "orders.view", "is_warning": False}]
        if scenario == "new_model" and branch_id:
            rows.append({"message": "New model error", "yaml_path": "orders.view", "is_warning": False})
        return rows

    def content(model_id, branch_id=None, **kwargs):
        errors = ["Existing filter error"]
        if scenario == "new_content" and branch_id:
            errors.append("New filter error")
        return {"content": [{"document_id": "doc", "identifier": "doc",
                             "dashboard_filter_issues": [{"filter_name": "filter", "issues": errors}]}]}

    client.get_model_yaml = yaml_snapshot
    client.validate_model = model
    client.validate_content = content
    monkeypatch.setattr("omniflow.cli._client_and_branch_for_context", lambda *a, **kw: (client, "candidate-id"))
    if scenario.startswith("stale") or scenario == "base_drift":
        with pytest.raises(ConfigError) as caught:
            _run_context(config=config, context=context, output_dir=root / ".omniflow")
        assert caught.value.omniflow_progress.current == "candidate"
        assert caught.value.omniflow_progress.evidence()["validation_complete"] is False
    else:
        report, code = _run_context(config=config, context=context, output_dir=root / ".omniflow")
        assert code == int(scenario in {"new_model", "new_content"})
        assert report["validation_complete"] is True
        assert report["candidate_verification"]["status"] == "verified"
        assert len(base_reads) == 2
        assert report["summary"]["existing_issues"] == 2
        assert report["summary"]["blocking_issues"] == int(scenario in {"new_model", "new_content"})


@pytest.mark.parametrize("target", ["development", "production"])
@pytest.mark.parametrize("orientation", ["name_to_path", "path_to_name"])
def test_two_environment_run_validates_only_selected_candidate(promotion, monkeypatch, target, orientation):
    root, targets, event, head_text, event_path = promotion
    selected = next(item for item in targets if item["environment"] == target)
    event["pull_request"]["base"]["ref"] = selected["base_branch"]
    event_path.write_text(json.dumps(event))
    monkeypatch.setenv("GITHUB_BASE_REF", selected["base_branch"])
    monkeypatch.setenv("OMNIFLOW_ENVIRONMENT", target)
    client = TargetClient(selected["model_id"], selected["base_branch"], selected["git_follower"], head_text)
    if orientation == "path_to_name":
        client.view_names = {"orders.view": "orders"}
    with capture_private_reports() as receipts, \
         mock.patch("omniflow.cli.OmniClient", return_value=client) as constructor:
        assert cmd_run(run_args(), changed_files=["omni/shared/orders.view"]) == 0
    assert constructor.call_args.kwargs["base_url"] == selected["base_url"]
    assert ("content", "candidate-id") in client.calls
    assert ("model", "candidate-id") in client.calls
    assert ("yaml", "candidate-id") in client.calls
    report = json.loads((root / ".omniflow/public/report.json").read_text())
    assert report["diagnostic_schema_version"] == 2
    assert report["validation_complete"] is True
    assert len(report["models"]) == 1
    assert len(receipts) == 1
    private_report = receipts[0]
    assert private_report["models"][0]["model_id"] == selected["model_id"]
    assert private_report["models"][0]["environment"] == target
    proof = private_report["model_reports"][0]["candidate_verification"]
    assert proof["status"] == "verified"
    assert proof["head_sha"] == event["pull_request"]["head"]["sha"]
    assert report["model_reports"][0]["candidate_verification"]["status"] == "verified"
    assert "head_sha" not in report["model_reports"][0]["candidate_verification"]
    markdown = (root / ".omniflow/public/report.md").read_text()
    for identity in (selected["model_id"], selected["base_url"], target, proof["head_sha"], "omni/shared"):
        assert identity not in json.dumps(report) + markdown
    assert "Validation complete: `true`" in markdown
    private_markdown = render_markdown_report(private_report)
    assert target in private_markdown and "before and after checks" in private_markdown
    assert "does not authorize deployment" in private_markdown


@pytest.mark.parametrize("redaction", ["standard", "strict"])
def test_metadata_failure_keeps_environment_and_incomplete_candidate_evidence(promotion, redaction):
    root, _, _, head_text, _ = promotion
    client = TargetClient("prod-model", "main", True, head_text)
    client.view_names = {"PRIVATE-METADATA": "missing.view"}
    config = _to_config({}, None)
    config.security.redaction_level = redaction
    with capture_private_reports() as receipts, \
         mock.patch("omniflow.cli.OmniClient", return_value=client), \
         mock.patch("omniflow.cli.load_config", return_value=config):
        assert cmd_run(run_args(), changed_files=["omni/shared/orders.view"]) != 0
    report_text = (root / ".omniflow/public/report.json").read_text()
    report = json.loads(report_text)
    context = report["model_reports"][0]
    assert len(receipts) == 1
    private_context = receipts[0]["model_reports"][0]
    assert private_context["environment"] == "production"
    assert "environment" not in context
    assert context["validation_complete"] is False
    assert context["candidate_verification"]["status"] == "pending_recheck"
    issue = next(item for item in context["issues"] if item.get("metadata_stage") == "view_names")
    assert issue["metadata_reason"] == "unsupported_orientation"
    assert issue["validator"] == "semantic_diff"
    states = {item["validator"]: item["status"] for item in context["check_states"]}
    assert states["semantic_diff"] == "failed" and states["contracts"] == "not_run"
    markdown = (root / ".omniflow/public/report.md").read_text()
    assert "Candidate coverage is incomplete" in render_markdown_report(receipts[0])
    assert "Validation complete: `false`" in markdown
    for identity in ("PRIVATE-METADATA", "production", "prod-model", "omni/shared", "candidate-id"):
        assert identity not in report_text + markdown


@pytest.mark.parametrize("failure", ["missing", "stale", "changed_during_checks"])
def test_candidate_failures_are_incomplete_not_clean_content(promotion, failure):
    root, _, _, head_text, _ = promotion
    client = TargetClient("prod-model", "main", True, head_text)
    client.disappear = failure == "missing"
    client.stale = failure == "stale"
    client.change_during_validation = failure == "changed_during_checks"
    with capture_private_reports() as receipts, mock.patch("omniflow.cli.OmniClient", return_value=client):
        assert cmd_run(run_args(), changed_files=["omni/shared/orders.view"]) != 0
    report = json.loads((root / ".omniflow/public/report.json").read_text())
    assert report["validation_complete"] is False
    assert report["model_reports"][0]["candidate_verification"]["status"] == "unverified"
    assert len(receipts) == 1
    assert "Candidate coverage is incomplete" in render_markdown_report(receipts[0])
    assert "Validation complete: `false`" in (root / ".omniflow/public/report.md").read_text()
    assert "environment" not in report["model_reports"][0]
    assert "model_id" not in report["model_reports"][0]
    if failure != "changed_during_checks":
        assert not any(kind in {"model", "content"} for kind, _ in client.calls)


def test_wrong_environment_binding_fails_before_client_construction(monkeypatch):
    monkeypatch.setenv("OMNIFLOW_ENVIRONMENT", "development")
    context = ModelContext("https://prod.example", "model", "omni/shared", environment="production")
    with mock.patch("omniflow.cli.OmniClient") as client, pytest.raises(SecurityPolicyError):
        _client_and_branch_for_context(context, 60, api_key="not-sent")
    client.assert_not_called()


def test_wrong_target_credential_never_falls_back_to_another_environment(promotion):
    root, _, _, head_text, _ = promotion
    client = TargetClient("prod-model", "main", True, head_text)
    with capture_private_reports() as receipts, \
         mock.patch("omniflow.cli.OmniClient", return_value=client) as constructor, \
         mock.patch.object(client, "get_git_configuration", side_effect=OmniAuthError("Access denied")):
        assert cmd_run(run_args(), changed_files=["omni/shared/orders.view"]) != 0
    constructor.assert_called_once()
    assert client.calls == []
    report = json.loads((root / ".omniflow/public/report.json").read_text())
    assert report["validation_complete"] is False
    assert len(receipts) == 1
    assert receipts[0]["models"][0]["environment"] == "production"
    assert receipts[0]["models"][0]["model_id"] == "prod-model"
    assert "environment" not in report["models"][0]
    assert "prod-model" not in json.dumps(report)
    assert report["models"][0]["candidate_verification"]["status"] == "unverified"


@pytest.mark.parametrize("setting", ["breaking_change_hold", "dbt_sync"])
def test_repository_wide_sync_policy_rejected_before_api_use(setting, tmp_path):
    config = _to_config({}, None)
    getattr(config, setting).enabled = True
    context = ModelContext("https://prod.example", "model", "omni/shared", environment="production")
    with mock.patch("omniflow.cli.OmniClient") as client, pytest.raises(SecurityPolicyError):
        _run_context(config=config, context=context, output_dir=tmp_path)
    client.assert_not_called()


@pytest.mark.parametrize("mismatch", ["baseBranch", "modelPath", "gitFollower", "branchPerPullRequest", "webUrl"])
def test_wrong_live_target_settings_fail_before_branch_or_validation(promotion, mismatch):
    _, _, _, head_text, _ = promotion
    client = TargetClient("prod-model", "main", True, head_text)
    settings = client.get_git_configuration("prod-model")
    settings[mismatch] = False if mismatch in {"gitFollower", "branchPerPullRequest"} else "wrong"
    context = ModelContext("https://production.omni.example", "prod-model", "omni/shared",
                           branch_name="release/test", base_branch="main", environment="production",
                           git_follower=True, web_url="https://github.com/test/models")
    with mock.patch("omniflow.cli.OmniClient", return_value=client), \
         mock.patch.object(client, "get_git_configuration", return_value=settings), \
         mock.patch.object(client, "list_models") as branches, pytest.raises(ConfigError):
        _client_and_branch_for_context(context, 60)
    branches.assert_not_called()
