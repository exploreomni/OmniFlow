import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from xml.etree import ElementTree

import pytest
import yaml

from omniflow.cli import build_parser, cmd_run
from omniflow.config import _to_config
from omniflow.diagnostics import public_diagnostic
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
        list_content=lambda **kwargs: [{"identifier": "doc-short", "name": "Private dashboard"}],
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
    assert existing["severity"] == "error"
    assert existing["blocking"] is False
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
    # This test isolates API/report behavior; immutable PR snapshots are covered
    # by the real-Git regression integration tests.
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    action_sha, input_sha = "a" * 40, "b" * 40
    monkeypatch.setenv("OMNIFLOW_ACTION_REF", action_sha)
    # Model the PR head separately from the merge ref, without inheriting the
    # runner's real pull_request event during repository CI.
    monkeypatch.setenv("GITHUB_SHA", "c" * 40)
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({"pull_request": {"head": {"sha": input_sha}}}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    config = _to_config({}, None)
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
    assert report["diagnostic_schema_version"] == 2
    assert len(report["run_reference"]) == 32
    assert "git_sha" not in report
    assert report["tool_revision"] == {"commit": action_sha, "source": "github_action_ref"}
    assert "model_id" not in report["models"][0]
    assert "model_path" not in report["models"][0]
    finding = report["issues"][0]
    assert finding["type"] == "content_evidence_unavailable"
    assert finding["validation_scope"] == scope
    assert finding["reason_code"] == "missing_issue_message"
    assert finding["comparison_status"] == "unavailable"
    assert finding["content_health"] == finding["regression_status"] == "unknown"
    assert "document_identifier" not in finding
    assert "document_name" not in finding
    assert finding["context_reference"] == report["models"][0]["context_reference"]
    assert report["policy_decision"] == "fail"
    assert report["model_reports"][0]["check_reports"][0]["coverage_complete"] is False
    evidence = json.loads((public / "evidence.json").read_text())
    assert evidence["exit_code_reason"] == "Omni API error"
    assert evidence["diagnostic_schema_version"] == 2
    assert evidence["run_reference"] == report["run_reference"]
    assert evidence["tool_revision"] == report["tool_revision"]
    assert json.loads((public / "report.sarif").read_text())["runs"][0]["results"][0]["level"] == "error"
    assert ElementTree.fromstring((public / "junit.xml").read_text()).get("failures") == "1"
    for path in public.iterdir():
        text = path.read_text()
        assert "private payload do not publish" not in text
        assert "private.example" not in text
        assert "Private owner" not in text
        assert "Private dashboard" not in text
        assert "doc-id" not in text
        assert "doc-short" not in text
        assert input_sha not in text
    assert not (tmp_path / ".omniflow/restricted").exists()


def test_cli_unverified_draft_is_safe_operational_failure_not_new_content_defect(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    config = _to_config({}, None)
    config.content_validation.fail_on_new_only = True
    config.reporting.formats = ["json", "markdown", "sarif", "junit"]
    context = ModelContext(base_url="https://omni.example", model_id="model", model_path="model", branch_id="branch")
    current = payload("Synthetic private field missing")
    current["content"][0].update(identifier="synthetic-draft", document_id="synthetic-draft-physical")
    client = SimpleNamespace(
        validate_content=lambda *args, branch_id=None, **kwargs: (
            current if branch_id else payload("Synthetic private field missing")
        ),
        list_content=lambda **kwargs: [{"identifier": "doc-short"}],
        get_document_identity=lambda identifier, **kwargs: {
            "identifier": "synthetic-draft", "publishedIdentifier": "doc-short",
        },
        list_document_drafts=lambda parent: [{
            "identifier": "synthetic-draft", "publishedIdentifier": "doc-short",
            "branch": {"id": "wrong-private-branch"}, "status": "active", "draftOutOfDate": False,
            "updatedAt": "2026-10-15T12:00:00.000Z",
        }],
    )
    args = build_parser().parse_args(["run", "--auto"])
    with mock.patch("omniflow.cli.load_config", return_value=config), \
            mock.patch("omniflow.cli.discover_contexts", return_value=[context]), \
            mock.patch("omniflow.cli._client_and_branch_for_context", return_value=(client, "branch")), \
            mock.patch("omniflow.cli.run_model_validation") as model_validation:
        assert cmd_run(args, changed_files=[]) == 4
    model_validation.assert_not_called()
    public = tmp_path / ".omniflow/public"
    report = json.loads((public / "report.json").read_text())
    assert report["exit_code"] == 4 and report["policy_decision"] == "fail"
    assert report["diagnostic_schema_version"] == 2
    finding = report["issues"][0]
    assert finding["type"] == "content_evidence_unavailable"
    assert finding["reason_code"] == "document_identity_wrong_branch"
    assert finding["validation_scope"] == "branch"
    assert finding["comparison_mode"] == "new_only"
    assert finding["comparison_status"] == "unavailable"
    assert finding["regression_status"] == "unknown"
    assert finding["blocking"] is True
    assert all(issue["type"] != "content_validation_issue" for issue in report["issues"])
    assert report["model_reports"][0]["check_reports"][0]["coverage_complete"] is False
    captured = capsys.readouterr()
    rendered = captured.out + captured.err + "".join(path.read_text() for path in public.iterdir())
    for private in ("synthetic-draft", "doc-short", "wrong-private-branch", "Synthetic private field missing",
                    "Private dashboard", "private.example"):
        assert private not in rendered
    assert not (tmp_path / ".omniflow/restricted").exists()


@pytest.mark.parametrize("action_ref, expected", [
    ("A" * 40, "a" * 40), ("main", None), ("v0.5.0", None), ("", None),
    ("g" * 40, None), ("a" * 39, None), ("a" * 41, None),
])
def test_action_provenance_never_uses_the_input_sha(monkeypatch, action_ref, expected):
    monkeypatch.setenv("OMNIFLOW_ACTION_REF", action_ref)
    monkeypatch.setenv("GITHUB_SHA", "c" * 40)
    result = tool_revision()
    assert result["commit"] == expected
    assert result["source"] == ("github_action_ref" if expected else "unavailable")
    safe = public_diagnostic({"tool_revision": {**result, "private_detail": "DO-NOT-PUBLISH"}})
    if expected:
        assert safe["tool_revision"] == result
    else:
        assert "tool_revision" not in safe
    assert "DO-NOT-PUBLISH" not in json.dumps(safe)
    assert "c" * 40 not in json.dumps(safe)
    for source in ("git_sha", "github_sha", "unavailable", "DO-NOT-PUBLISH"):
        assert "tool_revision" not in public_diagnostic({"tool_revision": {"commit": "a" * 40, "source": source}})


def test_all_cli_action_steps_receive_action_ref_via_environment():
    action = yaml.safe_load((Path(__file__).resolve().parents[1] / "action.yml").read_text())
    cli_steps = [step for step in action["runs"]["steps"] if "\nomniflow " in "\n" + step.get("run", "")]
    assert len(cli_steps) == 8
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
