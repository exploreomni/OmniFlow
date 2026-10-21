from __future__ import annotations

from typing import Any

from .semantic_graph import SemanticGraph, effective_topic_relationships, relationship_definition


def diff_graphs(base: SemanticGraph, head: SemanticGraph) -> dict[str, Any]:
    relocations, coverage_gaps = _relationship_relocations(base, head)
    moved_base = {before for before, _ in relocations}
    moved_head = {after for _, after in relocations}
    base_relationships = {key: value for key, value in base.relationships.items() if key not in moved_base}
    head_relationships = {key: value for key, value in head.relationships.items() if key not in moved_head}
    changes = []
    changes.extend(_diff_named("view", base.views, head.views))
    topic_changes = _diff_named("topic", base.topics, head.topics)
    relocated_topics = {r["scope"][len("topic:"):] for graph, keys in ((base, moved_base), (head, moved_head))
                        for key in keys for r in [graph.relationships[key]] if r["scope"].startswith("topic:")}
    for change in topic_changes:
        name = change.get("name")
        if (change["type"] == "topic_modified" and name in relocated_topics
                and _topic_without_relocated_relationships(base.topics[name], moved_base, base)
                == _topic_without_relocated_relationships(head.topics[name], moved_head, head)):
            continue
        changes.append(change)
    changes.extend(_diff_named("relationship", base_relationships, head_relationships))
    changes.extend(_diff_named("field", base.fields, head.fields))
    changes.extend(_field_renames(base.fields, head.fields))
    changes.extend(_field_property_changes(base.fields, head.fields))
    changes.extend(_relationship_property_changes(base_relationships, head_relationships))
    for before, after in relocations:
        previous, current = base.relationships[before], head.relationships[after]
        change = _change("relationship_relocated", current, after, "info",
                         "Equivalent relationship definition relocated; explicitly declared topic joins are unchanged.")
        change.update(previous_file=previous.get("file"), affected_views=_relationship_views(previous, current),
                      relationship_endpoints_complete=True)
        changes.append(change)
    changes.extend(_governance_property_changes("view", base.views, head.views))
    changes.extend(_governance_property_changes("topic", base.topics, head.topics))
    changes.extend(_model_governance_changes(base.model, head.model))
    changes.extend(_file_renames(base, head))
    risk_level = _max_risk(changes)
    return {"risk_level": risk_level, "changes": changes, "relationship_coverage_gaps": coverage_gaps}


def _relationship_relocations(base: SemanticGraph, head: SemanticGraph) -> tuple[list[tuple[str, str]], list[dict]]:
    deleted = {key: value for key, value in base.relationships.items() if key not in head.relationships}
    added = {key: value for key, value in head.relationships.items() if key not in base.relationships}
    candidates = []
    for before, previous in deleted.items():
        for after, current in added.items():
            scopes = (previous.get("scope", ""), current.get("scope", ""))
            if ("global" in scopes and any(scope.startswith("topic:") for scope in scopes)
                    and relationship_definition(previous) == relationship_definition(current)):
                candidates.append((before, after))
    if not candidates:
        return [], []
    unique = (len({before for before, _ in candidates}) == len(candidates)
              and len({after for _, after in candidates}) == len(candidates))
    base_effective, head_effective = effective_topic_relationships(base), effective_topic_relationships(head)
    if not unique or base_effective is None or head_effective is None:
        return [], [{"reason_code": "relationship_scope_unresolved",
                     "message": "Relationship relocation equivalence could not be verified across explicit topic joins. "
                                "Resolve ambiguous, automatic or unsupported join definitions before treating this as a safe move."}]
    if base_effective != head_effective:
        return [], []  # A changed/wider topic effect remains an ordinary delete/add.
    return candidates, []


def _topic_without_relocated_relationships(topic: dict, moved: set[str], graph: SemanticGraph) -> dict:
    # Suppress a topic-modified duplicate only when its sole edit is the proved
    # relocation; all other topic settings and remaining relationships survive.
    result = {key: value for key, value in _normalized(topic).items() if key != "relationships"}
    scope = f"topic:{topic.get('name') or next((name for name, item in graph.topics.items() if item is topic), '')}"
    remaining = [relationship_definition(value) for key, value in graph.relationships.items()
                 if value.get("scope") == scope and key not in moved]
    result["relationships"] = remaining
    return result


def _diff_named(kind: str, base_items: dict[str, Any], head_items: dict[str, Any]) -> list[dict[str, Any]]:
    changes = []
    for name in sorted(set(head_items) - set(base_items)):
        change = _change(f"{kind}_added", head_items[name], name, "info", f"Added {kind} {name}.")
        if kind == "relationship":
            change["affected_views"] = _relationship_views(head_items[name])
            change["relationship_endpoints_complete"] = _relationship_endpoints_complete(head_items[name])
        changes.append(change)
    for name in sorted(set(base_items) - set(head_items)):
        risk = "breaking" if kind in {"field", "view", "topic", "relationship"} else "warning"
        change = _change(
            f"{kind}_deleted", base_items[name], name, risk, f"Deleted {kind} may break content references."
        )
        if kind == "relationship":
            change["affected_views"] = _relationship_views(base_items[name])
            change["relationship_endpoints_complete"] = _relationship_endpoints_complete(base_items[name])
        changes.append(change)
    for name in sorted(set(base_items) & set(head_items)):
        if _normalized(base_items[name]) != _normalized(head_items[name]):
            if kind == "topic" and _topic_organization_only(base_items[name], head_items[name]):
                change = _change(
                    "topic_organization_changed", head_items[name], name, "info",
                    "Only the topic group_label changed; this reorganizes topic navigation without changing query logic.",
                )
                if base_items[name].get("file") != head_items[name].get("file"):
                    change["previous_file"] = base_items[name].get("file")
                changes.append(change)
                continue
            change = _change(f"{kind}_modified", head_items[name], name, "warning", f"Modified {kind} {name}.")
            if kind == "relationship":
                change["affected_views"] = _relationship_views(base_items[name], head_items[name])
                change["relationship_endpoints_complete"] = _relationship_endpoints_complete(
                    base_items[name], head_items[name]
                )
            changes.append(change)
    return changes


def _topic_organization_only(base: dict[str, Any], head: dict[str, Any]) -> bool:
    # A directory move is source location, not identity. Only the documented
    # topic grouping property is advisory; do not hide other edits or invalid
    # values behind an organizational classification. Validators still run.
    for item in (base, head):
        if "group_label" in item and (
            not isinstance(item["group_label"], str) or not item["group_label"].strip()
        ):
            return False
    return (
        {key: value for key, value in _normalized(base).items() if key != "group_label"}
        == {key: value for key, value in _normalized(head).items() if key != "group_label"}
    )


def _field_property_changes(base_fields: dict[str, Any], head_fields: dict[str, Any]) -> list[dict[str, Any]]:
    changes = []
    for name in sorted(set(base_fields) & set(head_fields)):
        base = base_fields[name]
        head = head_fields[name]
        if base.get("type") != head.get("type"):
            changes.append(_change("field_type_changed", head, name, "breaking", "Field type changed."))
        if base.get("field_kind") != head.get("field_kind"):
            change = _change("field_kind_changed", head, name, "breaking", "Field kind changed.")
            change.update({"previous_kind": base.get("field_kind"), "kind": head.get("field_kind")})
            changes.append(change)
        if base.get("aggregate_type") != head.get("aggregate_type"):
            changes.append(
                _change("measure_aggregation_changed", head, name, "warning", "Measure aggregation changed.")
            )
        for key in ("access_grants", "access_filters", "required_access_grants"):
            if base.get(key) != head.get(key):
                changes.append(_change("governance_changed", head, name, "governance_sensitive", f"{key} changed."))
    return changes


def _field_renames(base_fields: dict[str, Any], head_fields: dict[str, Any]) -> list[dict[str, Any]]:
    deleted = {name: field for name, field in base_fields.items() if name not in head_fields}
    added = {name: field for name, field in head_fields.items() if name not in base_fields}
    changes = []
    for previous_name, previous in deleted.items():
        for next_name, current in added.items():
            if previous.get("file") != current.get("file"):
                continue
            if _normalized_field_for_rename(previous) != _normalized_field_for_rename(current):
                continue
            changes.append(
                {
                    "type": "field_renamed",
                    "file": current.get("file"),
                    "risk": "breaking",
                    "message": "Renamed field may break content references.",
                    "previous_field": previous_name,
                    "field": next_name,
                }
            )
    return changes


def _relationship_property_changes(
    base_relationships: dict[str, Any], head_relationships: dict[str, Any]
) -> list[dict[str, Any]]:
    changes = []
    for name in sorted(set(base_relationships) & set(head_relationships)):
        base = base_relationships[name]
        head = head_relationships[name]
        for key in ("relationship", "relationship_type", "cardinality", "type"):
            if base.get(key) != head.get(key):
                change = _change(
                    "relationship_cardinality_changed",
                    head,
                    name,
                    "breaking",
                    "Join relationship/cardinality changed.",
                )
                change["affected_views"] = _relationship_views(base, head)
                change["relationship_endpoints_complete"] = _relationship_endpoints_complete(base, head)
                changes.append(change)
                break
    return changes


def _change(change_type: str, item: dict[str, Any], name: str, risk: str, message: str) -> dict[str, Any]:
    payload = {"type": change_type, "file": item.get("file"), "risk": risk, "message": message}
    if change_type.startswith("field_") or change_type.startswith("measure_"):
        payload["field"] = item.get("view") and f"{item['view']}.{item.get('name')}" or name
    else:
        payload["name"] = item.get("name") or name
    return payload


def _relationship_views(*relationships: dict[str, Any]) -> list[str]:
    views = []
    for relationship in relationships:
        for key in ("join_from_view", "from_view", "join_to_view", "to_view"):
            value = relationship.get(key)
            if isinstance(value, str) and value and value not in views:
                views.append(value)
    return views


def _relationship_endpoints_complete(*relationships: dict[str, Any]) -> bool:
    # Check roles before deduplication: a self-join has two endpoints but one name.
    return all(
        any(isinstance(value := relationship.get(key), str) and value and value == value.strip() for key in keys)
        for relationship in relationships
        for keys in (("join_from_view", "from_view"), ("join_to_view", "to_view"))
    )


def _governance_property_changes(
    kind: str,
    base_items: dict[str, dict[str, Any]],
    head_items: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    keys = {
        "required_access_grants",
        "access_grants",
        "access_filters",
        "always_where_filters",
        "hidden",
        "owners",
    }
    changes = []
    for name in sorted(set(base_items) & set(head_items)):
        for key in sorted(keys):
            if base_items[name].get(key) != head_items[name].get(key):
                changes.append(
                    _change(
                        "governance_changed",
                        head_items[name],
                        name,
                        "governance_sensitive",
                        f"{kind} {key} changed.",
                    )
                )
    return changes


def _model_governance_changes(base: dict[str, Any], head: dict[str, Any]) -> list[dict[str, Any]]:
    changes = []
    for key in ("access_grants", "default_access_grants"):
        if base.get(key) != head.get(key):
            changes.append(
                {
                    "type": "governance_changed",
                    "file": head.get("file") or base.get("file"),
                    "name": key,
                    "risk": "governance_sensitive",
                    "message": f"Model {key} changed.",
                }
            )
    return changes


def _file_renames(base: SemanticGraph, head: SemanticGraph) -> list[dict[str, Any]]:
    changes = []
    for kind, base_items, head_items in (
        ("view", base.views, head.views),
        ("topic", base.topics, head.topics),
    ):
        for name in sorted(set(base_items) & set(head_items)):
            previous_file = base_items[name].get("file")
            file_name = head_items[name].get("file")
            if (
                previous_file
                and file_name
                and previous_file != file_name
                and _normalized(base_items[name]) == _normalized(head_items[name])
            ):
                changes.append(
                    {
                        "type": "file_renamed",
                        "entity_type": kind,
                        "name": name,
                        "previous_file": previous_file,
                        "file": file_name,
                        "risk": "info",
                        "message": f"Renamed {kind} file without changing its semantic definition.",
                    }
                )
    return changes


def _normalized(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key != "file"}


def _normalized_field_for_rename(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key not in {"file", "name"}}


def _max_risk(changes: list[dict[str, Any]]) -> str:
    order = ["info", "warning", "governance_sensitive", "security_sensitive", "breaking"]
    if not changes:
        return "info"
    return max(
        (change.get("risk", "info") for change in changes), key=lambda risk: order.index(risk) if risk in order else 0
    )
