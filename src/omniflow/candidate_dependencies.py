"""Resolve a bounded set of authored dependencies for a changed Omni view or topic."""

from __future__ import annotations

import re
from collections import defaultdict

from .artifact_identity import _canonical_stem
from .candidate_change_scope import _structure
from .model_paths import filename_identity, is_git_model_file

# This is a deliberately small grammar, not an Omni compiler. Other forms must
# pass the whole-model verifier; unrecognized keys are never assumed to be local.
_LOCAL_SCALARS = {
    "type", "label", "description", "primary_key", "hidden", "format", "table_name", "schema",
    "catalog", "data_type", "aggregate_type", "group_label", "label_prefix", "ignored",
}
_FIELD_GROUPS = {"dimensions", "measures", "fields"}
_IDENT = r"[a-zA-Z_][a-zA-Z_0-9]*"
_REFERENCE = re.compile(r"\$\{(" + _IDENT + r")(?:\.(" + _IDENT + r"))?\}")
# Plain qualified names and SQL query blocks can depend on external relations or
# Omni table macros; only prove a bounded scalar expression with known references.
_COMPLEX_SQL = re.compile(r"\b(?:FROM|JOIN|WITH|UNION|CALL|EXECUTE)\b|\b\w+\s*\.\s*\w|[{}$;@]", re.I)


class _FullModel(Exception):
    """The narrow grammar cannot establish all dependencies."""


def _sql_refs(value: str) -> set[str]:
    refs = set()
    for match in _REFERENCE.finditer(value):
        owner, name = match.groups()
        if owner == "TABLE" and name is None:
            continue
        if name is None or owner == "TABLE":
            raise _FullModel
        refs.add(owner)
    if _COMPLEX_SQL.search(_REFERENCE.sub(" ", value)):
        raise _FullModel
    return refs


def _property_refs(node: tuple, *, kind: str, field: bool = False) -> set[str]:
    if node[0] != "mapping":
        raise _FullModel
    refs: set[str] = set()
    for key, child in node[1]:
        if key in _FIELD_GROUPS and not field and kind == "view" and child[0] == "mapping":
            for _, definition in child[1]:
                refs.update(_property_refs(definition, kind=kind, field=True))
            continue
        if child[0] != "scalar":
            raise _FullModel
        value = child[2]
        if key == "base_view" and kind == "topic" and re.fullmatch(_IDENT, value):
            refs.add(value)
        elif key == "connection" and kind == "model" and re.fullmatch(_IDENT, value):
            continue  # Literal connection on a global definition that must itself match Git.
        elif key == "sql":
            refs.update(_sql_refs(value))
        elif key not in _LOCAL_SCALARS or any(marker in value for marker in ("${", "{{", "{%", "{#")):
            raise _FullModel
    return refs


def _global_kind(path: str) -> str | None:
    name = filename_identity(path).removesuffix(".yaml").removesuffix(".yml")
    if name == "model" or name.endswith(".model"):
        return "model"
    if name == "relationships" or name.endswith(".relationships"):
        return "relationships"
    return None


def _relationship_edges(node: tuple) -> list[set[str]]:
    # Only simple explicit, unaliased joins can be attributed to particular
    # endpoints. Automatic, aliased or dynamic joins require the full model.
    if node[0] == "mapping" and dict(node[1]).keys() == {"relationships"}:
        node = dict(node[1])["relationships"]
    if node[0] == "sequence":
        rows = node[1]
    elif node[0] == "mapping":
        rows = [value for _, value in node[1]]
    else:
        raise _FullModel
    edges = []
    for row in rows:
        if row[0] != "mapping":
            raise _FullModel
        values = dict(row[1])
        if any(value[0] != "scalar" or key not in {
            "join_from_view", "join_to_view", "on_sql", "relationship_type", "join_type", "name", "reversible",
        } for key, value in values.items()):
            raise _FullModel
        endpoints = {values[k][2] for k in ("join_from_view", "join_to_view") if k in values}
        if len(endpoints) != 2 or not all(re.fullmatch(_IDENT, name) for name in endpoints):
            raise _FullModel
        if "on_sql" in values and not _sql_refs(values["on_sql"][2]) <= endpoints:
            raise _FullModel
        edges.append(endpoints)
    return edges


def referenced_files(
    start: set[str], api: dict[str, bytes], git: dict[str, dict], view_names: dict[str, str] | None,
) -> tuple[set[str], str | None]:
    """Identify live references; every selected file must subsequently match its immutable Git source.

    Unknown schema keys, query bodies, joins, relationships, inheritance and model-wide rules require
    the existing exact whole-model comparison. The name index covers the entire model before narrowing.
    """
    lookup: dict[str, set[str]] = defaultdict(set)
    possible_view_files = False
    for path in api:
        identity = filename_identity(path)
        if identity.endswith(".view"):
            lookup[_canonical_stem(path)].add(path)
        possible_view_files |= identity.endswith(".view") or not is_git_model_file(path)
    for name, path in (view_names or {}).items():
        lookup[name].add(path)
    possible_view_files |= any(filename_identity(p).endswith(".view") or not is_git_model_file(p) for p in git)
    names_by_path: dict[str, set[str]] = defaultdict(set)
    for name, paths in lookup.items():
        for path in paths:
            names_by_path[path].add(name)
    globals_by_name = {
        filename_identity(p) for files in (api, git) for p in files if _global_kind(p)
    }
    api_globals = {p for p in api if _global_kind(p)}
    graph: dict[str, set[str]] = defaultdict(set)
    globals_read = False
    seen, required = set(), set()
    queue = list(start)
    parsed_bytes = 0

    def parse(path):
        nonlocal parsed_bytes
        parsed_bytes += len(api[path])
        if parsed_bytes > 4 * 1024 * 1024:
            raise _FullModel
        node = _structure(api[path].decode("utf-8"))
        if node is None:
            raise _FullModel
        return node

    def include_globals():
        # A Git-only or ambiguously matched rule source must never disappear.
        if globals_by_name != {filename_identity(p) for p in api_globals} or len(api_globals) > 128:
            raise _FullModel
        for path in api_globals:
            node = parse(path)
            if _global_kind(path) == "model":
                if node != ("empty-document",) and _property_refs(node, kind="model"):
                    raise _FullModel  # A model-wide expression can affect every view.
            else:
                for left, right in _relationship_edges(node):
                    graph[left].add(right)
                    graph[right].add(left)
        required.update(api_globals)

    try:
        while queue:
            path = queue.pop()
            if path in seen:
                continue
            seen.add(path)
            if len(seen) > 128:
                return set(), "dependency_scope_limit"
            identity = filename_identity(path)
            if identity.endswith(".view"):
                kind = "view"
            elif identity.endswith(".topic") and not identity.endswith(".composite_topic"):
                kind = "topic"
            else:
                raise _FullModel
            refs = _property_refs(parse(path), kind=kind)
            if refs and possible_view_files and not globals_read:
                include_globals()
                globals_read = True
            if globals_read:
                # Explicitly reachable global joins can introduce intermediate
                # views. Walk only their connected component.
                for name in refs | names_by_path.get(path, set()):
                    refs.update(graph.get(name, ()))
            for name in refs:
                choices = lookup.get(name, set())
                if not choices and not possible_view_files:
                    # No view exists on either side. The live validators can now
                    # report the unresolved reference against the same inventory.
                    continue
                if len(choices) != 1:
                    return set(), "dependency_identity_unresolved"
                target = next(iter(choices))
                required.add(target)
                queue.append(target)
    except _FullModel:
        return set(), "dependency_scope_unresolved"
    return required, None
