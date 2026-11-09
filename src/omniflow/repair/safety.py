from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from ..config import AIRepairSettings
from ..exceptions import ConfigError, SecurityPolicyError
from ..model_yaml import validate_editable_yaml_file_name
from ..security import reject_secret_keys
from ..yaml_security import parse_secure_yaml
from .snapshot import ModelSnapshot

MAX_REPAIR_ISSUES = 20
SENSITIVE_KEYS = {
    "access_filter",
    "access_filters",
    "access_grant",
    "access_grants",
    "default_access_grants",
    "required_access_grants",
    "user_attribute",
    "user_attributes",
    "permission",
    "permissions",
    "role",
    "roles",
    "row_level_security",
    "security",
    "sql",
    "sql_on",
    "on_sql",
    "sql_where",
    "always_where",
    "default_filters",
    "filters",
    "hidden",
    "ignored",
    "owner",
    "owners",
    "connection",
    "catalog",
    "database",
    "schema",
    "table",
    "table_name",
    "allowed_values",
    "ai_context",
}
_REPAIR_SCALAR_TAGS = {f"tag:yaml.org,2002:{name}" for name in ("str", "bool", "int", "float", "null")}
SECRET_LIKE_VALUE_RE = re.compile(
    r"(?:Bearer\s+[A-Za-z0-9._~+/=-]+|OMNI_API_KEY\s*[:=]|github_pat_|gh[pousr]_|AKIA[A-Z0-9]{16})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SnapshotDiff:
    added_files: tuple[str, ...]
    removed_files: tuple[str, ...]
    modified_files: tuple[str, ...]
    changed_lines: int

    def report(self) -> dict[str, Any]:
        return {
            "added_files": list(self.added_files),
            "removed_files": list(self.removed_files),
            "modified_files": list(self.modified_files),
            "changed_file_count": len(set(self.added_files + self.removed_files + self.modified_files)),
            "changed_lines": self.changed_lines,
        }


def repair_target_files(validation_issues: list[dict[str, Any]], snapshot: ModelSnapshot) -> tuple[str, ...]:
    errors = [issue for issue in validation_issues if not bool(issue.get("is_warning"))]
    if not errors:
        return ()
    if len(errors) > MAX_REPAIR_ISSUES:
        raise SecurityPolicyError("AI repair supports at most 20 model validation errors per attempt")
    targets: set[str] = set()
    for issue in errors:
        yaml_path = issue.get("yaml_path")
        if not isinstance(yaml_path, str) or not yaml_path.strip():
            raise SecurityPolicyError("Every model validation error must identify an authored YAML file")
        file_name = _resolve_authored_file_name(yaml_path, snapshot)
        try:
            validate_editable_yaml_file_name(file_name)
        except ConfigError as exc:
            raise SecurityPolicyError(
                "A model validation error targets a file type that the documented Omni YAML API cannot restore"
            ) from exc
        targets.add(file_name)
    return tuple(sorted(targets))


def _resolve_authored_file_name(yaml_path: str, snapshot: ModelSnapshot) -> str:
    reference = yaml_path.split(",", 1)[0].strip()
    if reference in snapshot.files:
        return reference

    matches = sorted(file_name for file_name in snapshot.files if file_name.replace("/", "__") == reference)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise SecurityPolicyError("A model validation error maps ambiguously to multiple authored YAML files")

    if "/" not in reference:
        basename_matches = sorted(
            file_name for file_name in snapshot.files if file_name.rsplit("/", 1)[-1] == reference
        )
        if len(basename_matches) == 1:
            return basename_matches[0]
        if len(basename_matches) > 1:
            raise SecurityPolicyError("A model validation error maps ambiguously to multiple authored YAML files")
    raise SecurityPolicyError("A model validation error references an authored YAML file that was not fetched")


def compare_snapshots(before: ModelSnapshot, after: ModelSnapshot) -> SnapshotDiff:
    before_names = set(before.files)
    after_names = set(after.files)
    modified = sorted(
        name for name in before_names & after_names if before.files[name].sha256 != after.files[name].sha256
    )
    changed_lines = 0
    for name in modified:
        changed_lines += _changed_line_count(before.files[name].text, after.files[name].text)
    for name in sorted(after_names - before_names):
        changed_lines += len(after.files[name].text.splitlines())
    for name in sorted(before_names - after_names):
        changed_lines += len(before.files[name].text.splitlines())
    return SnapshotDiff(
        added_files=tuple(sorted(after_names - before_names)),
        removed_files=tuple(sorted(before_names - after_names)),
        modified_files=tuple(modified),
        changed_lines=changed_lines,
    )


def inspect_repair_change(
    *,
    before: ModelSnapshot,
    after: ModelSnapshot,
    allowed_files: tuple[str, ...],
    settings: AIRepairSettings,
) -> SnapshotDiff:
    difference = compare_snapshots(before, after)
    changed_files = set(difference.added_files + difference.removed_files + difference.modified_files)
    if not changed_files:
        raise SecurityPolicyError("Omni AI completed without changing authored YAML")
    if difference.added_files or difference.removed_files:
        raise SecurityPolicyError("Omni AI added or deleted authored YAML files; the repair was rejected")
    if not changed_files.issubset(set(allowed_files)):
        raise SecurityPolicyError("Omni AI changed authored YAML outside the files identified by validation")
    if len(changed_files) > settings.max_changed_files:
        raise SecurityPolicyError("Omni AI repair exceeded the configured changed-file limit")
    if difference.changed_lines > settings.max_changed_lines:
        raise SecurityPolicyError("Omni AI repair exceeded the configured changed-line limit")

    for file_name in difference.modified_files:
        before_protected, before_secrets = _repair_fields(before.files[file_name].text, file_name)
        after_protected, after_secrets = _repair_fields(after.files[file_name].text, file_name, proposed=True)
        if before_protected != after_protected:
            raise SecurityPolicyError(
                f"Omni AI repair changed security, governance, SQL, or data-source metadata in {file_name}"
            )
        if any(before_secrets.get(path) != value for path, value in after_secrets.items()):
            raise SecurityPolicyError(f"Omni AI repair introduced a secret-like value in {file_name}")
    return difference


def _changed_line_count(before: str, after: str) -> int:
    return sum(
        1
        for line in difflib.ndiff(before.splitlines(), after.splitlines())
        if line.startswith(("+ ", "- "))
    )


def _repair_fields(text: str, source: str, *, proposed: bool = False) -> tuple[dict, dict]:
    """Index protected subtrees and secret-like scalars by typed path before comparing revisions."""
    # Apply the shared size, depth, graph and YAML parser limits before composition.
    payload = parse_secure_yaml(text, source=source)
    if proposed:
        reject_secret_keys(payload, source=f"AI-repaired YAML file {source}")
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    if proposed and not isinstance(root, (MappingNode, SequenceNode)):
        raise SecurityPolicyError("AI repair requires an authored YAML mapping or sequence")

    protected_fields = {}
    secret_values = {}
    seen = set()
    prefix = "tag:yaml.org,2002:"

    def walk(node: Node, path: tuple[str | int, ...], protected: bool) -> None:
        # Aliased locations and non-standard tags may change how Omni resolves
        # the same authored path. Leave those files for a human to repair.
        if id(node) in seen:
            raise SecurityPolicyError("AI repair cannot verify YAML aliases")
        seen.add(id(node))
        if isinstance(node, MappingNode) and node.tag == prefix + "map":
            keys = []
            for key, _ in node.value:
                if not isinstance(key, ScalarNode) or key.tag != prefix + "str":
                    raise SecurityPolicyError("AI repair requires unique string YAML mapping keys")
                if id(key) in seen:
                    raise SecurityPolicyError("AI repair cannot verify YAML aliases")
                seen.add(id(key))
                keys.append(key.value)
            if len(set(keys)) != len(keys):
                raise SecurityPolicyError("AI repair requires unique string YAML mapping keys")
            if protected:
                protected_fields[path] = (node.tag, tuple(keys))
            for key, child in node.value:
                sensitive_key = key.value.lower().replace("-", "_") in SENSITIVE_KEYS
                walk(child, (*path, key.value), protected or sensitive_key)
        elif isinstance(node, SequenceNode) and node.tag == prefix + "seq":
            if protected:
                protected_fields[path] = (node.tag, len(node.value))
            for index, item in enumerate(node.value):
                walk(item, (*path, index), protected)
        elif isinstance(node, ScalarNode) and node.tag in _REPAIR_SCALAR_TAGS:
            # Keep the YAML type and value. Python coercion would equate True,
            # 1 and 1.0, including when they occur inside a protected list.
            value = (node.tag, node.value)
            if protected:
                protected_fields[path] = value
            if SECRET_LIKE_VALUE_RE.search(node.value):
                secret_values[path] = value
        else:
            raise SecurityPolicyError("AI repair cannot verify this YAML node type")

    if root is not None:
        walk(root, (), False)
    return protected_fields, secret_values
