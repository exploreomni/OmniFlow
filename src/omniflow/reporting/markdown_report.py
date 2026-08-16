from __future__ import annotations

from pathlib import Path
from typing import Any

from ..security import secure_write_text


def render_markdown_report(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    decision = str(report.get("policy_decision") or "unknown")
    exit_reason = str(report.get("exit_code_reason") or "")
    issues = report.get("issues", [])
    blocking = [issue for issue in issues if _is_blocking(issue)]
    impacts = [issue for issue in issues if issue.get("validator") == "contracts" or issue.get("impact_level")]
    coverage_gaps = _coverage_gaps(report)
    dbt_exposure_summaries = _dbt_exposure_summaries(report)
    operation = str(report.get("operation") or "validation")
    dbt_sync_summaries = _dbt_sync_summaries(report)
    lines = [
        "# OmniFlow",
        "",
        "## Decision",
        "",
        f"**{_decision_label(decision, operation=operation)}**",
        "",
        f"- Policy decision: `{_safe_code(decision)}`",
        f"- Exit code reason: `{_safe_code(exit_reason)}`",
        f"- Blocking issues: `{len(blocking)}`",
        f"- Downstream impacts: `{len(impacts)}`",
        f"- Coverage gaps: `{len(coverage_gaps)}`",
        "",
        "## Model Context",
        "",
        *_model_lines(report),
        "",
        "## dbt Synchronization",
        "",
        *_dbt_sync_lines(dbt_sync_summaries, operation=operation),
        "",
        "## Blocking Issues",
        "",
        *_issue_lines(blocking, empty="_No blocking issues._", limit=20),
        "",
        "## Downstream Contract Impact",
        "",
        *_impact_lines(impacts),
        "",
        "## Coverage Gaps",
        "",
        *_coverage_gap_lines(coverage_gaps),
        "",
        "## dbt Exposure Coverage",
        "",
        *_dbt_exposure_lines(dbt_exposure_summaries, decision=decision),
        "",
        "## Validation Summary",
        "",
        f"- Total issues: `{summary.get('total_issues', 0)}`",
        f"- Errors: `{summary.get('errors', 0)}`",
        f"- Warnings: `{summary.get('warnings', 0)}`",
        f"- New issues: `{summary.get('new_issues', 0)}`",
        f"- Existing issues: `{summary.get('existing_issues', 0)}`",
        f"- Resolved issues: `{summary.get('resolved_issues', 0)}`",
        f"- Risk level: `{summary.get('risk_level', 'info')}`",
        "",
        "## Reviewer Actions",
        "",
        *_reviewer_actions(decision, blocking, impacts, coverage_gaps, operation=operation),
        "",
        "## Audit Metadata",
        "",
        f"- Tool version: `{_safe_code(report.get('tool_version', 'unknown'))}`",
        f"- Generated at: `{_safe_code(report.get('generated_at', ''))}`",
        f"- Git SHA: `{_safe_code(report.get('git_sha', ''))}`",
        f"- Git branch: `{_safe_code(report.get('git_branch', ''))}`",
        f"- Config hash: `{_safe_code(report.get('config_hash', ''))}`",
    ]
    lines.append("")
    return "\n".join(lines)


def write_markdown_report(path: str | Path, report: dict[str, Any]) -> None:
    target = Path(path)
    secure_write_text(target, render_markdown_report(report))


def _decision_label(decision: str, *, operation: str) -> str:
    if decision == "pass":
        if operation == "dbt_sync":
            return "Pass: Omni refresh and post-sync checks passed."
        return "Pass: OmniFlow checks passed."
    if decision == "skipped":
        return "Skipped: no Omni semantic-layer changes were detected."
    if decision == "fail":
        if operation == "dbt_sync":
            return "Fail: do not mark the dbt deployment complete."
        return "Fail: review blocking issues before merge."
    return "Review required: OmniFlow could not determine a final decision."


def _model_lines(report: dict[str, Any]) -> list[str]:
    if report.get("policy_decision") == "skipped":
        return ["_Not evaluated because no Omni semantic-layer changes were detected._"]
    models = report.get("models")
    if isinstance(models, list) and models:
        lines = []
        for model in models:
            if not isinstance(model, dict):
                continue
            branch = model.get("branch_name") or model.get("branch_id") or model.get("base_branch") or ""
            lines.append(
                f"- `{_safe_code(model.get('model_id', ''))}` path "
                f"`{_safe_code(model.get('model_path', ''))}` branch `{_safe_code(branch)}`"
            )
        return lines or ["- Model context unavailable."]
    if any(report.get(key) for key in ("model_id", "model_path", "branch_name", "branch_id")):
        return [
            f"- Model ID: `{_safe_code(report.get('model_id', ''))}`",
            f"- Model path: `{_safe_code(report.get('model_path', ''))}`",
            f"- Branch: `{_safe_code(report.get('branch_name') or report.get('branch_id') or '')}`",
        ]
    return ["_Model context unavailable._"]


def _issue_lines(issues: list[dict[str, Any]], *, empty: str, limit: int) -> list[str]:
    if not issues:
        return [empty]
    lines = []
    for issue in issues[:limit]:
        severity = issue.get("severity") or issue.get("risk") or "info"
        location = issue.get("file") or issue.get("yaml_path") or issue.get("field") or issue.get("name") or ""
        lines.append(
            f"- **{_safe_text(severity)}** `{_safe_code(location)}` "
            f"{_safe_text(issue.get('message') or issue.get('summary') or '')}"
        )
    if len(issues) > limit:
        lines.append(f"- _{len(issues) - limit} more issue(s) omitted from PR summary._")
    return lines


def _impact_lines(impacts: list[dict[str, Any]]) -> list[str]:
    if not impacts:
        return ["_No downstream contract impacts._"]
    lines = []
    for issue in impacts[:20]:
        impact_level = issue.get("impact_level") or "unknown"
        target = issue.get("field") or issue.get("previous_field") or issue.get("name") or ""
        referenced = issue.get("referenced_content") if isinstance(issue.get("referenced_content"), list) else []
        lines.append(f"- **{_safe_text(impact_level)}** `{_safe_code(target)}` referenced content: `{len(referenced)}`")
    if len(impacts) > 20:
        lines.append(f"- _{len(impacts) - 20} more impact(s) omitted from PR summary._")
    return lines


def _coverage_gaps(report: dict[str, Any]) -> list[dict[str, Any]]:
    gaps = []
    for key in ("coverage_gaps", "dependency_coverage_gaps"):
        value = report.get(key)
        if isinstance(value, list):
            gaps.extend(item for item in value if isinstance(item, dict))
    for model_report in report.get("model_reports", []) if isinstance(report.get("model_reports"), list) else []:
        if not isinstance(model_report, dict):
            continue
        for check_report in (
            model_report.get("check_reports", []) if isinstance(model_report.get("check_reports"), list) else []
        ):
            value = check_report.get("coverage_gaps") if isinstance(check_report, dict) else None
            if isinstance(value, list):
                gaps.extend(item for item in value if isinstance(item, dict))
    return gaps


def _coverage_gap_lines(gaps: list[dict[str, Any]]) -> list[str]:
    if not gaps:
        return ["_No dependency coverage gaps._"]
    lines = []
    for gap in gaps[:10]:
        lines.append(
            f"- `{_safe_code(gap.get('type', ''))}` `{_safe_code(gap.get('name', ''))}` "
            f"{_safe_text(gap.get('message', ''))}"
        )
    if len(gaps) > 10:
        lines.append(f"- _{len(gaps) - 10} more coverage gap(s) omitted from PR summary._")
    return lines


def _dbt_exposure_summaries(report: dict[str, Any]) -> list[dict[str, Any]]:
    summaries = []
    if report.get("validator") == "dbt_exposures" and isinstance(report.get("summary"), dict):
        summaries.append({"model_id": report.get("model_id", ""), "summary": report["summary"]})
    for model_report in report.get("model_reports", []) if isinstance(report.get("model_reports"), list) else []:
        if not isinstance(model_report, dict):
            continue
        for check_report in (
            model_report.get("check_reports", []) if isinstance(model_report.get("check_reports"), list) else []
        ):
            if not isinstance(check_report, dict) or check_report.get("validator") != "dbt_exposures":
                continue
            summary = check_report.get("summary")
            if isinstance(summary, dict):
                summaries.append({"model_id": model_report.get("model_id", ""), "summary": summary})
    return summaries


def _dbt_exposure_lines(summaries: list[dict[str, Any]], *, decision: str) -> list[str]:
    if not summaries:
        if decision == "skipped":
            return ["_Not evaluated because no Omni semantic-layer changes were detected._"]
        return ["_No dbt exposure result was produced for this run._"]
    lines = []
    for entry in summaries:
        summary = entry["summary"]
        lines.append(
            f"- Model `{_safe_code(entry.get('model_id', ''))}`: "
            f"`{summary.get('total_exposures', 0)}` mapped exposure(s) across "
            f"`{summary.get('total_records', summary.get('total_exposures', 0))}` dashboard record(s); "
            f"unmapped `{summary.get('unmapped_dashboards', 0)}`; "
            f"coverage `{_safe_code(summary.get('coverage_status', 'unknown'))}`."
        )
    return lines


def _dbt_sync_summaries(report: dict[str, Any]) -> list[dict[str, Any]]:
    if report.get("operation") != "dbt_sync":
        return []
    summaries = []
    indexes: dict[tuple[Any, Any], int] = {}
    validation_order = {"not_run": 0, "passed": 1, "failed": 2}
    for model_report in report.get("model_reports", []) if isinstance(report.get("model_reports"), list) else []:
        if not isinstance(model_report, dict):
            continue
        refresh = model_report.get("refresh")
        if not isinstance(refresh, dict):
            continue
        key = (refresh.get("connection_id"), refresh.get("job_id"))
        validation_status = str(model_report.get("post_sync_validation_status", "unknown"))
        if key in indexes:
            existing = summaries[indexes[key]]
            if validation_order.get(validation_status, 2) > validation_order.get(
                str(existing["validation_status"]), 2
            ):
                existing["validation_status"] = validation_status
            continue
        indexes[key] = len(summaries)
        summaries.append(
            {
                "refresh": refresh,
                "validation_status": validation_status,
            }
        )
    return summaries


def _dbt_sync_lines(summaries: list[dict[str, Any]], *, operation: str) -> list[str]:
    if operation != "dbt_sync":
        return ["_Not a dbt synchronization run._"]
    if not summaries:
        return ["_No schema refresh job was started._"]
    lines = []
    for entry in summaries:
        refresh = entry["refresh"]
        affected = refresh.get("affected_model_ids")
        affected_count = len(affected) if isinstance(affected, list) else 1
        lines.append(
            f"- Connection `{_safe_code(refresh.get('connection_id', ''))}` job "
            f"`{_safe_code(refresh.get('job_id', 'not-started'))}`: "
            f"refresh `{_safe_code(refresh.get('status', 'unknown'))}` in "
            f"`{_safe_code(refresh.get('refresh_mode', 'unknown'))}` mode; "
            f"affected models `{affected_count}`; post-sync validation "
            f"`{_safe_code(entry.get('validation_status', 'unknown'))}`."
        )
    return lines


def _reviewer_actions(
    decision: str,
    blocking: list[dict[str, Any]],
    impacts: list[dict[str, Any]],
    coverage_gaps: list[dict[str, Any]],
    *,
    operation: str,
) -> list[str]:
    if operation == "dbt_sync":
        if decision == "pass":
            return ["- Confirm refreshed metadata and any Omni-generated Git change before closing deployment."]
        return ["- Keep deployment open and resolve the refresh or post-sync validation failure."]
    if decision == "pass":
        actions = ["- Review semantic diff and downstream impact artifacts before approving."]
        if coverage_gaps:
            actions.append("- Review dependency coverage gaps before treating impact analysis as complete.")
        return actions
    if decision == "skipped":
        return ["- No reviewer action needed for OmniFlow unless this PR was expected to contain Omni changes."]
    actions = []
    if blocking:
        actions.append("- Resolve blocking validation, lint, or contract issues before merge.")
    if impacts:
        actions.append("- Review referenced dashboards, reports, and queries before approving semantic changes.")
    if coverage_gaps:
        actions.append("- Re-run or inspect dependency coverage gaps; impact analysis may be incomplete.")
    return actions or ["- Review OmniFlow artifacts for setup or configuration errors."]


def _is_blocking(issue: dict[str, Any]) -> bool:
    return issue.get("active", True) and issue.get("severity") == "error"


def _safe_text(value: Any) -> str:
    text = _normalized_text(value)
    for character in ("\\", "`", "*", "_", "{", "}", "[", "]", "(", ")", "#", "+", "-", ".", "!", "|", "~"):
        text = text.replace(character, f"\\{character}")
    return text.replace("@", "&#64;")


def _safe_code(value: Any) -> str:
    return _normalized_text(value).replace("`", "'").replace("@", "&#64;")


def _normalized_text(value: Any) -> str:
    return (
        str(value or "")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
