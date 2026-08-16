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


class SecurityPolicyError(OmniFlowError):
    exit_code = ExitCodes.SECURITY_POLICY_VIOLATION


class OmniAPIError(OmniFlowError):
    exit_code = ExitCodes.OMNI_API_ERROR


class OmniAuthError(OmniFlowError):
    exit_code = ExitCodes.AUTHORIZATION_ERROR
