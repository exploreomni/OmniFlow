import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from omniflow import cli
from omniflow.artifacts import write_artifact_manifest, write_public_json, write_public_reports
from omniflow.config import _to_config, load_config
from omniflow.diagnostics import capture_private_reports, diagnostic_session, public_diagnostic
from omniflow.exceptions import ConfigError, OmniFlowError, OmniResponseError, SecurityPolicyError
from omniflow.github.annotations import annotation_lines
from omniflow.security import RedactingFilter
from omniflow.validators.content import run_content_validation

# All fixtures are fabricated. None represent a person, account or credential.
SENSITIVE = "SYNTHETIC_PATIENT MRN-TEST-123 diagnosis-TEST SSN-000-12-3456 CARD-4242424242424242 CVV-123"


def private_report():
    issue = {
        "validator": "content", "type": "content_evidence_unavailable", "severity": "error",
        "reason_code": "missing_issue_message", "validation_scope": "base", "comparison_status": "unavailable",
        "model_id": SENSITIVE, "branch_id": SENSITIVE, "branch_name": SENSITIVE,
        "document_id": SENSITIVE, "document_name": SENSITIVE, "query_name": SENSITIVE,
        "filter_name": SENSITIVE,
        "message": SENSITIVE, "file": SENSITIVE, "yaml_path": SENSITIVE, "authorization": SENSITIVE,
        "raw": {"nested": [SENSITIVE]}, "payload": {"message": SENSITIVE},
    }
    return {
        "model_id": SENSITIVE, "model_path": SENSITIVE, "git_branch": SENSITIVE, "runner": SENSITIVE,
        "git_sha": SENSITIVE, "tool_revision": SENSITIVE, "tool_version": SENSITIVE, "run_reference": SENSITIVE,
        "policy_decision": "fail", "exit_code": 4, "exit_code_reason": SENSITIVE,
        "validation_complete": False, "summary": {"errors": 1, "warnings": 0, "private": SENSITIVE},
        "issues": [issue], "models": [{"model_id": SENSITIVE}],
        "model_reports": [{"model_id": SENSITIVE, "issues": [issue],
                           "check_states": [{"validator": "content", "status": "failed", "message": SENSITIVE}]}],
        "unexpected_customer_field": SENSITIVE,
    }


@pytest.mark.parametrize("level", ["standard", "strict"])
def test_actual_shareable_formats_exclude_sensitive_values_and_identity(tmp_path, level):
    with diagnostic_session():
        safe = write_public_reports(private_report(), output_dir=tmp_path,
                                    formats=["json", "markdown", "sarif", "junit"], redaction_level=level)
        evidence = write_public_json(tmp_path / "public/evidence.json", private_report(), redaction_level=level)
    assert evidence["run_reference"] == safe["run_reference"]
    assert safe["diagnostic_schema_version"] == 2
    assert safe["issues"][0]["reason_code"] == "missing_issue_message"
    assert safe["issues"][0]["validation_scope"] == "base"
    assert safe["exit_code"] == 4 and safe["policy_decision"] == "fail"
    for path in tmp_path.rglob("*"):
        if path.is_file():
            text = path.read_text()
            assert SENSITIVE not in text
            for marker in ("MRN-TEST", "000-12-3456", "4242424242424242", "CVV-123"):
                assert marker not in text
    assert SENSITIVE not in "\n".join(annotation_lines(safe["issues"]))
    assert json.loads((tmp_path / "report.json").read_text()) == json.loads(
        (tmp_path / "public/report.json").read_text())
    assert not (tmp_path / "restricted").exists()


@pytest.mark.parametrize("field", ["type", "validator", "reason_code", "severity", "comparison_status",
                                  "status", "state", "exit_code", "errors", "content_validation_required"])
def test_allowlisted_field_names_do_not_allow_unvalidated_values(field):
    safe = public_diagnostic({"issues": [{field: SENSITIVE}]})
    assert SENSITIVE not in json.dumps(safe)


def test_private_receipt_is_scoped_in_memory_and_not_serialized(tmp_path):
    with capture_private_reports() as receipts:
        safe = write_public_reports(private_report(), output_dir=tmp_path,
                                    formats=["json"], redaction_level="standard")
    assert receipts[0]["model_id"] == SENSITIVE
    assert "model_id" not in safe
    write_public_reports({}, output_dir=tmp_path, formats=["json"], redaction_level="standard")
    assert len(receipts) == 1


@pytest.mark.parametrize("status", ["not_needed", "rolled_back", "rollback_failed", "committed"])
def test_public_diagnostics_preserve_fixed_repair_safety_statuses(status):
    safe = public_diagnostic({"status": status, "rollback": {"status": status, "message": SENSITIVE}})
    assert safe["status"] == safe["rollback"]["status"] == status
    assert SENSITIVE not in json.dumps(safe)


def test_public_failure_fallback_never_serializes_exception(tmp_path):
    with patch("omniflow.artifacts.write_reports", side_effect=ValueError(SENSITIVE)):
        with pytest.raises(OmniFlowError):
            write_public_reports(private_report(), output_dir=tmp_path,
                                 formats=["json", "markdown"], redaction_level="standard")
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert SENSITIVE not in path.read_text()
    assert json.loads((tmp_path / "public/report.json").read_text())["exit_code"] == 6


def test_public_logs_and_cli_errors_drop_free_text_and_tracebacks(capsys):
    record = logging.LogRecord("test", logging.ERROR, "private.py", 1, SENSITIVE, (),
                               (ValueError, ValueError(SENSITIVE), None))
    record.stack_info = SENSITIVE
    RedactingFilter().filter(record)
    assert SENSITIVE not in logging.Formatter().format(record)
    with patch("omniflow.cli.cmd_doctor", side_effect=ConfigError(SENSITIVE)):
        assert cli.main(["doctor"]) == 2
    assert SENSITIVE not in capsys.readouterr().err


def test_readiness_entrypoint_does_not_print_unexpected_exception(capsys):
    from omniflow.github import revalidation

    with patch("sys.argv", ["revalidation", "--pull-request", "1"]), \
            patch("omniflow.github.revalidation.revalidate", side_effect=RuntimeError(SENSITIVE)):
        assert revalidation.main() == 1
    assert SENSITIVE not in capsys.readouterr().out


def test_legacy_report_rerender_is_not_a_privacy_bypass(tmp_path):
    source = tmp_path / "private-input.json"
    source.write_text(json.dumps(private_report()))
    target = tmp_path / "output"
    cli.cmd_report(SimpleNamespace(input=source, output_dir=target, format=["json", "markdown", "sarif", "junit"]))
    for path in target.rglob("*"):
        if path.is_file():
            assert SENSITIVE not in path.read_text()


def test_artifact_manifest_does_not_list_customer_named_public_files(tmp_path):
    public = tmp_path / "public"
    public.mkdir()
    (public / "patient-MRN-TEST-123.txt").write_text(SENSITIVE)
    write_artifact_manifest(output_dir=tmp_path, restricted_artifacts_enabled=False, redaction_level="standard")
    manifest = json.loads((tmp_path / "artifact-manifest.json").read_text())
    assert "patient-MRN-TEST-123" not in json.dumps(manifest)
    assert manifest["unrecognized_public_file_count"] == 1


def test_classification_is_opt_in_and_never_skips_content(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = _to_config({}, None)
    assert config.content_validation.classify_comment_only is False
    config.content_validation.classify_comment_only = True
    context = cli.ModelContext(base_url="https://synthetic.example", model_id="model", model_path="model")
    args = cli.build_parser().parse_args(["run", "--auto"])
    with patch("omniflow.cli.load_config", return_value=config), \
            patch("omniflow.cli.discover_contexts", return_value=[context]), \
            patch("omniflow.cli.classify_pr_change", return_value={
                "classification": "comment_only", "reason_code": "live_content_provenance_unavailable",
                "content_validation_required": True,
            }), patch("omniflow.cli._run_context", return_value=(private_report(), 4)) as validate:
        assert cli.cmd_run(args) == 4
    validate.assert_called_once()
    report = json.loads(Path(".omniflow/public/report.json").read_text())
    assert report["change_classification"]["classification"] == "comment_only"
    assert report["change_classification"]["content_validation_required"] is True
    assert report["policy_decision"] == "fail"


def test_classification_config_rejects_non_boolean_and_default_preserves_policy(tmp_path):
    path = tmp_path / ".omniflow.yml"
    path.write_text("checks:\n  content_validation:\n    classify_comment_only: true\n    fail_on_new_only: true\n")
    config = load_config(str(path))
    assert config.content_validation.classify_comment_only and config.content_validation.fail_on_new_only
    assert _to_config({}, None).content_validation.fail_on_new_only is False
    path.write_text("checks:\n  content_validation:\n    classify_comment_only: maybe\n")
    with pytest.raises(ConfigError):
        load_config(str(path))


def test_stale_report_formats_are_replaced_when_format_preference_changes(tmp_path):
    for directory in (tmp_path, tmp_path / "public"):
        directory.mkdir(exist_ok=True)
        for name in ("report.json", "report.md", "report.sarif", "junit.xml", "evidence.json"):
            (directory / name).write_text(SENSITIVE)
    write_public_reports(private_report(), output_dir=tmp_path, formats=["json"], redaction_level="standard")
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert SENSITIVE not in path.read_text()
    assert json.loads((tmp_path / "public/report.sarif").read_text())["runs"][0]["results"]


def test_standalone_content_diagnostics_are_minimal_and_private_scratch_is_purged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = _to_config({}, None)
    config.omni.model_id = SENSITIVE
    args = cli.build_parser().parse_args(["content", "validate"])
    for directory in (Path(".omniflow"), Path(".omniflow/public")):
        directory.mkdir(exist_ok=True)
        for name in ("report.json", "report.md", "report.sarif", "junit.xml", "evidence.json"):
            (directory / name).write_text(SENSITIVE)

    def validate(**kwargs):
        for key in ("history_out", "report_out"):
            target = Path(kwargs[key])
            assert target.is_relative_to(Path(".omniflow/restricted"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(SENSITIVE)
        return {**private_report(), "total_issues": 1, "new_issues": 1,
                "existing_issues": 0, "resolved_issues": 0}, 1

    with patch("omniflow.cli.load_config", return_value=config), \
            patch("omniflow.cli._client_and_branch", return_value=(object(), "branch")), \
            patch("omniflow.cli.run_content_validation", side_effect=validate):
        assert cli.cmd_content_validate(args) == 1
    assert not Path(".omniflow/restricted/content-validation").exists()
    public = Path(".omniflow/report.json").read_text()
    assert SENSITIVE not in public
    assert json.loads(public)["exit_code"] == 1
    for path in Path(".omniflow").rglob("*"):
        if path.is_file():
            assert SENSITIVE not in path.read_text(), f"Stale sensitive output: {path}"


@pytest.mark.parametrize("ci,retention", [("true", True), ("false", False)])
def test_standalone_raw_debug_requires_local_explicit_retention(tmp_path, monkeypatch, ci, retention):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", ci)
    config = _to_config({}, None)
    config.security.retain_restricted_artifacts = retention
    args = cli.build_parser().parse_args(["content", "validate", "--unsafe-raw-output"])
    with patch("omniflow.cli.load_config", return_value=config), \
            patch("omniflow.cli._client_and_branch", return_value=(object(), "branch")), \
            patch("omniflow.cli.run_content_validation") as validate:
        with pytest.raises(SecurityPolicyError):
            cli.cmd_content_validate(args)
    validate.assert_not_called()


def grouped_privacy_payload(*, malformed=False):
    return {"content": [{
        "document_id": "synthetic-document", "identifier": "synthetic-document", "name": SENSITIVE,
        "dashboard_filter_issues": [{
            "filter_name": f"  {SENSITIVE} synthetic-filter-\u03a9  ",
            "issues": [SENSITIVE, {"message": SENSITIVE}] if malformed else [SENSITIVE, f"Second {SENSITIVE}"],
        }],
    }]}


@pytest.mark.parametrize("level", ["standard", "strict"])
@pytest.mark.parametrize("malformed_scope", [None, "branch", "base"])
def test_grouped_filter_api_to_cli_keeps_all_shareable_outputs_minimal(
    tmp_path, monkeypatch, capsys, level, malformed_scope,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    # This fixture covers API-to-report privacy, not PR Git provenance. Keep
    # annotation output enabled without inheriting the hosting CI pull request.
    # Real Git snapshot verification is covered in test_environment_validation.
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    config = _to_config({}, None)
    config.content_validation.fail_on_new_only = True
    config.security.redaction_level = level
    config.model_validation.enabled = config.semantic_lint.enabled = config.contracts.enabled = False
    config.reporting.formats = ["json", "markdown", "sarif", "junit"]
    context = cli.ModelContext(
        base_url="https://synthetic.example", model_id="synthetic-model", model_path="model", branch_id="branch",
    )
    for directory in (tmp_path / ".omniflow", tmp_path / ".omniflow/public"):
        directory.mkdir(exist_ok=True)
        for name in ("report.json", "report.md", "report.sarif", "junit.xml", "evidence.json"):
            (directory / name).write_text(SENSITIVE)

    def validate(*args, branch_id=None, **kwargs):
        scope = "branch" if branch_id else "base"
        response = grouped_privacy_payload(malformed=scope == malformed_scope) if branch_id or scope == malformed_scope else {
            "content": [],
        }
        # Exercise the installed public logging filter, including exception text.
        logging.getLogger("omniflow.omni_client").error(
            "%s", json.dumps(response), exc_info=(ValueError, ValueError(SENSITIVE), None),
        )
        return response

    client = SimpleNamespace(validate_content=validate, list_content=lambda **kwargs: [])
    root_logger = logging.getLogger()
    previous_handlers, previous_level = root_logger.handlers[:], root_logger.level
    try:
        with capture_private_reports() as receipts, \
                patch("omniflow.cli.load_config", return_value=config), \
                patch("omniflow.cli.discover_contexts", return_value=[context]), \
                patch("omniflow.cli._client_and_branch_for_context", return_value=(client, "branch")):
            code = cli.main(["run", "--auto"], changed_files=[])
    finally:
        root_logger.handlers[:] = previous_handlers
        root_logger.setLevel(previous_level)

    assert code == (4 if malformed_scope else 1)
    public = tmp_path / ".omniflow/public"
    report = json.loads((public / "report.json").read_text())
    assert report["diagnostic_schema_version"] == 2
    assert report["policy_decision"] == "fail"
    assert report["exit_code"] == code
    assert json.loads((public / "evidence.json").read_text())["run_reference"] == report["run_reference"]
    assert len(receipts) == 1
    if malformed_scope:
        finding = report["issues"][0]
        assert finding["type"] == "content_evidence_unavailable"
        assert finding["validation_scope"] == malformed_scope
        assert finding["comparison_status"] == "unavailable"
        assert finding["content_health"] == finding["regression_status"] == "unknown"
    else:
        assert len(report["issues"]) == 2
        private_issues = receipts[0]["issues"]
        assert len(private_issues) == 2
        assert all(issue["raw"]["filter_name"] == grouped_privacy_payload()["content"][0][
            "dashboard_filter_issues"][0]["filter_name"] for issue in private_issues)
        assert {issue["raw"]["message"] for issue in private_issues} == {SENSITIVE, f"Second {SENSITIVE}"}

    assert not (tmp_path / ".omniflow/restricted").exists()
    captured = capsys.readouterr()
    assert "detailed text withheld from public logs" in captured.err
    shareable_text = captured.out + captured.err + "\n".join(annotation_lines(report["issues"]))
    for path in (tmp_path / ".omniflow").rglob("*"):
        if path.is_file():
            shareable_text += path.read_text()
    for marker in (SENSITIVE, "MRN-TEST", "000-12-3456", "4242424242424242", "CVV-123", "synthetic-filter", "filter_name"):
        assert marker not in shareable_text
    sarif = json.loads((public / "report.sarif").read_text())
    assert all(result["level"] == "error" for result in sarif["runs"][0]["results"])


@pytest.mark.parametrize("schema2", [False, True])
def test_public_report_is_never_trusted_as_content_history(tmp_path, schema2):
    prior = private_report()
    prior.update(total_issues=1, new_issues=0, existing_issues=1)
    public_history = public_diagnostic(prior) if schema2 else prior
    history_in = tmp_path / "public-report.json"
    history_in.write_text(json.dumps(public_history))
    current = {"content": [{
        "document_id": "synthetic-document", "identifier": "synthetic-document",
        "dashboard_filter_issues": ["Existing but not proven by a public report"],
    }]}
    client = SimpleNamespace(validate_content=lambda *args, **kwargs: current, list_content=lambda **kwargs: [])
    report, code = run_content_validation(
        client=client, model_id="synthetic-model", branch_id=None, user_id="synthetic-user",
        include_personal_folders=False, labels=[], fail_on_new_only=True,
        history_in=history_in, history_out=tmp_path / "restricted/history.json",
        report_out=tmp_path / "restricted/report.json",
    )
    assert code == 4
    assert report["validation_complete"] is False
    assert any(issue.get("reason_code") == "content_comparison_unavailable" and issue["blocking"]
               for issue in report["issues"])
    assert report["history_status"] == ("invalidated_public_diagnostic" if schema2 else "invalidated_legacy")
    assert report["comparison_status"] == "baseline_unavailable"
    assert report["baseline_issues"] is None
    assert report["new_issues"] == 1 and report["existing_issues"] == 0
    assert report["regression_status"] == "unknown"


@pytest.mark.parametrize("transport_failure", [False, True])
def test_standalone_model_replaces_stale_outputs_on_success_and_transport_failure(
    tmp_path, monkeypatch, capsys, transport_failure,
):
    monkeypatch.chdir(tmp_path)
    config = _to_config({}, None)
    config.omni.model_id = SENSITIVE
    args = cli.build_parser().parse_args(["model", "validate"])
    root = Path(".omniflow")
    for directory in (root, root / "public"):
        directory.mkdir(exist_ok=True)
        for name in ("report.json", "report.md", "report.sarif", "junit.xml", "evidence.json"):
            (directory / name).write_text(SENSITIVE)
    (root / "model-report.json").write_text(SENSITIVE)
    result = {"validator": "model", "model_id": SENSITIVE, "message": SENSITIVE,
              "issues": [], "summary": {"errors": 0, "warnings": 0}}
    with patch("omniflow.cli.load_config", return_value=config), \
            patch("omniflow.cli._client_and_branch", return_value=(object(), "branch")), \
            patch("omniflow.cli.run_model_validation", return_value=(result, 0),
                  side_effect=OmniResponseError("empty_api_response") if transport_failure else None):
        if transport_failure:
            with pytest.raises(OmniResponseError):
                cli.cmd_model_validate(args)
        else:
            assert cli.cmd_model_validate(args) == 0
    for name in ("report.json", "public/report.json", "model-report.json"):
        report = json.loads((root / name).read_text())
        assert report["diagnostic_schema_version"] == 2
        assert report["exit_code"] == (4 if transport_failure else 0)
        assert report["policy_decision"] == ("fail" if transport_failure else "pass")
        assert report["validation_complete"] is not transport_failure
        if transport_failure:
            assert report["issues"][0]["reason_code"] == "empty_api_response"
    for path in root.rglob("*"):
        if path.is_file():
            assert SENSITIVE not in path.read_text(), f"Stale sensitive output: {path}"
    captured = capsys.readouterr()
    assert SENSITIVE not in captured.out + captured.err
