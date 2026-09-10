from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..exceptions import ConfigError
from ..view_identity import validate_view_names
from .yaml_loader import load_yaml_snapshot


@dataclass
class SemanticGraph:
    model: dict[str, Any] = field(default_factory=dict)
    views: dict[str, dict[str, Any]] = field(default_factory=dict)
    topics: dict[str, dict[str, Any]] = field(default_factory=dict)
    relationships: dict[str, dict[str, Any]] = field(default_factory=dict)
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    files: dict[str, Any] = field(default_factory=dict)


def load_yaml_graph(root: str | Path, *, require_view_names: bool = False) -> SemanticGraph:
    files, manifest = load_yaml_snapshot(root)
    view_names = manifest.get("view_names") if manifest is not None else None
    if manifest is not None and not isinstance(view_names, dict):
        raise ConfigError("YAML snapshot is missing canonical viewNames metadata; pull a fresh snapshot")
    if view_names is None and require_view_names:
        raise ConfigError("Missing canonical viewNames metadata; pull a fresh YAML snapshot")
    if view_names is None and any(
        "/" in path and _infer_kind(path, payload) == "view"
        and "__" not in _name(path, {}) and "." not in _name(path, {})
        for path, payload in files.items()
    ):
        raise ConfigError("Scoped view identity is unresolved; use a YAML pull snapshot with viewNames metadata")
    return build_graph(files, view_names=view_names, fully_resolved=bool(manifest and manifest.get("fully_resolved") is True))


def has_inheritance(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            (key == "extends" and isinstance(child, (str, list)) and bool(child)) or has_inheritance(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(has_inheritance(child) for child in value)
    return False


def build_graph(
    files: dict[str, Any], *, view_names: dict[str, str] | None = None, fully_resolved: bool = False,
) -> SemanticGraph:
    if not fully_resolved and has_inheritance(files):
        raise ConfigError("Unresolved inheritance prevents complete impact analysis; pull fully-resolved YAML")
    graph = SemanticGraph()
    names_by_path = {}
    if view_names is not None:
        names_by_path = {path: name for name, path in validate_view_names(view_names, files).items()}
        if any(_infer_kind(path, files[path]) != "view" for path in names_by_path):
            raise ConfigError("Omni viewNames metadata targets a non-view file")
    for file_path, payload in files.items():
        graph.files[file_path] = payload
        kind = _infer_kind(file_path, payload)
        if kind == "view" and view_names is not None:
            if file_path not in names_by_path or not isinstance(payload, dict):
                raise ConfigError("Incomplete canonical view identity coverage; refresh the YAML snapshot")
        if kind == "model":
            if isinstance(payload, dict):
                if graph.model:
                    raise ConfigError("Ambiguous semantic model identity across multiple model files")
                graph.model.update({"file": file_path, **payload})
                _add_inline_topics(graph, file_path, payload)
            continue
        if kind == "relationship":
            _add_relationships(graph, file_path, payload)
            continue
        if not isinstance(payload, dict):
            continue
        name = names_by_path.get(file_path) or _name(file_path, payload)
        if kind == "topic":
            _require_unique(graph.topics, name, kind="topic")
            graph.topics[name] = {"file": file_path, **payload}
            _add_relationships(graph, file_path, payload, scope=f"topic:{name}")
        else:
            _require_unique(graph.views, name, kind="view")
            graph.views[name] = {**payload, "file": file_path, "name": name}
            _add_fields(graph, file_path, name, payload)
            _add_relationships(graph, file_path, payload, scope=f"view:{name}")
    return graph


def _infer_kind(file_path: str, payload: Any) -> str:
    lower = file_path.lower()
    basename = lower.rsplit("/", 1)[-1]
    typed_name = basename
    for suffix in (".yaml", ".yml"):
        if typed_name.endswith(suffix):
            typed_name = typed_name[:-len(suffix)]
            break
    # Explicit Omni file types outrank reserved basenames and payload hints.
    if typed_name.endswith(".view"):
        return "view"
    if typed_name.endswith((".topic", ".composite_topic")):
        return "topic"
    if typed_name.endswith(".relationships"):
        return "relationship"
    stem = basename.rsplit(".", 1)[0]
    if stem == "model" or (isinstance(payload, dict) and payload.get("type") == "model"):
        return "model"
    if lower.endswith(".topic") or ".topic." in lower:
        return "topic"
    if lower.endswith(".composite_topic") or ".composite_topic." in lower:
        return "topic"
    if (
        stem == "relationships"
        or stem.endswith(".relationships")
        or lower.endswith(".relationships")
        or isinstance(payload, list)
    ):
        return "relationship"
    if isinstance(payload, dict) and (payload.get("type") == "topic" or "base_view" in payload):
        return "topic"
    return "view"


def _name(file_path: str, payload: dict[str, Any]) -> str:
    value = payload.get("name") or payload.get("view") or payload.get("topic")
    if isinstance(value, str) and value.strip():
        return value.strip()
    name = file_path.rsplit("/", 1)[-1]
    for suffix in (".yaml", ".yml"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    for suffix in (".query.view", ".view", ".topic", ".composite_topic"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name


def _require_unique(items: dict[str, Any], name: str, *, kind: str) -> None:
    if name in items:
        raise ConfigError(f"Ambiguous semantic {kind} identity; duplicate definitions cannot be compared safely")


def _iter_field_groups(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    groups = []
    for key, kind in (("fields", "field"), ("dimensions", "dimension"), ("measures", "measure"), ("filters", "filter")):
        value = payload.get(key)
        if isinstance(value, dict):
            groups.append((kind, value))
        elif isinstance(value, list):
            group = {}
            for item in value:
                if isinstance(item, dict) and item.get("name"):
                    name = str(item["name"])
                    _require_unique(group, name, kind="field")
                    group[name] = item
            groups.append((kind, group))
    return groups


def _add_fields(graph: SemanticGraph, file_path: str, view_name: str, payload: dict[str, Any]) -> None:
    for kind, group in _iter_field_groups(payload):
        for field_name, definition in group.items():
            if not isinstance(definition, dict):
                continue
            key = f"{view_name}.{field_name}"
            _require_unique(graph.fields, key, kind="field")
            graph.fields[key] = {
                **definition, "file": file_path, "view": view_name, "name": field_name, "field_kind": kind,
            }


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
            _require_unique(graph.topics, str(name), kind="topic")
            graph.topics[str(name)] = {"file": file_path, "name": str(name), **topic}
