import unittest

from omniflow.config import ContractSettings
from omniflow.contracts import evaluate_contracts
from omniflow.diff.diff_engine import diff_graphs
from omniflow.diff.semantic_graph import build_graph
from omniflow.downstream import generate_downstream_dependencies


class ReferenceClient:
    def __init__(self, *, referenced=False):
        self.searches = []
        self.referenced = referenced

    def search_content_references(self, model_id, **kwargs):
        self.searches.append((kwargs["find_type"], kwargs["find"]))
        return {"content": [{"document_id": "document-1"}] if self.referenced else []}


def evaluate(base_files, head_files, *, referenced=False):
    diff = diff_graphs(build_graph(base_files), build_graph(head_files))
    client = ReferenceClient(referenced=referenced)
    dependencies = generate_downstream_dependencies(
        client=client, model_id="model-1", branch_id="branch-1", diff_result=diff,
    )
    report, exit_code = evaluate_contracts(
        diff_result=diff, dependencies=dependencies, settings=ContractSettings(), model_id="model-1",
    )
    changes = [change for change in diff["changes"] if change["type"].startswith("relationship_")]
    return changes, client.searches, dependencies, report, exit_code


class RelationshipContractRegressions(unittest.TestCase):
    def test_issue_additions_and_deletions_preserve_endpoints_aliases_and_scope(self):
        relationships = [
            {"join_from_view": "table_1", "join_to_view": "table_2", "join_type": "always_left",
             "on_sql": "${table_1.id}=${table_2.id}", "relationship_type": "many_to_one"},
            {"join_from_view": "table_2", "join_to_view": "table_3", "join_to_view_as": "table_3_alias",
             "join_type": "always_left", "on_sql": "${table_2.user_id}=${table_3_alias.user_id}",
             "relationship_type": "many_to_one"},
        ]
        expected = {"table_1:->table_2:": ["table_1", "table_2"],
                    "table_2:->table_3:table_3_alias": ["table_2", "table_3"]}
        for scope in ("global", "topic"):
            empty = {"relationships.yaml": []} if scope == "global" else {
                "topics/table_1.topic": {"base_view": "table_1", "relationships": []},
            }
            populated = {"relationships.yaml": relationships} if scope == "global" else {
                "topics/table_1.topic": {"base_view": "table_1", "relationships": relationships},
            }
            graph = build_graph(populated)
            prefix = "global" if scope == "global" else "topic:table_1"
            self.assertEqual(set(graph.relationships), {f"{prefix}:{name}" for name in expected})
            for operation, base, head in (("added", empty, populated), ("deleted", populated, empty)):
                with self.subTest(scope=scope, operation=operation):
                    changes, searches, dependencies, _, code = evaluate(base, head)
                    self.assertEqual({change["name"]: change["affected_views"] for change in changes}, expected)
                    self.assertEqual({change["type"] for change in changes}, {f"relationship_{operation}"})
                    self.assertEqual({name for kind, name in searches if kind == "view"},
                                     {"table_1", "table_2", "table_3"})
                    self.assertEqual(dependencies["coverage_gaps"], [])
                    self.assertEqual(code, 0)

    def test_modifications_union_endpoints_without_masking_referenced_breaking_changes(self):
        relationship = {"name": "stable_join", "join_from_view": "orders", "join_to_view": "users",
                        "join_to_view_as": "buyers", "relationship_type": "many_to_one",
                        "on_sql": "${orders.buyer_id}=${buyers.id}"}
        cases = (
            ({"on_sql": "${orders.customer_id}=${buyers.id}"}, ["orders", "users"], False),
            ({"relationship_type": "one_to_many"}, ["orders", "users"], True),
            ({"join_to_view": "accounts", "relationship_type": "one_to_many"},
             ["orders", "users", "accounts"], True),
        )
        for updates, endpoints, breaking in cases:
            with self.subTest(updates=updates):
                changes, searches, dependencies, report, code = evaluate(
                    {"relationships.yaml": [relationship]},
                    {"relationships.yaml": [{**relationship, **updates}]}, referenced=True,
                )
                expected_types = {"relationship_modified"}
                if breaking:
                    expected_types.add("relationship_cardinality_changed")
                self.assertEqual({change["type"] for change in changes}, expected_types)
                self.assertTrue(all(change["name"] == "stable_join" for change in changes))
                self.assertTrue(all(change["affected_views"] == endpoints for change in changes))
                self.assertEqual(searches, [("view", endpoint) for endpoint in endpoints])
                self.assertEqual(dependencies["coverage_gaps"], [])
                self.assertEqual(code, int(breaking))
                self.assertEqual({issue["type"] for issue in report["issues"] if issue["severity"] == "error"},
                                 {"relationship_cardinality_changed"} if breaking else set())

    def test_genuinely_unavailable_relationship_endpoints_still_fail_closed(self):
        unknown = {"name": "unknown_join", "relationship_type": "many_to_one"}
        cases = (
            ({}, {"relationships.yaml": [unknown]}),
            ({"relationships.yaml": [unknown]}, {}),
            ({"relationships.yaml": [unknown]},
             {"relationships.yaml": [{**unknown, "relationship_type": "one_to_many"}]}),
        )
        for base, head in cases:
            with self.subTest(base=base, head=head):
                changes, searches, dependencies, report, code = evaluate(base, head)
                self.assertTrue(changes)
                self.assertTrue(all(change["affected_views"] == [] for change in changes))
                self.assertEqual(searches, [])
                self.assertEqual(len(dependencies["coverage_gaps"]), len(changes))
                self.assertEqual(code, 1)
                self.assertTrue(any(issue["type"] == "dependency_coverage_gap" and issue["severity"] == "error"
                                    for issue in report["issues"]))
