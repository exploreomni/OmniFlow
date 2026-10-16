from __future__ import annotations

from typing import Any

from ..reporting.finding_groups import group_findings
from ..reporting.issue_policy import is_blocking


def public_annotation_lines(issues: list[dict[str, Any]], *, limit: int) -> list[str]:
    """Group public categories before sampling so repeated warnings cannot crowd out causes."""
    groups = group_findings([issue for issue in issues if issue.get("active") is not False])
    selected = []
    for finding, count in groups[:limit]:
        qualifiers = [finding.get(key) for key in ("context_reference", "validator", "rule_id", "state")]
        context = " / ".join(str(value) for value in qualifiers if value)
        message = f"{context}: {count} occurrence{'s' if count != 1 else ''}. {finding.get('message', '')}"
        selected.append({**finding, "message": message})
    lines = annotation_lines(selected)
    if len(groups) > limit:
        omitted = groups[limit:]
        count = sum(size for _, size in omitted)
        blocking = sum(size for finding, size in omitted if is_blocking(finding))
        lines.append(f"::notice::Additional findings omitted from annotations: {len(omitted)} categories, "
                     f"{count} occurrences, {blocking} blocking. Review the public report for the complete counts.")
    return lines


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
