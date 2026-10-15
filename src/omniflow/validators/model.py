from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..exceptions import OmniAPIError, OmniResponseError
from ..model_response import normalize_model_response
from ..omni_client import OmniClient
from ..timestamps import utc_now_iso


def parse_model_issues(payload: Any) -> list[dict[str, Any]]:
    issues = []
    for item in normalize_model_response(payload):
        is_warning = item["is_warning"]
        issues.append(
            {
                "validator": "model",
                "type": "model_validation_issue",
                "severity": "warning" if is_warning else "error",
                "message": item["message"],
                "yaml_path": item["yaml_path"],
                "auto_fix": item["auto_fix"],
            }
        )
    return issues


def _identity(issue: dict[str, Any]) -> tuple[str, str] | None:
    # Match exact private evidence, never redacted/public messages or auto-fix
    # suggestions. The API does not supply a stable validation issue ID.
    path, message = issue.get("yaml_path"), issue.get("message")
    if not all(isinstance(value, str) and value.strip() for value in (path, message)):
        return None
    return path, message


def _compare(current: list[dict], baseline: list[dict]) -> tuple[list[dict], int]:
    groups: dict[tuple[str, str], dict[str, list[dict]]] = defaultdict(
        lambda: {"current": [], "baseline": []}
    )
    ambiguous = 0
    output = []
    for side, issues in (("current", current), ("baseline", baseline)):
        for issue in issues:
            key = _identity(issue)
            if key is None:
                ambiguous += 1
                if side == "current":
                    output.append(dict(issue, active=True, blocking=False))
            else:
                groups[key][side].append(issue)
    for group in groups.values():
        previous = {severity: [i for i in group["baseline"] if i["severity"] == severity]
                    for severity in ("error", "warning")}
        unmatched = {"error": [], "warning": []}
        # Match equal severities first, so iteration order cannot consume the
        # wrong occurrence and manufacture or conceal a severity regression.
        for issue in group["current"]:
            severity = issue["severity"]
            if previous[severity]:
                previous[severity].pop()
                output.append(dict(issue, state="existing", active=True, blocking=False))
            else:
                unmatched[severity].append(issue)
        for severity, opposite in (("error", "warning"), ("warning", "error")):
            for issue in unmatched[severity]:
                state = "new"
                if previous[opposite]:
                    previous[opposite].pop()
                    if severity == "warning":
                        state = "improved"
                output.append(dict(issue, state=state, active=True, blocking=False))
        for remaining in previous.values():
            output.extend(dict(issue, state="resolved", active=False, blocking=False) for issue in remaining)
    return output, ambiguous


def _fetch(client: OmniClient, model_id: str, branch_id: str | None, mode: str) -> list[dict]:
    try:
        return parse_model_issues(client.validate_model(model_id, branch_id=branch_id))
    except OmniResponseError as exc:
        exc.validation_scope = "branch" if branch_id else "base"
        exc.comparison_mode = mode
        raise


def _evidence_issue(reason: str) -> dict[str, Any]:
    return {
        "validator": "model", "type": "model_evidence_unavailable", "reason_code": reason,
        "severity": "error", "blocking": True, "active": True, "coverage_complete": False,
        "message": "Model comparison evidence is unavailable or ambiguous; no regression conclusion can be made.",
    }


def run_model_validation(
    *,
    client: OmniClient,
    model_id: str,
    branch_id: str | None,
    fail_on_warnings: bool = False,
    fail_on_new_only: bool = False,
) -> tuple[dict[str, Any], int]:
    # Without a distinct branch this is a strict model health check, never a
    # comparison against the model itself (which would forgive every error).
    new_only = fail_on_new_only and bool(branch_id)
    mode = "new_only" if new_only else "all_issues"
    current = _fetch(client, model_id, branch_id, mode)
    baseline = None
    ambiguous = 0
    status = "not_applicable"
    evidence = []
    if new_only:
        try:
            baseline = _fetch(client, model_id, None, mode)
        except OmniAPIError as exc:
            status = "unavailable"
            reason = exc.reason_code if isinstance(exc, OmniResponseError) else "model_comparison_unavailable"
            evidence = [dict(_evidence_issue(reason), validation_scope="base")]
        if baseline is not None:
            issues, ambiguous = _compare(current, baseline)
            status = "ambiguous" if ambiguous else "complete"
            if ambiguous:
                evidence = [_evidence_issue("ambiguous_model_identity")]
        else:
            issues = [dict(issue, active=True, blocking=False) for issue in current]
    else:
        issues = [dict(issue, active=True) for issue in current]
    for issue in issues:
        policy_error = issue["severity"] == "error" or (fail_on_warnings and issue["severity"] == "warning")
        issue["blocking"] = policy_error and (not new_only or issue.get("state") == "new")
        issue["validation_scope"] = "base" if issue.get("state") == "resolved" else "branch" if branch_id else "base"
    error_count = sum(issue["severity"] == "error" for issue in current)
    warning_count = sum(issue["severity"] == "warning" for issue in current)
    new_count = sum(issue.get("state") == "new" for issue in issues)
    blocking = sum(issue["blocking"] for issue in issues)
    report = {
        "tool": "omniflow",
        "validator": "model",
        "generated_at": utc_now_iso(),
        "model_id": model_id,
        "branch_id": branch_id,
        "comparison_mode": mode,
        "comparison_source": "base_model" if new_only else "none",
        "comparison_status": status,
        "comparison_ambiguous_issues": ambiguous,
        "model_health": "issues_in_scope" if current else "no_issues_in_scope",
        "regression_status": ("new_issues_detected" if new_count else "no_new_issues_detected")
        if status == "complete" else "unknown",
        "current_issues": len(current),
        "baseline_issues": len(baseline) if baseline is not None else None,
        "new_issues": new_count,
        "existing_issues": sum(issue.get("state") == "existing" for issue in issues),
        "resolved_issues": sum(issue.get("state") == "resolved" for issue in issues),
        "improved_issues": sum(issue.get("state") == "improved" for issue in issues),
        "blocking_issues": blocking + len(evidence),
        "validation_complete": not evidence,
        "summary": {
            "total_issues": len(current),
            "errors": error_count,
            "warnings": warning_count,
        },
        "issues": [*evidence, *issues],
    }
    exit_code = 4 if evidence else 1 if blocking else 0
    return report, exit_code
