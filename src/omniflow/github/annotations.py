from __future__ import annotations

from typing import Any

from ..reporting.finding_groups import group_findings
from ..reporting.issue_policy import is_blocking


def public_annotation_lines(issues: list[dict[str, Any]], *, limit: int) -> list[str]:
    """Group public categories before sampling so repeated warnings cannot crowd out causes."""
    groups = group_findings([issue for issue in issues if issue.get("active") is not False])
    selected = []
    for finding, count in groups[:limit]:
        qualifiers = [finding.get(key) for key in (
            "diagnostic_code", "context_reference", "validator", "rule_id", "validation_scope", "state",
        )]
        context = " / ".join(str(value) for value in qualifiers if value)
        message = f"{context}: {count} occurrence{'s' if count != 1 else ''}. {finding.get('message', '')}"
        facts = [f"{key}={finding[key]}" for key in (
            "reason_code", "cause_code", "candidate_reason", "api_operation", "http_status",
            "attempt_count", "max_attempts", "config_key", "expected_type", "source_kind",
            "source_line", "source_column", "metadata_stage", "metadata_reason",
        ) if key in finding]
        if facts:
            message += " Evidence: " + "; ".join(facts) + "."
        if finding.get("next_action"):
            message += f" Next: {finding['next_action']}"
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
        properties = []
        if issue.get("file"):
            properties.append(f"file={_escape_property(issue['file'])}")
            if type(issue.get("line")) is int and issue["line"] > 0:
                properties.append(f"line={issue['line']}")
        message = _escape_data(issue.get("message") or issue.get("summary") or "OmniFlow issue")
        if is_blocking(issue):
            message = f"Blocks under configured policy: {message}"
        elif issue.get("blocking") is False:
            message = f"Does not block under configured policy: {message}"
        location = " " + ",".join(properties) if properties else ""
        lines.append(f"::{level}{location}::{message}")
    return lines


def _escape_data(value: Any) -> str:
    return str(value).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(value: Any) -> str:
    return _escape_data(value).replace(":", "%3A").replace(",", "%2C")
