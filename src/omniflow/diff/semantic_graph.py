from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SemanticGraph:
    model: dict[str, Any] = field(default_factory=dict)
    views: dict[str, dict[str, Any]] = field(default_factory=dict)
    topics: dict[str, dict[str, Any]] = field(default_factory=dict)
    relationships: dict[str, dict[str, Any]] = field(default_factory=dict)
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    files: dict[str, Any] = field(default_factory=dict)


def build_graph(files: dict[str, Any]) -> SemanticGraph:
    graph = SemanticGraph()
    for file_path, payload in files.items():
        graph.files[file_path] = payload
        kind = _infer_kind(file_path, payload)
        if kind == "model":
            if isinstance(payload, dict):
                graph.model.update({"file": file_path, **payload})
                _add_inline_topics(graph, file_path, payload)
            continue
        if kind == "relationship":
            _add_relationships(graph, file_path, payload)
            continue
        if not isinstance(payload, dict):
            continue
        name = _name(file_path, payload)
        if kind == "topic":
            graph.topics[name] = {"file": file_path, **payload}
            _add_relationships(graph, file_path, payload, scope=f"topic:{name}")
        else:
            graph.views[name] = {"file": file_path, **payload}
            _add_fields(graph, file_path, name, payload)
            _add_relationships(graph, file_path, payload, scope=f"view:{name}")
    return graph


def _infer_kind(file_path: str, payload: Any) -> str:
    lower = file_path.lower()
    basename = lower.rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0]
    if stem == "model" or (isinstance(payload, dict) and payload.get("type") == "model"):
        return "model"
    if lower.endswith(".topic") or ".topic." in lower:
        return "topic"
    if lower.endswith(".composite_topic") or ".composite_topic." in lower:
        return "topic"
    if stem == "relationships" or "relationship" in lower or isinstance(payload, list):
        return "relationship"
    if isinstance(payload, dict) and (payload.get("type") == "topic" or "base_view" in payload):
        return "topic"
    return "view"


def _name(file_path: str, payload: dict[str, Any]) -> str:
    value = payload.get("name") or payload.get("view") or payload.get("topic")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return file_path.rsplit("/", 1)[-1].split(".", 1)[0]


def _iter_field_groups(payload: dict[str, Any]) -> list[dict[str, Any]]:
    groups = []
    for key in ("fields", "dimensions", "measures"):
        value = payload.get(key)
        if isinstance(value, dict):
            groups.append(value)
        elif isinstance(value, list):
            groups.append(
                {str(item.get("name")): item for item in value if isinstance(item, dict) and item.get("name")}
            )
    return groups


def _add_fields(graph: SemanticGraph, file_path: str, view_name: str, payload: dict[str, Any]) -> None:
    for group in _iter_field_groups(payload):
        for field_name, definition in group.items():
            if not isinstance(definition, dict):
                continue
            key = f"{view_name}.{field_name}"
            graph.fields[key] = {"file": file_path, "view": view_name, "name": field_name, **definition}


def _add_relationships(
    graph: SemanticGraph,
    file_path: str,
    payload: Any,
    *,
    scope: str = "global",
) -> None:
    relationships = payload if isinstance(payload, list) else payload.get("relationships")
    if isinstance(relationships, dict):
        items = relationships.items()
    elif isinstance(relationships, list):
        items = [
            (_relationship_name(item, index), item)
            for index, item in enumerate(relationships)
            if isinstance(item, dict)
        ]
    else:
        return
    for name, relationship in items:
        if isinstance(relationship, dict):
            display_name = str(name)
            identity = f"{scope}:{display_name}"
            duplicate = 2
            while identity in graph.relationships:
                identity = f"{scope}:{display_name}#{duplicate}"
                duplicate += 1
            graph.relationships[identity] = {
                "file": file_path,
                "name": display_name,
                "scope": scope,
                **relationship,
            }


def _relationship_name(item: dict[str, Any], index: int) -> str:
    explicit = item.get("name") or item.get("join_to")
    if explicit:
        return str(explicit)
    source = item.get("join_from_view") or item.get("from_view") or "unknown"
    target = item.get("join_to_view") or item.get("to_view") or "unknown"
    source_alias = item.get("join_from_view_as") or ""
    target_alias = item.get("join_to_view_as") or ""
    if source != "unknown" or target != "unknown":
        return f"{source}:{source_alias}->{target}:{target_alias}"
    return f"unnamed:{index}"


def _add_inline_topics(graph: SemanticGraph, file_path: str, payload: dict[str, Any]) -> None:
    topics = payload.get("topics")
    if isinstance(topics, dict):
        items = topics.items()
    elif isinstance(topics, list):
        items = [
            (item.get("name") or item.get("base_view") or str(index), item)
            for index, item in enumerate(topics)
            if isinstance(item, dict)
        ]
    else:
        return
    for name, topic in items:
        if isinstance(topic, dict):
            graph.topics[str(name)] = {"file": file_path, "name": str(name), **topic}
