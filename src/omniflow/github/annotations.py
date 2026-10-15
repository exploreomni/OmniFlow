from __future__ import annotations

from typing import Any

from ..reporting.issue_policy import is_blocking


def annotation_lines(issues: list[dict[str, Any]]) -> list[str]:
    lines = []
    for issue in issues:
        if issue.get("active") is False:
            continue
        severity = issue.get("severity")
        level = "error" if severity == "error" else ("warning" if severity in {"warning", "warn"} else "notice")
        if severity == "error" and not is_blocking(issue):
            level = "notice"
        file_path = _escape_property(issue.get("file") or "omniflow")
        message = _escape_data(issue.get("message") or issue.get("summary") or "OmniFlow issue")
        if is_blocking(issue):
            message = f"Blocks under configured policy: {message}"
        elif issue.get("blocking") is False:
            message = f"Does not block under configured policy: {message}"
        lines.append(f"::{level} file={file_path},line=1::{message}")
    return lines


def _escape_data(value: Any) -> str:
    return str(value).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(value: Any) -> str:
    return _escape_data(value).replace(":", "%3A").replace(",", "%2C")
