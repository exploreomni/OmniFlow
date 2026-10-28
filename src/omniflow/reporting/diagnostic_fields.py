"""Fixed evidence keys shared by report renderers, after audience projection.

This is not a sanitizer. Public callers must first use public_diagnostic;
repository-only locations are added separately by the authorized projection.
"""

DIAGNOSTIC_FACTS = (
    "context_reference", "validation_scope", "state", "reason_code", "candidate_reason",
    "cause_code", "transport_reason", "document_reference", "service", "api_operation", "http_status", "attempt_count", "max_attempts",
    "operation_phase", "retryable", "config_key", "expected_type", "source_kind", "source_line",
    "source_column", "entry_index", "contents_comparison", "compared_file_count",
    "git_file_count", "omni_file_count", "git_only_count", "omni_only_count",
    "candidate_sample_count", "candidate_sample_omitted_count", "last_observed_status",
    "comparison_mode", "comparison_status", "content_health", "model_health",
    "regression_status", "requested_validation_depth", "active", "metadata_stage", "metadata_reason",
)
