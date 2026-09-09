from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from .exceptions import ConfigError


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
