import pytest

from omniflow.github.annotations import annotation_lines
from omniflow.reporting.markdown_report import render_markdown_report
from omniflow.reporting.sarif_report import to_sarif
from omniflow.security import public_safe


@pytest.mark.parametrize("message", [
    "[" * 1100 + "PRIVATE-PAYLOAD" + "]" * 1100,
    "{'private': 'PRIVATE-PAYLOAD'}",
    '{"private": "PRIVATE-PAYLOAD"',
    '[ "PRIVATE-PAYLOAD" ]',
    "[{'private': 'PRIVATE-PAYLOAD'}]",
])
def test_object_like_diagnostics_are_bounded_without_parsing_or_disclosure(message):
    report = public_safe({"policy_decision": "fail", "issues": [{"severity": "error", "message": message}]})
    markdown = render_markdown_report(report)
    assert "Details are unavailable" in markdown
    assert "PRIVATE" not in markdown


@pytest.mark.parametrize("message", ["[warning] Field is missing", "[click](https://example.invalid) @here <script>bad</script>"])
def test_human_bracket_messages_remain_visible_and_escaped(message):
    markdown = render_markdown_report({"issues": [{"severity": "error", "message": message}]})
    assert "\\[warning\\]" in markdown or "\\[click\\]" in markdown
    assert "Details are unavailable" not in markdown
    assert "@here" not in markdown
    assert "<script>" not in markdown


@pytest.mark.parametrize("explicit", [False, True])
def test_generic_context_failure_does_not_claim_missing_checks_completed(explicit):
    model = {
        "model_id": "model-aborted", "branch_id": "head-aborted", "check_reports": [],
        "issues": [{"validator": "context", "severity": "error", "message": "API request failed"}],
    }
    if explicit:
        model.update(validation_complete=False, check_states=[
            {"validator": "context", "status": "failed", "exit_code": 4},
            {"validator": "content", "status": "not_run"},
            {"validator": "contracts", "status": "not_run"},
            {"validator": "ai_eval", "status": "disabled"},
        ])
    markdown = render_markdown_report({
        "policy_decision": "fail", "exit_code": 4, "summary": {"risk_level": "info"},
        "issues": model["issues"], "model_reports": [model],
    })
    assert "Content comparison unavailable" in markdown
    assert "Downstream impacts: `0` recorded; analysis incomplete" in markdown
    assert "check coverage is incomplete or unknown" in markdown
    assert "Semantic risk: unestablished" in markdown
    assert "Risk level: `info`" not in markdown
    assert "_No dependency coverage gaps._" not in markdown
    assert "new `0`, existing `0`, resolved `0`" not in markdown
    assert "missing results are not successful checks" in markdown
    if explicit:
        assert "`context`: failed operationally" in markdown
        assert "`content`: not run" in markdown
        assert "`ai_eval`: disabled" in markdown
    else:
        assert "execution status was not recorded" in markdown


def test_partial_execution_preserves_completed_content_and_scopes_other_models():
    issue = {"validator": "content", "severity": "error", "state": "new", "document_id": "shared-doc"}
    completed = {
        "model_id": "model-completed", "branch_id": "head-completed", "validation_complete": True,
        "check_states": [{"validator": "content", "status": "completed", "exit_code": 1}],
        "check_reports": [{"validator": "content", "issues": [issue]}], "issues": [issue],
    }
    aborted = {
        "model_id": "model-aborted", "branch_id": None, "branch_name": "review-branch", "validation_complete": False,
        "check_states": [{"validator": "content", "status": "failed"}, {"validator": "contracts", "status": "not_run"}],
        "check_reports": [], "issues": [],
    }
    report = {"policy_decision": "fail", "validation_complete": False, "issues": [issue], "model_reports": [completed, aborted]}
    markdown = render_markdown_report(report)
    assert "Content comparison partial: recorded new `1`" in markdown
    assert "Model `model-completed`; branch `head-completed` · `content`: completed" in markdown
    assert "Model `model-aborted`; branch `review-branch` · `content`: failed operationally" in markdown
    assert "recorded findings may still block" in markdown
    # A later operational failure must not discard an earlier completed content comparison.
    completed["validation_complete"] = False
    completed["check_states"].append({"validator": "contracts", "status": "failed"})
    report["model_reports"] = [completed]
    assert "Content issue history only: new `1`" in render_markdown_report(report)


@pytest.mark.parametrize("partial", [False, True])
def test_post_sync_execution_uses_nested_evidence_and_preserves_wrapper_failures(partial):
    issue = {"validator": "model", "severity": "warning", "blocking": True, "message": "Policy warning"}
    validation = {
        "validation_complete": not partial, "exit_code": 4 if partial else 1, "issues": [issue],
        "check_states": [
            {"validator": "content", "status": "completed", "exit_code": 0},
            {"validator": "model", "status": "failed" if partial else "completed", "exit_code": 4 if partial else 1},
            {"validator": "contracts", "status": "not_run" if partial else "completed"},
        ],
    }
    wrapper = {
        "model_id": "sync-model", "branch_id": None, "refresh": {"status": "completed"},
        "post_sync_validation": validation, "post_sync_validation_status": "failed", "issues": [issue],
    }
    report = {"operation": "dbt_sync", "policy_decision": "fail", "issues": [issue], "model_reports": [wrapper]}
    markdown = render_markdown_report(report)
    assert "Model `sync-model`; branch `main` · `content`: completed" in markdown
    assert "execution status was not recorded" not in markdown
    assert "Content comparison unavailable" not in markdown
    assert "Content issue history only: new `0`" in markdown
    assert ("Semantic risk: unestablished" in markdown) is partial
    assert ("analysis incomplete" in markdown) is partial
    if not partial:
        assert "policy\\-blocking warning" in markdown
        # A successful nested comparison must not erase an independent outer failure.
        wrapper["validation_complete"] = False
        wrapper["check_states"] = [{"validator": "dbt_sync", "status": "failed", "exit_code": 4}]
        wrapper["refresh"] = {"status": "failed"}
        failed_wrapper = render_markdown_report(report)
        assert "outer refresh or post-sync wrapper evidence is incomplete" in failed_wrapper
        assert "`dbt_sync`: failed operationally" in failed_wrapper
        assert "Semantic risk: unestablished" in failed_wrapper


def test_coverage_dedup_preserves_model_branch_and_inherits_nested_scope():
    gap = {
        "validator": "content", "type": "content_evidence_unavailable", "issue_type": "dashboard_filter",
        "document_id": "shared-doc", "document_name": "PRIVATE-DOCUMENT", "message": "PRIVATE-DIAGNOSTIC",
        "validation_scope": "branch", "severity": "error",
    }
    contexts = [
        {"model_id": "model-a", "branch_id": "branch-one"},
        {"model_id": "model-a", "branch_id": "branch-two"},
        {"model_id": "model-b", "branch_id": "branch-one"},
    ]
    report = {
        "policy_decision": "fail", "issues": [{**gap, **context} for context in contexts],
        "model_reports": [
            {**context, "check_reports": [{"validator": "content", "coverage_gaps": [gap]}]} for context in contexts
        ],
    }
    markdown = render_markdown_report(public_safe(report, redaction_level="strict"))
    assert "Coverage gaps: `3`" in markdown
    for context in contexts:
        assert f"document ID `shared-doc`; Model `{context['model_id']}`; branch `{context['branch_id']}`" in markdown
    assert "PRIVATE" not in markdown


@pytest.mark.parametrize("blocking,active", [(True, True), (False, True), (True, False)])
def test_warning_outputs_preserve_severity_and_explain_policy_blocking(blocking, active):
    issue = {"severity": "warning", "blocking": blocking, "active": active, "message": "Warning detail"}
    result = to_sarif({"issues": [issue]})["runs"][0]["results"][0]
    assert result["level"] == "warning"
    assert result["properties"]["policy_blocking"] is (blocking and active)
    annotations = annotation_lines([issue])
    if not active:
        assert annotations == []
    else:
        assert annotations[0].startswith("::warning ")
        assert ("Blocks under configured policy: " in annotations[0]) is blocking
