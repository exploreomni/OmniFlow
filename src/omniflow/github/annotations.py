from __future__ import annotations

from typing import Any

from ..git import tool_revision
from ..reporting.finding_groups import group_findings
from ..reporting.issue_policy import is_blocking
from ..reporting.recovery import (
    SOURCE_SYNC_REASONS,
    diagnostic_guide,
    first_step,
    has_matched_difference,
    mismatch_description,
)
from ..repository_diagnostics import repository_diagnostic_availability


def public_annotation_lines(issues: list[dict[str, Any]], *, limit: int) -> list[str]:
    """Group public categories before sampling so repeated warnings cannot crowd out causes."""
    groups = group_findings([issue for issue in issues if issue.get("active") is not False])
    selected = []
    source = {"tool_revision": tool_revision()}
    for finding, count in groups[:limit]:
        reason = finding.get("candidate_reason")
        if reason in SOURCE_SYNC_REASONS:
            guide = diagnostic_guide(finding, source)
            title = finding["title"].rstrip(".")
            message = f"{title}. {mismatch_description(finding)} Fix: {first_step(finding)} [{finding['diagnostic_code']}]"
            if has_matched_difference(finding) and repository_diagnostic_availability().get("candidate_summary_written") is True:
                message += " Open this run’s Summary for available file and line details."
            elif guide:
                label = "Find the filename and line" if reason == "content_mismatch" else "Troubleshoot the changed files"
                message += f" {label}: {guide}"
            selected.append({"severity": finding.get("severity"), "blocking": is_blocking(finding),
                             "candidate_reason": reason, "message": message})
            continue
        qualifiers = [finding.get(key) for key in (
            "diagnostic_code", "context_reference", "validator", "rule_id", "validation_scope", "state",
        )]
        context = " / ".join(str(value) for value in qualifiers if value)
        description = finding.get("title") or finding.get("message", "")
        if description in {"A validation finding was recorded.", "The check could not establish complete evidence."}:
            description = finding.get("message", description)
        message = f"{description} ({context}, {count} occurrence{'s' if count != 1 else ''})."
        facts = [f"{key}={finding[key]}" for key in (
            "reason_code", "cause_code", "transport_reason", "document_reference", "candidate_reason", "api_operation", "http_status",
            "attempt_count", "max_attempts", "config_key", "expected_type", "source_kind",
            "source_line", "source_column", "metadata_stage", "metadata_reason",
        ) if key in finding]
        if facts:
            message += " Evidence: " + "; ".join(facts) + "."
        if finding.get("coverage_description"):
            message += " " + finding["coverage_description"]
        if finding.get("next_action") or finding.get("recovery_steps"):
            message += f" Next: {first_step(finding)}"
        if is_blocking(finding):
            url = diagnostic_guide(finding, source)
            if url:
                message += f" Guide: {url}"
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
        if is_blocking(issue) and issue.get("candidate_reason") not in SOURCE_SYNC_REASONS:
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
