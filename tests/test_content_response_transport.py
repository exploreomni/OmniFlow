import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

from omniflow import cli
from omniflow.config import _to_config
from omniflow.diagnostics import public_diagnostic, safe_exception_message
from omniflow.exceptions import OmniResponseError
from omniflow.omni_client import OmniClient
from omniflow.reporting.markdown_report import render_markdown_report


@pytest.mark.parametrize("body,reason", [
    (b"", "empty_api_response"),
    (b" \n", "invalid_json_response"),
    (b"<html>SYNTHETIC_PRIVATE_BODY</html>", "invalid_json_response"),
    (b'{"private": "SYNTHETIC_PRIVATE_BODY"', "invalid_json_response"),
])
def test_real_client_distinguishes_empty_body_from_unparseable_json(body, reason):
    response = requests.Response()
    response.status_code = 200
    response._content = body
    response._content_consumed = True
    session = SimpleNamespace(headers={}, request=Mock(return_value=response))
    client = OmniClient(base_url="https://synthetic.example", api_key="synthetic", session=session)
    with pytest.raises(OmniResponseError) as caught:
        client.validate_content("model")
    assert caught.value.exit_code == 4
    assert caught.value.reason_code == reason
    assert "SYNTHETIC_PRIVATE_BODY" not in safe_exception_message(caught.value)
    assert "SYNTHETIC_PRIVATE_BODY" not in str(caught.value)


@pytest.mark.parametrize("scope", ["branch", "base"])
@pytest.mark.parametrize("reason", ["empty_api_response", "invalid_json_response"])
def test_cli_reports_transport_failure_without_claiming_content_defect(tmp_path, monkeypatch, capsys, scope, reason):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    config = _to_config({}, None)
    config.content_validation.fail_on_new_only = True
    config.reporting.formats = ["json", "markdown", "sarif", "junit"]
    context = cli.ModelContext(base_url="https://synthetic.example", model_id="model", branch_id="branch",
                               model_path="model")

    def validate(*args, branch_id=None, **kwargs):
        if bool(branch_id) == (scope == "branch"):
            raise OmniResponseError(reason)
        return {"content": []}

    client = SimpleNamespace(validate_content=validate, list_content=lambda **kwargs: [])
    with patch("omniflow.cli.load_config", return_value=config), \
            patch("omniflow.cli.discover_contexts", return_value=[context]), \
            patch("omniflow.cli._client_and_branch_for_context", return_value=(client, "branch")):
        assert cli.main(["run", "--auto"]) == 4
    report = json.loads((tmp_path / ".omniflow/public/report.json").read_text())
    issue = report["issues"][0]
    assert issue["reason_code"] == reason
    assert issue["validation_scope"] == scope
    assert issue["comparison_status"] == "unavailable"
    assert issue["regression_status"] == "unknown"
    assert issue["type"] == "validation_execution_failed"
    assert report["validation_complete"] is False
    assert "This is not a content finding" in issue["message"]
    assert reason in capsys.readouterr().out or reason in json.dumps(report)


@pytest.mark.parametrize("mode,current,new,existing,comparison,expected", [
    ("new_only", 0, 0, 0, "complete", "No current content issues"),
    ("new_only", 1, 0, 1, "complete", "Existing issues remain"),
    ("new_only", 1, 1, 0, "complete", "New content issues were detected"),
    ("all_issues", 1, 0, 1, "complete", "all-issues policy blocks"),
    ("new_only", 1, 1, 0, "ambiguous", "complete baseline comparison is unavailable"),
])
def test_public_report_explains_content_decision(mode, current, new, existing, comparison, expected):
    report = public_diagnostic({"validator": "content", "comparison_mode": mode,
                               "current_issues": current, "new_issues": new,
                               "existing_issues": existing, "comparison_status": comparison})
    text = render_markdown_report(report)
    assert expected in text
    assert "same model, branch, and visibility scope" in text


def test_exception_reason_cannot_introduce_free_text_or_crash_safe_reporting():
    error = RuntimeError("SYNTHETIC_PRIVATE_BODY")
    error.reason_code = {"private": "SYNTHETIC_PRIVATE_BODY"}
    assert "SYNTHETIC_PRIVATE_BODY" not in safe_exception_message(error)
