import json
import re
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from omniflow import cli
from omniflow.artifacts import write_public_reports
from omniflow.diagnostic_catalog import CHANGE_MESSAGES, CHECK_GUIDANCE, LINT_ACTIONS, LINT_MESSAGES
from omniflow.diagnostics import public_diagnostic
from omniflow.github.annotations import annotation_lines, public_annotation_lines
from omniflow.reporting.junit_report import to_junit
from omniflow.reporting.markdown_report import render_markdown_report
from omniflow.reporting.sarif_report import to_sarif
from omniflow.validators.yaml_lint import DEFAULT_RULES

# Fabricated markers only: no real person, health record, card, or credential.
MARKERS = (
    "SYNTHETIC_CATALOG_PERSON", "catalog-person@example.invalid", "MRN-CATALOG-123",
    "DIAGNOSIS-CATALOG-TEST", "4242424242424242", "CVV-CATALOG-123",
    "SELECT PRIVATE_CATALOG_COLUMN FROM PRIVATE_CATALOG_TABLE",
    "SYNTHETIC_MODEL_ID", "SYNTHETIC_DASHBOARD_ID", "SYNTHETIC_CUSTOMER_PATH",
)
PRIVATE = " | ".join(MARKERS)
ACTUAL_CHANGE_TYPES = {
    f"{kind}_{change}"
    for kind in ("field", "view", "topic", "relationship")
    for change in ("added", "deleted", "modified")
} | {
    "field_renamed", "file_renamed", "field_type_changed", "field_kind_changed",
    "measure_aggregation_changed", "relationship_cardinality_changed", "governance_changed",
    "topic_organization_changed",
}
FORMATS = ["json", "markdown", "sarif", "junit"]


def assert_no_private_markers(text):
    for marker in MARKERS:
        assert marker not in text


def sensitive_fields():
    return {
        "message": PRIVATE, "summary": PRIVATE, "file": PRIVATE, "yaml_path": PRIVATE,
        "field": PRIVATE, "name": PRIVATE, "sql": PRIVATE, "model_id": PRIVATE,
        "document_id": PRIVATE, "filter_name": PRIVATE, "query_name": PRIVATE,
        "reason": PRIVATE, "reason_code": PRIVATE, "raw": {"nested": [PRIVATE]},
        "referenced_content": [{"content_id": PRIVATE, "content_name": PRIVATE}],
    }


def test_catalog_covers_configurable_lint_and_actual_semantic_change_vocabulary():
    assert len(DEFAULT_RULES) == 11
    assert set(DEFAULT_RULES) == set(LINT_MESSAGES)
    assert set(DEFAULT_RULES) == set(LINT_ACTIONS)
    assert ACTUAL_CHANGE_TYPES <= set(CHANGE_MESSAGES)


@pytest.mark.parametrize("rule_id", sorted(DEFAULT_RULES))
def test_each_lint_rule_survives_with_fixed_actionable_guidance(rule_id):
    safe = public_diagnostic({"issues": [{
        **sensitive_fields(), "validator": "semantic_lint", "rule_id": rule_id,
        "severity": "warning", "blocking": False, "active": True,
    }]})
    issue = safe["issues"][0]
    assert issue["rule_id"] == rule_id
    assert issue["message"] == LINT_MESSAGES[rule_id]
    assert issue["next_action"] == LINT_ACTIONS[rule_id]
    assert "no recognized lint rule code" not in json.dumps(issue)
    assert issue["severity"] == "warning" and issue["blocking"] is False
    assert "sensitive details are withheld" not in issue["message"]
    assert_no_private_markers(json.dumps(safe))


@pytest.mark.parametrize("rule_id", sorted(DEFAULT_RULES))
def test_lint_action_survives_all_report_formats_without_changing_gate(rule_id):
    safe = public_diagnostic({"issues": [{
        **sensitive_fields(), "validator": "semantic_lint", "rule_id": rule_id,
        "severity": "error", "blocking": True, "active": True,
    }]})
    expected = LINT_ACTIONS[rule_id]
    assert safe["issues"][0]["blocking"] is True
    sarif = to_sarif(safe)["runs"][0]["results"][0]
    assert sarif["properties"]["next_action"] == expected
    junit = ElementTree.fromstring(to_junit(safe))
    assert expected in junit.find("testcase/failure").text
    assert expected in "\n".join(public_annotation_lines(safe["issues"], limit=20))
    # Markdown escapes punctuation; inspect its structure as well as safe JSON.
    markdown = render_markdown_report(safe)
    assert "next action:" in markdown and "no recognized lint rule code" not in markdown
    assert_no_private_markers(markdown + json.dumps(sarif) + to_junit(safe))


def test_unknown_lint_rule_keeps_honest_fallback_without_echoing_input():
    issue = public_diagnostic({"issues": [{
        **sensitive_fields(), "validator": "semantic_lint", "rule_id": PRIVATE,
        "severity": "warning", "blocking": False,
    }]})["issues"][0]
    assert "rule_id" not in issue
    assert issue["next_action"] == CHECK_GUIDANCE["semantic_lint"]
    assert_no_private_markers(json.dumps(issue))


@pytest.mark.parametrize("change_type", sorted(ACTUAL_CHANGE_TYPES))
def test_actual_change_types_survive_with_fixed_remediation(change_type):
    safe = public_diagnostic({"issues": [{
        **sensitive_fields(), "validator": "contracts", "type": change_type,
        "severity": "error", "blocking": False, "active": True,
    }]})
    issue = safe["issues"][0]
    assert issue["type"] == change_type
    assert issue["message"] == CHANGE_MESSAGES[change_type]
    assert issue["severity"] == "error" and issue["blocking"] is False
    assert_no_private_markers(json.dumps(safe))


@pytest.mark.parametrize("validator", ["contracts", "model", "content", "ai_eval"])
def test_lint_rule_catalog_cannot_be_selected_by_another_validator(validator):
    issue = public_diagnostic({"issues": [{
        "validator": validator, "rule_id": "require_primary_keys", "severity": "warning",
    }]})["issues"][0]
    assert "rule_id" not in issue
    assert issue["message"] != LINT_MESSAGES["require_primary_keys"]


@pytest.mark.parametrize("level", ["standard", "strict"])
def test_catalog_privacy_and_explicit_policy_survive_every_public_format(tmp_path, level, capsys):
    report = {
        "policy_decision": "fail", "exit_code": 1, "validation_complete": True,
        "summary": {"total_issues": 4, "errors": 2, "warnings": 1},
        "issues": [
            {**sensitive_fields(), "validator": "semantic_lint", "rule_id": "require_primary_keys",
             "severity": "warning", "blocking": True, "active": True},
            {**sensitive_fields(), "validator": "contracts", "type": "field_kind_changed",
             "rule_id": "require_field_descriptions", "severity": "error", "blocking": False, "active": True},
            {**sensitive_fields(), "validator": "semantic_lint", "rule_id": "require_primary_keys-" + PRIVATE,
             "type": PRIVATE, "severity": "error", "blocking": True, "active": False},
            {**sensitive_fields(), "validator": "ai_eval", "severity": "error", "blocking": False, "active": True},
        ],
    }
    safe = write_public_reports(report, output_dir=tmp_path, formats=FORMATS, redaction_level=level)
    assert safe["issues"][0]["rule_id"] == "require_primary_keys"
    assert "rule_id" not in safe["issues"][1]
    assert "rule_id" not in safe["issues"][2]
    assert "reason" not in safe["issues"][3] and "reason_code" not in safe["issues"][3]
    assert CHECK_GUIDANCE["ai_eval"] in safe["issues"][3]["message"]
    assert [(issue["severity"], issue["blocking"], issue["active"]) for issue in safe["issues"]] == [
        ("warning", True, True), ("error", False, True), ("error", True, False), ("error", False, True),
    ]
    for directory in (tmp_path, tmp_path / "public"):
        assert {"report.json", "report.md", "report.sarif", "junit.xml"} <= {p.name for p in directory.iterdir()}
        for name in ("report.json", "report.md", "report.sarif", "junit.xml"):
            assert_no_private_markers((directory / name).read_text())
        suite = ElementTree.parse(directory / "junit.xml").getroot()
        assert suite.get("tests") == "4" and suite.get("failures") == "1"
        sarif = json.loads((directory / "report.sarif").read_text())["runs"][0]["results"]
        assert [row["properties"]["policy_blocking"] for row in sarif] == [True, False, False, False]
    annotations = annotation_lines(safe["issues"])
    assert len(annotations) == 3
    assert "Blocks under configured policy" in annotations[0]
    assert all("Does not block under configured policy" in line for line in annotations[1:])
    output = capsys.readouterr()
    assert_no_private_markers("\n".join(annotations) + output.out + output.err)


def test_saved_public_report_reanonymizes_contexts_without_merging(tmp_path):
    issues = [{
        "model_id": model, "branch_id": "private-branch-" + model,
        "validator": "semantic_lint", "rule_id": "require_field_descriptions",
        "severity": "error", "blocking": blocking, "active": True, "message": PRIVATE,
    } for model, blocking in ((PRIVATE + "-A", True), (PRIVATE + "-B", False), (PRIVATE + "-A", True))]
    report = {
        "policy_decision": "fail", "exit_code": 1, "validation_complete": True,
        "issues": issues,
        "model_reports": [{
            "model_id": issue["model_id"], "branch_id": issue["branch_id"],
            "issues": [issue], "check_states": [{"validator": "semantic_lint", "status": "completed"}],
        } for issue in issues[:2]],
    }
    first = tmp_path / "first"
    write_public_reports(report, output_dir=first, formats=FORMATS, redaction_level="standard")
    saved = json.loads((first / "public/report.json").read_text())
    references = [issue["context_reference"] for issue in saved["issues"]]
    assert references[0] == references[2] and references[0] != references[1]
    aliases = {references[0]: PRIVATE + "-context-A", references[1]: PRIVATE + "-context-B"}

    def hostile_labels(value):
        if isinstance(value, dict):
            return {key: aliases[item] if key == "context_reference" else hostile_labels(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [hostile_labels(item) for item in value]
        return value

    # A schema-version marker never makes incoming public labels trusted text.
    source = tmp_path / "saved-public-input.json"
    source.write_text(json.dumps(hostile_labels(saved)))
    target = tmp_path / "rerendered"
    assert cli.cmd_report(SimpleNamespace(input=source, output_dir=target, format=FORMATS)) == 0
    rerendered = json.loads((target / "public/report.json").read_text())
    actual = [issue["context_reference"] for issue in rerendered["issues"]]
    assert actual[0] == actual[2] and actual[0] != actual[1]
    assert all(re.fullmatch(r"context-[1-9][0-9]*", label) for label in actual)
    assert set(actual) == {"context-1", "context-2"}
    assert [context["context_reference"] for context in rerendered["model_reports"]] == actual[:2]
    assert [context["issues"][0]["context_reference"] for context in rerendered["model_reports"]] == actual[:2]
    assert [issue["blocking"] for issue in rerendered["issues"]] == [True, False, True]
    assert all(issue["rule_id"] == "require_field_descriptions" for issue in rerendered["issues"])
    for path in target.rglob("*"):
        if path.is_file():
            assert_no_private_markers(path.read_text())
    assert_no_private_markers("\n".join(annotation_lines(rerendered["issues"])))
    again = write_public_reports(rerendered, output_dir=tmp_path / "again", formats=["json"], redaction_level="strict")
    assert len({issue["context_reference"] for issue in again["issues"]}) == 2


@pytest.mark.parametrize("rule,code", [
    ("dbt_column_removal_orphans_omni_field", "OF-DBT-IMPACT-ORPHANED-COLUMN"),
    ("dbt_model_removal_orphans_omni_view", "OF-DBT-IMPACT-ORPHANED-MODEL"),
])
def test_dbt_orphan_findings_get_fixed_specific_guidance(rule, code):
    issue = public_diagnostic({"issues": [{
        "validator": "dbt_impact", "rule": rule, "reason_code": rule, "severity": "error",
        "message": PRIVATE, "dbt_relation": PRIVATE, "column": PRIVATE,
    }]})["issues"][0]
    assert issue["diagnostic_code"] == code
    assert issue["category"] == "validation"
    assert "No recognized diagnostic category" not in issue["message"]
    assert "Omni" in issue["message"] and "migrat" in issue["next_action"]
    assert_no_private_markers(json.dumps(issue))


def test_missing_model_identity_explains_the_fix(tmp_path, monkeypatch):
    from omniflow.diagnostics import safe_exception_message
    from omniflow.discovery import discover_contexts
    from omniflow.exceptions import ConfigError

    # This is a local missing-input case, not a PR trusted-metadata test. A real
    # Actions event must not redirect this fixture to the runner's base commit.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    with pytest.raises(ConfigError) as caught:
        discover_contexts(auto=False)
    message = safe_exception_message(caught.value)
    assert message.startswith("OF-SETUP-MODEL-IDENTITY:")
    assert "--auto" in message and "--model-id" in message
