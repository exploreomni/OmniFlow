"""Affected artifact selection and conservative YAML structural correspondence.

This evidence is deliberately narrower than a complete Git/Omni snapshot.
Paths are immutable PR comparison data, never glob patterns or commands.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode
from yaml.tokens import AliasToken, AnchorToken, DirectiveToken, TagToken

from .artifact_identity import _canonical_stem
from .exceptions import ConfigError, OmniFlowError
from .model_paths import (
    authored_file_identity,
    filename_identity,
    is_git_model_file,
    is_repo_control_file,
    is_semantic_file,
)
from .view_identity import ViewNamesMetadataError, normalize_api_view_names, validate_view_names
from .yaml_security import parse_secure_yaml

MAX_SCOPE_PATHS = 10_000
MAX_SEMANTIC_COMPARE_BYTES = 512 * 1024


@dataclass
class ChangeScope:
    touched: set[str]
    git: dict[str, dict]
    api: dict[str, bytes]
    view_names: dict[str, str] | None
    unqualified_absences: set[str]


def _canonical_names(raw: Any, api: dict[str, bytes]) -> dict[str, str]:
    """Check the entire metadata orientation/bijection without parsing unrelated file bodies."""
    if (not isinstance(raw, dict) or len(raw) > len(api)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in raw.items())):
        raise ViewNamesMetadataError("invalid_mapping")
    keys_are_paths, values_are_paths = all(k in api for k in raw), all(v in api for v in raw.values())
    if raw and keys_are_paths and values_are_paths:
        raise ViewNamesMetadataError("ambiguous_orientation")
    if not keys_are_paths and not values_are_paths:
        raise ViewNamesMetadataError("unsupported_orientation")
    if keys_are_paths:
        names = {}
        for path, name in raw.items():
            if not name:
                if filename_identity(path).endswith(".view"):
                    raise ViewNamesMetadataError("incomplete_identity")
                continue
            if name in names:
                raise ViewNamesMetadataError("duplicate_name")
            names[name] = path
    else:
        names = dict(raw)
    if len(set(names.values())) != len(names):
        raise ViewNamesMetadataError("duplicate_path")
    covered = set(names.values())
    if any(filename_identity(p).endswith(".view") and p not in covered for p in api):
        raise ViewNamesMetadataError("incomplete_identity")
    if any(is_git_model_file(p) and not filename_identity(p).endswith(".view") for p in covered):
        raise ViewNamesMetadataError("non_view_identity")
    try:
        return validate_view_names(names, api)
    except ConfigError as exc:
        raise ViewNamesMetadataError("invalid_identity") from exc


def _identity_keys(path: str) -> set[tuple[str, str]]:
    name = filename_identity(path)
    return {("name", name), ("view", _canonical_stem(path))} if name.endswith(".view") else {("name", name)}


def select_scope(changed_paths: Sequence[str], model_path: str, git: dict[str, dict],
                 api: dict[str, bytes], payload: dict) -> ChangeScope:
    prefix = "" if model_path == "." else model_path + "/"
    if (not isinstance(changed_paths, Sequence) or isinstance(changed_paths, (str, bytes))
            or len(changed_paths) > MAX_SCOPE_PATHS):
        raise ConfigError("Expected a bounded immutable changed-file inventory.", reason_code="changed_files_unavailable")
    api_names = {filename_identity(path) for path in api}
    touched = set()
    for path in changed_paths:
        if (not isinstance(path, str) or not path or len(path) > 1024 or path != path.strip()
                or any(not c.isprintable() for c in path) or "\\" in path or ":" in path
                or PurePosixPath(path).as_posix() != path or path.startswith("/") or path == "."
                or ".." in PurePosixPath(path).parts):
            raise ConfigError("Invalid immutable changed-file path.", reason_code="changed_files_unavailable")
        if is_repo_control_file(path) or not path.startswith(prefix):
            continue
        relative = path[len(prefix):]
        if is_git_model_file(relative) or (is_semantic_file(relative) and filename_identity(relative) in api_names):
            touched.add(relative)

    # Expand complete filename and canonical-view collision groups. In
    # particular, a deleted Git path is still a seed even though it cannot be
    # present in this side's tree. An unreflected Omni deletion must not vanish.
    names = _canonical_names(payload["viewNames"], api) if "viewNames" in payload else None
    reverse = {path: name for name, path in (names or {}).items()}
    edges: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    def keys(path, *, omni=False):
        result = _identity_keys(path)
        if omni and path in reverse:
            result.add(("view", reverse[path]))
        return result
    for paths, omni in ((git, False), (api, True)):
        for path in paths:
            group = keys(path, omni=omni)
            for key in group:
                edges[key].update(group)
    connected = set()
    pending = [key for path in touched for key in _identity_keys(path)]
    while pending:
        key = pending.pop()
        if key not in connected:
            connected.add(key)
            pending.extend(edges[key] - connected)
    selected_git = {p: e for p, e in git.items() if keys(p) & connected}
    selected_api = {p: v for p, v in api.items() if keys(p, omni=True) & connected}
    view_names = None
    if names is not None:
        texts = {p: raw.decode("utf-8") for p, raw in selected_api.items()}
        # Recheck only selected file kinds and completeness. Full metadata
        # orientation was independently verified before narrowing the set.
        view_names = normalize_api_view_names({n: p for n, p in names.items() if p in texts}, texts, parse_contents=False)
    absent_views = {filename_identity(p) for p in touched - selected_git.keys() if filename_identity(p).endswith(".view")}
    unqualified = bool(absent_views) and any(
        (names is None and filename_identity(p).endswith(".view") and filename_identity(p) not in absent_views)
        or (not is_git_model_file(p) and p not in reverse)
        for p in api
    )
    return ChangeScope(touched, selected_git, selected_api, view_names, absent_views if unqualified else set())


def unproven_absence(scope: ChangeScope, methods: dict[str, str], other_inventory: Callable[[], set[str]]) -> bool:
    """Distinguish an actual one-to-one move from dropping a colliding sibling."""
    absent = scope.touched - scope.git.keys()
    names = {filename_identity(p) for p in absent if filename_identity(p).endswith(".view")}
    unresolved = set(scope.unqualified_absences)
    unresolved.update(filename_identity(p) for p, method in methods.items()
                      if filename_identity(p) in names and method != "authored_path")
    other = None
    for name in unresolved:
        present = [p for p in scope.git if filename_identity(p) == name]
        missing = [p for p in absent if filename_identity(p) == name]
        if len(present) != 1 or len(missing) != 1 or present[0] not in scope.touched:
            return True
        if authored_file_identity(present[0]) == authored_file_identity(missing[0]):
            continue  # Same directory + exact name after removing a permitted wrapper.
        if other is None:
            other = other_inventory()
        if {p for p in other if filename_identity(p) == name} != set(missing):
            return True  # Modifying B while deleting same-name A is not moving A to B.
    return False


def _node_identity(node) -> tuple:
    if isinstance(node, ScalarNode) and node.tag in {
        "tag:yaml.org,2002:" + t for t in ("str", "int", "bool", "float", "null")
    }:
        # Compare typed scalar values, including resolved block indentation,
        # contents, escapes and chomping. Python bool/int coercion is not proof.
        return ("scalar", node.tag, node.value)
    if isinstance(node, SequenceNode) and node.tag == "tag:yaml.org,2002:seq":
        return ("sequence", tuple(_node_identity(item) for item in node.value))
    if isinstance(node, MappingNode) and node.tag == "tag:yaml.org,2002:map":
        pairs, seen = [], set()
        for key, val in node.value:
            if (not isinstance(key, ScalarNode) or key.tag != "tag:yaml.org,2002:str"
                    or key.value in seen or key.value == "<<"):
                raise ValueError()
            seen.add(key.value)
            pairs.append((key.value, _node_identity(val)))
        # Field/key order can matter to consumers; do not invent equivalence.
        return ("mapping", tuple(pairs))
    raise ValueError()


def _structure(text: str) -> tuple | None:
    if len(text.encode("utf-8")) > MAX_SEMANTIC_COMPARE_BYTES:
        return None
    try:
        parse_secure_yaml(text, source="changed Omni artifact")
        if any(isinstance(token, (AliasToken, AnchorToken, DirectiveToken, TagToken))
               for token in yaml.scan(text, Loader=yaml.SafeLoader)):
            return None
        node = yaml.compose(text, Loader=yaml.SafeLoader)
        return _node_identity(node) if node is not None else ("empty-document",)
    except (OmniFlowError, yaml.YAMLError, ValueError, TypeError, RecursionError, OverflowError):
        return None


def equivalent(git: str, omni: str) -> bool:
    if git == omni:
        return True
    before = _structure(git)
    return before is not None and before == _structure(omni)


def scope_digest(scope: ChangeScope, pairs: dict[str, str], *, sha: str, side: str,
                 model_path: str, model_id: str, branch_id: str | None) -> str:
    # These are immutable Git identities, observed correspondences and
    # confirmed tombstones. This is NOT an assertion that all Omni bytes (or
    # even representational bytes of a matched file) equal the Git tree.
    bound = [sha, side, model_path, model_id, branch_id,
             sorted((p, scope.git[p]["sha"] if p in scope.git else None, pairs.get(p)) for p in scope.touched)]
    return hashlib.sha256(json.dumps(bound, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
