"""One explicit trusted report directory for preparation, writing and delivery."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from ..exceptions import ConfigError
from ..security import validate_repo_output_path


def report_directory(value: str) -> Path:
    """Reject broad paths and upload-glob syntax; spaces in names are supported."""
    if (not isinstance(value, str) or not value or len(value) > 512 or value != value.strip()
            or value.startswith("~") or any(ord(char) < 32 or char in "\\*?[]{}!()" for char in value)):
        raise _path_error()
    path = PurePosixPath(value)
    if (path.is_absolute() or path.as_posix() != value or not path.parts or ".." in path.parts
            or path.parts[0] in {".git", ".github", ".omni"}):
        raise _path_error()
    return validate_repo_output_path(Path(value))


def require_matching_report_directory(configured: str) -> None:
    """Called after trusted config load; never silently substitute another path."""
    declared = os.getenv("OMNIFLOW_REPORT_DIR")
    if declared is not None and report_directory(declared) != report_directory(configured):
        raise ConfigError(
            "Workflow OMNIFLOW_REPORT_DIR must match trusted reporting.output_dir. "
            "Update both together; no report from another directory is substituted.",
            reason_code="config_invalid_value",
            diagnostic_context={"config_key": "reporting.output_dir", "expected_type": "repository_path"},
        )


def _path_error() -> ConfigError:
    return ConfigError(
        "Report delivery needs a nonempty repository-relative directory without traversal, "
        "symlinks, protected metadata paths or upload glob characters. Spaces are supported.",
        reason_code="config_invalid_value",
        diagnostic_context={"config_key": "reporting.output_dir", "expected_type": "repository_path"},
    )
