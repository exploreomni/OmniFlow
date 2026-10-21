"""Short, public-safe notification; detailed evidence stays in report artifacts.

GitHub comment creation is not proof of email delivery. Actions' own notification
email is controlled by GitHub and is not customized by this renderer.
"""
from __future__ import annotations

import re
from typing import Any

from ..diagnostics import ENUMS, public_diagnostic
from .finding_groups import group_findings
from .issue_policy import is_blocking


def query_result_coverage(report: dict[str, Any]) -> str:
    """Only explicit query-result evidence can establish this extra coverage."""
    contexts = report.get("model_reports") or [report]
    checks = []
    missing = False
    active_state = False
    for context in contexts:
        scoped = [check for check in context.get("check_reports", [context])
                  if check.get("validator") == "query_validation"]
        states = [state for state in context.get("check_states", [])
                  if state.get("validator") == "query_validation"]
        checks.extend(scoped)
        active_state |= any(state.get("status") != "disabled" for state in states)
        if any(state.get("status") not in {"disabled", "completed"} for state in states):
            missing = True
        if not scoped and not (states and all(state.get("status") == "disabled" for state in states)):
            # If another model ran queries, absent evidence in this model is not
            # an implicit disabled check. Explicit disabled states are required.
            missing = True
    enabled = [check for check in checks if check.get("query_validation_status") != "disabled"]
    if not enabled and not active_state:
        return "Not tested — model/content validation does not establish query-result correctness."
    if any(check.get("query_validation_status") == "regression" for check in enabled):
        return ("Failed for one or more configured query-result checks; inspect detailed evidence."
                + (" Other model query coverage is incomplete." if missing else ""))
    if not missing and enabled and all(check.get("query_validation_status") == "passed"
                                       and check.get("validation_complete") is True for check in enabled):
        return "Passed for the explicitly configured query-result checks only; not all business logic."
    return "Inconclusive — query-result checks did not establish complete result evidence."


def query_policy_reference(issue: dict[str, Any]) -> str | None:
    """One-based trusted policy positions only; never contract IDs or values."""
    contract = issue.get("query_contract_number")
    assertion = issue.get("query_assertion_number")
    kind = issue.get("query_assertion_type")
    if issue.get("validator") != "query_validation" or type(contract) is not int or not 1 <= contract <= 2**31 - 1:
        return None
    reference = f"trusted policy `checks.query_validation.contracts` entry {contract}"
    if type(assertion) is int and 1 <= assertion <= 2**31 - 1:
        reference += f", assertion {assertion}"
    if isinstance(kind, str) and kind in ENUMS["query_assertion_type"]:
        reference += f" (`{kind}`)"
    return reference


def notification_decision(report: dict[str, Any]) -> str:
    if report.get("policy_decision") == "fail" or any(is_blocking(item) for item in report.get("issues", [])):
        return "fail"
    if type(report.get("exit_code")) is int and report["exit_code"] > 0:
        return "fail"
    if report.get("policy_decision") == "pass" and report.get("validation_complete") is True:
        return "pass"
    if report.get("policy_decision") in {"skipped", "advisory"}:
        return report["policy_decision"]
    return "incomplete"


def render_notification(
    report: dict[str, Any], *, head_sha: str, run_url: str, run_attempt: int,
    publication: list[dict[str, str]] | None = None,
) -> str:
    """Re-project even a repository report; never copy caller-provided prose.

The publisher validates the run URL and publication receipts. This renderer also
restricts their shape, preventing Markdown or identity injection by other callers.
"""
    safe = public_diagnostic(report)
    decision = notification_decision(safe)
    issues = safe.get("issues", [])
    blockers = [item for item in issues if is_blocking(item)]
    nonblocking = [item for item in issues if not is_blocking(item) and item.get("active", True)]
    lint_count = sum(item.get("validator") == "semantic_lint" for item in nonblocking)
    title = {
        "fail": "Action required", "pass": "Validation passed", "skipped": "Skipped — not validated",
        "advisory": "Advisory — impact unverified", "incomplete": "Validation incomplete",
    }[decision]
    head = head_sha if re.fullmatch(r"[0-9a-f]{40}", head_sha) else "unavailable"
    valid_run = re.fullmatch(r"https://[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[1-9][0-9]*", run_url)
    lines = [f"## OmniFlow: {title}", "", f"**Blocking findings: {len(blockers)}.**",
             f"Head: `{head}` · Run attempt: `{run_attempt if type(run_attempt) is int and run_attempt > 0 else 'unknown'}`",
             f"[Open this run]({run_url})" if valid_run else "Run link unavailable.", ""]
    summary = safe.get("summary", {})
    if "new_issues" in summary or "existing_issues" in summary:
        lines.append(f"Compared findings: **{summary.get('new_issues', 0)} new or worsened**, "
                     f"{summary.get('existing_issues', 0)} existing. Counts are occurrences, not unique objects.")
    for issue, count in group_findings(blockers)[:3]:
        policy = query_policy_reference(issue)
        lines.append(f"- **{issue.get('diagnostic_code', 'OF-UNKNOWN')}**: "
                     f"{issue.get('title', 'A blocking check needs review.')} ({count} occurrence(s))"
                     + (f" — {policy}." if policy else ""))
    primary = next(iter(blockers), None)
    if primary:
        lines.extend(["", "**Next action:** " + primary.get("next_action", "Inspect the failed check in this run.")])
    elif decision == "pass":
        lines.append("**Next action:** Review the current-head result and remaining nonblocking findings before merging.")
    else:
        lines.append("**Next action:** Inspect this run's routing, coverage and policy before treating it as validated.")
    lines.extend(["", f"Nonblocking findings: {len(nonblocking)} ({lint_count} lint). "
                  "Lint describes current model health; it is not proof of a new PR regression.",
                  f"**Query-result correctness:** {query_result_coverage(safe)}"])
    if publication is not None:
        lines.extend(["", "### Diagnostic publication", ""])
        for item in publication:
            label = {"public": "Public summary", "repository": "Repository-only detail"}.get(item.get("kind"))
            status = item.get("status")
            if label is None or status not in {"uploaded", "failed", "skipped", "unconfirmed"}:
                continue
            url = item.get("url", "")
            link = (f" — [Download artifact]({url})" if status == "uploaded" and valid_run
                    and re.fullmatch(re.escape(run_url) + r"/artifacts/[1-9][0-9]*", url) else "")
            if status == "uploaded" and not link:
                status = "unconfirmed"
            lines.append(f"- {label}: **{status}**{link}.")
        lines.append("Publication is separate from validation. Repository-only detail is not a shareable support attachment.")
    lines.extend(["", "Detailed findings and check coverage are in the artifacts; this short summary excludes source text and data."])
    return "\n".join(lines) + "\n"
