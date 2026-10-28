"""Minimal public diagnostics, deliberately not a general-purpose DLP filter.

Only fixed vocabulary, bounded counts and booleans cross this boundary. Detailed
decision evidence remains in memory (or explicitly retained restricted output).
Never add a free-form string field here merely to make a renderer more helpful.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import Any
from uuid import uuid4

from .diagnostic_catalog import CHANGE_MESSAGES, CHECK_GUIDANCE, LINT_MESSAGES
from .diagnostic_contract import CATALOG_VERSION, DOCUMENT_IDENTITY_REASONS, REASON_DETAILS, diagnostic_descriptor
from .exceptions import API_OPERATIONS, TRANSPORT_REASON_CODES, MissingPolicyError, exception_diagnostic
from .repository_policy import POLICY_REASONS
from .validators.content_identity import IDENTITY_MESSAGES

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
    "ai_eval", "ai_repair", "query_validation", "breaking_change_hold", "reporting",
})
TYPES = frozenset({
    "content_evidence_unavailable", "content_validation_issue", "validation_execution_failed",
    "model_evidence_unavailable",
    "query_validation_issue", "query_validation_unavailable",
    "internal_execution_failure", "dependency_coverage_gap", "candidate_verification_failed",
    "breaking_change_sync_state_unavailable", "breaking_change_dbt_overlap",
    "breaking_change_pending_deployment", "dbt_exposures", "model_validation_issue",
    "field_deleted", "field_added", "field_renamed", "field_type_changed", "field_changed",
    "file_renamed", "governance_changed", "relationship_changed", "relationship_deleted",
    "relationship_added", "view_deleted", "view_added", "topic_changed", "topic_deleted",
    "omniflow_finding", *CHANGE_MESSAGES,
})
REASONS = frozenset({
    "dbt_column_removal_orphans_omni_field", "dbt_model_removal_orphans_omni_view", "missing_model_identity",
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
    "unsupported_yaml_construct", "missing_policy_config",
    "model_comparison_unavailable", "ambiguous_model_identity", "content_comparison_unavailable", *IDENTITY_MESSAGES,
    *REASON_DETAILS, *TRANSPORT_REASON_CODES,
})
CANDIDATE_REASONS = frozenset({
    "api_snapshot_unavailable", "content_mismatch", "duplicate_git_path", "empty_git_snapshot",
    "file_inventory_mismatch", "git_snapshot_unavailable", "incomplete_git_snapshot", "invalid_api_snapshot",
    "invalid_git_inventory", "invalid_pr_revision", "invalid_snapshot_encoding", "missing_candidate_branch",
    "missing_pr_revision", "snapshot_limit_exceeded", "unsafe_git_entry", "unsafe_snapshot_path",
    "changed_during_validation", "verification_unavailable", "ambiguous_file_identity",
    "non_omni_source_conflict", "non_omni_rule_conflict",
})
ENUMS = {
    "validator": VALIDATORS, "type": TYPES, "issue_type": {"content", "dashboard_filter", "query"},
    "severity": {"error", "warning", "warn", "info"},
    "risk": {"info", "warning", "governance_sensitive", "security_sensitive", "breaking"},
    "risk_level": {"info", "warning", "governance_sensitive", "security_sensitive", "breaking"},
    "policy_decision": {"pass", "fail", "skipped", "advisory"},
    "decision_status": {"complete", "incomplete", "no_impact"},
    "enforcement_mode": {"strict", "advisory"},
    "content_scope_mode": {"broad", "targeted"},
    "content_scope_reason": {
        "targeted_disabled", "semantic_coverage_unverified", "draft_coverage_unverified",
        "targeted_request_limit", "unsupported_semantic_change", "empty_semantic_scope",
        "qualified_semantic_scope", "targeted_inventory_changed",
        "strict_health_scope", "targeted_inventory_unverified", "targeted_overlap_inconsistent",
    },
    "operation": {"validation", "dbt_impact", "dbt_sync", "ai_repair", "ai_repair_beta"},
    "validation_scope": {"branch", "base"}, "state": {"new", "existing", "resolved", "improved"},
    "status": {"completed", "failed", "not_run", "disabled", "skipped", "passed", "unknown",
               "verified", "unverified", "pending_recheck", "not_applicable", "success", "refused",
               "proposed", "rolled_back", "manual_review_required", "available", "unavailable", "committed",
               "not_needed", "rollback_failed", "partial", "status_unknown", "timed_out", "running", "generated"},
    # Uploaded status/URLs belong to the workflow's current-run receipt, never
    # untrusted input reports or a CLI that runs before artifact publication.
    "publication_status": {"unconfirmed"},
    "execution_status": {"completed", "skipped", "partial", "unavailable", "failed", "not_applicable"},
    "service": {"omni", "github", "git"},
    "api_operation": API_OPERATIONS,
    "cause_code": REASONS,
    "transport_reason": TRANSPORT_REASON_CODES,
    "local_git_reason": TRANSPORT_REASON_CODES,
    "expected_type": {"mapping", "non_empty_mapping", "list", "non_empty_list", "string", "non_empty_string",
                      "boolean", "integer", "enum", "repository_path", "https_url"},
    "source_kind": {"policy", "model_metadata", "model_yaml"},
    "operation_phase": {"start", "poll", "validation", "cleanup", "rollback", "commit",
                        "prompt_set_preflight", "run_creation", "run_polling", "run_cleanup"},
    "last_observed_status": {"RUNNING", "COMPLETED", "FAILED", "UNKNOWN"},
    "skipped_reason": {"no_branch", "no_matching_prompt_set"},
    "incomplete_reason": {"final_recheck_not_run", "check_interrupted"},
    "contents_comparison": {"not_compared", "different", "equal", "partial"},
    "blocked_by": VALIDATORS,
    "diagnostics_reason": POLICY_REASONS | {"no_trusted_run_context", "snapshot_evidence_unavailable", "repository_report_unavailable"},
    "validation_status": {"passed", "failed", "skipped", "incomplete"},
    "query_validation_status": {"disabled", "passed", "regression", "inconclusive"},
    "query_validation_mode": {"off", "report", "enforce"},
    "assertion_type": {"null_rate", "unique_grain", "group_count", "aggregate_delta", "rate_delta", "aggregate_range"},
    "query_assertion_type": {"null_rate", "unique_grain", "group_count", "aggregate_delta", "rate_delta", "aggregate_range"},
    "post_sync_validation_status": {"not_run", "failed", "passed", "incomplete"},
    "coverage_status": {"available", "partial", "unavailable", "unknown"},
    "comparison_mode": {"all_issues", "new_only", "unknown"},
    "requested_validation_depth": {"api_default", "full"},
    "comparison_status": {"complete", "ambiguous", "baseline_unavailable", "unavailable", "not_applicable"},
    "comparison_source": {"base_model", "history", "none"},
    "content_health": {"no_issues_in_scope", "issues_in_scope", "unknown"},
    "model_health": {"no_issues_in_scope", "issues_in_scope", "unknown"},
    "regression_status": {"new_issues_detected", "no_new_issues_detected", "unknown"},
    "classification": {"comment_only", "meaningful", "unverified", "disabled"},
    "reason_code": REASONS,
    "candidate_reason": CANDIDATE_REASONS,
    "metadata_stage": {"view_names"},
    "metadata_reason": {"unsupported_orientation", "duplicate_name", "duplicate_path", "invalid_identity",
                        "non_view_identity", "incomplete_identity"},
    "history_status": {"not_used_live_base", "not_found", "missing", "accepted", "invalidated_context",
                       "invalidated_version", "invalidated_legacy", "invalidated_unscoped_user",
                       "invalidated_public_diagnostic", "invalidated_identity"},
}
COUNTS = frozenset({
    "exit_code", "total_issues", "errors", "warnings", "new_issues", "existing_issues", "resolved_issues",
    "blocking_issues", "improved_issues",
    "baseline_issues", "current_issues", "comparison_ambiguous_issues", "model_count", "dbt_file_count",
    "total_records", "total_exposures", "unmapped_dashboards", "coverage_gaps", "checks_completed",
    "checks_failed", "checks_not_run", "checks_disabled", "file_count", "changed_lines",
    "git_file_count", "omni_file_count", "git_only_count", "omni_only_count",
    "excluded_file_count", "case_only_count", "directory_only_count", "suffix_only_count",
    "content_selector_count",
    "modified_file_count", "regressed_count", "improved_count", "unchanged_count", "prompt_set_count",
    "refreshes_completed", "refreshes_failed", "post_sync_validations_failed", "refreshed_models", "models_requested",
    "http_status", "attempt_count", "max_attempts", "elapsed_ms", "source_line", "source_column", "entry_index",
    "compared_file_count", "candidate_sample_count", "candidate_sample_omitted_count",
    "failed_finding_count", "failed_check_count",
    "contracts_total", "contracts_checked", "new_issue_count", "existing_issue_count", "query_ordinal",
    "query_submission_count", "query_poll_count", "query_contract_number", "pending_job_count", "occurrence_count",
    "query_assertion_number",
})
FLAGS = frozenset({
    "validation_complete", "coverage_complete", "blocking", "active", "content_validation_required",
    "manual_review_required", "verified", "raw_query_results_stored", "query_execution_acknowledged",
    "operational_failure", "cleanup_required",
    "health_only", "retryable",
    "snapshot_attested", "native_qualified", "remote_execution_may_continue",
})
OBJECTS = frozenset({
    "summary", "change_classification", "candidate_verification", "refresh", "post_sync_validation",
    "full_validation", "rollback", "commit", "change_summary",
    "repository_diagnostics",
})
LISTS = frozenset({
    "issues", "models", "model_reports", "check_reports", "check_states",
    "new_issue_samples", "existing_issue_samples", "resolved_issue_samples", "improved_issue_samples",
    "failed_checks",
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
        elif payload.get("diagnostic_schema_version") == 2:
            # Re-rendering a public report must retain context associations, not
            # copy its labels or collapse distinct models after identity removal.
            reference = value.get("context_reference")
            if isinstance(reference, str) and reference:
                key = ("public_context", reference)
                result["context_reference"] = contexts.setdefault(key, f"context-{len(contexts) + 1}")
        for key, item in value.items():
            if key == "dbt_impact_snapshot" and isinstance(item, dict):
                from .omni_repository import public_snapshot
                result[key] = public_snapshot(item)
            elif key == "content_impact" and isinstance(item, dict):
                from .content_impact_reporting import public_content_impact
                result[key] = public_content_impact(item)
            elif key == "rule_id" and value.get("validator") == "semantic_lint":
                if isinstance(item, str) and item in LINT_MESSAGES:
                    result[key] = item
            elif key in ENUMS and isinstance(item, str) and item in ENUMS[key]:
                result[key] = item
            elif key == "http_status" and type(item) is int and 100 <= item <= 599:
                result[key] = item
            elif key == "document_reference" and isinstance(item, str) and re.fullmatch(r"document-[1-9][0-9]{0,5}", item):
                result[key] = item
            elif key in COUNTS - {"http_status"} and type(item) is int and 0 <= item <= 2**31 - 1:
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
            elif key == "config_key" and isinstance(item, str):
                # Values and user-defined mapping keys must never pass through.
                from .config import CONFIG_DIAGNOSTIC_KEYS
                if item in CONFIG_DIAGNOSTIC_KEYS:
                    result[key] = item
            elif key == "tool_version" and isinstance(item, str) and re.fullmatch(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?(?:\.(?:post|dev)\d+)?", item):
                result[key] = item
            elif key == "tool_revision" and isinstance(item, dict):
                commit = item.get("commit")
                if item.get("source") == "github_action_ref" and isinstance(commit, str) and re.fullmatch(r"[a-fA-F0-9]{40}", commit):
                    result[key] = {"commit": commit.lower(), "source": "github_action_ref"}
        if "exit_code" in result:
            result["exit_code_reason"] = EXIT_REASONS.get(result["exit_code"], "unknown failure")
        if "severity" in value or "type" in value and "validator" in value:
            result.setdefault("type", "omniflow_finding")
            result["message"] = _finding_message(result)
            result.update(diagnostic_descriptor(result))
        return result

    safe = project(payload)
    safe.update(tool="omniflow", diagnostic_schema_version=2, diagnostic_catalog_version=CATALOG_VERSION,
                run_reference=_run_reference.get() or uuid4().hex)
    return safe


def _finding_message(issue: dict[str, Any]) -> str:
    descriptor = diagnostic_descriptor(issue)
    if descriptor["diagnostic_code"] == "OF-CANDIDATE-VIEW-METADATA":
        return descriptor["title"] + " Validation is incomplete; this does not establish a model or content defect."
    noun = "model" if issue.get("validator") == "model" else "content"
    reason = (issue.get("reason_code") if issue.get("reason_code") in {
                  "dbt_refresh_status_unknown", "ai_repair_timeout", *DOCUMENT_IDENTITY_REASONS}
              else issue.get("cause_code") or issue.get("reason_code"))
    if reason in REASON_DETAILS and reason not in {"empty_api_response", "invalid_json_response"}:
        return REASON_DETAILS[reason][1]
    if issue.get("validator") == "semantic_lint" and issue.get("rule_id") in LINT_MESSAGES:
        return LINT_MESSAGES[issue["rule_id"]]
    if isinstance(issue.get("reason_code"), str) and issue["reason_code"] in IDENTITY_MESSAGES:
        return IDENTITY_MESSAGES[issue["reason_code"]]
    if issue.get("reason_code") == "missing_policy_config":
        return MissingPolicyError.MESSAGE
    if issue.get("reason_code") == "empty_api_response":
        return f"Omni API returned an empty response; validation could not complete. This is not a {noun} finding."
    if issue.get("reason_code") == "invalid_json_response":
        return f"Omni API returned invalid JSON; validation could not complete. This is not a {noun} finding."
    if issue.get("type") == "candidate_verification_failed":
        reasons = {
            "non_omni_source_conflict": (
                "A non-Omni file rule matches an authored Omni source. Narrow the trusted files.non_omni "
                "rule; deployed model files and their dependencies cannot be excluded."
            ),
            "non_omni_rule_conflict": (
                "Multiple non-Omni rules match the same source path. Make the trusted classifications "
                "unambiguous, then rerun."
            ),
            "file_inventory_mismatch": (
                "Git and Omni have unmatched authored artifacts within the selected model. Check missing/extra "
                "files, view database/catalog and schema qualification, topic names, capitalization and the "
                "trusted model_path, then synchronize the intended Omni branch/base and PR Git revision."
            ),
            "ambiguous_file_identity": (
                "Authored artifacts cannot be matched unambiguously. Repeated view filenames across databases or "
                "schemas are valid; preserve canonical Omni identity or catalog/schema/table qualification. "
                "Check duplicate topic names, model/global relationships definitions and export copies. "
                "Inspect repository-only path examples; do not rename valid schema-scoped views."
            ),
            "content_mismatch": (
                "Git and Omni file inventories match, but authored file contents differ. "
                "Save and synchronize the selected Omni branch/base with its PR Git revision, then rerun."
            ),
            "changed_during_validation": (
                "The verified snapshot changed during validation. Wait for synchronization to finish, "
                "then rerun against the current PR revision."
            ),
            "api_snapshot_unavailable": (
                "Omni authored YAML could not be retrieved. Check Omni API access and availability, then rerun."
            ),
            "git_snapshot_unavailable": (
                "The immutable PR Git snapshot could not be retrieved. Check repository access and checkout "
                "history, then rerun."
            ),
            "empty_git_snapshot": (
                "No authored model files were found at the trusted model_path in the PR Git revision. "
                "Check the exact registered path and initial model export."
            ),
            "missing_candidate_branch": (
                "No corresponding Omni candidate branch was selected. Create or synchronize the matching "
                "Omni branch before rerunning."
            ),
        }
        message = reasons.get(issue.get("candidate_reason"), (
            "The selected Omni scope could not be verified against the corresponding pull-request Git revision. "
            "Check branch/base synchronization and the reason category, then rerun."
        ))
        counts = ", ".join(
            f"{label}: {issue[key]}" for key, label in (
                ("git_file_count", "Git files"), ("omni_file_count", "Omni files"),
                ("git_only_count", "Git-only"), ("omni_only_count", "Omni-only"),
                ("case_only_count", "case-only pairs"), ("directory_only_count", "directory-only pairs"),
                ("suffix_only_count", "suffix-only pairs"),
            ) if type(issue.get(key)) is int and 0 <= issue[key] <= 2**31 - 1
        )
        if counts:
            message += f" Counts: {counts}."
        return message + (
            " Validation is incomplete; this does not establish a model or content defect. "
            "Check execution states for checks not run. Do not bypass verification."
        )
    if issue.get("reason_code") == "content_comparison_unavailable":
        return ("Content validation comparison could not be established; this does not prove a new content defect. "
                "Restore complete, unambiguous baseline and branch evidence before rerunning.")
    if issue.get("type") == "content_evidence_unavailable":
        return ("Content Validator returned an unsupported issue structure; comparison is unavailable. "
                "This does not establish a new content defect. Review the reason code and the same scope in Omni.")
    if issue.get("reason_code") in {"model_comparison_unavailable", "ambiguous_model_identity"}:
        return ("Model validation comparison could not be established; this does not prove a new model defect. "
                "Restore complete, unambiguous baseline and branch evidence before rerunning.")
    if issue.get("type") == "dependency_coverage_gap":
        return ("Downstream dependency coverage is incomplete. Verify content read permissions and reference lookup "
                "coverage, then rerun. Missing evidence does not establish that dependent content is safe.")
    if issue.get("type") in {"validation_execution_failed", "internal_execution_failure"}:
        return "This check did not complete. " + CHECK_GUIDANCE.get(issue.get("validator"), CHECK_GUIDANCE["execution"])
    if issue.get("validator") in {"contracts", "semantic_diff"} and issue.get("type") in CHANGE_MESSAGES:
        return CHANGE_MESSAGES[issue["type"]]
    if issue.get("state") == "existing":
        if issue.get("active") is False:
            return f"Existing {noun} issue is inactive and does not block this run."
        if issue.get("blocking") is False:
            return f"Existing {noun} issue matched the baseline and does not block under the configured policy."
        if issue.get("blocking") is True:
            return f"Existing {noun} issue matched the baseline, but the configured policy still blocks it."
        return f"Existing {noun} issue; blocking status follows the configured comparison policy."
    if issue.get("state") == "resolved":
        return f"Previously reported {noun} issue is resolved."
    if issue.get("state") == "improved":
        return f"Previously reported {noun} issue has improved; current severity remains visible."
    if issue.get("type") == "model_validation_issue":
        return "Model validation reported an issue. Validate the same model branch in Omni and inspect its reported definition errors."
    if issue.get("type") == "content_validation_issue":
        if issue.get("issue_type") == "dashboard_filter":
            return ("Dashboard-filter validation failed. Open Content Validator for the same model and branch, "
                    "inspect the reported dashboard filter, and correct its configuration or model references.")
        if issue.get("issue_type") == "query":
            return ("Content query validation failed. Open Content Validator for the same model and branch "
                    "and inspect the reported query and model references.")
        return "Content validation failed. Open Content Validator in Omni for the same model and branch."
    return ("No recognized diagnostic category was supplied for this check. "
            + CHECK_GUIDANCE.get(issue.get("validator"), CHECK_GUIDANCE["execution"]))


def safe_exception_message(exc: Exception) -> str:
    code = getattr(exc, "exit_code", 6)
    category = EXIT_REASONS.get(code, "internal tool error") if type(code) is int else "internal tool error"
    reason = getattr(exc, "reason_code", None)
    suffix = f" ({reason})" if isinstance(reason, str) and reason in REASONS else ""
    if isinstance(reason, str) and reason in {"empty_api_response", "invalid_json_response", "missing_policy_config"}:
        return f"{_finding_message({'reason_code': reason})} Reason: {reason}."
    issue = public_diagnostic({"issues": [{"validator": "setup" if code == 2 else "execution",
                                          "severity": "error", "type": "validation_execution_failed",
                                          **exception_diagnostic(exc)}]})["issues"][0]
    return (f"{issue['diagnostic_code']}: OmniFlow {category}{suffix}. {issue['message']} "
            f"Next: {issue['next_action']}")
