from __future__ import annotations

from typing import Any

from ..diff.semantic_graph import SemanticGraph
from ..exceptions import ConfigError

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
    return any(issue["severity"] == "error" for issue in issues)


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
        _emit(issues, rules, rule_id, change, change.get("message", "Semantic diff rule triggered."))
