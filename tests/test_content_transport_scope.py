import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from omniflow.cli import build_parser, cmd_run
from omniflow.config import _to_config
from omniflow.diagnostics import public_diagnostic
from omniflow.discovery import ModelContext
from omniflow.exceptions import OmniAPIError, OmniAuthError, OmniFlowError, OmniResponseError, exception_diagnostic
from omniflow.validation_scope import ValidationEvidence
from omniflow.validators.content import ContentEvidenceError, _fetch_validator_payload, _prepare_validator_payload

PRIVATE = "SYNTHETIC_PRIVATE_CONTENT_RESPONSE"


def failure(kind):
    context = {
        "service": "omni", "api_operation": "content_validation",
        "attempt_count": 1, "max_attempts": 4, "retryable": False,
    }
    if kind == "forbidden":
        return OmniAuthError(PRIVATE, reason_code="omni_forbidden", diagnostic_context=context | {"http_status": 403})
    if kind == "rate_limit":
        return OmniAPIError(PRIVATE, reason_code="omni_rate_limited",
                            diagnostic_context=context | {"http_status": 429, "attempt_count": 4, "retryable": True})
    if kind == "timeout":
        return OmniAPIError(PRIVATE, reason_code="omni_timeout", diagnostic_context=context)
    error = OmniResponseError("empty_api_response")
    error.diagnostic_context.update(context)
    return error


@pytest.mark.parametrize("kind", ["forbidden", "rate_limit", "timeout", "empty_body"])
@pytest.mark.parametrize("branch_id,scope,full", [(None, "base", True), ("branch", "branch", False)])
def test_transport_failures_retain_scope_cause_exit_and_single_request(kind, branch_id, scope, full):
    error = failure(kind)
    before = exception_diagnostic(error)
    client = SimpleNamespace(validate_content=Mock(side_effect=error))
    with pytest.raises(type(error)) as caught:
        _fetch_validator_payload(
            client, "model", branch_id=branch_id, user_id=None, include_personal_folders=False,
            comparison_mode="new_only", force_full_validation=full, evidence=ValidationEvidence(),
        )
    assert caught.value is error
    assert error.exit_code == (3 if kind == "forbidden" else 4)
    evidence = exception_diagnostic(error)
    assert evidence == before | {
        "validation_scope": scope, "comparison_mode": "new_only",
        "requested_validation_depth": "full" if full else "api_default",
    }
    client.validate_content.assert_called_once_with(
        "model", branch_id=branch_id, user_id=None, include_personal_folders=False,
        **({"force_full_validation": True} if full else {}),
    )
    safe = public_diagnostic({"issues": [{
        "validator": "content", "type": "validation_execution_failed", "severity": "error", **evidence,
    }]})
    assert safe["issues"][0]["validation_scope"] == scope
    assert PRIVATE not in json.dumps(safe)


@pytest.mark.parametrize("scope", ["base", "branch"])
@pytest.mark.parametrize("payload,error_class,reason", [
    ({"content": {}}, OmniAPIError, "invalid_content_response_envelope"),
    ({"content": ["unsupported"]}, OmniAPIError, "invalid_content_document"),
    ({"content": [{"dashboard_filter_issues": [None]}]}, ContentEvidenceError, "invalid_issue_entry"),
])
def test_malformed_evidence_retains_scope_without_changing_specialized_exception(scope, payload, error_class, reason):
    with pytest.raises(error_class) as caught:
        _prepare_validator_payload(
            payload, [], {}, validation_scope=scope, comparison_mode="new_only", requested_validation_depth="full",
        )
    error = caught.value
    assert type(error) is error_class and error.exit_code == 4
    evidence = exception_diagnostic(error)
    assert evidence["reason_code"] == reason
    assert evidence["validation_scope"] == scope
    assert evidence["comparison_mode"] == "new_only" and evidence["requested_validation_depth"] == "full"
    if isinstance(error, ContentEvidenceError):
        assert error.report_issue()["validation_scope"] == scope
        assert error.report_issue()["type"] == "content_evidence_unavailable"


def test_unexpected_programming_exception_is_not_reclassified_as_api_evidence():
    error = RuntimeError(PRIVATE)
    client = SimpleNamespace(validate_content=Mock(side_effect=error))
    with pytest.raises(RuntimeError) as caught:
        _fetch_validator_payload(client, "model", branch_id=None, user_id=None, include_personal_folders=False,
                                 comparison_mode="all_issues", evidence=ValidationEvidence())
    assert caught.value is error and not hasattr(error, "diagnostic_context")
    client.validate_content.assert_called_once()


@pytest.mark.parametrize("scope,kind", [("base", "forbidden"), ("branch", "timeout"), ("base", "malformed")])
def test_cli_public_reports_keep_actual_failed_content_scope(tmp_path, monkeypatch, scope, kind):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    config = _to_config({}, None)
    config.content_validation.fail_on_new_only = True
    config.content_validation.force_full_validation = True
    config.reporting.formats = ["json", "markdown", "sarif", "junit"]
    context = ModelContext(base_url="https://omni.example", model_id="model", model_path="model", branch_id="branch")
    error = failure(kind) if kind != "malformed" else None

    def validate(*args, branch_id=None, **kwargs):
        if bool(branch_id) == (scope == "branch"):
            if error is not None:
                raise error
            return {"content": {}}
        return {"content": []}

    client = SimpleNamespace(validate_content=Mock(side_effect=validate))
    args = build_parser().parse_args(["run", "--auto"])
    with patch("omniflow.cli.load_config", return_value=config), \
            patch("omniflow.cli.discover_contexts", return_value=[context]), \
            patch("omniflow.cli._client_and_branch_for_context", return_value=(client, "branch")), \
            patch("omniflow.cli.run_model_validation") as model_validation:
        code = cmd_run(args, changed_files=[])
    assert code == (3 if kind == "forbidden" else 4)
    model_validation.assert_not_called()
    assert [call.kwargs["branch_id"] for call in client.validate_content.call_args_list] == (
        [None] if scope == "base" else [None, "branch"]
    )
    public = tmp_path / ".omniflow/public"
    report = json.loads((public / "report.json").read_text())
    issue = report["issues"][0]
    assert report["policy_decision"] == "fail" and report["validation_complete"] is False
    assert issue["validation_scope"] == scope and issue["comparison_mode"] == "new_only"
    assert issue["requested_validation_depth"] == "full"
    assert issue["reason_code"] == (
        "invalid_content_response_envelope" if error is None else exception_diagnostic(error)["reason_code"]
    )
    assert issue["type"] == "validation_execution_failed"
    assert "validation scope" in (public / "report.md").read_text()
    for path in public.iterdir():
        assert PRIVATE not in path.read_text()
    assert not isinstance(error, OmniFlowError) or error.exit_code == code
