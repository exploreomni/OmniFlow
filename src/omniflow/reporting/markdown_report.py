from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..security import redact, secure_write_text


def render_markdown_report(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    decision = str(report.get("policy_decision") or "unknown")
    exit_reason = str(report.get("exit_code_reason") or "")
    issues = report.get("issues", [])
    incomplete = _execution_incomplete(report)
    incomplete_downstream = _checks_incomplete(report, {"semantic_diff", "downstream", "contracts"})
    blocking = [issue for issue in issues if _is_blocking(issue)]
    failed = decision == "fail" or isinstance(report.get("exit_code"), int) and report["exit_code"] > 0
    uncertain_warnings = [
        issue for issue in issues
        if failed and not blocking and issue.get("active", True) and issue.get("severity") in {"warn", "warning"}
        and not isinstance(issue.get("blocking"), bool)
    ]
    advisory = [
        issue for issue in issues
        if not _is_blocking(issue) and not _is_coverage_gap(issue) and issue not in uncertain_warnings
    ]
    warnings = [
        issue for issue in issues
        if issue.get("active", True) and issue.get("severity") in {"warn", "warning"}
        and not _is_blocking(issue) and issue not in uncertain_warnings
    ]
    impacts = [
        issue for issue in issues
        if (issue.get("validator") == "contracts" or issue.get("impact_level")) and not _is_coverage_gap(issue)
    ]
    blocking_impacts = [issue for issue in impacts if _is_blocking(issue)]
    advisory_impacts = [issue for issue in impacts if not _is_blocking(issue) and issue not in uncertain_warnings]
    coverage_gaps = _coverage_gaps(report)
    dbt_exposure_summaries = _dbt_exposure_summaries(report)
    ai_eval_summaries = _ai_eval_summaries(report)
    ai_eval_reports = _ai_eval_reports(report)
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
        f"- Blocking issues (all checks): `{len(blocking)}`",
        f"- Advisory warnings (not blocking): `{len(warnings)}`",
        f"- Downstream impacts: `{len(impacts)}` recorded; analysis incomplete."
        if incomplete_downstream else f"- Downstream impacts: `{len(impacts)}`",
        f"- Coverage gaps: `{len(coverage_gaps)}` recorded; check coverage is incomplete or unknown."
        if incomplete else f"- Coverage gaps: `{len(coverage_gaps)}`",
        "",
        "## Model Context",
        "",
        *_model_lines(report),
        "",
        "## Check Execution",
        "",
        *_execution_lines(report),
        "",
        "## dbt Synchronization",
        "",
        *_dbt_sync_lines(dbt_sync_summaries, operation=operation),
        "",
        "## Blocking Issues",
        "",
        *_issue_lines(
            blocking,
            empty="_No confirmed blockers; warning policy needs review below._"
            if uncertain_warnings else "_No blocking issues._",
            limit=20,
        ),
        *_uncertain_warning_lines(uncertain_warnings),
        "",
        "## Downstream Contract Impact",
        "",
        *([
            "_No downstream result is available for the context that stopped before completing dependency analysis. "
            "Recorded findings from completed checks or other contexts do not establish complete analysis._",
            "",
        ] if incomplete_downstream else []),
        *_impact_lines(
            blocking_impacts, coverage_unavailable=bool(coverage_gaps) or incomplete_downstream,
            empty="_No blocking downstream findings were recorded; analysis is incomplete._"
            if incomplete_downstream else "_No blocking downstream contract impacts; see advisory changes below._",
        ),
        "",
        "## Coverage Gaps",
        "",
        *_coverage_gap_lines(coverage_gaps, incomplete=incomplete),
        "",
        "## dbt Exposure Coverage",
        "",
        *_dbt_exposure_lines(dbt_exposure_summaries, decision=decision),
        "",
        "## AI Eval",
        "",
        *_ai_eval_lines(ai_eval_summaries, decision=decision, check_reports=ai_eval_reports),
        "",
        "## Validation Summary",
        "",
        f"- Total issues: `{_safe_code(summary.get('total_issues', 0))}`",
        f"- Errors: `{_safe_code(summary.get('errors', 0))}`",
        f"- Warnings: `{_safe_code(summary.get('warnings', 0))}`",
        *_content_state_lines(report, issues),
        "- Semantic risk: unestablished for incomplete contexts; any results from other contexts are partial."
        if incomplete else f"- Risk level: `{_safe_code(summary.get('risk_level', 'info'))}`",
        "",
        "<details>",
        "<summary>Advisory and historical detail (not blocking)</summary>",
        "",
        *_issue_lines(advisory, empty="_No advisory or historical issues._", limit=20),
        "",
        "### Non-blocking downstream changes",
        "",
        *_impact_lines(
            advisory_impacts, coverage_unavailable=bool(coverage_gaps) or incomplete_downstream,
            empty="_No non-blocking downstream findings were recorded; analysis is incomplete._"
            if incomplete_downstream else "_No non-blocking downstream changes._",
        ),
        "",
        "</details>",
        "",
        "## Reviewer Actions",
        "",
        *_reviewer_actions(decision, blocking, impacts, coverage_gaps, operation=operation),
        *(["- Resolve the failed or unavailable execution state and rerun enabled checks that did not complete; "
           "missing results are not successful checks."] if incomplete else []),
        "",
        "## Audit Metadata",
        "",
        f"- Tool version: `{_safe_code(report.get('tool_version', 'unknown'))}`",
        f"- Generated at: `{_safe_code(report.get('generated_at', ''))}`",
        *_tool_revision_lines(report),
        f"- Input/PR Git SHA: `{_safe_code(report.get('git_sha', ''))}`",
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


def _execution_contexts(report: dict[str, Any]) -> list[dict[str, Any]]:
    models = report.get("model_reports")
    contexts = [model for model in models if isinstance(model, dict)] if isinstance(models, list) else []
    resolved = []
    for context in contexts or [report]:
        validation = context.get("post_sync_validation")
        if not isinstance(validation, dict):
            resolved.append(context)
            continue
        nested = _with_context(validation, context)
        outer_states = _check_states(context)
        nested["check_states"] = outer_states + _check_states(validation)
        refresh = context.get("refresh")
        wrapper_incomplete = context.get("validation_complete") is False or any(
            state.get("status") not in ("completed", "disabled") for state in outer_states
        ) or isinstance(refresh, dict) and refresh.get("status") != "completed"
        if wrapper_incomplete:
            nested["validation_complete"] = False
            nested["execution_wrapper_incomplete"] = True
        resolved.append(nested)
    return resolved


def _check_states(context: dict[str, Any]) -> list[dict[str, Any]]:
    states = context.get("check_states")
    return [state for state in states if isinstance(state, dict)] if isinstance(states, list) else []


def _context_incomplete(context: dict[str, Any], *, failed: bool) -> bool:
    states = _check_states(context)
    if context.get("validation_complete") is False or any(
        state.get("status") not in ("completed", "disabled") for state in states
    ):
        return True
    if context.get("validation_complete") is True or states:
        return False
    # A legacy failed aggregate cannot establish that every enabled check ran.
    # Individual completed results remain usable through _checks_incomplete.
    return failed and not context.get("validator")


def _execution_incomplete(report: dict[str, Any]) -> bool:
    if report.get("validation_complete") is False or any(
        issue.get("type") == "content_evidence_unavailable" for issue in report.get("issues", [])
    ):
        return True
    failed = report.get("policy_decision") == "fail" or (
        isinstance(report.get("exit_code"), int) and report["exit_code"] > 0
    )
    return any(_context_incomplete(context, failed=failed) for context in _execution_contexts(report))


def _checks_incomplete(report: dict[str, Any], validators: set[str]) -> bool:
    if any(issue.get("type") == "content_evidence_unavailable" for issue in report.get("issues", [])):
        return True
    failed = report.get("policy_decision") == "fail" or (
        isinstance(report.get("exit_code"), int) and report["exit_code"] > 0
    )
    for context in _execution_contexts(report):
        states = _check_states(context)
        matching = [state for state in states if state.get("validator") in validators]
        if matching:
            if any(state.get("status") not in ("completed", "disabled") for state in matching):
                return True
            continue
        issues = context.get("issues", [])
        if any(issue.get("type") == "content_evidence_unavailable" for issue in issues):
            return True
        checks = context.get("check_reports")
        if isinstance(checks, list) and any(
            isinstance(check, dict) and check.get("validator") in validators for check in checks
        ):
            continue
        # Preserve recorded legacy content history, without treating a context failure as evidence.
        if validators == {"content"} and not states and any(
            issue.get("validator") == "content" and issue.get("state") in ("new", "existing", "resolved")
            for issue in issues
        ) and not any(issue.get("validator") == "context" for issue in issues):
            continue
        if _context_incomplete(context, failed=failed):
            return True
    return False


def _execution_lines(report: dict[str, Any]) -> list[str]:
    lines = []
    labels = {"completed": "completed", "failed": "failed operationally", "not_run": "not run", "disabled": "disabled"}
    for context in _execution_contexts(report):
        scope = "; ".join(_scope_parts(context)) or "Context identity unavailable"
        states = _check_states(context)
        if context.get("execution_wrapper_incomplete"):
            lines.append(f"- {scope}: outer refresh or post-sync wrapper evidence is incomplete or unavailable.")
        if not states:
            lines.append(f"- {scope}: execution status was not recorded; missing results do not establish completed checks.")
        for state in states:
            status = state.get("status")
            label = labels.get(status, "status unavailable") if isinstance(status, str) else "status unavailable"
            lines.append(f"- {scope} · `{_safe_code(state.get('validator'))}`: {label}.")
    if any(_check_states(context) for context in _execution_contexts(report)):
        lines.append("Completed describes execution only; recorded findings may still block under configured policy.")
    return lines


def _issue_lines(issues: list[dict[str, Any]], *, empty: str, limit: int) -> list[str]:
    if not issues:
        return [empty]
    lines = []
    for issue in issues[:limit]:
        severity = issue.get("severity") or issue.get("risk") or "info"
        if not issue.get("active", True):
            severity = f"inactive {severity}"
        elif severity in {"warn", "warning"} and _is_blocking(issue):
            severity = "policy-blocking warning"
        category, guidance = _issue_guidance(issue)
        message = _readable_message(issue.get("message")) or _readable_message(issue.get("summary"))
        if not message:
            message = "Details are unavailable; use the check category and affected object to investigate."
        lines.append(
            f"- **{_safe_text(severity)} — {_safe_text(category)}** · {_affected_object(issue)}. "
            f"{_safe_text(message)} {guidance}"
        )
    if len(issues) > limit:
        lines.append(f"- _{len(issues) - limit} more issue(s) omitted from PR summary._")
    return lines


def _impact_lines(
    impacts: list[dict[str, Any]], *, coverage_unavailable: bool = False, empty: str = "_No downstream contract impacts._",
) -> list[str]:
    if not impacts:
        return [empty]
    lines = []
    for issue in impacts[:20]:
        impact_level = issue.get("impact_level") or "unknown"
        target = issue.get("field") or issue.get("previous_field") or issue.get("name") or ""
        scope = "; ".join(_scope_parts(issue))
        scope = f" · {scope}" if scope else ""
        referenced = issue.get("referenced_content") if isinstance(issue.get("referenced_content"), list) else []
        if coverage_unavailable and not referenced:
            lines.append(
                f"- **Coverage unavailable** `{_safe_code(target)}`{scope}: no references established; "
                "absence of downstream dependencies is not proven."
            )
            continue
        lines.append(f"- **{_safe_text(impact_level)}** `{_safe_code(target)}`{scope} referenced content: `{len(referenced)}`")
    if len(impacts) > 20:
        lines.append(f"- _{len(impacts) - 20} more impact(s) omitted from PR summary._")
    return lines


def _coverage_gaps(report: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = []
    for key in ("coverage_gaps", "dependency_coverage_gaps"):
        value = report.get(key)
        if isinstance(value, list):
            candidates.extend(_with_context(item, report) for item in value if isinstance(item, dict))
    for model_report in _execution_contexts(report):
        for check_report in (
            model_report.get("check_reports", []) if isinstance(model_report.get("check_reports"), list) else []
        ):
            value = check_report.get("coverage_gaps") if isinstance(check_report, dict) else None
            if isinstance(value, list):
                context = _with_context(check_report, model_report)
                candidates.extend(_with_context(item, context) for item in value if isinstance(item, dict))
    for issue in report.get("issues", []):
        if _is_coverage_gap(issue):
            candidates.append(_with_context(issue, report))
    gaps = {}
    for gap in candidates:
        key = (
            _first_identity(gap, ("model_id",)), _branch_identity(gap),
            _first_identity(gap, ("document_id", "document_identifier", "content_id")),
            _first_identity(gap, ("query_id", "query_identifier", "query_name")),
            _first_identity(gap, ("name",)), _first_identity(gap, ("validation_scope",)),
            _first_identity(gap, ("message",)),
        )
        # Flat findings can add category/severity omitted from legacy nested gap summaries.
        gaps[key] = {**gaps.get(key, {}), **gap}
    return list(gaps.values())


def _with_context(item: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return {**{key: context[key] for key in ("model_id", "branch_id", "branch_name") if key in context}, **item}


def _coverage_gap_lines(gaps: list[dict[str, Any]], *, incomplete: bool = False) -> list[str]:
    if not gaps:
        if incomplete:
            return ["_No dependency coverage gaps were recorded; incomplete or unavailable checks do not establish full coverage._"]
        return ["_No dependency coverage gaps._"]
    lines = []
    for gap in gaps[:10]:
        if gap.get("type") == "content_evidence_unavailable":
            lines.append(
                f"- **Content Validator coverage unavailable** · {_affected_object(gap)}. "
                "See the expanded Content validation finding in Blocking Issues."
            )
        else:
            message = _readable_message(gap.get("message")) or "Dependency evidence is incomplete."
            lines.append(
                f"- **Coverage unavailable** · {_affected_object(gap)}. {_safe_text(message)} "
                "Verify model identity, dependency-search access, and supported references before rerunning."
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


def _ai_eval_summaries(report: dict[str, Any]) -> list[dict[str, Any]]:
    summaries = []
    for check_report in _ai_eval_reports(report):
        if isinstance(check_report.get("prompt_set_summaries"), list):
            summaries.extend(check_report["prompt_set_summaries"])
    return summaries


def _ai_eval_reports(report: dict[str, Any]) -> list[dict[str, Any]]:
    reports = [report] if report.get("validator") == "ai_eval" else []
    for model_report in report.get("model_reports", []) if isinstance(report.get("model_reports"), list) else []:
        if not isinstance(model_report, dict):
            continue
        for check_report in (
            model_report.get("check_reports", []) if isinstance(model_report.get("check_reports"), list) else []
        ):
            if not isinstance(check_report, dict) or check_report.get("validator") != "ai_eval":
                continue
            reports.append(check_report)
    return reports


def _ai_eval_lines(
    summaries: list[dict[str, Any]], *, decision: str, check_reports: list[dict[str, Any]]
) -> list[str]:
    incomplete = any(check.get("operational_failure") for check in check_reports)
    lines = ["_AI eval produced an incomplete comparison; see validation issues and JSON evidence._"] if incomplete else []
    if not summaries:
        if lines:
            return lines
        if check_reports:
            return ["_AI eval was skipped for these model contexts; see the recorded reason in JSON evidence._"]
        if decision == "skipped":
            return ["_Not evaluated because no Omni semantic-layer changes were detected._"]
        if decision != "pass":
            return ["_No AI eval result was produced; its enablement or execution cannot be established from this report._"]
        return ["_AI eval is disabled or has no configured prompt sets._"]
    for entry in summaries:
        delta = entry.get("accuracy_delta_pts")
        delta_str = "n/a" if delta is None else f"{delta:+.1f} pts"
        lines.append(
            f"- `{_safe_code(entry.get('prompt_set_id', ''))}`: accuracy "
            f"`{_pct(entry.get('main_accuracy'))}` → `{_pct(entry.get('branch_accuracy'))}` ({delta_str}); "
            f"regressed `{entry.get('regressed_count', 0)}`, improved `{entry.get('improved_count', 0)}`."
        )
    return lines


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


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
    content_gaps = [gap for gap in coverage_gaps if gap.get("type") == "content_evidence_unavailable"]
    dependency_gaps = [gap for gap in coverage_gaps if gap.get("type") != "content_evidence_unavailable"]
    coverage_actions = []
    if content_gaps:
        coverage_actions.append(
            "- Restore readable Content Validator evidence for the affected context and rerun the configured checks; "
            "do not treat the content comparison or later checks as complete."
        )
    if dependency_gaps:
        coverage_actions.append("- Re-run or inspect dependency coverage gaps; impact analysis may be incomplete.")
    if operation == "dbt_sync":
        if decision == "pass":
            return ["- Confirm refreshed metadata and any Omni-generated Git change before closing deployment."] + coverage_actions
        return ["- Keep deployment open and resolve the refresh or post-sync validation failure."] + coverage_actions
    if decision == "pass":
        actions = ["- Review semantic diff and downstream impact artifacts before approving."]
        return actions + coverage_actions
    if decision == "skipped":
        return ["- No reviewer action needed for OmniFlow unless this PR was expected to contain Omni changes."]
    actions = []
    if any(issue.get("type") != "content_evidence_unavailable" for issue in blocking):
        actions.append("- Resolve blocking validation, lint, or contract issues before merge.")
    if impacts:
        actions.append("- Review referenced dashboards, reports, and queries before approving semantic changes.")
    actions.extend(coverage_actions)
    return actions or ["- Review OmniFlow artifacts for setup or configuration errors."]


def _is_blocking(issue: dict[str, Any]) -> bool:
    return issue.get("active", True) and (issue.get("severity") == "error" or issue.get("blocking") is True)


def _uncertain_warning_lines(issues: list[dict[str, Any]]) -> list[str]:
    if not issues:
        return []
    return [
        "",
        "### Warnings requiring policy review",
        "",
        "This run failed, but the blocking status of these warnings was not recorded. "
        "Do not assume they are advisory; inspect the configured warning policy and structured evidence.",
        "",
        *_issue_lines(issues, empty="", limit=20),
    ]


def _is_coverage_gap(issue: dict[str, Any]) -> bool:
    return issue.get("impact_level") == "coverage_gap" or issue.get("type") in {
        "dependency_coverage_gap", "content_evidence_unavailable",
    }


def _issue_guidance(issue: dict[str, Any]) -> tuple[str, str]:
    if issue.get("validator") == "content" or issue.get("type") in {
        "content_validation_issue", "content_evidence_unavailable",
    }:
        kind = {
            "dashboard_filter": "Dashboard filter",
            "query": "Query",
        }.get(issue.get("issue_type"), "Content")
        scope = {
            "branch": "the validated Omni branch",
            "base": "the validated main/base model",
        }.get(issue.get("validation_scope"), "the validated model context")
        if issue.get("type") == "content_evidence_unavailable":
            return (
                f"Content validation / {kind} — coverage unavailable",
                f"Rerun Content Validator against {scope} and inspect the original response privately. "
                "This evidence does not identify a specific defect; do not guess which filter or field to change.",
            )
        if kind == "Dashboard filter":
            guidance = (
                f"In {scope}, inspect the affected document's dashboard filter mappings and referenced fields. "
                "Confirm the exact failing filter in Omni before editing."
            )
        elif kind == "Query":
            guidance = (
                f"In {scope}, inspect the affected query's field and topic references in Content Validator "
                "before editing."
            )
        else:
            guidance = f"In {scope}, inspect the affected document in Content Validator before editing."
        return f"Content validation / {kind}", guidance
    if _is_coverage_gap(issue):
        return (
            "Downstream contracts / Coverage unavailable",
            "Verify model identity, dependency-search access, and supported references before rerunning. "
            "Missing coverage does not prove that a change has no consumers.",
        )
    return {
        "model": ("Model validation", "Inspect the indicated model YAML and rerun model validation."),
        "semantic_lint": ("Semantic lint", "Review the indicated YAML and the configured lint rule."),
        "contracts": (
            "Downstream contracts",
            "Review the semantic change and affected consumers; restore compatibility or update them through review.",
        ),
        "ai_eval": (
            "AI eval",
            "Review comparison completeness and approved restricted run evidence; reconcile owned jobs before retrying.",
        ),
        "dbt_impact": ("dbt impact", "Review the dbt relation change, Omni references, and coverage evidence."),
        "dbt_sync": ("dbt synchronization", "Inspect refresh and post-sync evidence before closing deployment."),
    }.get(issue.get("validator"), ("Validation", "Inspect this check's structured evidence before rerunning."))


def _first_identity(issue: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = issue.get(key)
        if isinstance(value, str) and value.strip() and value.strip() != "[REDACTED]":
            return value.strip()
    return None


def _affected_object(issue: dict[str, Any]) -> str:
    document = _first_identity(issue, ("document_name", "content_name"))
    identifier = _first_identity(issue, ("document_id", "document_identifier", "content_id"))
    query = _first_identity(issue, ("query_name",))
    parts = []
    if document:
        parts.append(f"Document `{_safe_code(document)}`")
    if identifier:
        parts.append(f"document ID `{_safe_code(identifier)}`")
    if query:
        parts.append(f"query `{_safe_code(query)}`")
    if not parts:
        location = _first_identity(issue, ("file", "yaml_path", "field", "previous_field", "name"))
        parts.append(f"Object `{_safe_code(location)}`" if location else "Object identity unavailable")
    return "; ".join(parts + _scope_parts(issue))


def _branch_identity(issue: dict[str, Any]) -> str | None:
    branch = _first_identity(issue, ("branch_id", "branch_name"))
    if branch:
        return branch
    return "main" if "branch_id" in issue and issue["branch_id"] is None else None


def _scope_parts(issue: dict[str, Any]) -> list[str]:
    model = _first_identity(issue, ("model_id",))
    branch = _branch_identity(issue)
    return ([f"Model `{_safe_code(model)}`"] if model else []) + (
        [f"branch `{_safe_code(branch)}`"] if branch else []
    )


def _readable_message(value: Any) -> str | None:
    if not isinstance(value, str) or value.strip().lower() in {"", "null", "none", "[redacted]"}:
        return None
    # Object-like legacy diagnostics may be JSON, Python reprs, truncated, or
    # deeply nested. Never parse or dump them; fixed category guidance is safer.
    bounded = value[:2000].strip()
    if bounded.startswith("{") or re.match(
        r'''\[\s*(?:[\[\]{'"\d-]|(?:true|false|null|None|True|False|NaN|Infinity)\b)''', bounded
    ):
        return None
    # Ordinary labels and Markdown links such as [warning] or [details](...)
    # are human diagnostics, not serialized arrays; the renderer still escapes them.
    return bounded


def _content_state_lines(report: dict[str, Any], issues: list[dict[str, Any]]) -> list[str]:
    content = [issue for issue in issues if issue.get("validator") == "content"]
    incomplete = _checks_incomplete(report, {"content"})
    summary = report.get("summary", {})
    counts = {
        state: sum(issue.get("state") == state for issue in content)
        if content else summary.get(f"{state}_issues", 0)
        for state in ("new", "existing", "resolved")
    }
    if incomplete:
        if not any(counts.values()):
            return ["- Content comparison unavailable: new, existing, and resolved issue counts were not established."]
        return [
            f"- Content comparison partial: recorded new `{_safe_code(counts['new'])}`, "
            f"existing `{_safe_code(counts['existing'])}`, resolved `{_safe_code(counts['resolved'])}`; "
            "contexts with incomplete or unavailable evidence are not counted."
        ]
    return [
        f"- Content issue history only: new `{_safe_code(counts['new'])}`, "
        f"existing `{_safe_code(counts['existing'])}`, resolved `{_safe_code(counts['resolved'])}`. "
        "These counts are not the blocking total across all checks."
    ]


def _tool_revision_lines(report: dict[str, Any]) -> list[str]:
    revision = report.get("tool_revision")
    if isinstance(revision, dict) and revision.get("source") == "github_action_ref":
        commit = revision.get("commit")
        if isinstance(commit, str) and re.fullmatch(r"[0-9a-fA-F]{40}", commit):
            return [f"- OmniFlow Action revision: `{commit}` (source: `github_action_ref`)"]
    return ["- OmniFlow Action revision: unavailable (not inferred from the input/PR Git SHA)."]


def _safe_text(value: Any) -> str:
    text = _normalized_text(value)
    for character in ("\\", "`", "*", "_", "{", "}", "[", "]", "(", ")", "#", "+", "-", ".", "!", "|", "~"):
        text = text.replace(character, f"\\{character}")
    return text.replace("@", "&#64;")


def _safe_code(value: Any) -> str:
    return (_normalized_text(value).strip() or "unavailable").replace("`", "'").replace("@", "&#64;")


def _normalized_text(value: Any) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    return (
        redact(str(value))
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
