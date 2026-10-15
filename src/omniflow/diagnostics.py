"""Minimal public diagnostics, deliberately not a general-purpose DLP filter.

Only fixed vocabulary, bounded counts and booleans cross this boundary. Detailed
decision evidence remains in memory (or explicitly retained restricted output).
Never add a free-form string field here merely to make a renderer more helpful.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import Any
from uuid import uuid4

_receipt: ContextVar[list | None] = ContextVar("omniflow_private_receipt", default=None)
_run_reference: ContextVar[str | None] = ContextVar("omniflow_run_reference", default=None)


@contextmanager
def diagnostic_session():
    """One random, non-content-derived reference for all outputs in a CLI run."""
    token = _run_reference.set(uuid4().hex)
    try:
        yield
    finally:
        _run_reference.reset(token)


@contextmanager
def capture_private_reports():
    """Internal readiness evidence; no secret-bearing file or public side channel."""
    reports: list[dict[str, Any]] = []
    token = _receipt.set(reports)
    try:
        yield reports
    finally:
        _receipt.reset(token)


def record_private_report(report: dict[str, Any]) -> None:
    sink = _receipt.get()
    if sink is not None:
        sink.append(deepcopy(report))


VALIDATORS = frozenset({
    "context", "setup", "execution", "content", "model", "candidate", "semantic_diff",
    "semantic_lint", "downstream", "contracts", "dbt_exposures", "dbt_impact", "dbt_sync",
    "ai_eval", "ai_repair", "breaking_change_hold", "reporting",
})
TYPES = frozenset({
    "content_evidence_unavailable", "content_validation_issue", "validation_execution_failed",
    "internal_execution_failure", "dependency_coverage_gap", "candidate_verification_failed",
    "breaking_change_sync_state_unavailable", "breaking_change_dbt_overlap",
    "breaking_change_pending_deployment", "dbt_exposures", "model_validation_issue",
    "field_deleted", "field_added", "field_renamed", "field_type_changed", "field_changed",
    "file_renamed", "governance_changed", "relationship_changed", "relationship_deleted",
    "relationship_added", "view_deleted", "view_added", "topic_changed", "topic_deleted",
    "omniflow_finding",
})
REASONS = frozenset({
    "invalid_issue_container", "invalid_issue_entry", "missing_issue_message",
    "invalid_issue_message", "unsupported_issue_shape", "invalid_message_type", "blank_issue_message",
    "invalid_filter_identity", "empty_api_response", "invalid_json_response",
    "live_content_provenance_unavailable", "classification_disabled", "not_pull_request",
    "missing_pr_revision", "invalid_pr_revision", "snapshot_unavailable", "snapshot_limit_exceeded",
    "inventory_changed", "unsupported_yaml", "meaningful_change", "comment_only",
    "missing_model_scope", "file_inventory_changed", "file_type_or_mode_changed", "no_changed_files",
    "empty_model_inventory", "unsupported_changed_path", "authored_content_changed", "invalid_git_inventory",
    "unsupported_git_entry", "incomplete_git_snapshot", "ambiguous_yaml_key", "duplicate_yaml_key",
    "empty_yaml_document", "external_dependency_unverified", "invalid_or_unsupported_yaml",
    "unsupported_yaml_construct",
})
ENUMS = {
    "validator": VALIDATORS, "type": TYPES, "issue_type": {"content", "dashboard_filter", "query"},
    "severity": {"error", "warning", "warn", "info"},
    "risk": {"info", "warning", "governance_sensitive", "security_sensitive", "breaking"},
    "risk_level": {"info", "warning", "governance_sensitive", "security_sensitive", "breaking"},
    "policy_decision": {"pass", "fail", "skipped"},
    "operation": {"validation", "dbt_impact", "dbt_sync", "ai_repair", "ai_repair_beta"},
    "validation_scope": {"branch", "base"}, "state": {"new", "existing", "resolved"},
    "status": {"completed", "failed", "not_run", "disabled", "skipped", "passed", "unknown",
               "verified", "unverified", "pending_recheck", "not_applicable", "success", "refused",
               "proposed", "rolled_back", "manual_review_required", "available", "unavailable", "committed",
               "not_needed", "rollback_failed"},
    "validation_status": {"passed", "failed", "skipped"},
    "post_sync_validation_status": {"not_run", "failed", "passed"},
    "coverage_status": {"available", "partial", "unavailable", "unknown"},
    "comparison_mode": {"all_issues", "new_only", "unknown"},
    "comparison_status": {"complete", "ambiguous", "baseline_unavailable", "unavailable"},
    "comparison_source": {"base_model", "history"},
    "content_health": {"no_issues_in_scope", "issues_in_scope", "unknown"},
    "regression_status": {"new_issues_detected", "no_new_issues_detected", "unknown"},
    "classification": {"comment_only", "meaningful", "unverified", "disabled"},
    "reason_code": REASONS,
    "metadata_stage": {"view_names"},
    "metadata_reason": {"unsupported_orientation", "duplicate_name", "duplicate_path", "invalid_identity",
                        "non_view_identity", "incomplete_identity"},
    "history_status": {"not_used_live_base", "not_found", "missing", "accepted", "invalidated_context",
                       "invalidated_version", "invalidated_legacy", "invalidated_unscoped_user",
                       "invalidated_public_diagnostic"},
}
COUNTS = frozenset({
    "exit_code", "total_issues", "errors", "warnings", "new_issues", "existing_issues", "resolved_issues",
    "baseline_issues", "current_issues", "comparison_ambiguous_issues", "model_count", "dbt_file_count",
    "total_records", "total_exposures", "unmapped_dashboards", "coverage_gaps", "checks_completed",
    "checks_failed", "checks_not_run", "checks_disabled", "file_count", "changed_lines",
    "modified_file_count", "regressed_count", "improved_count", "unchanged_count", "prompt_set_count",
    "refreshes_completed", "refreshes_failed", "post_sync_validations_failed", "refreshed_models", "models_requested",
})
FLAGS = frozenset({
    "validation_complete", "coverage_complete", "blocking", "active", "content_validation_required",
    "manual_review_required", "verified", "raw_query_results_stored", "query_execution_acknowledged",
    "operational_failure", "cleanup_required",
})
OBJECTS = frozenset({
    "summary", "change_classification", "candidate_verification", "refresh", "post_sync_validation",
    "full_validation", "rollback", "commit", "change_summary",
})
LISTS = frozenset({
    "issues", "models", "model_reports", "check_reports", "check_states",
    "new_issue_samples", "existing_issue_samples", "resolved_issue_samples",
})
EXIT_REASONS = {
    0: "success", 1: "validation failed", 2: "configuration error", 3: "authorization error",
    4: "Omni API error", 5: "security policy violation", 6: "internal tool error",
}


def public_diagnostic(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a new schema, never copy unknown fields or unbounded source text."""
    contexts: dict[tuple, str] = {}

    def project(value: dict, depth: int = 0) -> dict:
        if depth > 20:
            raise ValueError("Diagnostic nesting limit exceeded")
        result: dict[str, Any] = {}
        identity = tuple(value.get(key) for key in ("model_id", "branch_id", "branch_name"))
        if any(identity) and all(item is None or isinstance(item, str) for item in identity):
            result["context_reference"] = contexts.setdefault(identity, f"context-{len(contexts) + 1}")
        for key, item in value.items():
            if key in ENUMS and isinstance(item, str) and item in ENUMS[key]:
                result[key] = item
            elif key in COUNTS and type(item) is int and 0 <= item <= 2**31 - 1:
                result[key] = item
            elif key == "baseline_issues" and item is None:
                result[key] = None
            elif key in FLAGS and type(item) is bool:
                result[key] = item
            elif key in OBJECTS and isinstance(item, dict):
                result[key] = project(item, depth + 1)
            elif key in LISTS and isinstance(item, list):
                result[key] = [project(child, depth + 1) for child in item if isinstance(child, dict)]
            elif key == "modified_files" and isinstance(item, list):
                result["modified_file_count"] = len(item)
        if "exit_code" in result:
            result["exit_code_reason"] = EXIT_REASONS.get(result["exit_code"], "unknown failure")
        if "severity" in value or "type" in value and "validator" in value:
            result.setdefault("type", "omniflow_finding")
            result["message"] = _finding_message(result)
        return result

    safe = project(payload)
    safe.update(tool="omniflow", diagnostic_schema_version=2, run_reference=_run_reference.get() or uuid4().hex)
    return safe


def _finding_message(issue: dict[str, Any]) -> str:
    if issue.get("reason_code") == "empty_api_response":
        return "Omni API returned an empty response; validation could not complete. This is not a content finding."
    if issue.get("reason_code") == "invalid_json_response":
        return "Omni API returned invalid JSON; validation could not complete. This is not a content finding."
    if issue.get("type") == "content_evidence_unavailable":
        return ("Content Validator returned an unsupported issue structure; comparison is unavailable. "
                "This does not establish a new content defect. Review the reason code and the same scope in Omni.")
    if issue.get("state") == "existing":
        return "Existing content issue; blocking status follows the configured comparison policy."
    if issue.get("state") == "resolved":
        return "Previously reported content issue is resolved."
    if issue.get("type") == "content_validation_issue":
        if issue.get("issue_type") == "dashboard_filter":
            return "Dashboard-filter validation finding; inspect Content Validator in Omni for the same model and branch."
        return "Content validation finding; review new-versus-existing counts and Content Validator in Omni."
    return "OmniFlow finding; sensitive details are withheld from shareable diagnostics."


def safe_exception_message(exc: Exception) -> str:
    code = getattr(exc, "exit_code", 6)
    category = EXIT_REASONS.get(code, "internal tool error") if type(code) is int else "internal tool error"
    reason = getattr(exc, "reason_code", None)
    suffix = f" ({reason})" if isinstance(reason, str) and reason in REASONS else ""
    if isinstance(reason, str) and reason in {"empty_api_response", "invalid_json_response"}:
        return f"{_finding_message({'reason_code': reason})} Reason: {reason}."
    return f"OmniFlow {category}{suffix}. Review the sanitized report and trusted configuration; details withheld."
