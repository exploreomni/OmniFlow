from __future__ import annotations

from typing import Any

API_OPERATIONS = frozenset({
    "model_yaml", "model_validation", "content_validation", "git_configuration",
    "model_list", "content_list", "identity_lookup", "document_drafts", "dbt_exposures", "schema_refresh",
    "job_status", "ai_job", "ai_job_status", "ai_job_cancel", "ai_prompt_set",
    "ai_eval_run", "ai_eval_status", "ai_eval_cancel", "git_commit", "omni_request",
    "github_compare", "github_tree", "github_blob", "github_pull_request",
    "github_check", "github_variable", "github_label", "github_request",
    "git_revision_read", "git_tree_read",
    "query_run", "query_wait", "query_proposal",
})
TRANSPORT_REASON_CODES = frozenset({
    "omni_unauthorized", "omni_forbidden", "omni_rate_limited", "omni_service_unavailable",
    "omni_http_error", "omni_timeout", "omni_tls_error", "omni_connection_error",
    "omni_request_failed", "omni_response_interrupted", "omni_response_limit_exceeded",
    "omni_pagination_invalid", "omni_pagination_limit_exceeded",
    "empty_api_response", "invalid_json_response",
    "github_missing_token", "github_invalid_repository", "github_unauthorized", "github_forbidden",
    "github_not_found_or_inaccessible", "github_rate_limited", "github_service_unavailable",
    "github_http_error", "github_timeout", "github_tls_error", "github_connection_error",
    "github_request_failed", "github_response_interrupted", "github_invalid_json_response",
    "github_response_limit_exceeded", "git_revision_unavailable", "git_missing_executable",
    "git_timeout", "git_invalid_encoding",
})


def exception_diagnostic(exc: BaseException) -> dict[str, Any]:
    """Copy structured evidence only; renderers must apply their own allowlist.

    Exception messages, response bodies and exception chains are not diagnostic
    authority. Existing subclasses that set reason_code themselves remain valid.
    """
    context = getattr(exc, "diagnostic_context", None)
    result = dict(context) if isinstance(context, dict) else {}
    reason = getattr(exc, "reason_code", None)
    if isinstance(reason, str):
        result["reason_code"] = reason
    return result


class ExitCodes:
    SUCCESS = 0
    VALIDATION_FAILED = 1
    CONFIGURATION_ERROR = 2
    AUTHORIZATION_ERROR = 3
    OMNI_API_ERROR = 4
    SECURITY_POLICY_VIOLATION = 5
    INTERNAL_ERROR = 6


class OmniFlowError(Exception):
    exit_code = ExitCodes.INTERNAL_ERROR

    def __init__(
        self, message: str, *, exit_code: int | None = None, reason_code: str | None = None,
        diagnostic_context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code
        if reason_code is not None:
            self.reason_code = reason_code
        self.diagnostic_context = dict(diagnostic_context or {})


class ConfigError(OmniFlowError):
    exit_code = ExitCodes.CONFIGURATION_ERROR


class MissingPolicyError(ConfigError):
    """Fixed, public-safe guidance; never interpolate a customer-supplied path."""

    reason_code = "missing_policy_config"
    MESSAGE = (
        "Required OmniFlow policy is missing from the trusted source. "
        "Add .omniflow.yml at the repository root (or provide an existing --config file). "
        "For pull requests, commit the policy to the target base branch first and fetch that branch; "
        "a file added only on the PR branch is not trusted. "
        "The GitHub Actions workflow is a separate file."
    )

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


class SecurityPolicyError(OmniFlowError):
    exit_code = ExitCodes.SECURITY_POLICY_VIOLATION


class OmniAPIError(OmniFlowError):
    exit_code = ExitCodes.OMNI_API_ERROR


class OmniResponseError(OmniAPIError):
    """A transport-body failure, separate from readable content findings."""

    MESSAGES = {
        "empty_api_response": "Omni API returned an empty response; validation evidence is unavailable.",
        "invalid_json_response": "Omni API did not return valid JSON; validation evidence is unavailable.",
    }

    def __init__(self, reason_code: str) -> None:
        if reason_code not in self.MESSAGES:
            raise ValueError("Unknown response failure category")
        self.reason_code = reason_code
        self.validation_scope: str | None = None
        self.comparison_mode: str | None = None
        super().__init__(self.MESSAGES[reason_code])

    def report_issue(self, *, validator: str = "content") -> dict:
        issue = {
            "validator": validator, "type": "validation_execution_failed", "severity": "error",
            "reason_code": self.reason_code, "message": str(self), "coverage_complete": False,
        }
        if validator in {"content", "model"}:
            issue.update(comparison_status="unavailable", regression_status="unknown")
            issue[f"{validator}_health"] = "unknown"
            if self.validation_scope in {"branch", "base"}:
                issue["validation_scope"] = self.validation_scope
            if self.comparison_mode in {"new_only", "all_issues"}:
                issue["comparison_mode"] = self.comparison_mode
        return issue


class OmniAuthError(OmniFlowError):
    exit_code = ExitCodes.AUTHORIZATION_ERROR


class GitHubAPIError(OmniAPIError):
    """GitHub transport/evidence failure, not a policy configuration error."""


class GitHubAuthError(OmniAuthError):
    """GitHub rejected the supplied credential or its access scope."""
