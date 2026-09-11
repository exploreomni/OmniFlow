import pytest

from omniflow.reporting.markdown_report import render_markdown_report
from omniflow.security import public_safe


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("unavailable", [False, True])
def test_pr214_content_category_identity_and_private_guidance(strict, unavailable):
    issue = {
        "validator": "content",
        "type": "content_evidence_unavailable" if unavailable else "content_validation_issue",
        "issue_type": "dashboard_filter",
        "document_id": "document-214",
        "document_name": "PRIVATE-DOCUMENT",
        "query_name": "PRIVATE-QUERY",
        "validation_scope": "branch",
        "severity": "error",
        "message": {"PRIVATE-OBJECT": "Do not render this"} if unavailable else "Filter validation failed",
        "document_url": "https://private.omniapp.co/document/214",
        "raw_payload": {"secret": "PRIVATE-PAYLOAD"},
    }
    report = public_safe(
        {"policy_decision": "fail", "issues": [issue]},
        redaction_level="strict" if strict else "standard",
    )
    markdown = render_markdown_report(report)
    assert "Content validation / Dashboard filter" in markdown
    assert "document ID `document-214`" in markdown
    assert "the validated Omni branch" in markdown
    if unavailable:
        assert "coverage unavailable" in markdown
        assert "do not guess which filter or field to change" in markdown
    else:
        assert "Confirm the exact failing filter in Omni before editing" in markdown
    for value in ("PRIVATE-OBJECT", "PRIVATE-PAYLOAD", "https://private", "``"):
        assert value not in markdown
    if strict:
        assert "PRIVATE-DOCUMENT" not in markdown
        assert "PRIVATE-QUERY" not in markdown


def test_pr199_unavailable_coverage_does_not_prove_zero_reference_changes_safe():
    gap = {
        "validator": "contracts", "type": "dependency_coverage_gap", "impact_level": "coverage_gap",
        "name": "orders", "message": "Dependency lookup unavailable", "severity": "error",
        "referenced_content": [],
    }
    markdown = render_markdown_report({
        "policy_decision": "fail",
        "issues": [gap, {
            "validator": "contracts", "type": "field_deleted", "impact_level": "unreferenced",
            "field": "orders.total", "message": "Field removed", "severity": "warning",
            "referenced_content": [],
        }],
        "coverage_gaps": [{"name": "orders", "message": "Dependency lookup unavailable"}],
    })
    prominent, collapsed = markdown.split("<details>", maxsplit=1)
    assert "Coverage unavailable" in prominent
    assert "Coverage gaps: `1`" in prominent
    assert "absence of downstream dependencies is not proven" in collapsed
    assert "unreferenced" not in markdown
    assert "coverage_gap" not in markdown


def test_advisories_are_collapsed_and_content_history_is_not_all_check_blockers():
    markdown = render_markdown_report({
        "policy_decision": "fail",
        "summary": {"new_issues": 99, "existing_issues": 99, "resolved_issues": 99},
        "issues": [
            {"validator": "model", "severity": "error", "file": "orders.view", "message": "MODEL-BLOCKER"},
            {"validator": "content", "severity": "error", "state": "new", "message": "CONTENT-BLOCKER"},
            {"validator": "semantic_lint", "severity": "warning", "message": "ADVISORY-ONLY"},
            {"validator": "content", "severity": "error", "state": "existing", "active": False,
             "message": "HISTORICAL-ONLY"},
        ],
    })
    prominent, collapsed = markdown.split("<details>", maxsplit=1)
    assert "Blocking issues (all checks): `2`" in prominent
    assert "Advisory warnings (not blocking): `1`" in prominent
    assert "Content issue history only: new `1`, existing `1`, resolved `0`" in prominent
    assert "MODEL\\-BLOCKER" in prominent and "CONTENT\\-BLOCKER" in prominent
    assert "ADVISORY\\-ONLY" not in prominent and "HISTORICAL\\-ONLY" not in prominent
    assert "ADVISORY\\-ONLY" in collapsed and "HISTORICAL\\-ONLY" in collapsed
    assert "<summary>Advisory and historical detail (not blocking)</summary>" in collapsed


@pytest.mark.parametrize("revision,known", [
    (None, False),
    ({"commit": None, "source": "unavailable"}, False),
    ({"commit": "short-sha", "source": "github_action_ref"}, False),
    ({"commit": "a" * 40, "source": "unavailable"}, False),
    ({"commit": "a" * 40, "source": "github_action_ref"}, True),
])
def test_action_provenance_is_separate_from_input_revision(revision, known):
    markdown = render_markdown_report({"git_sha": "b" * 40, "tool_revision": revision})
    assert f"Input/PR Git SHA: `{'b' * 40}`" in markdown
    if known:
        assert f"OmniFlow Action revision: `{'a' * 40}` (source: `github_action_ref`)" in markdown
    else:
        assert "OmniFlow Action revision: unavailable (not inferred from the input/PR Git SHA)" in markdown


@pytest.mark.parametrize("message", [None, "", {}, {"private": "payload"}, '{"private": "payload"}', "null"])
def test_legacy_unreadable_messages_have_safe_nonempty_fallbacks(message):
    markdown = render_markdown_report({
        "policy_decision": "fail",
        "issues": [{"severity": "error", "message": message}],
        "models": [{"model_id": None, "model_path": "", "branch_id": None}],
    })
    assert "Details are unavailable" in markdown
    assert "Object identity unavailable" in markdown
    assert "Inspect this check's structured evidence before rerunning" in markdown
    assert "private" not in markdown
    assert "payload" not in markdown
    assert "``" not in markdown


@pytest.mark.parametrize("flag,active", [(True, True), (False, True), (None, True), (True, False)])
def test_warning_policy_blockers_and_legacy_uncertainty_remain_prominent(flag, active):
    issue = {"validator": "model", "severity": "warning", "message": "MODEL-WARNING", "active": active}
    if flag is not None:
        issue["blocking"] = flag
    markdown = render_markdown_report({"policy_decision": "fail", "exit_code": 1, "issues": [issue]})
    prominent, collapsed = markdown.split("<details>", maxsplit=1)
    if active and flag is True:
        assert "Blocking issues (all checks): `1`" in prominent
        assert "policy\\-blocking warning" in prominent
        assert "MODEL\\-WARNING" not in collapsed
    elif active and flag is None:
        assert "Warnings requiring policy review" in prominent
        assert "Do not assume they are advisory" in prominent
        assert "MODEL\\-WARNING" in prominent
        assert "MODEL\\-WARNING" not in collapsed
        assert "_No blocking issues._" not in prominent
    else:
        assert "Blocking issues (all checks): `0`" in prominent
        assert "MODEL\\-WARNING" in collapsed
    advisory_count = 1 if active and flag is False else 0
    assert f"Advisory warnings (not blocking): `{advisory_count}`" in prominent


def test_failed_run_without_ai_result_does_not_claim_eval_is_disabled():
    markdown = render_markdown_report({"policy_decision": "fail", "exit_code": 4, "issues": []})
    assert "No AI eval result was produced" in markdown
    assert "AI eval is disabled" not in markdown


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("dependency_gap", [False, True])
def test_aborted_content_context_does_not_imply_completed_zero_results(partial, dependency_gap):
    issues = [{
        "validator": "content", "type": "content_evidence_unavailable", "issue_type": "dashboard_filter",
        "severity": "error", "document_id": "doc-aborted", "validation_scope": "branch",
    }]
    if partial:
        issues.append({"validator": "content", "severity": "error", "state": "new", "document_id": "doc-other"})
    if dependency_gap:
        issues.append({
            "validator": "contracts", "type": "dependency_coverage_gap", "impact_level": "coverage_gap",
            "severity": "error", "name": "other-view",
        })
    markdown = render_markdown_report({
        "policy_decision": "fail", "exit_code": 4, "summary": {"risk_level": "info"}, "issues": issues,
    })
    assert "Downstream impacts: `0` recorded; analysis incomplete" in markdown
    assert "No downstream result is available for the context that stopped" in markdown
    assert "Semantic risk: unestablished" in markdown
    assert "Risk level: `info`" not in markdown
    assert "No blocking downstream contract impacts" not in markdown
    if partial:
        assert "Content comparison partial: recorded new `1`" in markdown
    else:
        assert "Content comparison unavailable" in markdown
        assert "new `0`, existing `0`, resolved `0`" not in markdown
    assert markdown.count("do not guess which filter or field to change") == 1
    assert "See the expanded Content validation finding in Blocking Issues" in markdown
    actions = markdown.split("## Reviewer Actions", maxsplit=1)[1].split("## Audit Metadata", maxsplit=1)[0]
    assert "Restore readable Content Validator evidence" in actions
    assert ("dependency coverage gaps" in actions) is dependency_gap
