import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from xml.etree import ElementTree

import pytest
import yaml

from omniflow.cli import build_parser, cmd_run
from omniflow.config import load_config
from omniflow.discovery import ModelContext
from omniflow.git import tool_revision
from omniflow.reporting.junit_report import to_junit
from omniflow.validators.content import ContentEvidenceError, extract_issues, issue_summary, run_content_validation
from omniflow.validators.model import run_model_validation


def payload(issue):
    return {"content": [{
        "document_id": "doc-id", "identifier": "doc-short", "name": "Private dashboard",
        "dashboard_filter_issues": [issue],
        "url": "https://private.example/dashboard", "owner": {"name": "Private owner"},
    }]}


def run_validation(tmp_path, current, base=None, **overrides):
    client = SimpleNamespace(
        validate_content=lambda *args, branch_id=None, **kwargs: current if branch_id else base,
        list_content=lambda **kwargs: [],
    )
    return run_content_validation(
        client=client, model_id="model", branch_id="branch", user_id=None,
        include_personal_folders=False, labels=[], fail_on_new_only=True,
        history_in=tmp_path / "missing.json", history_out=tmp_path / "history.json",
        report_out=tmp_path / "report.json", **overrides,
    )


@pytest.mark.parametrize("issue", [None, "", "  ", 42, {"message": None}, {"message": " "},
                                      {"unexpected_private_shape": "private detail"}])
def test_unreadable_issue_is_evidence_failure_not_invented_defect(issue):
    with pytest.raises(ContentEvidenceError) as caught:
        extract_issues(payload(issue))
    error = caught.value
    assert error.exit_code == 4
    finding = error.report_issue()
    assert finding["type"] == "content_evidence_unavailable"
    assert finding["issue_type"] == "dashboard_filter"
    assert finding["document_identifier"] == "doc-short"
    assert finding["document_name"] == "Private dashboard"
    assert error.report_issue(redact_document_names=True)["document_name"] == "[REDACTED]"
    assert "Private dashboard" not in str(error)
    assert "private detail" not in json.dumps(finding)
    assert "owner" not in json.dumps(finding)
    assert "url" not in json.dumps(finding)


@pytest.mark.parametrize("issue", ["Filter references an unknown field", {"message": "Filter references an unknown field"}])
def test_valid_filter_issues_keep_identity_and_new_only_policy(tmp_path, issue):
    current = payload(issue)
    current["content"][0]["queries_and_issues"] = [{
        "query_name": "Private query", "query_presentation_id": "query-id", "issues": ["Broken query field"],
    }]
    report, code = run_validation(tmp_path, current, payload(issue))
    assert code == 1
    assert (report["new_issues"], report["existing_issues"], report["resolved_issues"]) == (1, 1, 0)
    existing = next(item for item in report["issues"] if item["state"] == "existing")
    assert existing["severity"] == "info"
    assert existing["issue_type"] == "dashboard_filter"
    assert existing["document_name"] == "Private dashboard"
    assert "Filter references an unknown field" in existing["message"]
    assert existing["validation_scope"] == "branch"
    new = next(item for item in report["issues"] if item["state"] == "new")
    assert new["query_name"] == "Private query"
    assert new["query_presentation_id"] == "query-id"
    assert new["issue_type"] == "query"
    redacted, _ = run_validation(tmp_path, current, {"content": []}, redact_document_names=True)
    assert all(item["document_name"] == "[REDACTED]" for item in redacted["issues"])
    assert "Private query" not in json.dumps(redacted)


def test_resolved_base_finding_is_not_labeled_branch_evidence(tmp_path):
    report, code = run_validation(tmp_path, {"content": []}, payload("Existing broken filter"))
    assert code == 0
    assert report["issues"][0]["validation_scope"] == "base"
    assert report["issues"][0]["active"] is False


def test_summary_never_stringifies_unknown_message_object():
    for value in ({"message": None, "private_detail": "do not print"}, {"message": {"private": "do not print"}}, None):
        summary = issue_summary(value)
        assert "details are unavailable" in summary
        assert "do not print" not in summary


def test_evidence_context_redacts_before_truncating_long_values(monkeypatch):
    credential = "synthetic-credential-crossing-the-length-boundary"
    monkeypatch.setenv("OMNI_API_KEY", credential)
    value = "x" * 500 + credential
    error = ContentEvidenceError(document={"name": value}, query={"query_name": value})
    finding = error.report_issue()
    for key in ("document_name", "query_name"):
        assert finding[key] == "x" * 500 + "[REDACTED]"
        assert "synthetic" not in finding[key]


@pytest.mark.parametrize("scope", ["branch", "base"])
@pytest.mark.parametrize("privacy", ["standard", "names", "strict"])
def test_api_to_cli_reports_incomplete_evidence_and_preserves_privacy(tmp_path, monkeypatch, scope, privacy):
    monkeypatch.chdir(tmp_path)
    action_sha, input_sha = "a" * 40, "b" * 40
    monkeypatch.setenv("OMNIFLOW_ACTION_REF", action_sha)
    monkeypatch.setenv("GITHUB_SHA", input_sha)
    config = load_config(None)
    config.content_validation.fail_on_new_only = True
    config.security.redaction_level = "strict" if privacy == "strict" else "standard"
    config.security.redact_document_names = privacy == "names"
    config.reporting.formats = ["json", "markdown", "sarif", "junit"]
    context = ModelContext(base_url="https://omni.example", model_id="model", model_path="model", branch_id="branch")
    malformed = payload({"unknown_nested_detail": "private payload do not publish"})
    client = SimpleNamespace(
        validate_content=lambda *args, branch_id=None, **kwargs: (
            malformed if (bool(branch_id) == (scope == "branch")) else {"content": []}
        ),
        list_content=lambda **kwargs: [],
    )
    args = build_parser().parse_args(["run", "--auto"])
    with mock.patch("omniflow.cli.load_config", return_value=config), \
            mock.patch("omniflow.cli.discover_contexts", return_value=[context]), \
            mock.patch("omniflow.cli._client_and_branch_for_context", return_value=(client, "branch")), \
            mock.patch("omniflow.cli.run_model_validation") as model_validation:
        code = cmd_run(args, changed_files=[])
    assert code == 4
    model_validation.assert_not_called()
    public = tmp_path / ".omniflow/public"
    report = json.loads((public / "report.json").read_text())
    assert report["exit_code_reason"] == "Omni API error"
    assert report["git_sha"] == input_sha
    assert report["tool_revision"] == {"commit": action_sha, "source": "github_action_ref"}
    finding = report["issues"][0]
    assert finding["type"] == "content_evidence_unavailable"
    assert finding["validation_scope"] == scope
    assert report["policy_decision"] == "fail"
    assert report["model_reports"][0]["check_reports"][0]["coverage_complete"] is False
    evidence = json.loads((public / "evidence.json").read_text())
    assert evidence["exit_code_reason"] == "Omni API error"
    assert evidence["tool_revision"] == report["tool_revision"]
    assert json.loads((public / "report.sarif").read_text())["runs"][0]["results"][0]["level"] == "error"
    assert ElementTree.fromstring((public / "junit.xml").read_text()).get("failures") == "1"
    for path in public.iterdir():
        text = path.read_text()
        assert "private payload do not publish" not in text
        assert "private.example" not in text
        assert "Private owner" not in text
        if privacy != "standard":
            assert "Private dashboard" not in text
    assert not (tmp_path / ".omniflow/restricted").exists()


@pytest.mark.parametrize("action_ref, expected", [("A" * 40, "a" * 40), ("main", None), ("v0.5.0", None), ("", None)])
def test_action_provenance_never_uses_the_input_sha(monkeypatch, action_ref, expected):
    monkeypatch.setenv("OMNIFLOW_ACTION_REF", action_ref)
    monkeypatch.setenv("GITHUB_SHA", "c" * 40)
    result = tool_revision()
    assert result["commit"] == expected
    assert result["source"] == ("github_action_ref" if expected else "unavailable")


def test_all_cli_action_steps_receive_action_ref_via_environment():
    action = yaml.safe_load((Path(__file__).resolve().parents[1] / "action.yml").read_text())
    cli_steps = [step for step in action["runs"]["steps"] if "\nomniflow " in "\n" + step.get("run", "")]
    assert len(cli_steps) == 6
    for step in cli_steps:
        assert step["env"]["OMNIFLOW_ACTION_REF"] == "${{ github.action_ref }}"
        assert "${{ github.action_ref }}" not in step["run"]


@pytest.mark.parametrize("fail_on_warnings", [False, True])
def test_model_warning_blocking_metadata_matches_unchanged_policy(fail_on_warnings):
    client = SimpleNamespace(validate_model=lambda *args, **kwargs: [{"message": "Model warning", "is_warning": True}])
    report, code = run_model_validation(client=client, model_id="model", branch_id="branch", fail_on_warnings=fail_on_warnings)
    assert code == int(fail_on_warnings)
    assert report["issues"][0]["severity"] == "warning"
    assert report["issues"][0]["blocking"] is fail_on_warnings
    assert ElementTree.fromstring(to_junit(report)).get("failures") == str(int(fail_on_warnings))
