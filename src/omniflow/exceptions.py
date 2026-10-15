from __future__ import annotations


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

    def __init__(self, message: str, *, exit_code: int | None = None) -> None:
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code


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
