import copy
import json
from xml.etree import ElementTree

import pytest

from omniflow import cli
from omniflow.diagnostics import public_diagnostic
from omniflow.github.annotations import public_annotation_lines
from omniflow.reporting.finding_groups import group_findings
from omniflow.reporting.junit_report import to_junit
from omniflow.reporting.markdown_report import render_markdown_report
from omniflow.reporting.sarif_report import to_sarif


def email_report():
    # Synthetic fixture matching the supplied email's verified occurrence totals.
    findings = [
        {"validator": "content", "type": "content_validation_issue", "issue_type": "dashboard_filter",
         "severity": "error", "state": state, "blocking": blocking, "active": True,
         "validation_scope": "branch"}
        for state, blocking in (("new", True), ("existing", False))
    ] + [{"validator": "model", "type": "model_validation_issue", "severity": "error", "state": "existing",
          "blocking": False, "active": True, "validation_scope": "branch"}]
    for rule, count in (("require_field_descriptions", 279), ("require_measure_descriptions", 33),
                        ("require_primary_keys", 25), ("require_owner_metadata", 2)):
        findings.extend({"validator": "semantic_lint", "rule_id": rule, "severity": "warning"} for _ in range(count))
    findings.append({"validator": "contracts", "type": "topic_modified", "risk": "warning", "severity": "warning"})
    return {"policy_decision": "fail", "exit_code": 1, "validation_complete": True,
            "summary": {"errors": 3, "warnings": 340},
            "issues": [{**finding, "model_id": "private-model", "branch_id": "private-branch"} for finding in findings]}


def test_email_groups_340_warnings_without_losing_the_actual_blocker():
    original = email_report()
    before = copy.deepcopy(original)
    safe = public_diagnostic(original)
    markdown = render_markdown_report(safe)
    assert original == before
    assert len(safe["issues"]) == 343
    assert "Blocking issues (all checks): `1`" in markdown
    assert "Recorded errors: `3`; warnings: `340`" in markdown
    assert "### Blocking findings — 1 occurrence" in markdown
    assert "### Nonblocking findings — 342 occurrences" in markdown
    assert markdown.index("filter validation failed") < markdown.index("### Nonblocking findings")
    assert markdown.index("## Findings") < markdown.index("## Check Execution")
    for count in (279, 33, 25, 2):
        assert f"**{count} occurrences**" in markdown
    assert markdown.count("Field description is missing") == 1
    assert "topic_modified" in markdown and "semantic_lint" in markdown
    assert "These findings do not cause this run to fail" in markdown
    assert "not proof of a new PR regression" in markdown
    assert "OmniFlow finding; sensitive details" not in markdown
    assert len(to_sarif(safe)["runs"][0]["results"]) == 343
    junit = ElementTree.fromstring(to_junit(safe))
    assert junit.attrib["tests"] == "343" and junit.attrib["failures"] == "1"
    assert sum(count for _, count in group_findings(safe["issues"])) == 343


@pytest.mark.parametrize("change", [
    {"model_id": "second-model"}, {"validator": "model"}, {"severity": "warning"},
    {"state": "existing"}, {"blocking": False}, {"active": False}, {"validation_scope": "base"},
    {"reason_code": "missing_issue_message"}, {"type": "model_evidence_unavailable"},
    {"candidate_reason": "changed_during_validation"}, {"comparison_status": "ambiguous"},
])
def test_grouping_preserves_scope_policy_and_evidence_distinctions(change):
    issue = {"model_id": "first-model", "validator": "content", "type": "content_validation_issue",
             "issue_type": "dashboard_filter", "severity": "error", "state": "new",
             "blocking": True, "active": True, "validation_scope": "branch"}
    safe = public_diagnostic({"issues": [issue, {**issue, **change}]})
    assert len(group_findings(safe["issues"])) == 2


def test_public_annotations_group_before_limit_and_keep_policy_levels(monkeypatch, capsys):
    safe = public_diagnostic(email_report())
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    cli._emit_github_annotations(safe["issues"], limit=20)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 8
    assert lines[0].startswith("::error ") and "Dashboard-filter validation failed" in lines[0]
    assert sum(line.startswith("::error ") for line in lines) == 1
    assert sum(line.startswith("::notice ") for line in lines) == 2
    assert sum(line.startswith("::warning ") for line in lines) == 5
    assert any("require_field_descriptions" in line and "279 occurrences" in line for line in lines)
    assert "private-model" not in "".join(lines)


def test_annotation_omission_is_explicit_and_inactive_findings_do_not_use_limit():
    safe = public_diagnostic(email_report())
    inactive = {**safe["issues"][0], "active": False}
    lines = public_annotation_lines([inactive, *safe["issues"]], limit=1)
    assert len(lines) == 2
    assert "Dashboard-filter validation failed" in lines[0]
    assert "7 categories, 342 occurrences, 0 blocking" in lines[1]


def test_markdown_category_limit_reports_omitted_blocking_occurrences():
    issues = [{"validator": "content", "type": "content_evidence_unavailable", "severity": "error",
               "model_id": f"private-{i}"} for i in range(25)]
    safe = public_diagnostic({"issues": issues})
    markdown = render_markdown_report(safe)
    assert "### Blocking findings — 25 occurrences" in markdown
    assert "5 more categories, 5 occurrences" in markdown
    assert "private-" not in markdown + json.dumps(safe)
