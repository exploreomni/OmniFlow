import json
from unittest.mock import Mock
from xml.etree import ElementTree

import pytest

from omniflow.breaking_hold import evaluate_breaking_hold
from omniflow.config import BreakingChangeHoldSettings, ContractSettings
from omniflow.contracts import evaluate_contracts
from omniflow.diagnostic_catalog import CHANGE_MESSAGES
from omniflow.diagnostics import public_diagnostic
from omniflow.diff.diff_engine import diff_graphs
from omniflow.diff.semantic_graph import build_graph
from omniflow.downstream import generate_downstream_dependencies
from omniflow.github.annotations import public_annotation_lines
from omniflow.reporting.junit_report import to_junit
from omniflow.reporting.markdown_report import render_markdown_report
from omniflow.reporting.sarif_report import to_sarif

KIND = "topic_organization_changed"
BEFORE = "Topics/Sales/orders.topic.yaml"
AFTER = "Topics/Finance/North America/orders.topic.yaml"
DEFINITION = {"base_view": "orders", "label": "Orders"}


def diff(before, after, path=AFTER):
    return diff_graphs(build_graph({BEFORE: before}), build_graph({path: after}))


@pytest.mark.parametrize("before,after", [
    ({}, {"group_label": "Finance"}),
    ({"group_label": "Sales"}, {"group_label": "Finance"}),
    ({"group_label": "Sales"}, {}),
])
@pytest.mark.parametrize("path", [BEFORE, AFTER])
def test_group_add_rename_remove_is_info_with_or_without_folder_move(before, after, path):
    result = diff({**DEFINITION, **before}, {**DEFINITION, **after}, path)
    assert result["risk_level"] == "info"
    assert [change["type"] for change in result["changes"]] == [KIND]
    change = result["changes"][0]
    assert change["file"] == path and change["name"] == "orders"
    assert ("previous_file" in change) == (path != BEFORE)
    assert "group_label" in change["message"]
    assert "Finance" not in change["message"]


@pytest.mark.parametrize("extra", [
    {"base_view": "different_orders"}, {"fields": ["orders.new_field"]},
    {"always_where_sql": "${orders.id} > 0"}, {"required_access_grants": ["restricted"]},
    {"joins": {"customers": {}}}, {"hidden": True},
    {"unknown_property": "changed"},
])
def test_group_change_does_not_hide_logic_or_access_edits(extra):
    result = diff({**DEFINITION, "group_label": "Sales"},
                  {**DEFINITION, "group_label": "Finance", **extra})
    types = {change["type"] for change in result["changes"]}
    assert KIND not in types and "topic_modified" in types
    assert result["risk_level"] != "info"
    if "required_access_grants" in extra or "hidden" in extra:
        assert "governance_changed" in types


@pytest.mark.parametrize("invalid", [None, "", "  ", 42, True, [], {"nested": "Finance"}])
@pytest.mark.parametrize("reverse", [False, True])
def test_invalid_group_values_are_not_downgraded(invalid, reverse):
    before, after = {**DEFINITION, "group_label": "Sales"}, {**DEFINITION, "group_label": invalid}
    result = diff(after, before) if reverse else diff(before, after)
    assert [change["type"] for change in result["changes"]] == ["topic_modified"]
    assert result["risk_level"] == "warning"


def test_folder_only_move_is_still_file_rename_and_topic_deletion_is_breaking():
    result = diff(DEFINITION, DEFINITION)
    assert result["risk_level"] == "info"
    assert [change["type"] for change in result["changes"]] == ["file_renamed"]
    deleted = diff_graphs(build_graph({BEFORE: DEFINITION}), build_graph({}))
    assert deleted["risk_level"] == "breaking"
    assert [change["type"] for change in deleted["changes"]] == ["topic_deleted"]


def test_referenced_group_change_stays_info_and_public_reports_explain_without_names():
    result = diff({**DEFINITION, "group_label": "PRIVATE_OLD_GROUP"},
                  {**DEFINITION, "group_label": "PRIVATE_NEW_GROUP"})
    dependencies = {"dependencies": [{
        "content_id": "PRIVATE_DOCUMENT", "content_name": "PRIVATE_DASHBOARD",
        "references": [{"type": "topic", "name": "orders"}],
    }]}
    report, exit_code = evaluate_contracts(
        diff_result=result, dependencies=dependencies, settings=ContractSettings(enabled=True), model_id="PRIVATE_MODEL",
    )
    assert exit_code == 0
    issue = report["issues"][0]
    assert issue["referenced"] is True and issue["impact_level"] == "referenced_safe"
    assert issue["severity"] == "info"
    report.update(policy_decision="pass", exit_code=0, validation_complete=True)
    safe = public_diagnostic(report)
    assert safe["issues"][0]["type"] == KIND
    assert safe["issues"][0]["message"] == CHANGE_MESSAGES[KIND]
    assert "intended topic grouping" in safe["issues"][0]["next_action"]
    markdown = render_markdown_report(safe)
    assert KIND in markdown and "Blocking issues (all checks): `0`" in markdown
    annotations = public_annotation_lines(safe["issues"], limit=20)
    assert annotations and all(line.startswith("::notice") for line in annotations)
    sarif = to_sarif(safe)
    assert sarif["runs"][0]["results"][0]["level"] == "note"
    junit = to_junit(safe)
    assert ElementTree.fromstring(junit).get("failures") == "0"
    rendered = json.dumps(safe) + markdown + str(annotations) + json.dumps(sarif) + junit
    assert "PRIVATE_" not in rendered and BEFORE not in rendered and AFTER not in rendered
    assert evaluate_breaking_hold(
        diff_result=result, changed_files=["models/orders.sql"], last_sync_sha=None,
        settings=BreakingChangeHoldSettings(enabled=True, dbt_paths=["models/"]),
    ) == []


def test_group_change_keeps_dependency_lookup_and_coverage_errors():
    result = diff(DEFINITION, {**DEFINITION, "group_label": "Finance"})
    client = Mock()
    client.search_content_references.return_value = {"records": []}
    dependencies = generate_downstream_dependencies(client=client, model_id="model", branch_id="branch",
                                                  diff_result=result)
    assert dependencies["searches"] == [{"type": "topic", "name": "orders"}]
    client.search_content_references.assert_called_once()
    dependencies["coverage_gaps"] = [{"reason_code": "dependency_search_unavailable", "name": "orders"}]
    report, exit_code = evaluate_contracts(diff_result=result, dependencies=dependencies,
                                         settings=ContractSettings(enabled=True), model_id="model")
    assert exit_code == 1
    assert report["issues"][0]["severity"] == "info"
    assert report["issues"][1]["type"] == "dependency_coverage_gap"
    assert report["issues"][1]["severity"] == "error"
