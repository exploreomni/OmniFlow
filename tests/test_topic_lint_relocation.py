from copy import deepcopy

import pytest

from omniflow.diff.semantic_graph import build_graph
from omniflow.validators.yaml_lint import DEFAULT_RULES, compare_lint_findings, has_error, lint_graph

BASE_PATH = "Old Group/orders.topic.yaml"
HEAD_PATH = "New Group/orders.topic.yaml"
RULES = dict.fromkeys(DEFAULT_RULES, "off") | {
    "require_topic_labels": "error",
    "require_owner_metadata": "error",
    "forbid_many_to_many_without_comment": "error",
    "forbid_personal_folder_validation_scope": "error",
}


def topic(group="Original"):
    return {
        "base_view": "orders", "group_label": group,
        "relationships": [{
            "join_from_view": "orders", "join_to_view": "items",
            "relationship_type": "many_to_many",
        }],
    }


def graphs():
    return build_graph({BASE_PATH: topic()}), build_graph({HEAD_PATH: topic("Updated")})


def findings(graph):
    return lint_graph(graph, configured_rules=RULES)


def compare(base, head, *, current=None, baseline=None):
    return compare_lint_findings(
        findings(head) if current is None else current,
        findings(base) if baseline is None else baseline,
        base_graph=base, head_graph=head,
    )


def test_moved_topic_and_contained_relationship_debt_remain_existing_with_original_locations():
    base, head = graphs()
    current = findings(head)
    baseline = findings(base)
    originals = deepcopy((current, baseline))
    result = compare(base, head, current=current, baseline=baseline)
    assert len(result) == 3
    assert {issue["rule_id"] for issue in result} == {
        "require_topic_labels", "require_owner_metadata", "forbid_many_to_many_without_comment",
    }
    assert all(issue["state"] == "existing" and issue["blocking"] is False for issue in result)
    assert all(issue["file"] == HEAD_PATH for issue in result)
    assert not has_error(result)
    assert (current, baseline) == originals


@pytest.mark.parametrize("side", ["base", "head"])
def test_optional_grouping_addition_or_removal_does_not_change_debt_identity(side):
    base, head = graphs()
    graph = base if side == "base" else head
    path = BASE_PATH if side == "base" else HEAD_PATH
    graph.files[path].pop("group_label")
    graph.topics["orders"].pop("group_label")
    assert not has_error(compare(base, head))


def test_directory_only_move_retains_existing_topic_debt():
    base = build_graph({BASE_PATH: topic()})
    head = build_graph({HEAD_PATH: topic()})
    assert not has_error(compare(base, head))


def test_new_topic_does_not_borrow_existing_topic_debt():
    base, _ = graphs()
    head = build_graph({HEAD_PATH: topic("Updated"), "New Group/new.topic": {"base_view": "new"}})
    result = compare(base, head)
    old = [issue for issue in result if issue.get("topic") == "orders" or "relationship" in issue]
    new = [issue for issue in result if issue.get("topic") == "new"]
    assert len(old) == 3 and all(issue["state"] == "existing" for issue in old)
    assert len(new) == 2 and all(issue["state"] == "new" and issue["blocking"] for issue in new)


def test_no_graph_evidence_retains_conservative_path_comparison():
    base, head = graphs()
    for kwargs in ({}, {"base_graph": base}, {"head_graph": head}):
        result = compare_lint_findings(findings(head), findings(base), **kwargs)
        assert all(issue["state"] == "new" for issue in result)
        assert has_error(result)


@pytest.mark.parametrize("condition", ["missing_topic", "renamed_topic", "missing_source", "duplicate_location", "bad_name"])
def test_missing_or_ambiguous_topic_evidence_cannot_relocate_debt(condition):
    base, head = graphs()
    current, baseline = findings(head), findings(base)
    if condition == "missing_topic":
        base.topics.clear()
    elif condition == "renamed_topic":
        head.topics["renamed"] = head.topics.pop("orders")
    elif condition == "missing_source":
        head.files.clear()
    elif condition == "duplicate_location":
        head.topics["alias"] = dict(head.topics["orders"])
    else:
        head.topics["orders"]["name"] = "different"
    assert all(issue["state"] == "new" for issue in compare(base, head, current=current, baseline=baseline))


@pytest.mark.parametrize("side", ["current", "baseline"])
def test_issue_location_must_match_its_own_graph(side):
    base, head = graphs()
    current, baseline = findings(head), findings(base)
    target = current if side == "current" else baseline
    for issue in target:
        issue["file"] = "unrelated/orders.topic.yaml"
    assert all(issue["state"] == "new" for issue in compare(base, head, current=current, baseline=baseline))


@pytest.mark.parametrize("property_name,value", [
    ("base_view", "other"), ("owners", ["new-owner"]), ("access_filters", [{"field": "orders.id"}]),
    ("group_label", None),
])
def test_mixed_semantic_governance_or_invalid_grouping_changes_remain_conservative(property_name, value):
    base, _ = graphs()
    payload = topic("Updated") | {property_name: value}
    head = build_graph({HEAD_PATH: payload})
    result = compare(base, head)
    assert result and all(issue["state"] == "new" for issue in result)
    assert has_error(result)


def test_new_severity_increased_and_duplicate_occurrences_still_block():
    base, head = graphs()
    current, baseline = findings(head), findings(base)
    baseline = [issue for issue in baseline if issue["rule_id"] != "require_owner_metadata"]
    baseline[0]["severity"] = "warning"
    relationship = next(issue for issue in current if "relationship" in issue)
    current.append(dict(relationship))
    result = compare(base, head, current=current, baseline=baseline)
    assert [(issue["rule_id"], issue["state"]) for issue in result] == [
        ("require_topic_labels", "new"), ("require_owner_metadata", "new"),
        ("forbid_many_to_many_without_comment", "existing"),
        ("forbid_many_to_many_without_comment", "new"),
    ]
    assert has_error(result)


def test_security_scope_and_unrelated_view_or_file_findings_are_not_relocated():
    base, head = graphs()
    extras = [
        {"rule_id": "forbid_personal_folder_validation_scope"},
        {"rule_id": "require_primary_keys", "view": "orders"},
        {"rule_id": "require_field_descriptions", "field": "orders.id"},
        {"rule_id": "require_owner_metadata"},
    ]
    current = [dict(issue, validator="semantic_lint", severity="error", file=HEAD_PATH) for issue in extras]
    baseline = [dict(issue, file=BASE_PATH) for issue in current]
    # Scope policy must remain blocking even when its ordinary identity matches.
    baseline[0]["file"] = HEAD_PATH
    result = compare(base, head, current=current, baseline=baseline)
    assert all(issue["state"] == "new" and issue["blocking"] for issue in result)


def test_inline_topics_do_not_authorize_model_file_relocation():
    base = build_graph({"Old/model.yaml": {"topics": {"orders": topic()}}})
    head = build_graph({"New/model.yaml": {"topics": {"orders": topic("Updated")}}})
    result = compare(base, head)
    assert len(result) == 2
    assert all(issue["state"] == "new" for issue in result)


def test_ambiguous_relationship_name_or_mismatched_scope_does_not_relocate_relationship_debt():
    base, head = graphs()
    current, baseline = findings(head), findings(base)
    identity = next(iter(head.relationships))
    head.relationships[identity + "#2"] = dict(head.relationships[identity])
    result = compare(base, head, current=current, baseline=baseline)
    assert next(issue for issue in result if "relationship" in issue)["state"] == "new"
    assert all(issue["state"] == "existing" for issue in result if "topic" in issue)
    head.relationships.pop(identity + "#2")
    head.relationships[identity]["scope"] = "global"
    result = compare(base, head, current=current, baseline=baseline)
    assert next(issue for issue in result if "relationship" in issue)["state"] == "new"


def test_changed_relationship_evidence_and_reused_topic_name_do_not_hide_new_debt():
    base, head = graphs()
    identity = next(iter(head.relationships))
    head.relationships[identity]["join_to_view"] = "different"
    result = compare(base, head)
    assert next(issue for issue in result if "relationship" in issue)["state"] == "new"
