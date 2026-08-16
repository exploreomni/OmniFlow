from __future__ import annotations

from .exceptions import ConfigError, SecurityPolicyError

EDITABLE_YAML_FILES = {"model", "relationships"}
EDITABLE_YAML_SUFFIXES = (".topic", ".composite_topic", ".view")


def validate_editable_yaml_file_name(value: str, *, allow_special_files: bool = True) -> str:
    if not isinstance(value, str):
        raise ConfigError("YAML file name must be a string")
    normalized = _normalize_authored_yaml_path(value)
    parts = normalized.split("/")
    basename = parts[-1]
    is_root_special_file = len(parts) == 1 and normalized in EDITABLE_YAML_FILES
    has_editable_suffix = any(
        basename.endswith(suffix) and len(basename) > len(suffix) for suffix in EDITABLE_YAML_SUFFIXES
    )
    if (allow_special_files and is_root_special_file) or has_editable_suffix:
        return normalized
    expected = "model, relationships, .topic, .composite_topic, or .view"
    raise ConfigError(f"YAML file name must be one of the documented editable file types: {expected}")


def _normalize_authored_yaml_path(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 1_024:
        raise SecurityPolicyError("YAML file name contains an unsafe authored file path")
    if normalized.startswith("/") or "\\" in normalized or any(not character.isprintable() for character in normalized):
        raise SecurityPolicyError("YAML file name contains an unsafe authored file path")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} or part != part.strip() for part in parts):
        raise SecurityPolicyError("YAML file name contains an unsafe authored file path")
    return normalized
