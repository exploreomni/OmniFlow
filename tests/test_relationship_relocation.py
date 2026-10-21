from copy import deepcopy

import pytest

from omniflow.config import ContractSettings
from omniflow.contracts import evaluate_contracts
from omniflow.diff.diff_engine import diff_graphs
from omniflow.diff.semantic_graph import build_graph, effective_topic_relationships

RELATIONSHIP = {"join_from_view": "orders", "join_to_view": "users", "join_type": "always_left",
                "on_sql": "${orders.user_id} = ${users.id}", "relationship_type": "many_to_one"}


def snapshots(*, alias=None, inline=False):
    relationship = deepcopy(RELATIONSHIP)
    if alias:
        relationship.update(join_to_view_as=alias, on_sql=f"${{orders.user_id}} = ${{{alias}.id}}")
    topic = {"base_view": "orders", "joins": {alias or "users": {}}, "relationships": [relationship]}
    files = {"orders.view.yaml": {"dimensions": {"id": {}}},
             "users.view.yaml": {"dimensions": {"id": {}}}, "relationships.yaml": []}
    if inline:
        files["model.yaml"] = {"topics": {"purchases": topic}}
    else:
        files["purchases.topic.yaml"] = topic
    head = deepcopy(files)
    head_topic = head["model.yaml"]["topics"]["purchases"] if inline else head["purchases.topic.yaml"]
    head["relationships.yaml"] = head_topic.pop("relationships")
    return files, head


def compare(base, head):
    diff = diff_graphs(build_graph(base), build_graph(head))
    # Existing content references these views; no missing-data shortcut.
    dependencies = {"coverage_gaps": [], "dependencies": [
        {"content_id": "synthetic", "references": [{"type": "view", "name": "orders"},
                                                     {"type": "view", "name": "users"}]}]}
    report, code = evaluate_contracts(diff_result=diff, dependencies=dependencies,
                                     settings=ContractSettings(), model_id="synthetic")
    return diff, report, code


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("alias,inline", [(None, False), ("buyers", False), (None, True)])
def test_exact_relocation_preserves_effective_topics_without_referenced_deletion_block(reverse, alias, inline):
    base, head = snapshots(alias=alias, inline=inline)
    if reverse:
        base, head = head, base
    diff, report, code = compare(base, head)
    assert code == 0 and diff["relationship_coverage_gaps"] == []
    assert [change["type"] for change in diff["changes"]] == ["relationship_relocated"]
    assert report["issues"][0]["severity"] == "info"
    assert report["issues"][0]["referenced"] is True
    assert report["coverage_complete"] is True


@pytest.mark.parametrize("change", [
    {"on_sql": "${orders.other_id} = ${users.id}"}, {"relationship_type": "one_to_many"},
    {"join_type": "always_inner"}, {"join_to_view_as": "buyers"}, {"join_from_view_as": "source"},
    {"where_sql": "${users.active} = true"}, {"reversible": True}, {"join_to_view": "accounts"},
])
def test_move_plus_actual_relationship_change_is_not_downgraded(change):
    base, head = snapshots()
    head["relationships.yaml"][0].update(change)
    diff, report, code = compare(base, head)
    assert code == 1
    assert "relationship_relocated" not in {change["type"] for change in diff["changes"]}
    assert any(issue["type"] == "relationship_deleted" and issue["severity"] == "error"
               for issue in report["issues"])


def test_wider_global_effect_is_not_hidden_by_target_topic_equality():
    base, head = snapshots()
    for files in (base, head):
        files["other.topic.yaml"] = {"base_view": "orders", "joins": {"users": {}}}
    diff, report, code = compare(base, head)
    assert code == 1 and diff["relationship_coverage_gaps"]
    assert any(issue["type"] == "dependency_coverage_gap" for issue in report["issues"])


def test_unchanged_other_topic_override_does_not_guess_global_local_precedence():
    base, head = snapshots()
    other = {"base_view": "orders", "joins": {"users": {}},
             "relationships": [{**RELATIONSHIP, "on_sql": "${orders.other_id} = ${users.id}"}]}
    for files in (base, head):
        files["other.topic.yaml"] = deepcopy(other)
    diff, report, code = compare(base, head)
    assert code == 1 and diff["relationship_coverage_gaps"][0]["reason_code"] == "relationship_scope_unresolved"
    assert report["coverage_complete"] is False


@pytest.mark.parametrize("mutation", ["missing_joins", "delta_joins", "duplicate", "malformed", "reverse", "unknown_view"])
def test_incomplete_or_ambiguous_evidence_retains_blockers_and_coverage_gap(mutation):
    base, head = snapshots()
    if mutation == "missing_joins":
        base["purchases.topic.yaml"].pop("joins")
        head["purchases.topic.yaml"].pop("joins")
    elif mutation == "delta_joins":
        head["purchases.topic.yaml"]["added_joins"] = {}
    elif mutation == "duplicate":
        head["relationships.yaml"].append(deepcopy(RELATIONSHIP))
    elif mutation == "malformed":
        head["relationships.yaml"].append("not a relationship")
    elif mutation == "reverse":
        for files in (base, head):
            files["purchases.topic.yaml"].update(base_view="users", joins={"orders": {}})
    else:
        for files in (base, head):
            files.pop("users.view.yaml")
    diff, report, code = compare(base, head)
    assert code == 1 and diff["relationship_coverage_gaps"]
    assert not any(change["type"] == "relationship_relocated" for change in diff["changes"])
    assert report["coverage_complete"] is False


def test_nested_aliases_and_explicit_unjoined_topics_have_same_effect():
    base, head = snapshots(alias="buyers")
    second = {"join_from_view": "users", "join_from_view_as": "buyers", "join_to_view": "accounts",
              "join_type": "always_left", "on_sql": "${buyers.account_id} = ${accounts.id}",
              "relationship_type": "many_to_one"}
    for files in (base, head):
        files["accounts.view.yaml"] = {}
        files["purchases.topic.yaml"]["joins"] = {"buyers": {"accounts": {}}}
        files["empty.topic.yaml"] = {"base_view": "orders", "joins": {}}
        files["relationships.yaml"].append(deepcopy(second))
    diff, _, code = compare(base, head)
    assert code == 0 and [change["type"] for change in diff["changes"]] == ["relationship_relocated"]
    assert effective_topic_relationships(build_graph(base)) == effective_topic_relationships(build_graph(head))


def test_move_does_not_hide_other_topic_edits_or_real_deletions():
    base, head = snapshots()
    head["purchases.topic.yaml"]["always_where_sql"] = "${orders.id} > 10"
    diff, _, code = compare(base, head)
    assert code == 0
    assert {x["type"] for x in diff["changes"]} == {"topic_modified", "relationship_relocated"}
    deleted = deepcopy(base)
    deleted["purchases.topic.yaml"]["relationships"] = []
    _, report, code = compare(base, deleted)
    assert code == 1 and any(x["type"] == "relationship_deleted" for x in report["issues"])


def test_unresolved_relocation_coverage_uses_existing_policy_not_unconditional_failure():
    base, head = snapshots()
    base["purchases.topic.yaml"].pop("joins")
    diff = diff_graphs(build_graph(base), build_graph(head))
    report, code = evaluate_contracts(diff_result=diff, dependencies={"dependencies": [], "coverage_gaps": []},
                                     settings=ContractSettings(fail_on_coverage_gaps=False), model_id="synthetic")
    assert code == 0 and report["coverage_complete"] is False
    assert any(x["type"] == "dependency_coverage_gap" and x["severity"] == "warning" for x in report["issues"])


@pytest.mark.parametrize("malformed", [
    {"other.topic.yaml": "not a topic mapping"},
    {"model.yaml": {"topics": {"other": "not a topic mapping"}}},
    {"model.yaml": {"topics": ["not a topic mapping"]}},
    {"model.yaml": {"topics": "not a topic collection"}},
])
def test_omitted_malformed_topics_cannot_establish_whole_model_equivalence(malformed):
    base, head = snapshots()
    for files in (base, head):
        files.update(deepcopy(malformed))
    diff, report, code = compare(base, head)
    assert code == 1 and report["coverage_complete"] is False
    assert diff["relationship_coverage_gaps"][0]["reason_code"] == "relationship_scope_unresolved"
    assert not any(change["type"] == "relationship_relocated" for change in diff["changes"])
