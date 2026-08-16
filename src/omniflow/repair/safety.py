from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any

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
    "sql_where",
    "always_where",
    "default_filters",
    "filters",
    "hidden",
    "ignored",
    "owner",
    "owners",
    "connection",
    "database",
    "schema",
    "table",
    "table_name",
    "allowed_values",
    "ai_context",
}
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
        old_payload = parse_secure_yaml(before.files[file_name].text, source=file_name)
        new_payload = parse_secure_yaml(after.files[file_name].text, source=file_name)
        if not isinstance(new_payload, (dict, list)):
            raise SecurityPolicyError("Omni AI repair replaced an authored YAML file with an unsupported scalar value")
        reject_secret_keys(new_payload, source=f"AI-repaired YAML file {file_name}")
        changed_paths = _changed_paths(old_payload, new_payload)
        sensitive = sorted(path for path in changed_paths if _path_contains_sensitive_key(path))
        if sensitive:
            raise SecurityPolicyError(
                f"Omni AI repair changed security, governance, SQL, or data-source metadata in {file_name}"
            )
        if any(_contains_secret_like_value(value) for _, value in _new_changed_values(old_payload, new_payload)):
            raise SecurityPolicyError(f"Omni AI repair introduced a secret-like value in {file_name}")
    return difference


def _changed_line_count(before: str, after: str) -> int:
    return sum(
        1
        for line in difflib.ndiff(before.splitlines(), after.splitlines())
        if line.startswith(("+ ", "- "))
    )


def _changed_paths(before: Any, after: Any, path: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    if isinstance(before, dict) and isinstance(after, dict):
        paths: set[tuple[str, ...]] = set()
        for key in set(before) | set(after):
            child_path = (*path, str(key))
            if key not in before or key not in after:
                paths.add(child_path)
            else:
                paths.update(_changed_paths(before[key], after[key], child_path))
        return paths
    if isinstance(before, list) and isinstance(after, list):
        paths = set()
        for index in range(max(len(before), len(after))):
            child_path = (*path, f"[{index}]")
            if index >= len(before) or index >= len(after):
                paths.add(child_path)
            else:
                paths.update(_changed_paths(before[index], after[index], child_path))
        return paths
    return {path} if before != after else set()


def _new_changed_values(before: Any, after: Any, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Any]]:
    if isinstance(before, dict) and isinstance(after, dict):
        values: list[tuple[tuple[str, ...], Any]] = []
        for key in after:
            child_path = (*path, str(key))
            if key not in before:
                values.extend(_all_leaf_values(after[key], child_path))
            else:
                values.extend(_new_changed_values(before[key], after[key], child_path))
        return values
    if isinstance(before, list) and isinstance(after, list):
        values = []
        for index, item in enumerate(after):
            child_path = (*path, f"[{index}]")
            if index >= len(before):
                values.extend(_all_leaf_values(item, child_path))
            else:
                values.extend(_new_changed_values(before[index], item, child_path))
        return values
    return [(path, after)] if before != after else []


def _all_leaf_values(value: Any, path: tuple[str, ...]) -> list[tuple[tuple[str, ...], Any]]:
    if isinstance(value, dict):
        values: list[tuple[tuple[str, ...], Any]] = []
        for key, item in value.items():
            values.extend(_all_leaf_values(item, (*path, str(key))))
        return values
    if isinstance(value, list):
        values = []
        for index, item in enumerate(value):
            values.extend(_all_leaf_values(item, (*path, f"[{index}]")))
        return values
    return [(path, value)]


def _path_contains_sensitive_key(path: tuple[str, ...]) -> bool:
    return any(segment.lower().replace("-", "_") in SENSITIVE_KEYS for segment in path)


def _contains_secret_like_value(value: Any) -> bool:
    return isinstance(value, str) and bool(SECRET_LIKE_VALUE_RE.search(value))
