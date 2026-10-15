"""Conservative, read-only classification of an immutable PR's YAML changes.

This is explanatory evidence, never permission to skip Content Validator. Git
snapshots cannot establish the state of attached drafts or inherited live
content. Every result therefore requires normal content validation.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode
from yaml.tokens import AliasToken, AnchorToken, DirectiveToken, TagToken

from .candidate import (
    MAX_TREE_ENTRIES,
    _bounded_git,
    _GitUnavailable,
    _local_tree,
    _safe_path,
    _semantic_inventory,
)
from .discovery import ModelContext
from .exceptions import OmniFlowError
from .revision_data import FULL_SHA, pull_request_revision
from .yaml_security import MAX_YAML_FILE_BYTES, MAX_YAML_TOTAL_BYTES, parse_secure_yaml

_MODEL_SUFFIXES = {".view", ".topic", ".composite_topic", ".relationships"}
_PROTECTED_NAMES = {"dbt_project.yml", "dbt_project.yaml", "packages.yml", "packages.yaml",
                    "dependencies.yml", "dependencies.yaml", "profiles.yml", "profiles.yaml"}
_DEPENDENCY_KEYS = {"extends", "extend", "extension", "extensions", "include", "includes",
                    "import", "imports", "inherit", "inherits", "inheritance", "dependencies",
                    "dependency", "parent_model", "base_model", "shared_model", "dbt"}
_SCALAR_TAGS = {f"tag:yaml.org,2002:{name}" for name in ("str", "int", "float", "bool", "null")}


class _Unverified(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


def _result(classification: str, reason: str) -> dict[str, Any]:
    # Do not export paths, authored content, hashes or direct resource IDs.
    return {"classification": classification, "reason_code": reason, "content_validation_required": True}


def classify_pr_change(contexts: list[ModelContext]) -> dict[str, Any]:
    """Classify the entire PR, not an independently skippable model subset.

    Both exact event commits must be locally available. We neither fetch nor
    check out PR code. All changed paths must belong to supplied model contexts;
    changes outside them remain unverified. A direct base/head comparison is
    deliberately conservative for branches behind their base.
    """
    try:
        base = pull_request_revision("base")
        head = pull_request_revision("head")
        if base is None or head is None:
            return _result("unverified", "missing_pr_revision")
        if not contexts:
            return _result("unverified", "missing_model_scope")
        root = Path(".")
        before = _tree(base[0], root=root)
        after = _tree(head[0], root=root)
        if set(before) != set(after):
            return _result("meaningful", "file_inventory_changed")
        if any((before[path]["type"], before[path]["mode"]) !=
               (after[path]["type"], after[path]["mode"]) for path in before):
            return _result("meaningful", "file_type_or_mode_changed")
        changed = {path for path in before if before[path]["sha"] != after[path]["sha"]}
        if not changed:
            return _result("unverified", "no_changed_files")

        relevant: dict[str, dict] = {}
        for context in contexts:
            scope = context.model_path
            scope = "." if scope == "." else _safe_path(scope)
            inventory = _semantic_inventory(list(before.values()), scope)
            if not inventory:
                return _result("unverified", "empty_model_inventory")
            relevant.update({entry["path"]: entry for entry in inventory.values()})
        if any(path not in relevant or not _comment_candidate(path) for path in changed):
            return _result("unverified", "unsupported_changed_path")

        # Inspect the complete authored model inventory, including unchanged
        # files, so inheritance/dependency or ambiguous YAML is not hidden by
        # a seemingly harmless edit to a different file.
        total = sum(entry["size"] for entry in relevant.values())
        total += sum(after[path]["size"] for path in changed)
        if total > MAX_YAML_TOTAL_BYTES:
            return _result("unverified", "snapshot_limit_exceeded")
        signatures: dict[str, tuple] = {}
        for path, entry in relevant.items():
            signatures[path] = _yaml_signature(_blob(entry, root=root))
        for path in sorted(changed):
            if signatures[path] != _yaml_signature(_blob(after[path], root=root)):
                return _result("meaningful", "authored_content_changed")
        return _result("comment_only", "live_content_provenance_unavailable")
    except _Unverified as exc:
        return _result("unverified", exc.reason)
    except (OmniFlowError, _GitUnavailable, UnicodeError, ValueError, TypeError, AttributeError):
        return _result("unverified", "snapshot_unavailable")


def _tree(sha: str, *, root: Path) -> dict[str, dict]:
    entries = _local_tree(sha, root=root)
    if len(entries) > MAX_TREE_ENTRIES:
        raise _Unverified("snapshot_limit_exceeded")
    result: dict[str, dict] = {}
    for entry in entries:
        path = _safe_path(entry.get("path"))
        if path in result or not FULL_SHA.fullmatch(str(entry.get("sha", ""))):
            raise _Unverified("invalid_git_inventory")
        if entry.get("type") == "blob" and (
            type(entry.get("size")) is not int or entry["size"] < 0
        ):
            raise _Unverified("invalid_git_inventory")
        result[path] = entry
    return result


def _comment_candidate(path: str) -> bool:
    value = PurePosixPath(path)
    if any(part.startswith(".") for part in value.parts) or value.name in _PROTECTED_NAMES:
        return False
    # Generic .yaml/.yml is inventoried, but never guessed to be an Omni model
    # edit: it may instead be workflow, policy, dbt or other executable config.
    return value.name in {"model", "relationships"} or value.suffix in _MODEL_SUFFIXES


def _blob(entry: dict, *, root: Path) -> str:
    if entry["type"] != "blob" or entry["mode"] not in {"100644", "100755"}:
        raise _Unverified("unsupported_git_entry")
    if entry["size"] > MAX_YAML_FILE_BYTES:
        raise _Unverified("snapshot_limit_exceeded")
    # Read independently inventoried object IDs with Git replacements disabled.
    raw = _bounded_git(["cat-file", "blob", entry["sha"]], root=root, limit=MAX_YAML_FILE_BYTES)
    if len(raw) != entry["size"]:
        raise _Unverified("incomplete_git_snapshot")
    return raw.decode("utf-8")


def _yaml_signature(text: str) -> tuple:
    try:
        # Shared limits bound file size, nesting and parser work. Construction
        # does not define equality: coercions would hide authored differences.
        parse_secure_yaml(text, source="PR snapshot")
        tokens = list(yaml.scan(text, Loader=yaml.SafeLoader))
        if any(isinstance(token, (AliasToken, AnchorToken, DirectiveToken, TagToken)) for token in tokens):
            raise _Unverified("unsupported_yaml_construct")
        node = yaml.compose(text, Loader=yaml.SafeLoader)
        if node is None:
            raise _Unverified("empty_yaml_document")
        _validate_node(node)
        # YAML's scanner, not regex comment stripping, identifies actual scalar
        # spans. Retain exact authored scalar bytes, types, order and delimiters;
        # a '#' inside SQL, a quoted value or a block is never discarded.
        return tuple((type(token).__name__, getattr(token, "value", None),
                      getattr(token, "style", None),
                      text[token.start_mark.index:token.end_mark.index]) for token in tokens)
    except (yaml.YAMLError, RecursionError, OmniFlowError):
        raise _Unverified("invalid_or_unsupported_yaml") from None


def _validate_node(node: Any) -> None:
    if isinstance(node, MappingNode):
        seen: set[str] = set()
        for key, value in node.value:
            if not isinstance(key, ScalarNode) or key.tag != "tag:yaml.org,2002:str":
                raise _Unverified("ambiguous_yaml_key")
            if key.value in seen:
                raise _Unverified("duplicate_yaml_key")
            seen.add(key.value)
            if key.value.casefold() in _DEPENDENCY_KEYS:
                raise _Unverified("external_dependency_unverified")
            _validate_node(value)
    elif isinstance(node, SequenceNode):
        for child in node.value:
            _validate_node(child)
    elif not isinstance(node, ScalarNode) or node.tag not in _SCALAR_TAGS:
        raise _Unverified("unsupported_yaml_construct")
    elif any(marker in node.value for marker in ("{{", "{%")):
        raise _Unverified("external_dependency_unverified")
