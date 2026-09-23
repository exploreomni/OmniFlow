from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from .exceptions import ConfigError
from .semantic_files import infer_file_kind
from .yaml_security import parse_secure_yaml


class ViewNamesMetadataError(ConfigError):
    """Bounded public diagnostics; never include API paths, names, or YAML."""

    metadata_stage = "view_names"

    def __init__(self, reason: str) -> None:
        explanations = {
            "invalid_mapping": "Expected a string-to-string map within the file inventory size.",
            "invalid_path": "File paths must be safe, exact, case-sensitive inventory paths.",
            "unsupported_orientation": "The map is mixed or references paths outside the file inventory.",
            "ambiguous_orientation": "Both map orientations match the file inventory; identity cannot be chosen safely.",
            "duplicate_name": "Multiple view files claim the same canonical name.",
            "duplicate_path": "Multiple canonical names claim the same view file.",
            "invalid_identity": "A canonical view name or path is invalid.",
            "non_view_identity": "A canonical view name targets a non-view file.",
            "incomplete_identity": "Every view file needs a non-empty canonical name and a mapping definition.",
        }
        self.metadata_reason = reason
        super().__init__(
            f"Omni YAML metadata processing failed (viewNames: {reason}). {explanations[reason]} "
            "Supported API maps are file path to canonical name or canonical name to file path. "
            "Check the response contract using a sanitized sample; do not infer names or bypass identity validation."
        )


def normalize_api_view_names(value: Any, files: dict[str, str]) -> dict[str, str]:
    """Normalize either supported API orientation; snapshots stay name -> path.

    File inventory membership chooses the orientation for the whole map, never
    entry-by-entry. Reject ambiguity rather than losing identities on inversion.
    """
    if (
        not isinstance(value, dict) or len(value) > len(files)
        or any(not isinstance(key, str) or not isinstance(name, str) for key, name in value.items())
    ):
        raise ViewNamesMetadataError("invalid_mapping")
    for path in files:
        if not _safe_file_path(path):
            raise ViewNamesMetadataError("invalid_path")

    paths_are_keys = all(path in files for path in value)
    paths_are_values = all(path in files for path in value.values())
    if value and paths_are_keys and paths_are_values:
        raise ViewNamesMetadataError("ambiguous_orientation")
    if not paths_are_keys and not paths_are_values:
        raise ViewNamesMetadataError("unsupported_orientation")

    # Reuse graph classification, including legacy YAML payload hints. Secure
    # parsing retains the YAML bounds and never expands unresolved inheritance.
    parsed = {path: parse_secure_yaml(text, source=path) for path, text in files.items()}
    kinds = {path: infer_file_kind(path, payload) for path, payload in parsed.items()}
    names: dict[str, str] = {}
    if paths_are_keys:
        for path, name in value.items():
            # Membership and path safety were checked before any empty entries
            # are discarded. Only known non-view files can have no view name.
            if name == "" and kinds[path] != "view":
                continue
            if name in names:
                raise ViewNamesMetadataError("duplicate_name")
            names[name] = path
    else:
        names = dict(value)
    if len(set(names.values())) != len(names):
        raise ViewNamesMetadataError("duplicate_path")
    try:
        names = validate_view_names(names, files)
    except ConfigError as exc:
        raise ViewNamesMetadataError("invalid_identity") from exc
    if any(kinds[path] != "view" for path in names.values()):
        raise ViewNamesMetadataError("non_view_identity")
    covered = set(names.values())
    if any(
        path not in covered or not isinstance(parsed[path], dict)
        for path, kind in kinds.items() if kind == "view"
    ):
        raise ViewNamesMetadataError("incomplete_identity")
    return names


def _safe_file_path(path: Any) -> bool:
    return (
        isinstance(path, str) and bool(path) and path == path.strip() and len(path) <= 1024
        and "\\" not in path and not any(ord(char) < 32 or ord(char) == 127 for char in path)
        and not PurePosixPath(path).is_absolute() and ".." not in PurePosixPath(path).parts
        and PurePosixPath(path).as_posix() == path
    )


def validate_view_names(value: Any, files: dict[str, Any]) -> dict[str, str]:
    """Validate Omni's canonical-name -> exact, case-sensitive file-path map."""
    if not isinstance(value, dict) or len(value) > len(files):
        raise ConfigError("Invalid Omni viewNames metadata; refresh the YAML snapshot")
    names: dict[str, str] = {}
    paths: set[str] = set()
    for name, path in value.items():
        if (
            not isinstance(name, str) or not name or name != name.strip() or len(name) > 1024
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            or not isinstance(path, str) or path not in files
            or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts
            or path in paths
        ):
            raise ConfigError("Invalid or ambiguous Omni viewNames metadata; refresh the YAML snapshot")
        names[name] = path
        paths.add(path)
    return names
