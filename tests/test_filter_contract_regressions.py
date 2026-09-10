import copy

import pytest

from omniflow.config import ContractSettings
from omniflow.contracts import evaluate_contracts
from omniflow.diff.diff_engine import diff_graphs
from omniflow.diff.semantic_graph import build_graph
from omniflow.downstream import generate_downstream_dependencies
from omniflow.exceptions import ConfigError


class NameSensitiveClient:
    def __init__(self):
        self.searches = []

    def search_content_references(self, model_id, *, find, find_type, **kwargs):
        self.searches.append((find_type, find))
        referenced = find in {"schema__orders", "schema__orders.window"}
        return {"content": [{"document_id": "synthetic-dashboard"}] if referenced else []}


@pytest.mark.parametrize(
    "operation,expected_type,expected_exit",
    [("delete", "field_deleted", 1), ("type", "field_type_changed", 1),
     ("rename", "field_renamed", 1), ("add", "field_added", 0)],
)
def test_filter_definitions_reach_name_sensitive_contract_gate(operation, expected_type, expected_exit):
    before = {"schema__orders.view": {
        "schema": "schema", "dimensions": {"id": {"type": "number"}},
        "filters": {"window": {"type": "string"}},
    }}
    after = copy.deepcopy(before)
    filters = after["schema__orders.view"]["filters"]
    if operation == "delete":
        del filters["window"]
    elif operation == "type":
        filters["window"]["type"] = "number"
    elif operation == "rename":
        filters["period"] = filters.pop("window")
    else:
        filters["period"] = {"type": "number"}
    base, head = build_graph(before), build_graph(after)
    assert base.fields["schema__orders.window"]["field_kind"] == "filter"
    assert base.fields["schema__orders.id"]["field_kind"] == "dimension"
    diff = diff_graphs(base, head)
    assert expected_type in {change["type"] for change in diff["changes"]}
    client = NameSensitiveClient()
    dependencies = generate_downstream_dependencies(
        client=client, model_id="model", branch_id="branch", diff_result=diff,
    )
    report, exit_code = evaluate_contracts(
        diff_result=diff, dependencies=dependencies, settings=ContractSettings(), model_id="model",
    )
    assert exit_code == expected_exit
    assert ("field", "schema__orders.period" if operation == "add" else "schema__orders.window") in client.searches
    assert report["summary"]["coverage_errors"] == 0


def test_filter_expressions_are_not_filter_only_field_definitions():
    graph = build_graph({
        "orders.view": {
            "dimensions": {"status": {"type": "string"}},
            "measures": {"count": {"aggregate_type": "count", "filters": {"status": {"is": "active"}}}},
        },
        "orders.topic": {"base_view": "orders", "default_filters": {"orders.status": {"is": "active"}}},
    })
    assert set(graph.fields) == {"orders.status", "orders.count"}
    assert graph.fields["orders.count"]["field_kind"] == "measure"


def test_duplicate_filter_identity_fails_and_cross_kind_rename_is_not_inferred():
    with pytest.raises(ConfigError, match="duplicate"):
        build_graph({"orders.view": {"dimensions": {"id": {}}, "filters": {"id": {}}}})
    base = build_graph({"orders.view": {"filters": {"old": {"type": "string"}}}})
    head = build_graph({"orders.view": {"dimensions": {"new": {"type": "string"}}}})
    assert "field_renamed" not in {change["type"] for change in diff_graphs(base, head)["changes"]}
