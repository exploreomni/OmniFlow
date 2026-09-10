import pytest

from omniflow.config import ContractSettings
from omniflow.contracts import evaluate_contracts
from omniflow.diff.diff_engine import diff_graphs
from omniflow.diff.semantic_graph import build_graph
from omniflow.downstream import generate_downstream_dependencies


class ReferenceClient:
    def __init__(self, referenced=False):
        self.referenced = referenced
        self.searches = []

    def search_content_references(self, model_id, *, find, find_type, **kwargs):
        self.searches.append((find_type, find))
        return {"model_id": model_id, "branch": {"id": kwargs["branch_id"]},
                "content": [{"document_id": "document-1"}] if self.referenced else []}


FROM_ONLY = {"join_from_view": "orders"}
TO_ONLY = {"join_to_view": "orders"}
SELF_JOIN = {"join_from_view": "orders", "join_to_view": "orders", "join_to_view_as": "parent"}


@pytest.mark.parametrize("base,head,complete", [
    (None, FROM_ONLY, False),
    (None, TO_ONLY, False),
    (FROM_ONLY, None, False),
    (FROM_ONLY, SELF_JOIN, False),
    (SELF_JOIN, TO_ONLY, False),
    (FROM_ONLY, {**TO_ONLY, "relationship_type": "one_to_many"}, False),
    (None, SELF_JOIN, True),
    (SELF_JOIN, None, True),
    (SELF_JOIN, {**SELF_JOIN, "relationship_type": "one_to_many"}, True),
])
def test_relationship_completeness_is_per_role_and_revision_not_distinct_view_count(base, head, complete):
    def graph(value):
        relationship = {"name": "join", "relationship_type": "many_to_one", **value} if value is not None else None
        return build_graph({"relationships.yaml": [relationship] if relationship else []})

    diff = diff_graphs(graph(base), graph(head))
    assert diff["changes"]
    assert all(change["relationship_endpoints_complete"] is complete for change in diff["changes"])
    assert all(change["affected_views"] == ["orders"] for change in diff["changes"])
    client = ReferenceClient()
    dependencies = generate_downstream_dependencies(
        client=client, model_id="model-1", branch_id="branch-1", diff_result=diff,
    )
    report, code = evaluate_contracts(
        diff_result=diff, dependencies=dependencies, settings=ContractSettings(), model_id="model-1",
    )
    assert bool(dependencies["coverage_gaps"]) is not complete
    assert code == int(not complete)
    assert report["summary"]["coverage_errors"] == (len(diff["changes"]) if not complete else 0)
    assert client.searches == [("view", "orders")]
    # Existing persisted/integrator diff records lack completeness metadata.
    legacy = {"changes": [{"type": "relationship_added", "name": "self_join", "affected_views": ["orders"]}]}
    assert generate_downstream_dependencies(
        client=ReferenceClient(), model_id="model-1", branch_id="branch-1", diff_result=legacy,
    )["coverage_gaps"] == []


@pytest.mark.parametrize("before,after,referenced,fail_on_type,expected", [
    ("dimensions", "filters", True, True, 1),
    ("filters", "dimensions", True, True, 1),
    ("dimensions", "measures", True, True, 1),
    ("measures", "filters", True, True, 1),
    ("dimensions", "filters", False, True, 0),
    ("dimensions", "filters", True, False, 0),
    ("filters", "filters", True, True, 0),
])
def test_same_name_kind_conversion_uses_existing_referenced_type_change_policy(
    before, after, referenced, fail_on_type, expected,
):
    def graph(kind):
        return build_graph({"orders.view": {kind: {"status": {"type": "string"}}}})

    diff = diff_graphs(graph(before), graph(after))
    kind_changes = [change for change in diff["changes"] if change["type"] == "field_kind_changed"]
    assert len(kind_changes) == int(before != after)
    assert all(change["risk"] == "breaking" and change["field"] == "orders.status" for change in kind_changes)
    client = ReferenceClient(referenced)
    dependencies = generate_downstream_dependencies(
        client=client, model_id="model-1", branch_id="branch-1", diff_result=diff,
    )
    report, code = evaluate_contracts(
        diff_result=diff, dependencies=dependencies, model_id="model-1",
        settings=ContractSettings(fail_on_referenced_field_type_changes=fail_on_type),
    )
    assert code == expected
    assert dependencies["coverage_gaps"] == []
    assert {issue["type"] for issue in report["issues"] if issue["severity"] == "error"} == (
        {"field_kind_changed"} if expected else set()
    )
    if kind_changes:
        assert ("field", "orders.status") in client.searches
