"""Versioned, fixed diagnostic vocabulary. No customer text is a template.

Producers preserve observations; this catalog supplies explanations and recovery
instructions. A reason is evidence of a category, not proof of its root cause.
"""
from __future__ import annotations

from .diagnostic_catalog import CHANGE_MESSAGES, CHECK_GUIDANCE, LINT_MESSAGES

CATALOG_VERSION = 1
# code, title, corrective/investigative action, category
REASON_DETAILS: dict[str, tuple[str, str, str, str]] = {}


def _add(reason: str, code: str, title: str, action: str, category: str = "incomplete_evidence") -> None:
    REASON_DETAILS[reason] = (code, title, action, category)


for _service in ("omni", "github"):
    for _reason, _title, _action, _category in (
        ("unauthorized", "The API rejected authentication.",
         "Verify the configured secret is present and valid for this service. Never paste the credential into a report.",
         "access"),
        ("forbidden", "The API denied this operation.",
         "Check this credential's access to the selected target and endpoint, including incompatible request scope. "
         "A 403 alone does not establish which permission is missing.", "access"),
        ("rate_limited", "The API rate limit prevented completion.",
         "Review the recorded retry attempts. Wait for the service's retry window before rerunning; "
         "do not increase concurrency to work around throttling.", "service"),
        ("service_unavailable", "The API returned a server error.",
         "Check service health and retry this read when service recovers. Reconcile any already-started write first.",
         "service"),
        ("http_error", "The API rejected the request.",
         "Review the HTTP status and operation. Verify the target and supported request parameters before rerunning.",
         "service"),
        ("timeout", "The API request timed out.",
         "Check network and service health. Read operations may be retried; reconcile an existing write/job "
         "before starting another.", "service"),
        ("tls_error", "A secure connection to the API could not be established.",
         "Verify the runner's trusted certificate configuration and proxy. Do not disable TLS verification.", "access"),
        ("connection_error", "The runner could not connect to the API.",
         "Check DNS, proxy, outbound network access and service availability from the runner.", "service"),
        ("request_failed", "The API request could not complete.",
         "Use the operation, stage and run reference to investigate runner connectivity or service failure.", "service"),
        ("response_limit_exceeded", "The API response exceeded the bounded evidence limit.",
         "Review the response size and supported pagination with the maintainer. Do not treat truncated evidence "
         "as an empty result.", "incomplete_evidence"),
    ):
        _add(f"{_service}_{_reason}", f"OF-{_service.upper()}-{_reason.upper().replace('_', '-')}",
             _title, _action, _category)

for _reason, _title, _action in (
    ("empty_api_response", "The API returned an empty response.",
     "Retry the read or investigate the service response. An empty body is not an empty validation result."),
    ("invalid_json_response", "The API response was not valid JSON.",
     "Check the service/proxy response through an authorized channel. Do not upload the raw body."),
    ("github_invalid_json_response", "GitHub returned an unreadable JSON response.",
     "Check the GitHub service and runner proxy, then retry the read."),
    ("omni_response_interrupted", "The API response stream ended before evidence was complete.",
     "Check runner connectivity and retry the read; partial response bytes cannot establish validation success."),
    ("github_response_interrupted", "The GitHub response stream ended before evidence was complete.",
     "Check runner connectivity and retry the read; partial response bytes cannot establish validation success."),
    ("omni_pagination_invalid", "The API returned inconsistent pagination evidence.",
     "Share the operation and run reference with the maintainer. Do not discard pages to obtain a pass."),
    ("omni_pagination_limit_exceeded", "The API pagination safety limit was reached.",
     "Review the selected scope and supported pagination limits; coverage is incomplete."),
    ("github_missing_token", "GitHub access credentials are unavailable.",
     "Pass the workflow GitHub token to OmniFlow and grant the required read access. Do not put tokens in YAML."),
    ("github_invalid_repository", "The GitHub repository identity is invalid.",
     "Verify the trusted workflow repository context and owner/repository configuration."),
    ("github_not_found_or_inaccessible", "The GitHub resource is absent or inaccessible to this credential.",
     "Verify both the recorded revision/resource and token access. A 404 does not distinguish absence from denied access."),
    ("git_revision_unavailable", "The recorded Git revision is unavailable.",
     "Fetch the trusted base and exact PR revisions, then rerun against the current event."),
    ("git_missing_executable", "Git is unavailable on the runner.",
     "Install Git in the runner environment and rerun the checkout/setup steps."),
    ("git_timeout", "Reading Git evidence timed out.",
     "Check runner load and repository access before rerunning."),
    ("git_invalid_encoding", "Git evidence is not valid UTF-8 text.",
     "Inspect the affected source through authorized repository diagnostics and use supported UTF-8 model files."),
    ("changed_files_unavailable", "Changed-file discovery could not complete.",
     "Check repository checkout and base revision availability. This is not evidence of an unrelated-files-only change."),
):
    _add(_reason, "OF-" + _reason.upper().replace("_", "-"), _title, _action)

for _reason in (
    "config_invalid_type", "config_invalid_value", "config_unknown_key", "policy_inaccessible",
    "model_metadata_missing", "model_metadata_inaccessible", "model_metadata_parse_error",
    "model_metadata_invalid_type", "model_metadata_invalid_value", "model_metadata_unknown_key", "yaml_parse_error",
    "trusted_file_unavailable", "trusted_file_invalid_encoding", "trusted_file_too_large", "trusted_file_unsafe",
):
    _add(_reason, "OF-" + _reason.upper().replace("_", "-"),
         {
             "config_invalid_type": "A policy setting has an unsupported type.",
             "config_invalid_value": "A policy setting has an unsupported value.",
             "config_unknown_key": "The policy contains an unknown setting.",
             "yaml_parse_error": "YAML syntax could not be parsed.",
             "model_metadata_parse_error": "Trusted model metadata is not valid JSON.",
             "model_metadata_missing": "Trusted .omni/flow.json metadata is missing.",
         }.get(_reason, "Trusted configuration evidence is unavailable or invalid."),
         "Correct the indicated configuration key or parser location using the documented schema. "
         "For pull requests, update the trusted target-branch policy/metadata first; a PR-only policy cannot authorize itself.",
         "configuration")

_add("file_inventory_mismatch", "OF-CANDIDATE-FILES", "Git and Omni have unmatched authored artifacts.",
     "Inspect the repository-only mismatch samples and exact base/candidate scope. Check missing/extra artifacts, "
     "view database/catalog and schema qualification, topic names and capitalization, then contents. "
     "Do not rename or overwrite model files solely to make this check pass.", "synchronization")
_add("content_mismatch", "OF-CANDIDATE-CONTENT", "Matched authored files have different contents.",
     "Inspect the reported file at the recorded Git revision and selected Omni scope; save/synchronize the intended "
     "definitions, then rerun. Do not overwrite either side without reviewing the difference.", "synchronization")
_add("changed_during_validation", "OF-CANDIDATE-DRIFT", "The selected snapshot changed during validation.",
     "Wait for synchronization/editing to finish and rerun against the latest PR revisions.", "synchronization")
_add("missing_candidate_branch", "OF-CANDIDATE-BRANCH", "No Omni candidate branch was selected.",
     "Verify the PR branch association and create or synchronize the intended Omni branch before rerunning.", "configuration")
_add("branch_not_found", "OF-BRANCH-NOT-FOUND", "No corresponding Omni branch was found.",
     "Verify the trusted model and exact PR branch association; create/synchronize the branch if appropriate.", "configuration")
_add("branch_ambiguous", "OF-BRANCH-AMBIGUOUS", "More than one Omni branch matched the selection.",
     "Resolve the ambiguous branch association; do not select the first result arbitrarily.", "configuration")
_add("branch_identity_invalid", "OF-BRANCH-INVALID", "The selected branch identity was malformed.",
     "Verify the supported branch API response using the operation and run reference.")
_add("follower_branch_creation_disabled", "OF-FOLLOWER-BRANCH-CREATION", "The follower cannot create the required PR branch.",
     "Enable Always create branches on the intended follower, synchronize the PR branch, and rerun. "
     "Do not substitute the production base model for the candidate.", "configuration")
_add("git_configuration_mismatch", "OF-SETUP-GIT-CONFIG", "Trusted and Omni Git configuration differ.",
     "Compare the exact registered model_path and base_branch, including case. Correct the authoritative configuration "
     "and run doctor again.", "configuration")

for _reason, _title, _action, _category in (
    ("model_validation_issue", "Omni reported a model definition error or warning.",
     "Use the repository-only source location when available, then validate that same model scope in Omni. "
     "Inspect the indicated definition and references; this category alone does not establish a missing field.", "validation"),
    ("content_validation_issue", "Omni reported a content validation finding.",
     "Open Content Validator for the same model and branch. Review the finding and new-versus-existing counts.", "validation"),
    ("content_query_validation_issue", "A content query failed validation.",
     "Open the affected query in Content Validator for the recorded scope and inspect its model references.", "validation"),
    ("content_dashboard_filter_validation_issue", "A dashboard filter failed validation.",
     "Open the affected dashboard filter in Content Validator for the recorded scope; inspect its referenced fields "
     "and configuration. Do not assume this PR introduced an existing finding.", "validation"),
    ("dbt_exposures_unmapped", "Some dashboards could not be mapped to dbt exposures.",
     "Inspect the selected exposure coverage and model mapping before relying on downstream impact coverage.", "incomplete_evidence"),
    ("dbt_refresh_status_unknown", "A refresh started, but its final status could not be established.",
     "Do not start another refresh automatically. Reconcile the already-started job in Omni through an authorized "
     "channel, then resume validation after its outcome is known.", "incomplete_evidence"),
    ("dbt_refresh_timeout", "The refresh did not finish within the polling window.",
     "The job may still be running. Check its status in Omni before retrying or starting another refresh.", "incomplete_evidence"),
    ("dbt_refresh_failed", "Omni confirmed that the refresh job failed.",
     "Inspect the refresh job in Omni and correct its failure before rerunning post-sync validation.", "service"),
    ("ai_repair_model_errors_remain", "Model errors remain after the repair attempt.",
     "Review the reported model findings. The repair is not validated and must not be committed as a successful fix.", "validation"),
    ("ai_repair_validation_failed", "Post-repair validation did not pass.",
     "Review failed_checks and the nested diagnostic codes. Verify rollback status before making another repair attempt.", "validation"),
    ("ai_repair_job_failed", "Omni reported that the repair job failed.",
     "Inspect the job through an authorized Omni session and verify rollback before another attempt.", "service"),
    ("ai_repair_timeout", "The repair job exceeded the waiting period.",
     "Reconcile the job and rollback status before retrying; a timeout does not establish that writes stopped.", "incomplete_evidence"),
    ("ai_repair_internal_error", "An internal error interrupted repair orchestration.",
     "Retain the run reference and verify rollback/manual-review status. Escalate to the maintainer before retrying.", "internal"),
    ("report_generation_failed", "Validation output could not be published completely.",
     "Do not infer a validation pass from missing output. Check runner disk/output permissions and the run reference.", "reporting"),
):
    _add(_reason, "OF-" + _reason.upper().replace("_", "-"), _title, _action, _category)

_add("ambiguous_file_identity", "OF-CANDIDATE-AMBIGUOUS-FILE-IDENTITY",
     "Authored artifacts cannot be matched to one unambiguous identity.",
     "Inspect the repository-only file examples. Repeated view filenames across databases or schemas are valid: "
     "retain their canonical Omni identity or catalog/schema/table qualification. Keep topic names unique and "
     "one model/global relationships definition. Remove duplicate export copies only after confirming ownership; "
     "identical contents do not resolve ambiguous identity. Do not rename valid schema-scoped views.")

for _reason in ("invalid_api_snapshot", "empty_git_snapshot", "duplicate_git_path",
                "unsafe_git_entry", "unsafe_snapshot_path", "invalid_snapshot_encoding", "invalid_git_inventory",
                "incomplete_git_snapshot", "snapshot_limit_exceeded", "verification_unavailable"):
    _add(_reason, "OF-CANDIDATE-" + _reason.upper().replace("_", "-"),
         "The candidate snapshot cannot establish complete, unambiguous correspondence.",
         "Inspect the candidate reason and selected scope. Resolve duplicate/unsafe/incomplete source evidence or "
         "the documented snapshot limit before rerunning; do not omit evidence to obtain a pass.")

for _reason in (
    "invalid_dbt_exposure_response", "invalid_model_validation_response", "invalid_content_history",
    "invalid_content_response_envelope", "invalid_content_document", "invalid_content_document_identity",
    "invalid_content_query_list", "invalid_content_query_identity", "content_label_identity_unavailable",
    "dbt_refresh_identity_mismatch", "dbt_refresh_invalid_status",
):
    _add(_reason, "OF-" + _reason.upper().replace("_", "-"),
         "The response is missing required or consistent structured evidence.",
         "Use the operation, reason code and run reference to investigate the response contract. "
         "Do not treat missing or malformed entries as a clean validation result.")

for _reason, _check in (
    ("dependency_search_unavailable", "downstream dependency search"),
    ("relationship_endpoints_unavailable", "relationship dependency mapping"),
    ("dependency_coverage_incomplete", "downstream dependency coverage"),
    ("dbt_impact_coverage_incomplete", "dbt impact coverage"),
    ("dbt_exposures_unavailable", "dbt exposure collection"),
    ("ai_eval_request_failed", "AI evaluation"),
):
    _add(_reason, "OF-" + _reason.upper().replace("_", "-"), f"The {_check} check lacks complete evidence.",
         "Review the underlying cause, operation and coverage counts. Restore evidence availability before relying "
         "on this check; unavailable evidence is not a clean result.")

for _reason, _secret in (("missing_api_key", "OMNI_API_KEY"), ("missing_repair_api_key", "OMNI_REPAIR_API_KEY"),
                         ("missing_sync_api_key", "OMNI_SYNC_API_KEY")):
    _add(_reason, "OF-" + _reason.upper().replace("_", "-"), "The operation's credential is missing.",
         f"Configure {_secret} in the trusted workflow secret environment. Never commit or paste the secret value.", "configuration")

for _reason in (
    "prompt_set_model_mismatch", "duplicate_run_id", "prompt_set_not_active", "empty_or_missing_prompts",
    "malformed_prompt_set", "duplicate_prompt_identity", "run_identity_mismatch", "unsupported_run_state",
    "run_cancelled_or_failed", "incomplete_scoring", "timeout", "incomplete_prompt_coverage",
    "prompt_job_failed_or_incomplete", "unsupported_nonbinary_score", "malformed_results", "malformed_result",
    "duplicate_result_identity",
):
    _add(_reason, "OF-AI-EVAL-" + _reason.upper().replace("_", "-"),
         "AI evaluation did not establish a complete comparison.",
         "Inspect the reason code and operation phase. Verify the prompt set, model association and owned run status "
         "in Omni; missing prompts, scores or results do not count as a pass.")
_add("cleanup_unconfirmed", "OF-AI-EVAL-CLEANUP", "Cleanup of an owned AI evaluation run is unconfirmed.",
     "Reconcile the owned run journal through an authorized channel and confirm cleanup before starting another evaluation.")
_add("accuracy_regression", "OF-AI-EVAL-REGRESSION", "AI evaluation detected a pass-to-fail regression.",
     "Review the affected evaluation prompts and branch-versus-base results in Omni.", "validation")

for _reason in ("breaking_change_sync_state_unavailable", "breaking_change_with_dbt_in_same_pull_request",
                "breaking_change_with_pending_dbt_deployment"):
    _add(_reason, "OF-" + _reason.upper().replace("_", "-"), "The breaking-change hold requires deployment evidence.",
         "Check the last successful sync revision and pending dbt deployment. Release only through current-head "
         "revalidation after synchronization; removing a label does not establish readiness.", "deployment")
_add("ai_repair_validation_incomplete", "OF-REPAIR-VALIDATION-INCOMPLETE", "Post-repair validation is incomplete.",
     "Review skipped, partial or unavailable checks and rollback status. A zero policy exit alone cannot authorize "
     "committing an unverified repair.")


def diagnostic_descriptor(issue: dict) -> dict[str, str]:
    """Fixed human guidance for a projected finding; never copy input prose."""
    # A write with unknown outcome must retain reconciliation guidance even if
    # its underlying HTTP failure also has a useful catalog entry.
    reason = (issue.get("reason_code") if issue.get("reason_code") in {"dbt_refresh_status_unknown", "ai_repair_timeout"}
              else next((issue.get(key) for key in ("cause_code", "reason_code", "candidate_reason")
                         if issue.get(key) in REASON_DETAILS), None))
    detail = REASON_DETAILS.get(reason) if isinstance(reason, str) else None
    validator = issue.get("validator", "execution")
    if detail:
        code, title, action, category = detail
    else:
        category = "incomplete_evidence" if issue.get("type") in {
            "validation_execution_failed", "candidate_verification_failed", "internal_execution_failure",
            "dependency_coverage_gap", "model_evidence_unavailable", "content_evidence_unavailable",
        } else "validation"
        code = "OF-" + str(validator).upper().replace("_", "-")
        rule = issue.get("rule_id")
        if rule:
            code += "-" + rule.upper().replace("_", "-")
        title = ("The check could not establish complete evidence." if category == "incomplete_evidence"
                 else "A validation finding was recorded.")
        if validator == "semantic_lint" and rule in LINT_MESSAGES:
            title = LINT_MESSAGES[rule]
        elif validator in {"semantic_diff", "contracts"} and issue.get("type") in CHANGE_MESSAGES:
            title = CHANGE_MESSAGES[issue["type"]]
        action = CHECK_GUIDANCE.get(validator, CHECK_GUIDANCE["execution"])
        if validator in {"semantic_diff", "contracts"} and issue.get("type") == "topic_organization_changed":
            action = "Confirm the intended topic grouping in Omni; review any separate validation or synchronization findings."
    return {"diagnostic_code": code, "title": title, "next_action": action, "category": category}
