"""Small regression matrix with occurrence, severity, and orchestration evidence."""

import copy
from types import SimpleNamespace

import pytest

from omniflow import cli
from omniflow.cli import _run_context
from omniflow.config import _to_config
from omniflow.discovery import ModelContext
from omniflow.exceptions import OmniAPIError, OmniAuthError, OmniResponseError
from omniflow.validators.model import run_model_validation


def issue(message="Existing error", path="orders.view", warning=False):
    return {"message": message, "yaml_path": path, "is_warning": warning}


def validate(current, base, **kwargs):
    calls = []

    def fetch(model_id, *, branch_id):
        calls.append((model_id, branch_id))
        value = current if branch_id else base
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    report, code = run_model_validation(
        client=SimpleNamespace(validate_model=fetch), model_id="model", branch_id="branch",
        fail_on_new_only=True, **kwargs,
    )
    assert calls == [("model", "branch"), ("model", None)]
    return report, code


@pytest.mark.parametrize("base,current,warning_policy,code,new,existing,resolved,improved", [
    ([issue()], [issue()], False, 0, 0, 1, 0, 0),
    ([issue()], [issue(), issue("New error")], False, 1, 1, 1, 0, 0),
    ([issue()], [issue("Replacement error")], False, 1, 1, 0, 1, 0),
    ([issue()], [issue(path="other.view")], False, 1, 1, 0, 1, 0),
    ([issue()], [issue(), issue()], False, 1, 1, 1, 0, 0),
    ([issue(), issue()], [issue()], False, 0, 0, 1, 1, 0),
    ([issue(warning=True)], [issue()], False, 1, 1, 0, 0, 0),
    ([issue()], [issue(warning=True)], True, 0, 0, 0, 0, 1),
    ([], [issue(warning=True)], False, 0, 1, 0, 0, 0),
    ([], [issue(warning=True)], True, 1, 1, 0, 0, 0),
    ([issue(warning=True)], [issue(warning=True)], True, 0, 0, 1, 0, 0),
    ([issue(), issue(warning=True)], [issue(warning=True), issue()], True, 0, 0, 2, 0, 0),
    ([issue()], [], False, 0, 0, 0, 1, 0),
    ([], [], False, 0, 0, 0, 0, 0),
])
def test_occurrence_and_severity_matrix(base, current, warning_policy, code, new, existing, resolved, improved):
    report, actual = validate(current, base, fail_on_warnings=warning_policy)
    assert actual == code
    assert report["comparison_status"] == "complete"
    assert [report[f"{state}_issues"] for state in ("new", "existing", "resolved", "improved")] == [
        new, existing, resolved, improved,
    ]
    assert report["blocking_issues"] == sum(i["blocking"] for i in report["issues"])
    assert report["current_issues"] == len(current)
    assert all(not i["blocking"] for i in report["issues"] if i["state"] != "new")


@pytest.mark.parametrize("bad", [issue(path=None), issue(path=""), issue(message=" ")])
@pytest.mark.parametrize("empty_current", [False, True])
def test_ambiguous_identity_never_proves_no_regressions(bad, empty_current):
    report, code = validate([] if empty_current else [bad], [bad])
    assert code == 4 and report["comparison_status"] == "ambiguous"
    assert report["validation_complete"] is False
    assert report["regression_status"] == "unknown"
    assert report["existing_issues"] == 0


@pytest.mark.parametrize("baseline", [None, {}, [{"message": 12}], OmniResponseError("empty_api_response"),
                                     OmniResponseError("invalid_json_response"), OmniAPIError("private error")])
def test_unavailable_baseline_is_not_clean_even_with_empty_current(baseline):
    report, code = validate([], baseline)
    assert code == 4 and report["comparison_status"] == "unavailable"
    assert report["baseline_issues"] is None
    assert report["regression_status"] == "unknown"
    assert report["blocking_issues"] == 1


def test_auth_failure_and_bad_candidate_still_fail():
    with pytest.raises(OmniAuthError):
        validate([], OmniAuthError("private authorization error"))
    with pytest.raises(OmniAPIError):
        validate({}, [])


def test_metadata_order_and_large_backlog_do_not_change_matching():
    previous = [issue(f"Existing {index}") for index in range(12_000)]
    current = [dict(row, auto_fix={"description_short": "Updated suggestion"}) for row in reversed(previous)]
    report, code = validate(current, previous)
    assert code == 0 and report["existing_issues"] == 12_000
    report, code = validate([*current, issue("Actual regression")], previous)
    assert code == 1 and report["new_issues"] == 1


@pytest.mark.parametrize("branch,new_only", [(None, True), ("branch", False)])
def test_health_mode_remains_strict(branch, new_only):
    calls = []
    client = SimpleNamespace(validate_model=lambda *a, **kw: calls.append(kw) or [issue()])
    report, code = run_model_validation(client=client, model_id="model", branch_id=branch,
                                        fail_on_new_only=new_only)
    assert code == 1 and len(calls) == 1
    assert report["comparison_mode"] == "all_issues"
    assert report["comparison_status"] == "not_applicable"


@pytest.mark.parametrize("strict,branch,expected", [(False, "branch", 0), (True, "branch", 1), (False, None, 1)])
def test_orchestrator_keeps_health_separate_from_regression(tmp_path, monkeypatch, strict, branch, expected):
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    config = _to_config({"contracts": {"enabled": False}, "checks": {
        "semantic_lint": {"enabled": False},
        "model_validation": {"fail_on_new_only": True},
        "content_validation": {"fail_on_new_only": True},
    }}, None)
    content = {"content": [{"document_id": "doc", "identifier": "doc",
                            "dashboard_filter_issues": [{"filter_name": "filter", "issues": ["Existing"]}]}]}
    client = SimpleNamespace(validate_model=lambda *a, **kw: [issue()],
                             validate_content=lambda *a, **kw: content,
                             list_content=lambda **kw: [])
    context = ModelContext(base_url="https://omni.example", model_id="model", model_path="omni", branch_id=branch)
    monkeypatch.setattr("omniflow.cli._client_and_branch_for_context", lambda *a, **kw: (client, branch))
    report, code = _run_context(config=config, context=context, output_dir=tmp_path, strict_health=strict)
    assert code == expected
    assert report["validation_complete"] is True
    assert all(c["blocking_issues"] == expected for c in report["check_reports"])


@pytest.mark.parametrize("validator", ["model", "content"])
def test_standalone_cannot_bypass_pr_snapshot_verification(tmp_path, monkeypatch, validator):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    config = _to_config({"checks": {f"{validator}_validation": {"fail_on_new_only": True}}}, None)
    monkeypatch.setattr(cli, "load_config", lambda *a: config)
    def forbidden(*a, **kw):
        pytest.fail("Standalone PR comparison must not reach the API")
    monkeypatch.setattr(cli, "_client_and_branch", forbidden)
    assert cli.main([validator, "validate"]) == 2


@pytest.mark.parametrize("explicit_history,expected", [(False, 0), (True, 4)])
def test_standalone_content_defaults_health_unless_history_requested(tmp_path, monkeypatch, explicit_history, expected):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    config = _to_config({"checks": {"content_validation": {"fail_on_new_only": True}}}, None)
    config.omni.model_id = "model"
    monkeypatch.setattr(cli, "load_config", lambda *a: config)
    client = SimpleNamespace(validate_content=lambda *a, **kw: {"content": []}, list_content=lambda **kw: [])
    monkeypatch.setattr(cli, "_client_and_branch", lambda *a: (client, None))
    args = ["content", "validate", *( ["--fail-on-new-only"] if explicit_history else [])]
    assert cli.main(args) == expected
