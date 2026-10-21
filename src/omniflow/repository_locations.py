"""Optional repository locations from an already Git-verified authored snapshot.

This module performs no IO and establishes no Git or tenant trust itself. Callers
must supply the verified snapshot for the finding's actual branch/base scope.
Only authored identifiers and source-key locations leave the index; values and
API prose never do. Conservative omissions are intentional, not DLP guarantees.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import yaml
from yaml.events import AliasEvent, MappingEndEvent, MappingStartEvent, SequenceEndEvent, SequenceStartEvent
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

from .diagnostic_catalog import CHANGE_MESSAGES, LINT_MESSAGES
from .model_paths import AmbiguousModelPath, authored_file_identity, filename_identity, identity_index
from .security import redact
from .semantic_files import infer_file_kind

MAX_FILES = 10_000
MAX_INVENTORY_FILES = 100_000
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 5 * 1024 * 1024
MAX_DEPTH = 32
MAX_EVENTS = 25_000
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}(?:\.[A-Za-z_][A-Za-z0-9_]{0,63}){0,3}\Z")
_SEGMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}\Z")
_SENSITIVE = re.compile(
    r"(?:^|[./ _-])(?:gh[opusr]_|github_pat_|sk[-_]|xox[baprs]-)[A-Za-z0-9_-]{16,}|"
    r"(?:^|[./ _-])(?:ssn|mrn|cvv|cvc|pan)[_-](?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]+",
    re.IGNORECASE,
)
# Paths can legitimately contain dates and multiple version numbers. Reject
# long numeric values (including common SSN/phone/card separators), not the
# sum of digits across unrelated directory names. This is a conservative
# metadata screen, not a promise to recognize every sensitive value.
_LONG_NUMBER = re.compile(r"(?<!\d)\d(?:[ _-]?\d){8,}(?!\d)")
_FIELD_GROUPS = frozenset({"fields", "dimensions", "measures", "filters"})
_SCALARS = frozenset(f"tag:yaml.org,2002:{kind}" for kind in ("str", "int", "float", "bool", "null"))
_MISSING_FIELD = re.compile(r"Field ([A-Za-z_][A-Za-z0-9_.]*) was not found\Z")
DIAGNOSTIC_MESSAGES = {
    **LINT_MESSAGES,
    **CHANGE_MESSAGES,
    "repository_validation_issue": (
        "Validation reported an issue at this repository location. Inspect the authored definition "
        "and the check guidance before making a change."
    ),
    "model_repository_issue": (
        "Model validation reported an issue at this repository location. Inspect the definition at this key "
        "and validate the same model branch in Omni to inspect the complete definition error."
    ),
    "content_repository_issue": (
        "Content validation reported an issue for this repository field. Inspect the authored definition "
        "and open Content Validator for the same model and branch to inspect affected content."
    ),
    "content_field_reference_unavailable": (
        "Content validation reported an unavailable field reference. Inspect the authored field and its "
        "dependencies, then rerun Content Validator for the same model and branch."
    ),
}
_LINT_CODES = frozenset(LINT_MESSAGES)


def _safe_text(value: Any, *, path_metadata: bool = False) -> bool:
    return (isinstance(value, str) and len(value) <= 512 and redact(value) == value
            and not _SENSITIVE.search(value)
            and (not _LONG_NUMBER.search(value) if path_metadata else sum(char.isdigit() for char in value) < 8))


def _identifier(value: Any) -> bool:
    return _safe_text(value) and _IDENTIFIER.fullmatch(value) is not None


def _path(value: Any) -> bool:
    # Paths are metadata in an independently authorized repository artifact,
    # not public output. Spaces/Unicode are legitimate; control characters,
    # traversal and URL/shell-like delimiters are not acceptable display paths.
    return (_safe_text(value, path_metadata=True) and len(value) <= 512
            and all(char.isalnum() or unicodedata.category(char)[0] == "M" or char in " /._-" for char in value)
            and all(unicodedata.category(char)[0] != "C" for char in value)
            and all(part not in {"", ".", ".."} and part == part.strip() for part in value.split("/")))


def _parse(text: str) -> MappingNode | SequenceNode | None:
    """Bound before composition; reject aliases, tags and ambiguous mapping keys."""
    try:
        if len(text.encode("utf-8")) > MAX_FILE_BYTES:
            return None
        depth = 0
        for count, event in enumerate(yaml.parse(text, Loader=yaml.SafeLoader), 1):
            if count > MAX_EVENTS or isinstance(event, AliasEvent) or getattr(event, "anchor", None) is not None:
                return None
            if getattr(event, "tag", None) is not None:
                return None
            if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
                depth += 1
                if depth > MAX_DEPTH:
                    return None
            elif isinstance(event, (MappingEndEvent, SequenceEndEvent)):
                depth -= 1
        root = yaml.compose(text, Loader=yaml.SafeLoader)
        if not isinstance(root, (MappingNode, SequenceNode)) or not _unambiguous(root):
            return None
        return root
    except (yaml.YAMLError, UnicodeError, RecursionError):
        return None


def _unambiguous(node: Any) -> bool:
    if isinstance(node, ScalarNode):
        return node.tag in _SCALARS
    if isinstance(node, SequenceNode):
        return node.tag == "tag:yaml.org,2002:seq" and all(_unambiguous(item) for item in node.value)
    if isinstance(node, MappingNode):
        if node.tag != "tag:yaml.org,2002:map":
            return False
        seen = set()
        for key, value in node.value:
            if not isinstance(key, ScalarNode) or key.tag != "tag:yaml.org,2002:str" or key.value in seen:
                return False
            seen.add(key.value)
            if not _unambiguous(value):
                return False
        return True
    return False


def _members(node: MappingNode) -> dict[str, tuple[ScalarNode, Any]]:
    return {key.value: (key, value) for key, value in node.value}


def _stem(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    for suffix in (".yaml", ".yml"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    for suffix in (".query.view", ".view", ".composite_topic", ".topic"):
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name


def _authored_kind(path: str, root: MappingNode) -> str | None:
    """Use authored structure, not a generic YAML filename's view fallback."""
    members = _members(root)
    shape = {}
    type_node = members.get("type", (None, None))[1]
    if isinstance(type_node, ScalarNode) and type_node.tag == "tag:yaml.org,2002:str":
        shape["type"] = type_node.value
    if "base_view" in members:
        shape["base_view"] = True  # Presence identifies topic shape; never retain its value.
    kind = infer_file_kind(path, shape)
    typed = path.rsplit("/", 1)[-1]
    for suffix in (".yaml", ".yml"):
        if typed.endswith(suffix):
            typed = typed[:-len(suffix)]
            break
    if typed in {"model", "relationships"} or typed.endswith((".view", ".topic", ".composite_topic", ".relationships")):
        return kind
    if shape.get("type") in {"model", "topic", "view"} or "base_view" in shape:
        return kind
    if "type" not in members and any(isinstance(members.get(group, (None, None))[1], MappingNode)
                                     for group in _FIELD_GROUPS):
        return "view"
    return None


@dataclass
class _File:
    path: str
    paths: dict[tuple[str, ...], int] = field(default_factory=dict)
    identities: dict[tuple[str, ...], dict[str, Any]] = field(default_factory=dict)


class SnapshotIndex:
    def __init__(self, model_path: str, files: dict[str, str], *,
                 api_aliases: dict[str, str] | None = None,
                 canonical_view_names: dict[str, str] | None = None):
        self._files: dict[str, _File] = {}
        self._references: dict[str, set[str]] = {}
        self._filename_references: dict[str, set[str]] = {}
        self._identities: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._canonical_views: dict[str, str] = {}
        self._model_path = model_path
        self._pending: dict[str, str] = {}
        self._parsed: set[str] = set()
        self._parse_success: set[str] = set()
        self._parse_failures: dict[str, str] = {}
        self._parsed_bytes = 0
        self._identity_complete = False
        self.unavailable_reason = "source_location_unmapped"
        self._initial_unavailable_reason: str | None = None
        if model_path != "." and not _path(model_path):
            self._initial_unavailable_reason = self.unavailable_reason = "unsupported_source_path"
            return
        if not isinstance(files, dict) or len(files) > MAX_INVENTORY_FILES:
            self._initial_unavailable_reason = self.unavailable_reason = "source_inventory_limit"
            return
        try:
            identity_index(path for path in files if _path(path))
        except AmbiguousModelPath:
            self._initial_unavailable_reason = self.unavailable_reason = "ambiguous_source_location"
            return
        for path, text in files.items():
            if not _path(path) or not isinstance(text, str):
                continue
            if (path.rsplit("/", 1)[-1] not in {"model", "relationships"}
                    and not path.endswith((".view", ".topic", ".composite_topic", ".relationships", ".yaml", ".yml"))):
                continue
            repo_path = path if model_path == "." else f"{model_path}/{path}"
            if not _path(repo_path):
                continue
            item = _File(repo_path)
            self._files[path] = item
            self._pending[path] = text
            identity = authored_file_identity(path)
            identity_repo_path = identity if model_path == "." else f"{model_path}/{identity}"
            # Aliases only identify this already verified Git file; source
            # coordinates always retain its original serialization suffix.
            for reference in {path, repo_path, identity, identity_repo_path}:
                self._references.setdefault(reference, set()).add(path)
            self._filename_references.setdefault(filename_identity(path), set()).add(path)
        # Callers supply these only after independent scope, inventory and byte
        # verification. API directory labels must not choose between duplicates.
        for reference, path in (api_aliases or {}).items():
            if path not in self._files or not _path(reference):
                continue
            identity = authored_file_identity(reference)
            for alias in {reference, identity}:
                self._references.setdefault(alias, set()).add(path)
                if model_path != ".":
                    self._references.setdefault(f"{model_path}/{alias}", set()).add(path)
        names_by_path: dict[str, list[str]] = {}
        for name, path in (canonical_view_names or {}).items():
            if path in self._files and isinstance(name, str):
                names_by_path.setdefault(path, []).append(name)
        self._canonical_views = {path: names[0] for path, names in names_by_path.items() if len(names) == 1}

    def _parse_file(self, path: str) -> bool:
        if path in self._parsed:
            if path in self._parse_failures:
                self.unavailable_reason = self._parse_failures[path]
            return path in self._parse_success
        text = self._pending[path]
        try:
            size = len(text.encode("utf-8"))
        except UnicodeError:
            self.unavailable_reason = "unsupported_source_yaml"
            return False
        if len(self._parsed) >= MAX_FILES or self._parsed_bytes + size > MAX_TOTAL_BYTES:
            self.unavailable_reason = "source_index_budget"
            return False
        self._parsed.add(path)
        self._parsed_bytes += size
        root = _parse(text)
        # Relationships are valid as a root sequence, but cannot define a view
        # identity. They must not invalidate identity coverage of other files.
        if isinstance(root, SequenceNode):
            identity = filename_identity(path)
            if identity == "relationships" or identity.endswith(".relationships"):
                self._parse_success.add(path)
                return True
            root = None
        if root is None:
            self.unavailable_reason = "unsupported_source_yaml"
            self._parse_failures[path] = self.unavailable_reason
            return False
        item = self._files[path]
        self._index_paths(item, root)
        default_name = _stem(path) if "/" not in path else None
        self._index_identities(item, root, _authored_kind(path, root), default_name,
                               canonical_name=self._canonical_views.get(path))
        self._parse_success.add(path)
        return True

    def _ensure_identities(self) -> bool:
        if self._identity_complete:
            return True
        # Identity-only lookup must inspect the full scope to rule out duplicates.
        # Exact file/key lookups remain useful even when this budget is exceeded.
        if len(self._pending) > MAX_FILES:
            self.unavailable_reason = "source_index_budget"
            return False
        for path in self._pending:
            if not self._parse_file(path):
                return False
        self._identity_complete = True
        return True

    def _index_paths(self, item: _File, node: MappingNode, prefix: tuple[str, ...] = ()) -> None:
        for key, value in node.value:
            if not _identifier(key.value):
                continue
            path = (*prefix, key.value)
            item.paths[path] = key.start_mark.line + 1
            if isinstance(value, MappingNode):
                self._index_paths(item, value, path)

    def _record(self, item: _File, path: tuple[str, ...], kind: str, name: str, line: int, **extra) -> None:
        if not _identifier(name):
            return
        location = {"file": item.path, "line": line, kind: name, **extra}
        item.identities[path] = location
        self._identities.setdefault((kind, name), []).append(location)

    def _index_identities(self, item: _File, root: MappingNode, kind: str | None, default_name: str | None,
                          *, canonical_name: str | None = None) -> None:
        members = _members(root)
        if kind == "model":
            topics = members.get("topics", (None, None))[1]
            if isinstance(topics, MappingNode):
                for key, definition in topics.value:
                    if isinstance(definition, MappingNode):
                        self._record(item, ("topics", key.value), "topic", key.value, key.start_mark.line + 1)
            return
        if kind not in {"view", "topic"}:
            return
        if any(key in members and (not isinstance(members[key][1], ScalarNode)
                                   or members[key][1].tag != "tag:yaml.org,2002:str") for key in ("name", kind)):
            return
        name_values = [value.value for key in ("name", kind) if key in members
                       for value in [members[key][1]] if isinstance(value, ScalarNode)]
        name = canonical_name if kind == "view" and canonical_name is not None else (
            name_values[0] if name_values else default_name
        )
        if (canonical_name is None and len(set(name_values)) > 1) or not _identifier(name):
            return
        line = next((members[key][0].start_mark.line + 1 for key in ("name", kind) if key in members),
                    root.start_mark.line + 1)
        if kind in {"view", "topic"}:
            self._record(item, (), kind, name, line)
        if kind == "view":
            for group in _FIELD_GROUPS:
                value = members.get(group, (None, None))[1]
                if isinstance(value, MappingNode):
                    for key, definition in value.value:
                        if _identifier(key.value) and "." not in key.value and isinstance(definition, MappingNode):
                            self._record(item, (group, key.value), "field", f"{name}.{key.value}",
                                         key.start_mark.line + 1, view=name)
    def _file(self, reference: Any) -> _File | None:
        if not _path(reference):
            self.unavailable_reason = "unsupported_source_path"
            return None
        # Prefer exact Git/verified API aliases. A filename-only fallback is
        # permitted only when unique across the complete model snapshot.
        references = {reference, authored_file_identity(reference)}
        prefix = f"{self._model_path}/"
        if self._model_path != "." and reference.startswith(prefix):
            references.add(prefix + authored_file_identity(reference[len(prefix):]))
        matches = set().union(*(self._references.get(path, set()) for path in references))
        if not matches:
            matches = self._filename_references.get(filename_identity(reference), set())
        if len(matches) != 1:
            self.unavailable_reason = "ambiguous_source_location" if matches else "source_location_unmapped"
            return None
        path = next(iter(matches))
        return self._files[path] if self._parse_file(path) else None

    def _identity(self, kind: str, name: Any) -> dict[str, Any] | None:
        if not _identifier(name) or not self._ensure_identities():
            return None
        matches = self._identities.get((kind, name), [])
        if kind == "field" and len(matches) == 1 and len(self._identities.get(("view", matches[0]["view"]), [])) != 1:
            return None
        return matches[0] if len(matches) == 1 else None

    def detail(self, issue: dict) -> dict:
        """Return only independently mapped authored metadata and fixed wording."""
        if not isinstance(issue, dict):
            return {}
        if self._initial_unavailable_reason is not None:
            self.unavailable_reason = self._initial_unavailable_reason
            return {}
        if self._files:
            self.unavailable_reason = "source_location_unmapped"
        selected = None
        if issue.get("file") is not None:
            selected = self._file(issue["file"])
            if selected is None:
                return {}
        location = {"file": selected.path} if selected else {}
        exact_key_line = None
        yaml_path = issue.get("yaml_path")
        if yaml_path is not None:
            if not isinstance(yaml_path, str) or len(yaml_path) > 1024:
                return {}
            reference, *keys = yaml_path.split(",")
            mapped = self._file(reference)
            if mapped is None or (selected is not None and selected is not mapped):
                return {}
            selected = mapped
            location = {"file": selected.path}
            if keys and all(_identifier(key) for key in keys) and tuple(keys) in selected.paths:
                exact_key_line = selected.paths[tuple(keys)]
                location["line"] = exact_key_line
                for length in range(len(keys), -1, -1):
                    candidate = selected.identities.get(tuple(keys[:length]))
                    if candidate:
                        for kind in ("field", "view", "topic"):
                            if kind in candidate and self._identity(kind, candidate[kind]) is not None:
                                location[kind] = candidate[kind]
                        break
        validator = issue.get("validator")
        known_missing = False
        supplied = {kind: issue[kind] for kind in ("field", "view", "topic") if kind in issue}
        if validator == "content" and not supplied:
            raw = issue.get("raw")
            if isinstance(raw, dict) and "field" in raw:
                supplied = {"field": raw["field"]}
            else:
                # Normalized reports may prefix a dashboard title to `message`.
                # Inspect the intact raw message as another exact format, never
                # strip arbitrary prefixes or extract tokens from free prose.
                messages = [issue.get("message"), raw.get("message") if isinstance(raw, dict) else None]
                matches = {match[1] for message in messages if isinstance(message, str) and len(message) < 512
                           for match in [_MISSING_FIELD.fullmatch(message)] if match is not None}
                if len(matches) > 1:
                    return {}
                if matches:
                    supplied = {"field": next(iter(matches))}
                    known_missing = True
        for kind, name in supplied.items():
            mapped = self._identity(kind, name)
            if mapped is None or (selected is not None and mapped["file"] != selected.path):
                return {}
            if location.get(kind) not in (None, name):
                return {}
            if "file" in location and mapped["file"] != location["file"]:
                return {}
            # A field is more precise than its containing view.
            if "field" not in location or kind == "field":
                location.update(mapped)
        if not location:
            return {}
        if exact_key_line is not None:
            # Structured field/view metadata identifies the definition, but
            # must not replace an exact property-key location (for example sql).
            location["line"] = exact_key_line
        code = "repository_validation_issue"
        if (validator == "semantic_lint" and isinstance(issue.get("rule_id"), str)
                and issue["rule_id"] in _LINT_CODES):
            code = issue["rule_id"]
        elif (validator in ("contracts", "semantic_diff") and isinstance(issue.get("type"), str)
              and issue["type"] in CHANGE_MESSAGES):
            code = issue["type"]
        elif validator == "model":
            code = "model_repository_issue"
        elif validator == "content" and "field" in location:
            code = "content_field_reference_unavailable" if known_missing else "content_repository_issue"
        return {**location, "diagnostic_code": code, "message": DIAGNOSTIC_MESSAGES[code]}
