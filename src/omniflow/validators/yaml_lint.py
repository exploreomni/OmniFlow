from __future__ import annotations

from collections import Counter
from typing import Any

from ..diff.semantic_graph import SemanticGraph
from ..exceptions import ConfigError
from ..semantic_files import infer_file_kind

SEVERITIES = {"off": 0, "info": 1, "warn": 2, "error": 3}
DEFAULT_RULES = {
    "require_field_descriptions": "warn",
    "require_measure_descriptions": "warn",
    "require_primary_keys": "warn",
    "require_topic_labels": "warn",
    "forbid_many_to_many_without_comment": "warn",
    "block_deleted_fields": "warn",
    "warn_field_type_change": "warn",
    "warn_measure_aggregation_change": "warn",
    "warn_relationship_cardinality_change": "warn",
    "require_owner_metadata": "warn",
    "forbid_personal_folder_validation_scope": "error",
}


def merged_rules(configured: dict[str, str] | None = None) -> dict[str, str]:
    rules = dict(DEFAULT_RULES)
    for key, value in (configured or {}).items():
        if value not in SEVERITIES:
            raise ConfigError(f"Invalid severity for {key}: {value}")
        if key not in DEFAULT_RULES:
            raise ConfigError(f"Unknown semantic lint rule: {key}")
        rules[key] = value
    return rules


def lint_graph(
    graph: SemanticGraph,
    *,
    configured_rules: dict[str, str] | None = None,
    include_personal_folders: bool = False,
    diff_result: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rules = merged_rules(configured_rules)
    issues: list[dict[str, Any]] = []
    if include_personal_folders:
        _emit(
            issues,
            rules,
            "forbid_personal_folder_validation_scope",
            None,
            "Personal folders should not be part of default CI validation scope.",
        )
    for name, field in sorted(graph.fields.items()):
        if not field.get("description"):
            rule = "require_measure_descriptions" if _is_measure(field) else "require_field_descriptions"
            noun = "Measure" if _is_measure(field) else "Field"
            _emit(issues, rules, rule, field, f"{noun} is missing a description.", field=name)
    for view_name, view in sorted(graph.views.items()):
        if not _has_primary_key_for_view(graph, view_name):
            _emit(issues, rules, "require_primary_keys", view, "View does not define a primary key.", view=view_name)
    for topic_name, topic in sorted(graph.topics.items()):
        if not topic.get("label"):
            _emit(issues, rules, "require_topic_labels", topic, "Topic is missing a label.", topic=topic_name)
        if not topic.get("owners"):
            _emit(
                issues,
                rules,
                "require_owner_metadata",
                topic,
                "Topic is missing documented owners metadata.",
                topic=topic_name,
            )
    for rel_name, relationship in sorted(graph.relationships.items()):
        cardinality = str(
            relationship.get("relationship_type")
            or relationship.get("relationship")
            or relationship.get("cardinality")
            or ""
        ).lower()
        if "many_to_many" in cardinality or "many-to-many" in cardinality:
            _emit(
                issues,
                rules,
                "forbid_many_to_many_without_comment",
                relationship,
                "Many-to-many relationship requires manual governance review; Omni does not document a relationship comment property.",
                relationship=rel_name,
            )
    for change in (diff_result or {}).get("changes", []):
        _emit_diff_issue(issues, rules, change)
    return issues


def has_error(issues: list[dict[str, Any]]) -> bool:
    return any(issue["severity"] == "error" and issue.get("blocking", True) for issue in issues)


def compare_lint_findings(
    current: list[dict], baseline: list[dict], *,
    base_graph: SemanticGraph | None = None, head_graph: SemanticGraph | None = None,
) -> list[dict]:
    """Apply PR regression policy to lint debt, not security-scope controls."""
    relocations = _topic_lint_relocations(base_graph, head_graph)

    def identity(issue: dict, side: int) -> tuple:
        values = [issue.get(key) for key in
                  ("rule_id", "file", "field", "view", "topic", "relationship", "severity")]
        kind = (
            "topic" if issue.get("rule_id") in {"require_topic_labels", "require_owner_metadata"}
            else "relationship" if issue.get("rule_id") == "forbid_many_to_many_without_comment" else None
        )
        name = issue.get(kind) if kind else None
        if (issue.get("validator") == "semantic_lint" and isinstance(name, str)
                and not any(issue.get(key) is not None for key in {"field", "view", "topic", "relationship"} - {kind})):
            paths = relocations.get((kind, name))
            if paths and issue.get("file") == paths[side]:
                values[1] = ("verified_topic_relocation", kind, name, *paths)
        return tuple(values)

    remaining = Counter(identity(issue, 0) for issue in baseline)
    result = []
    for issue in current:
        key = identity(issue, 1)
        # This is an execution-scope security policy, never grandfathered debt.
        protected = issue.get("rule_id") == "forbid_personal_folder_validation_scope"
        existing = not protected and remaining[key] > 0
        if existing:
            remaining[key] -= 1
        result.append(dict(issue, state="existing" if existing else "new",
                           blocking=issue["severity"] == "error" and not existing))
    return result


def _topic_lint_relocations(
    base: SemanticGraph | None, head: SemanticGraph | None,
) -> dict[tuple[str, str], tuple[str, str]]:
    """Authorize only per-entity debt matches for an unchanged topic relocation.

    Graphs must identify the same standalone topic on both sides. A model file
    containing inline topics is never treated as a file-wide relocation, and
    other semantic edits keep the original conservative path-based comparison.
    """
    if not isinstance(base, SemanticGraph) or not isinstance(head, SemanticGraph):
        return {}
    if any(not isinstance(item, dict) or not isinstance(item.get("file"), str)
           for graph in (base, head) for item in graph.topics.values()):
        return {}
    locations = {}
    topic_file_counts = [Counter(item.get("file") for item in graph.topics.values()) for graph in (base, head)]
    for name in base.topics.keys() & head.topics.keys():
        before, after = base.topics[name], head.topics[name]
        paths = before.get("file"), after.get("file")
        if paths[0] == paths[1] or not isinstance(name, str) or not name:
            continue
        valid = True
        for side, (graph, item, path) in enumerate(zip((base, head), (before, after), paths, strict=True)):
            if (not isinstance(path, str) or not path or path not in graph.files
                    or topic_file_counts[side][path] != 1
                    or infer_file_kind(path, graph.files[path]) != "topic"
                    or item.get("name", name) != name
                    or ("group_label" in item and
                        (not isinstance(item["group_label"], str) or not item["group_label"].strip()))):
                valid = False
                break
        if not valid or _without(before, "file", "group_label") != _without(after, "file", "group_label"):
            continue
        locations[("topic", name)] = paths

    relationship_counts = [Counter((item["scope"], item["name"])
                                   for item in graph.relationships.values()
                                   if isinstance(item, dict) and isinstance(item.get("scope"), str)
                                   and isinstance(item.get("name"), str)) for graph in (base, head)]
    for identity in base.relationships.keys() & head.relationships.keys():
        before, after = base.relationships[identity], head.relationships[identity]
        if not isinstance(before, dict) or not isinstance(after, dict):
            continue
        scope, name = before.get("scope"), before.get("name")
        if (not isinstance(scope, str) or not scope.startswith("topic:")
                or not isinstance(name, str) or not name or name.startswith("unnamed:")
                or identity != f"{scope}:{name}"
                or any(counts[(scope, name)] != 1 for counts in relationship_counts)
                or _without(before, "file") != _without(after, "file")):
            continue
        paths = locations.get(("topic", scope.removeprefix("topic:")))
        if paths and (before.get("file"), after.get("file")) == paths:
            locations[("relationship", identity)] = paths
    return locations


def _without(item: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key not in keys}


def _is_measure(field: dict[str, Any]) -> bool:
    return bool(field.get("aggregate_type") or field.get("measure") or field.get("type") == "measure")


def _has_primary_key_for_view(graph: SemanticGraph, view_name: str) -> bool:
    return any(field.get("view") == view_name and bool(field.get("primary_key")) for field in graph.fields.values())


def _emit(
    issues: list[dict[str, Any]],
    rules: dict[str, str],
    rule_id: str,
    item: dict[str, Any] | None,
    message: str,
    **extra: Any,
) -> None:
    severity = rules.get(rule_id, "off")
    if severity == "off":
        return
    issues.append(
        {
            "validator": "semantic_lint",
            "rule_id": rule_id,
            "severity": "warning" if severity == "warn" else severity,
            "file": item.get("file") if item else None,
            "message": message,
            **extra,
        }
    )


def _emit_diff_issue(issues: list[dict[str, Any]], rules: dict[str, str], change: dict[str, Any]) -> None:
    mapping = {
        "field_deleted": "block_deleted_fields",
        "field_type_changed": "warn_field_type_change",
        "measure_aggregation_changed": "warn_measure_aggregation_change",
        "relationship_cardinality_changed": "warn_relationship_cardinality_change",
    }
    rule_id = mapping.get(change.get("type"))
    if rule_id:
        _emit(
            issues, rules, rule_id, change, change.get("message", "Semantic diff rule triggered."),
            **{key: change[key] for key in (
                "field", "previous_field", "view", "topic", "relationship", "name", "previous_name", "type",
            ) if isinstance(change.get(key), str)},
        )
