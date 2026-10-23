from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .exceptions import ConfigError, MissingPolicyError, SecurityPolicyError
from .file_scope import NonOmniRule, parse_non_omni_rules
from .query_validation import QueryValidationSettings, parse_query_validation
from .security import contains_secret_key, reject_secret_keys, validate_repo_output_path
from .trust import read_trusted_repo_text
from .yaml_security import parse_secure_yaml

DEFAULT_CONFIG_PATH = ".omniflow.yml"
DEFAULT_REPORT_FORMATS = ["json", "markdown", "sarif", "junit"]
REPORT_FORMATS = {"json", "markdown", "md", "sarif", "junit", "xml"}
SEMANTIC_LINT_RULES = {
    "require_field_descriptions",
    "require_measure_descriptions",
    "require_primary_keys",
    "require_topic_labels",
    "forbid_many_to_many_without_comment",
    "block_deleted_fields",
    "warn_field_type_change",
    "warn_measure_aggregation_change",
    "warn_relationship_cardinality_change",
    "require_owner_metadata",
    "forbid_personal_folder_validation_scope",
}
RULE_SEVERITIES = {"off", "info", "warn", "error"}
CONFIG_KEYS = {"omni", "checks", "reporting", "security", "contracts", "repairs", "deployment", "files", "validation"}
FILE_KEYS = {"non_omni"}
VALIDATION_KEYS = {"mode"}
OMNI_KEYS = {
    "base_url",
    "model_id",
    "branch_id",
    "branch_name",
    "user_id",
    "include_personal_folders",
    "timeout",
}
CHECK_KEYS = {"content_validation", "model_validation", "semantic_lint", "dbt_exposures", "dbt_impact", "ai_eval", "query_validation"}
CONTENT_KEYS = {"enabled", "fail_on_new_only", "labels", "classify_comment_only", "force_full_validation", "targeted"}
MODEL_KEYS = {"enabled", "fail_on_warnings", "fail_on_new_only"}
LINT_KEYS = {"enabled", "rules"}
EXPOSURE_KEYS = {"enabled", "fail_on_unavailable"}
DBT_IMPACT_KEYS = {
    "enabled",
    "manifest_path",
    "fail_on_orphaned_references",
    "fail_on_incomplete_coverage",
    "omni_yaml_paths",
    "table_mapping",
    "content_impact",
    "omni_repository",
}
OMNI_REPOSITORY_KEYS = {"repository", "ref", "model_paths", "allow_repository_diagnostics"}
CONTENT_IMPACT_KEYS = {"enabled", "sql_dialect", "max_searches", "timeout_seconds"}
TABLE_MAPPING_KEYS = {"dbt_model", "omni_view", "sql_table_name"}
MAX_TABLE_MAPPINGS = 500
AI_EVAL_KEYS = {
    "enabled",
    "prompt_sets",
    "fail_on_regression",
    "poll_interval_seconds",
    "timeout_seconds",
    "scoring_grace_seconds",
}
PROMPT_SET_KEYS = {"id", "label", "model_id"}
MAX_PROMPT_SETS = 20
REPORTING_KEYS = {"formats", "output_dir", "diagnostics"}
DIAGNOSTICS_KEYS = {"mode"}
DIAGNOSTICS_MODES = {"repository", "minimal"}
SECURITY_KEYS = {
    "redact_logs",
    "allow_raw_response_output",
    "max_report_samples",
    "redact_document_names",
    "redaction_level",
    "retain_restricted_artifacts",
}
CONTRACT_KEYS = {"enabled", "fail_on"}
CONTRACT_FAIL_KEYS = {
    "deleted_referenced_fields",
    "renamed_referenced_fields",
    "referenced_field_type_changes",
    "referenced_join_cardinality_changes",
    "coverage_gaps",
}
REPAIR_KEYS = {"ai"}
AI_REPAIR_KEYS = {
    "enabled",
    "allow_query_execution",
    "max_changed_files",
    "max_changed_lines",
    "poll_timeout_seconds",
}
DEPLOYMENT_KEYS = {"dbt_sync", "breaking_change_hold"}
DBT_SYNC_KEYS = {
    "enabled",
    "refresh_mode",
    "poll_interval_seconds",
    "timeout_seconds",
    "post_sync_validation",
}
BREAKING_HOLD_KEYS = {
    "enabled",
    "action",
    "dbt_paths",
    "pending_label",
}
BREAKING_HOLD_ACTIONS = {"fail", "warn"}
DEFAULT_DBT_PATHS = ["models/", "seeds/", "snapshots/", "macros/"]
DEFAULT_PENDING_LABEL = "omniflow/awaiting-deploy"
MAX_DBT_PATHS = 50

# These are schema identifiers, never rejected input or repository paths. Public
# diagnostics may disclose them even when customer values remain private.
_SCHEMA_GROUPS = {
    "config": CONFIG_KEYS, "omni": OMNI_KEYS, "checks": CHECK_KEYS,
    "checks.content_validation": CONTENT_KEYS, "checks.model_validation": MODEL_KEYS,
    "checks.semantic_lint": LINT_KEYS, "checks.semantic_lint.rules": SEMANTIC_LINT_RULES,
    "checks.dbt_exposures": EXPOSURE_KEYS, "checks.dbt_impact": DBT_IMPACT_KEYS,
    "checks.dbt_impact.table_mapping": TABLE_MAPPING_KEYS,
    "checks.dbt_impact.content_impact": CONTENT_IMPACT_KEYS,
    "checks.dbt_impact.omni_repository": OMNI_REPOSITORY_KEYS,
    "checks.ai_eval": AI_EVAL_KEYS, "checks.ai_eval.prompt_sets": PROMPT_SET_KEYS,
    "checks.query_validation": {"mode", "contracts", "native_qualified", "snapshot_attestation",
                                "environment_connection_id", "user_id", "timezone", "max_queries",
                                "timeout_seconds", "max_response_bytes", "max_polls"},
    "reporting": REPORTING_KEYS, "reporting.diagnostics": DIAGNOSTICS_KEYS,
    "security": SECURITY_KEYS, "contracts": CONTRACT_KEYS,
    "contracts.fail_on": CONTRACT_FAIL_KEYS, "repairs": REPAIR_KEYS,
    "repairs.ai": AI_REPAIR_KEYS, "deployment": DEPLOYMENT_KEYS,
    "deployment.dbt_sync": DBT_SYNC_KEYS, "deployment.breaking_change_hold": BREAKING_HOLD_KEYS,
    "files": FILE_KEYS, "files.non_omni": {"tag", "pattern", "reason"},
    "validation": VALIDATION_KEYS,
}
CONFIG_DIAGNOSTIC_KEYS = frozenset(_SCHEMA_GROUPS) | frozenset(
    key if parent == "config" else f"{parent}.{key}"
    for parent, keys in _SCHEMA_GROUPS.items() for key in keys
) | frozenset({"version", "models"}) | frozenset(f"models.{key}" for key in {
    "base_url", "model_id", "model_path", "base_branch", "git_provider", "web_url", "environment", "git_follower",
})
_ENV_CONFIG_KEYS = {
    "OMNI_BASE_URL": "omni.base_url", "OMNI_MODEL_ID": "omni.model_id",
    "OMNI_BRANCH_ID": "omni.branch_id", "OMNI_BRANCH_NAME": "omni.branch_name",
    "OMNI_USER_ID": "omni.user_id", "OMNI_TIMEOUT": "omni.timeout",
    "OMNI_INCLUDE_PERSONAL_FOLDERS": "omni.include_personal_folders",
}


def _config_error(message: str, key: str, expected_type: str, *,
                  reason: str = "config_invalid_type", entry_index: int | None = None) -> ConfigError:
    key = _ENV_CONFIG_KEYS.get(key, key)
    if key not in CONFIG_DIAGNOSTIC_KEYS and f"checks.{key}" in CONFIG_DIAGNOSTIC_KEYS:
        key = f"checks.{key}"
    context = {"source_kind": "policy", "expected_type": expected_type}
    if key in CONFIG_DIAGNOSTIC_KEYS:
        context["config_key"] = key
    if entry_index is not None:
        context["entry_index"] = entry_index
    return ConfigError(message, reason_code=reason, diagnostic_context=context)


def _non_omni_rules(value: Any) -> list[NonOmniRule]:
    try:
        return parse_non_omni_rules(value)
    except ConfigError as exc:
        exc.reason_code = getattr(exc, "file_scope_reason", "config_invalid_value")
        exc.diagnostic_context = {
            "source_kind": "policy", "config_key": "files.non_omni", "expected_type": "list",
        }
        if isinstance(value, list):
            for index, item in enumerate(value, start=1):
                try:
                    parse_non_omni_rules([item])
                except ConfigError:
                    exc.diagnostic_context["entry_index"] = index
                    break
        else:
            exc.reason_code = "config_invalid_type"
        raise


def _expand_env_string(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        env_name = match.group(1) or match.group(2)
        if contains_secret_key(env_name):
            raise SecurityPolicyError(
                f"Secret-like environment variable {env_name} cannot be expanded from policy config"
            )
        return os.getenv(env_name, "")

    return re.sub(
        r"\$(\w+)|\$\{([^}]+)\}",
        replace,
        value,
    )


def expand_env_vars(value: Any) -> Any:
    if isinstance(value, str):
        return _expand_env_string(value)
    if isinstance(value, list):
        return [expand_env_vars(item) for item in value]
    if isinstance(value, dict):
        return {key: expand_env_vars(item) for key, item in value.items()}
    return value


def parse_bool(name: str, value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise _config_error(f"Invalid boolean value for {name}", name, "boolean")


def parse_csv(value: Any, *, name: str = "") -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    names: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise _config_error("Expected string list values", name, "list")
        for name in item.split(","):
            normalized = name.strip()
            if normalized and normalized not in names:
                names.append(normalized)
    return names


def config_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_raw_config(path: str | Path | None = None) -> tuple[dict[str, Any], Path]:
    candidate = Path(path) if path else Path(DEFAULT_CONFIG_PATH)
    try:
        text = read_trusted_repo_text(candidate)
    except ConfigError as exc:
        if getattr(exc, "reason_code", None) in {
            "trusted_file_unsafe", "trusted_file_too_large", "trusted_file_invalid_encoding",
        }:
            exc.diagnostic_context = {"source_kind": "policy"}
            raise
        raise ConfigError("Could not access the trusted policy file", reason_code="policy_inaccessible",
                          diagnostic_context={"source_kind": "policy"}) from exc
    if text is None:
        error = MissingPolicyError()
        error.diagnostic_context = {"source_kind": "policy"}
        raise error
    payload = parse_secure_yaml(text, source=str(candidate), source_kind="policy")
    if not isinstance(payload, dict) or not payload:
        raise _config_error("Policy must contain a non-empty top-level mapping", "config", "non_empty_mapping")
    reject_secret_keys(payload, source=str(candidate))
    _validate_config_schema(payload)
    return expand_env_vars(payload), candidate


@dataclass
class OmniSettings:
    base_url: str | None = None
    model_id: str | None = None
    branch_id: str | None = None
    branch_name: str | None = None
    user_id: str | None = None
    include_personal_folders: bool = False
    timeout: int = 60


@dataclass
class ContentValidationSettings:
    enabled: bool = True
    fail_on_new_only: bool = False
    classify_comment_only: bool = False
    force_full_validation: bool = False
    targeted: bool = False
    labels: list[str] = field(default_factory=list)


@dataclass
class ModelValidationSettings:
    enabled: bool = True
    fail_on_warnings: bool = False
    fail_on_new_only: bool = False


@dataclass
class SemanticLintSettings:
    enabled: bool = True
    rules: dict[str, str] = field(default_factory=dict)


@dataclass
class ContractSettings:
    enabled: bool = True
    fail_on_deleted_referenced_fields: bool = True
    fail_on_renamed_referenced_fields: bool = True
    fail_on_referenced_field_type_changes: bool = True
    fail_on_referenced_join_cardinality_changes: bool = True
    fail_on_coverage_gaps: bool = True


@dataclass
class DbtExposureSettings:
    enabled: bool = False
    fail_on_unavailable: bool = False


@dataclass
class DbtContentImpactSettings:
    enabled: bool = False
    sql_dialect: str | None = None
    max_searches: int = 100
    timeout_seconds: int = 120


@dataclass
class OmniRepositorySettings:
    repository: str
    ref: str
    model_paths: list[str]
    allow_repository_diagnostics: bool = False


@dataclass
class DbtImpactSettings:
    enabled: bool = False
    manifest_path: str | None = None
    fail_on_orphaned_references: bool = True
    fail_on_incomplete_coverage: bool = True
    omni_yaml_paths: list[str] = field(default_factory=list)
    table_mapping: list[dict[str, str]] = field(default_factory=list)
    content_impact: DbtContentImpactSettings = field(default_factory=DbtContentImpactSettings)
    omni_repository: OmniRepositorySettings | None = None


@dataclass
class AiEvalSettings:
    enabled: bool = False
    prompt_sets: list[dict[str, str]] = field(default_factory=list)
    fail_on_regression: bool = True
    poll_interval_seconds: int = 10
    timeout_seconds: int = 900
    scoring_grace_seconds: int = 180


@dataclass
class ReportingSettings:
    formats: list[str] = field(default_factory=lambda: list(DEFAULT_REPORT_FORMATS))
    output_dir: str = ".omniflow"
    diagnostics_mode: str = "repository"


@dataclass
class SecuritySettings:
    redact_logs: bool = True
    allow_raw_response_output: bool = False
    max_report_samples: int = 20
    redact_document_names: bool = False
    redaction_level: str = "standard"
    retain_restricted_artifacts: bool = False


@dataclass
class AIRepairSettings:
    enabled: bool = False
    allow_query_execution: bool = False
    max_changed_files: int = 3
    max_changed_lines: int = 200
    poll_timeout_seconds: int = 300


@dataclass
class DbtSyncSettings:
    enabled: bool = False
    refresh_mode: str = "hard"
    poll_interval_seconds: int = 5
    timeout_seconds: int = 900
    post_sync_validation: bool = True


@dataclass
class BreakingChangeHoldSettings:
    enabled: bool = False
    action: str = "fail"
    dbt_paths: list[str] = field(default_factory=lambda: list(DEFAULT_DBT_PATHS))
    pending_label: str = DEFAULT_PENDING_LABEL


@dataclass
class FileSettings:
    non_omni: list[NonOmniRule] = field(default_factory=list)


@dataclass
class ValidationSettings:
    mode: str = "strict"


@dataclass
class OmniFlowConfig:
    raw: dict[str, Any]
    source: Path | None
    omni: OmniSettings
    content_validation: ContentValidationSettings
    model_validation: ModelValidationSettings
    semantic_lint: SemanticLintSettings
    contracts: ContractSettings
    dbt_exposures: DbtExposureSettings
    dbt_impact: DbtImpactSettings
    ai_eval: AiEvalSettings
    reporting: ReportingSettings
    security: SecuritySettings
    ai_repair: AIRepairSettings
    dbt_sync: DbtSyncSettings
    breaking_change_hold: BreakingChangeHoldSettings
    hash: str
    files: FileSettings = field(default_factory=FileSettings)
    validation: ValidationSettings = field(default_factory=ValidationSettings)
    query_validation: QueryValidationSettings = field(default_factory=QueryValidationSettings)


def load_config(path: str | Path | None = None) -> OmniFlowConfig:
    raw, source = load_raw_config(path)
    return _to_config(raw, source)


def _to_config(raw: dict[str, Any], source: Path | None) -> OmniFlowConfig:
    omni_raw = _mapping(raw.get("omni"), "omni")
    files_raw = _mapping(raw.get("files"), "files")
    _reject_unknown_keys(files_raw, FILE_KEYS, "files")
    files = FileSettings(non_omni=_non_omni_rules(files_raw.get("non_omni")))
    validation_raw = _mapping(raw.get("validation"), "validation")
    _reject_unknown_keys(validation_raw, VALIDATION_KEYS, "validation")
    validation = ValidationSettings(mode=_validation_mode(validation_raw.get("mode", "strict")))
    checks_raw = _mapping(raw.get("checks"), "checks")
    reporting_raw = _mapping(raw.get("reporting"), "reporting")
    security_raw = _mapping(raw.get("security"), "security")
    contracts_raw = _mapping(raw.get("contracts"), "contracts")
    repairs_raw = _mapping(raw.get("repairs"), "repairs")
    deployment_raw = _mapping(raw.get("deployment"), "deployment")
    ai_repair_raw = _mapping(repairs_raw.get("ai"), "repairs.ai")
    dbt_sync_raw = _mapping(deployment_raw.get("dbt_sync"), "deployment.dbt_sync")
    breaking_hold_raw = _mapping(
        deployment_raw.get("breaking_change_hold"), "deployment.breaking_change_hold"
    )
    content_raw = _mapping(checks_raw.get("content_validation"), "checks.content_validation")
    model_raw = _mapping(checks_raw.get("model_validation"), "checks.model_validation")
    lint_raw = _mapping(checks_raw.get("semantic_lint"), "checks.semantic_lint")
    exposures_raw = _mapping(checks_raw.get("dbt_exposures"), "checks.dbt_exposures")
    dbt_impact_raw = _mapping(checks_raw.get("dbt_impact"), "checks.dbt_impact")
    ai_eval_raw = _mapping(checks_raw.get("ai_eval"), "checks.ai_eval")
    lint_rules = _lint_rules(lint_raw.get("rules"))
    formats = _report_formats(reporting_raw.get("formats"))
    output_dir = _output_dir(reporting_raw.get("output_dir"))
    allow_raw_response_output = parse_bool(
        "security.allow_raw_response_output",
        security_raw.get("allow_raw_response_output"),
        False,
    )
    if allow_raw_response_output:
        raise SecurityPolicyError(
            "security.allow_raw_response_output cannot be enabled in policy config. "
            "Use --unsafe-raw-output only for an explicit local debugging command."
        )

    omni = OmniSettings(
        base_url=_string_env("OMNI_BASE_URL", omni_raw.get("base_url")),
        model_id=_string_env("OMNI_MODEL_ID", omni_raw.get("model_id")),
        branch_id=_string_env("OMNI_BRANCH_ID", omni_raw.get("branch_id")),
        branch_name=_string_env("OMNI_BRANCH_NAME", omni_raw.get("branch_name")),
        user_id=_string_env("OMNI_USER_ID", omni_raw.get("user_id")),
        include_personal_folders=parse_bool(
            "OMNI_INCLUDE_PERSONAL_FOLDERS",
            os.getenv("OMNI_INCLUDE_PERSONAL_FOLDERS", omni_raw.get("include_personal_folders")),
            False,
        ),
        timeout=_bounded_int(
            "OMNI_TIMEOUT",
            os.getenv("OMNI_TIMEOUT", omni_raw.get("timeout", 60)),
            minimum=1,
            maximum=300,
            default=60,
        ),
    )
    content = ContentValidationSettings(
        enabled=parse_bool("content_validation.enabled", content_raw.get("enabled"), True),
        classify_comment_only=parse_bool(
            "content_validation.classify_comment_only", content_raw.get("classify_comment_only"), False
        ),
        force_full_validation=parse_bool(
            "content_validation.force_full_validation", content_raw.get("force_full_validation"), False
        ),
        targeted=parse_bool("content_validation.targeted", content_raw.get("targeted"), False),
        fail_on_new_only=parse_bool(
            "content_validation.fail_on_new_only",
            os.getenv("OMNI_FAIL_ON_NEW_ONLY", content_raw.get("fail_on_new_only")),
            False,
        ),
        labels=parse_csv(os.getenv("OMNI_LABELS", content_raw.get("labels")), name="checks.content_validation.labels"),
    )
    model = ModelValidationSettings(
        enabled=parse_bool("model_validation.enabled", model_raw.get("enabled"), True),
        fail_on_warnings=parse_bool("model_validation.fail_on_warnings", model_raw.get("fail_on_warnings"), False),
        fail_on_new_only=parse_bool("model_validation.fail_on_new_only", model_raw.get("fail_on_new_only"), False),
    )
    lint = SemanticLintSettings(
        enabled=parse_bool("semantic_lint.enabled", lint_raw.get("enabled"), True),
        rules=lint_rules,
    )
    contracts = ContractSettings(
        enabled=parse_bool("contracts.enabled", contracts_raw.get("enabled"), True),
        fail_on_deleted_referenced_fields=parse_bool(
            "contracts.fail_on.deleted_referenced_fields",
            (contracts_raw.get("fail_on") or {}).get("deleted_referenced_fields")
            if isinstance(contracts_raw.get("fail_on"), dict)
            else None,
            True,
        ),
        fail_on_renamed_referenced_fields=parse_bool(
            "contracts.fail_on.renamed_referenced_fields",
            (contracts_raw.get("fail_on") or {}).get("renamed_referenced_fields")
            if isinstance(contracts_raw.get("fail_on"), dict)
            else None,
            True,
        ),
        fail_on_referenced_field_type_changes=parse_bool(
            "contracts.fail_on.referenced_field_type_changes",
            (contracts_raw.get("fail_on") or {}).get("referenced_field_type_changes")
            if isinstance(contracts_raw.get("fail_on"), dict)
            else None,
            True,
        ),
        fail_on_referenced_join_cardinality_changes=parse_bool(
            "contracts.fail_on.referenced_join_cardinality_changes",
            (contracts_raw.get("fail_on") or {}).get("referenced_join_cardinality_changes")
            if isinstance(contracts_raw.get("fail_on"), dict)
            else None,
            True,
        ),
        fail_on_coverage_gaps=parse_bool(
            "contracts.fail_on.coverage_gaps",
            (contracts_raw.get("fail_on") or {}).get("coverage_gaps")
            if isinstance(contracts_raw.get("fail_on"), dict)
            else None,
            True,
        ),
    )
    dbt_exposures = DbtExposureSettings(
        enabled=parse_bool("dbt_exposures.enabled", exposures_raw.get("enabled"), False),
        fail_on_unavailable=parse_bool(
            "dbt_exposures.fail_on_unavailable",
            exposures_raw.get("fail_on_unavailable"),
            False,
        ),
    )
    dbt_impact = DbtImpactSettings(
        enabled=parse_bool("dbt_impact.enabled", dbt_impact_raw.get("enabled"), False),
        manifest_path=_relative_repo_path(
            dbt_impact_raw.get("manifest_path"), name="checks.dbt_impact.manifest_path"
        ),
        fail_on_orphaned_references=parse_bool(
            "dbt_impact.fail_on_orphaned_references",
            dbt_impact_raw.get("fail_on_orphaned_references"),
            True,
        ),
        fail_on_incomplete_coverage=parse_bool(
            "dbt_impact.fail_on_incomplete_coverage",
            dbt_impact_raw.get("fail_on_incomplete_coverage"),
            True,
        ),
        omni_yaml_paths=_repo_path_list(
            dbt_impact_raw.get("omni_yaml_paths"), name="checks.dbt_impact.omni_yaml_paths"
        ),
        table_mapping=_table_mapping(dbt_impact_raw.get("table_mapping")),
        content_impact=_content_impact_settings(dbt_impact_raw.get("content_impact")),
        omni_repository=_omni_repository_settings(dbt_impact_raw.get("omni_repository")),
    )
    ai_eval = AiEvalSettings(
        enabled=parse_bool("ai_eval.enabled", ai_eval_raw.get("enabled"), False),
        prompt_sets=_prompt_sets(ai_eval_raw.get("prompt_sets")),
        fail_on_regression=parse_bool(
            "ai_eval.fail_on_regression", ai_eval_raw.get("fail_on_regression"), True
        ),
        poll_interval_seconds=_bounded_int(
            "checks.ai_eval.poll_interval_seconds",
            ai_eval_raw.get("poll_interval_seconds", 10),
            minimum=2,
            maximum=30,
            default=10,
        ),
        timeout_seconds=_bounded_int(
            "checks.ai_eval.timeout_seconds",
            ai_eval_raw.get("timeout_seconds", 900),
            minimum=30,
            maximum=3_600,
            default=900,
        ),
        scoring_grace_seconds=_bounded_int(
            "checks.ai_eval.scoring_grace_seconds",
            ai_eval_raw.get("scoring_grace_seconds", 180),
            minimum=0,
            maximum=600,
            default=180,
        ),
    )
    reporting = ReportingSettings(
        formats=formats,
        output_dir=output_dir,
        diagnostics_mode=_diagnostics_mode(reporting_raw),
    )
    security = SecuritySettings(
        redact_logs=parse_bool("security.redact_logs", security_raw.get("redact_logs"), True),
        allow_raw_response_output=False,
        max_report_samples=_bounded_int(
            "security.max_report_samples",
            security_raw.get("max_report_samples", 20),
            minimum=0,
            maximum=1000,
            default=20,
        ),
        redact_document_names=parse_bool(
            "security.redact_document_names", security_raw.get("redact_document_names"), False
        ),
        redaction_level=_redaction_level(security_raw.get("redaction_level", "standard")),
        retain_restricted_artifacts=parse_bool(
            "security.retain_restricted_artifacts",
            security_raw.get("retain_restricted_artifacts"),
            False,
        ),
    )
    ai_repair = AIRepairSettings(
        enabled=parse_bool("repairs.ai.enabled", ai_repair_raw.get("enabled"), False),
        allow_query_execution=parse_bool(
            "repairs.ai.allow_query_execution",
            ai_repair_raw.get("allow_query_execution"),
            False,
        ),
        max_changed_files=_bounded_int(
            "repairs.ai.max_changed_files",
            ai_repair_raw.get("max_changed_files", 3),
            minimum=1,
            maximum=20,
            default=3,
        ),
        max_changed_lines=_bounded_int(
            "repairs.ai.max_changed_lines",
            ai_repair_raw.get("max_changed_lines", 200),
            minimum=1,
            maximum=2_000,
            default=200,
        ),
        poll_timeout_seconds=_bounded_int(
            "repairs.ai.poll_timeout_seconds",
            ai_repair_raw.get("poll_timeout_seconds", 300),
            minimum=30,
            maximum=900,
            default=300,
        ),
    )
    dbt_sync = DbtSyncSettings(
        enabled=parse_bool("deployment.dbt_sync.enabled", dbt_sync_raw.get("enabled"), False),
        refresh_mode=_refresh_mode(dbt_sync_raw.get("refresh_mode", "hard")),
        poll_interval_seconds=_bounded_int(
            "deployment.dbt_sync.poll_interval_seconds",
            dbt_sync_raw.get("poll_interval_seconds", 5),
            minimum=2,
            maximum=30,
            default=5,
        ),
        timeout_seconds=_bounded_int(
            "deployment.dbt_sync.timeout_seconds",
            dbt_sync_raw.get("timeout_seconds", 900),
            minimum=30,
            maximum=3_600,
            default=900,
        ),
        post_sync_validation=parse_bool(
            "deployment.dbt_sync.post_sync_validation",
            dbt_sync_raw.get("post_sync_validation"),
            True,
        ),
    )
    breaking_change_hold = BreakingChangeHoldSettings(
        enabled=parse_bool(
            "deployment.breaking_change_hold.enabled",
            breaking_hold_raw.get("enabled"),
            False,
        ),
        action=_hold_action(breaking_hold_raw.get("action", "fail")),
        dbt_paths=_dbt_paths(breaking_hold_raw.get("dbt_paths")),
        pending_label=_pending_label(breaking_hold_raw.get("pending_label")),
    )
    if dbt_impact.omni_repository is not None:
        if not dbt_impact.enabled or dbt_impact.omni_yaml_paths:
            raise ConfigError("External Omni snapshots require enabled dbt impact and model_paths, not omni_yaml_paths",
                              reason_code="omni_repository_configuration_invalid")
        if breaking_change_hold.enabled or dbt_sync.enabled:
            raise ConfigError("Cross-repository deployment synchronization and holds are not supported",
                              reason_code="omni_repository_configuration_invalid")
    return OmniFlowConfig(
        raw=raw,
        source=source,
        omni=omni,
        content_validation=content,
        model_validation=model,
        semantic_lint=lint,
        contracts=contracts,
        dbt_exposures=dbt_exposures,
        dbt_impact=dbt_impact,
        ai_eval=ai_eval,
        reporting=reporting,
        security=security,
        ai_repair=ai_repair,
        dbt_sync=dbt_sync,
        breaking_change_hold=breaking_change_hold,
        hash=config_hash(raw),
        files=files,
        validation=validation,
        query_validation=parse_query_validation(checks_raw.get("query_validation")),
    )


def _string_env(env_name: str, configured: Any) -> str | None:
    value = os.getenv(env_name, configured)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _config_error(f"Expected {env_name} to resolve to a string", env_name, "string")
    stripped = value.strip()
    return stripped or None


def _validation_mode(value: Any) -> str:
    if not isinstance(value, str) or value not in {"strict", "advisory"}:
        raise _config_error("validation.mode must be strict or advisory", "validation.mode", "enum",
                            reason="config_invalid_value")
    return value


def _redaction_level(value: Any) -> str:
    if value is None:
        return "standard"
    if not isinstance(value, str):
        raise _config_error("security.redaction_level must be 'standard' or 'strict'",
                            "security.redaction_level", "enum", reason="config_invalid_value")
    normalized = value.strip().lower()
    if normalized not in {"standard", "strict"}:
        raise _config_error("security.redaction_level must be 'standard' or 'strict'",
                            "security.redaction_level", "enum", reason="config_invalid_value")
    return normalized


def _refresh_mode(value: Any) -> str:
    if not isinstance(value, str):
        raise _config_error("deployment.dbt_sync.refresh_mode must be 'hard' or 'soft'",
                            "deployment.dbt_sync.refresh_mode", "enum", reason="config_invalid_value")
    normalized = value.strip().lower()
    if normalized not in {"hard", "soft"}:
        raise _config_error("deployment.dbt_sync.refresh_mode must be 'hard' or 'soft'",
                            "deployment.dbt_sync.refresh_mode", "enum", reason="config_invalid_value")
    return normalized


def _hold_action(value: Any) -> str:
    if not isinstance(value, str):
        raise _config_error("deployment.breaking_change_hold.action must be 'fail' or 'warn'",
                            "deployment.breaking_change_hold.action", "enum", reason="config_invalid_value")
    normalized = value.strip().lower()
    if normalized not in BREAKING_HOLD_ACTIONS:
        raise _config_error("deployment.breaking_change_hold.action must be 'fail' or 'warn'",
                            "deployment.breaking_change_hold.action", "enum", reason="config_invalid_value")
    return normalized


def _dbt_paths(value: Any) -> list[str]:
    if value is None:
        return list(DEFAULT_DBT_PATHS)
    if not isinstance(value, list):
        raise _config_error("deployment.breaking_change_hold.dbt_paths must be a list of repository paths",
                            "deployment.breaking_change_hold.dbt_paths", "list")
    normalized: list[str] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, str) or not item.strip():
            raise _config_error(
                "deployment.breaking_change_hold.dbt_paths entries must be non-empty strings",
                "deployment.breaking_change_hold.dbt_paths", "non_empty_string", entry_index=index,
            )
        raw_candidate = item.strip()
        # Reject absolute and traversal input before normalization so a leading
        # separator cannot be stripped into a seemingly relative path.
        if (
            Path(raw_candidate).is_absolute()
            or ".." in Path(raw_candidate).parts
            or len(raw_candidate) > 1_024
            or any(character in raw_candidate for character in ("\x00", "\r", "\n", "\\"))
        ):
            raise _config_error(
                "deployment.breaking_change_hold.dbt_paths entries must be relative repository paths",
                "deployment.breaking_change_hold.dbt_paths", "repository_path",
                reason="config_invalid_value", entry_index=index,
            )
        candidate = raw_candidate.strip("/")
        if not candidate or candidate == ".":
            raise _config_error(
                "deployment.breaking_change_hold.dbt_paths entries must be relative repository paths",
                "deployment.breaking_change_hold.dbt_paths", "repository_path",
                reason="config_invalid_value", entry_index=index,
            )
        if candidate not in normalized:
            normalized.append(candidate)
    if not normalized:
        raise _config_error("deployment.breaking_change_hold.dbt_paths must include at least one path",
                            "deployment.breaking_change_hold.dbt_paths", "non_empty_list")
    if len(normalized) > MAX_DBT_PATHS:
        raise SecurityPolicyError(
            f"deployment.breaking_change_hold.dbt_paths cannot exceed {MAX_DBT_PATHS} entries"
        )
    return normalized


def _pending_label(value: Any) -> str:
    if value is None:
        return DEFAULT_PENDING_LABEL
    if not isinstance(value, str) or not value.strip():
        raise _config_error("deployment.breaking_change_hold.pending_label must be a non-empty string",
                            "deployment.breaking_change_hold.pending_label", "non_empty_string")
    normalized = value.strip()
    if len(normalized) > 50 or any(character in normalized for character in ("\x00", "\r", "\n", ",")):
        raise _config_error(
            "deployment.breaking_change_hold.pending_label must be one line, no commas, and 50 characters or fewer",
            "deployment.breaking_change_hold.pending_label", "string", reason="config_invalid_value",
        )
    return normalized


def _relative_repo_path(value: Any, *, name: str) -> str | None:
    """Validate a single optional repository-relative path."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _config_error(f"{name} must be a non-empty string when provided", name, "non_empty_string")
    candidate = value.strip()
    path = Path(candidate)
    if (
        path.is_absolute()
        or ".." in path.parts
        or len(candidate) > 1_024
        or any(character in candidate for character in ("\x00", "\r", "\n", "\\"))
    ):
        raise _config_error(f"{name} must be a relative path inside the repository", name, "repository_path",
                            reason="config_invalid_value")
    return candidate


def _repo_path_list(value: Any, *, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _config_error(f"{name} must be a list of repository paths", name, "list")
    normalized: list[str] = []
    for item in value:
        candidate = _relative_repo_path(item, name=name)
        if candidate is None:
            raise _config_error(f"{name} entries must be non-empty strings", name, "non_empty_string")
        stripped = candidate.strip("/")
        if not stripped or stripped == ".":
            raise _config_error(f"{name} entries must be relative repository paths", name, "repository_path",
                                reason="config_invalid_value")
        if stripped not in normalized:
            normalized.append(stripped)
    if len(normalized) > MAX_DBT_PATHS:
        raise SecurityPolicyError(f"{name} cannot exceed {MAX_DBT_PATHS} entries")
    return normalized


def _table_mapping(value: Any) -> list[dict[str, str]]:
    """Validate explicit dbt-model to Omni-view mapping overrides."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise _config_error("checks.dbt_impact.table_mapping must be a list of mappings",
                            "checks.dbt_impact.table_mapping", "list")
    if len(value) > MAX_TABLE_MAPPINGS:
        raise SecurityPolicyError(
            f"checks.dbt_impact.table_mapping cannot exceed {MAX_TABLE_MAPPINGS} entries"
        )
    mappings: list[dict[str, str]] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise _config_error("checks.dbt_impact.table_mapping entries must be mappings",
                                "checks.dbt_impact.table_mapping", "mapping", entry_index=index)
        _reject_unknown_keys(item, TABLE_MAPPING_KEYS, "checks.dbt_impact.table_mapping entry")
        if not item.get("dbt_model"):
            raise _config_error("checks.dbt_impact.table_mapping entries require dbt_model",
                                "checks.dbt_impact.table_mapping.dbt_model", "non_empty_string", entry_index=index)
        entry: dict[str, str] = {}
        for key in sorted(TABLE_MAPPING_KEYS):
            raw = item.get(key)
            if raw is None:
                continue
            if not isinstance(raw, str) or not raw.strip() or len(raw.strip()) > 512:
                raise _config_error(
                    f"checks.dbt_impact.table_mapping {key} must be a non-empty string no longer than 512 characters",
                    f"checks.dbt_impact.table_mapping.{key}", "non_empty_string", entry_index=index,
                )
            entry[key] = raw.strip()
        mappings.append(entry)
    return mappings


def _prompt_sets(value: Any) -> list[dict[str, str]]:
    """Validate the ai_eval prompt sets to evaluate, modeled on _table_mapping."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise _config_error("checks.ai_eval.prompt_sets must be a list of prompt set mappings",
                            "checks.ai_eval.prompt_sets", "list")
    if len(value) > MAX_PROMPT_SETS:
        raise SecurityPolicyError(f"checks.ai_eval.prompt_sets cannot exceed {MAX_PROMPT_SETS} entries")
    prompt_sets: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise _config_error("checks.ai_eval.prompt_sets entries must be mappings",
                                "checks.ai_eval.prompt_sets", "mapping", entry_index=index)
        _reject_unknown_keys(item, PROMPT_SET_KEYS, "checks.ai_eval.prompt_sets entry")
        prompt_set_id = item.get("id")
        if not isinstance(prompt_set_id, str) or not prompt_set_id.strip() or len(prompt_set_id.strip()) > 128:
            raise _config_error(
                "checks.ai_eval.prompt_sets entries require a non-empty id no longer than 128 characters",
                "checks.ai_eval.prompt_sets.id", "non_empty_string", entry_index=index,
            )
        prompt_set_id = prompt_set_id.strip()
        if prompt_set_id in seen_ids:
            raise _config_error("checks.ai_eval.prompt_sets contains duplicate identities",
                                "checks.ai_eval.prompt_sets.id", "string", reason="config_invalid_value", entry_index=index)
        seen_ids.add(prompt_set_id)
        label = item.get("label")
        if label is not None and (not isinstance(label, str) or not label.strip() or len(label.strip()) > 200):
            raise _config_error(
                "checks.ai_eval.prompt_sets label must be a non-empty string no longer than 200 characters",
                "checks.ai_eval.prompt_sets.label", "non_empty_string", entry_index=index,
            )
        entry = {"id": prompt_set_id, "label": label.strip() if label else prompt_set_id}
        model_id = item.get("model_id")
        if model_id is not None:
            if not isinstance(model_id, str) or not model_id.strip() or len(model_id.strip()) > 128:
                raise _config_error("checks.ai_eval.prompt_sets model_id must be a non-empty string of at most 128 characters",
                                    "checks.ai_eval.prompt_sets.model_id", "non_empty_string", entry_index=index)
            entry["model_id"] = model_id.strip()
        prompt_sets.append(entry)
    return prompt_sets


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _config_error(f"{name} must be a mapping", name, "mapping")
    return value


def _validate_config_schema(raw: dict[str, Any]) -> None:
    _reject_unknown_keys(raw, CONFIG_KEYS, "config")
    files = _mapping(raw.get("files"), "files")
    _reject_unknown_keys(files, FILE_KEYS, "files")
    # Validate before environment expansion: file exclusions may not depend on
    # mutable workflow variables rather than reviewed repository policy.
    _non_omni_rules(files.get("non_omni"))
    validation = _mapping(raw.get("validation"), "validation")
    _reject_unknown_keys(validation, VALIDATION_KEYS, "validation")
    _validation_mode(validation.get("mode", "strict"))
    omni = _mapping(raw.get("omni"), "omni")
    checks = _mapping(raw.get("checks"), "checks")
    reporting = _mapping(raw.get("reporting"), "reporting")
    security = _mapping(raw.get("security"), "security")
    contracts = _mapping(raw.get("contracts"), "contracts")
    repairs = _mapping(raw.get("repairs"), "repairs")
    deployment = _mapping(raw.get("deployment"), "deployment")
    _reject_unknown_keys(omni, OMNI_KEYS, "omni")
    _reject_unknown_keys(checks, CHECK_KEYS, "checks")
    # Query cohorts, budgets and attestations must be fixed reviewed policy, not
    # mutable workflow-variable substitutions.
    parse_query_validation(checks.get("query_validation"))
    _omni_repository_settings(_mapping(checks.get("dbt_impact"), "checks.dbt_impact").get("omni_repository"))
    _reject_unknown_keys(reporting, REPORTING_KEYS, "reporting")
    _diagnostics_mode(reporting)
    _reject_unknown_keys(security, SECURITY_KEYS, "security")
    _reject_unknown_keys(contracts, CONTRACT_KEYS, "contracts")
    _reject_unknown_keys(repairs, REPAIR_KEYS, "repairs")
    _reject_unknown_keys(deployment, DEPLOYMENT_KEYS, "deployment")
    _reject_unknown_keys(_mapping(repairs.get("ai"), "repairs.ai"), AI_REPAIR_KEYS, "repairs.ai")
    _reject_unknown_keys(
        _mapping(deployment.get("dbt_sync"), "deployment.dbt_sync"),
        DBT_SYNC_KEYS,
        "deployment.dbt_sync",
    )
    _reject_unknown_keys(
        _mapping(deployment.get("breaking_change_hold"), "deployment.breaking_change_hold"),
        BREAKING_HOLD_KEYS,
        "deployment.breaking_change_hold",
    )
    _reject_unknown_keys(
        _mapping(checks.get("content_validation"), "checks.content_validation"),
        CONTENT_KEYS,
        "checks.content_validation",
    )
    _reject_unknown_keys(
        _mapping(checks.get("model_validation"), "checks.model_validation"),
        MODEL_KEYS,
        "checks.model_validation",
    )
    _reject_unknown_keys(
        _mapping(checks.get("semantic_lint"), "checks.semantic_lint"),
        LINT_KEYS,
        "checks.semantic_lint",
    )
    _reject_unknown_keys(
        _mapping(checks.get("dbt_exposures"), "checks.dbt_exposures"),
        EXPOSURE_KEYS,
        "checks.dbt_exposures",
    )
    _reject_unknown_keys(
        _mapping(checks.get("dbt_impact"), "checks.dbt_impact"),
        DBT_IMPACT_KEYS,
        "checks.dbt_impact",
    )
    _reject_unknown_keys(
        _mapping(_mapping(checks.get("dbt_impact"), "checks.dbt_impact").get("content_impact"),
                 "checks.dbt_impact.content_impact"),
        CONTENT_IMPACT_KEYS, "checks.dbt_impact.content_impact",
    )
    _reject_unknown_keys(
        _mapping(checks.get("ai_eval"), "checks.ai_eval"),
        AI_EVAL_KEYS,
        "checks.ai_eval",
    )
    _reject_unknown_keys(
        _mapping(contracts.get("fail_on"), "contracts.fail_on"),
        CONTRACT_FAIL_KEYS,
        "contracts.fail_on",
    )


def _reject_unknown_keys(payload: dict[str, Any], allowed: set[str], source: str) -> None:
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown:
        raise _config_error(f"{source} contains unsupported keys", source.removesuffix(" entry"), "mapping",
                            reason="config_unknown_key")


def _bounded_int(name: str, value: Any, *, minimum: int, maximum: int, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise _config_error(f"{name} must be an integer", name, "integer") from exc
    if not minimum <= parsed <= maximum:
        raise _config_error(f"{name} must be between {minimum} and {maximum}", name, "integer",
                            reason="config_invalid_value")
    return parsed


def _omni_repository_settings(value: Any) -> OmniRepositorySettings | None:
    if value is None:
        return None
    from .revision_data import FULL_SHA, REPOSITORY
    from .trust import SAFE_GIT_REF_RE, _safe_repo_path

    source = "checks.dbt_impact.omni_repository"
    raw = _mapping(value, source)
    _reject_unknown_keys(raw, OMNI_REPOSITORY_KEYS, source)
    repository, ref = raw.get("repository"), raw.get("ref")
    if (not isinstance(repository, str) or not REPOSITORY.fullmatch(repository)
            or any(part in {".", ".."} for part in repository.split("/"))):
        raise _config_error("External source requires owner/repository", f"{source}.repository", "string")
    # First release follows a named published branch, not a tag or a coordinated
    # second PR. Resolve it once to a commit; never let a mutable name reach blobs.
    if (not isinstance(ref, str) or not SAFE_GIT_REF_RE.fullmatch(ref) or len(ref) > 255
            or FULL_SHA.fullmatch(ref) or ref.startswith(("refs/", "-", "/")) or ref.endswith(("/", ".", ".lock"))
            or any(part in {".", "..", ""} or part.startswith(".") for part in ref.split("/"))
            or ".." in ref or "@{" in ref):
        raise _config_error("External ref must be a branch name", f"{source}.ref", "string")
    paths = raw.get("model_paths")
    if not isinstance(paths, list) or not paths or len(paths) > MAX_DBT_PATHS:
        raise _config_error("External model_paths must be a bounded non-empty list", f"{source}.model_paths", "non_empty_list")
    normalized = []
    for path in paths:
        if not isinstance(path, str) or not path or "$" in path or _safe_repo_path(Path(path)) != path:
            raise _config_error("External model_paths must be canonical repository paths", f"{source}.model_paths", "repository_path")
        if any(path == prior or path == "." or prior == "." or path.startswith(prior + "/")
               or prior.startswith(path + "/") for prior in normalized):
            raise _config_error("External model_paths must not overlap", f"{source}.model_paths", "repository_path")
        normalized.append(path)
    return OmniRepositorySettings(
        repository=repository, ref=ref, model_paths=normalized,
        allow_repository_diagnostics=parse_bool(f"{source}.allow_repository_diagnostics",
                                               raw.get("allow_repository_diagnostics"), False),
    )


def _content_impact_settings(value: Any) -> DbtContentImpactSettings:
    source = "checks.dbt_impact.content_impact"
    raw = _mapping(value, source)
    dialect = raw.get("sql_dialect")
    if dialect is not None:
        from sqlglot.dialects import Dialect

        if not isinstance(dialect, str) or dialect not in Dialect.classes:
            raise _config_error("Unsupported SQL dialect", f"{source}.sql_dialect", "enum",
                                reason="config_invalid_value")
    return DbtContentImpactSettings(
        enabled=parse_bool(f"{source}.enabled", raw.get("enabled"), False),
        sql_dialect=dialect,
        max_searches=_bounded_int(f"{source}.max_searches", raw.get("max_searches"),
                                 minimum=1, maximum=500, default=100),
        timeout_seconds=_bounded_int(f"{source}.timeout_seconds", raw.get("timeout_seconds"),
                                     minimum=5, maximum=300, default=120),
    )


def _lint_rules(value: Any) -> dict[str, str]:
    rules = _mapping(value, "checks.semantic_lint.rules")
    normalized: dict[str, str] = {}
    for key, severity in rules.items():
        rule_id = str(key)
        level = str(severity).strip().lower()
        if rule_id not in SEMANTIC_LINT_RULES:
            raise _config_error("Unknown semantic lint rule", "checks.semantic_lint.rules", "mapping",
                                reason="config_unknown_key")
        if level not in RULE_SEVERITIES:
            raise _config_error(f"Invalid severity for {rule_id}", f"checks.semantic_lint.rules.{rule_id}",
                                "enum", reason="config_invalid_value")
        normalized[rule_id] = level
    return normalized


def _report_formats(value: Any) -> list[str]:
    formats = [item.lower() for item in parse_csv(value, name="reporting.formats")] or list(DEFAULT_REPORT_FORMATS)
    unknown = sorted(set(formats) - REPORT_FORMATS)
    if unknown:
        raise _config_error("Unknown reporting formats", "reporting.formats", "enum",
                            reason="config_invalid_value")
    return formats


def _diagnostics_mode(reporting: dict[str, Any]) -> str:
    """Disclosure policy is literal; environment expansion must not enable it."""
    diagnostics = reporting.get("diagnostics", {})
    if not isinstance(diagnostics, dict):
        raise _config_error("reporting.diagnostics must be a mapping", "reporting.diagnostics", "mapping")
    if any(key not in DIAGNOSTICS_KEYS for key in diagnostics):
        raise _config_error("reporting.diagnostics contains unsupported keys", "reporting.diagnostics", "mapping",
                            reason="config_unknown_key")
    mode = diagnostics.get("mode", "repository")
    if not isinstance(mode, str) or mode not in DIAGNOSTICS_MODES:
        raise _config_error("reporting.diagnostics.mode must be 'repository' or 'minimal'",
                            "reporting.diagnostics.mode", "enum", reason="config_invalid_value")
    return mode


def _output_dir(value: Any) -> str:
    if value is not None and not isinstance(value, str):
        raise _config_error("reporting.output_dir must be a repository path", "reporting.output_dir", "repository_path")
    output = str(value or ".omniflow").strip()
    path = Path(output)
    if not output or path.is_absolute() or ".." in path.parts:
        raise _config_error("reporting.output_dir must be a relative path inside the repository",
                            "reporting.output_dir", "repository_path", reason="config_invalid_value")
    validate_repo_output_path(path)
    return output


def require_api_key() -> str:
    value = os.getenv("OMNI_API_KEY")
    if not value:
        raise ConfigError("Missing OMNI_API_KEY. API keys are only read from environment variables.",
                          reason_code="missing_api_key")
    return value


def require_repair_api_key() -> str:
    value = os.getenv("OMNIFLOW_REPAIR_API_KEY")
    if not value:
        raise ConfigError(
            "Missing OMNIFLOW_REPAIR_API_KEY. AI repair requires a separate write-capable Omni token.",
            reason_code="missing_repair_api_key",
        )
    return value


def require_sync_api_key() -> str:
    value = os.getenv("OMNIFLOW_SYNC_API_KEY")
    if not value:
        raise ConfigError(
            "Missing OMNIFLOW_SYNC_API_KEY. dbt synchronization requires a dedicated deployment token.",
            reason_code="missing_sync_api_key",
        )
    return value
