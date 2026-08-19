from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .exceptions import ConfigError, SecurityPolicyError
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
CONFIG_KEYS = {"omni", "checks", "reporting", "security", "contracts", "repairs", "deployment"}
OMNI_KEYS = {
    "base_url",
    "model_id",
    "branch_id",
    "branch_name",
    "user_id",
    "include_personal_folders",
    "timeout",
}
CHECK_KEYS = {"content_validation", "model_validation", "semantic_lint", "dbt_exposures", "dbt_impact"}
CONTENT_KEYS = {"enabled", "fail_on_new_only", "labels"}
MODEL_KEYS = {"enabled", "fail_on_warnings"}
LINT_KEYS = {"enabled", "rules"}
EXPOSURE_KEYS = {"enabled", "fail_on_unavailable"}
DBT_IMPACT_KEYS = {
    "enabled",
    "manifest_path",
    "fail_on_orphaned_references",
    "omni_yaml_paths",
    "table_mapping",
}
TABLE_MAPPING_KEYS = {"dbt_model", "omni_view", "sql_table_name"}
MAX_TABLE_MAPPINGS = 500
REPORTING_KEYS = {"formats", "output_dir"}
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
    raise ConfigError(f"Invalid boolean value for {name}: {value!r}")


def parse_csv(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    names: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise ConfigError(f"Expected string list value, got {item!r}")
        for name in item.split(","):
            normalized = name.strip()
            if normalized and normalized not in names:
                names.append(normalized)
    return names


def config_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_raw_config(path: str | Path | None = None) -> tuple[dict[str, Any], Path | None]:
    candidates = [Path(path)] if path else [Path(DEFAULT_CONFIG_PATH)]
    for candidate in candidates:
        text = read_trusted_repo_text(candidate)
        if text is None:
            continue
        payload = parse_secure_yaml(text, source=str(candidate))
        if not isinstance(payload, dict):
            raise ConfigError(f"Config file '{candidate}' must contain a top-level mapping")
        reject_secret_keys(payload, source=str(candidate))
        _validate_config_schema(payload)
        return expand_env_vars(payload), candidate
    return {}, None


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
    labels: list[str] = field(default_factory=list)


@dataclass
class ModelValidationSettings:
    enabled: bool = True
    fail_on_warnings: bool = False


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
class DbtImpactSettings:
    enabled: bool = False
    manifest_path: str | None = None
    fail_on_orphaned_references: bool = True
    omni_yaml_paths: list[str] = field(default_factory=list)
    table_mapping: list[dict[str, str]] = field(default_factory=list)


@dataclass
class ReportingSettings:
    formats: list[str] = field(default_factory=lambda: list(DEFAULT_REPORT_FORMATS))
    output_dir: str = ".omniflow"


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
    reporting: ReportingSettings
    security: SecuritySettings
    ai_repair: AIRepairSettings
    dbt_sync: DbtSyncSettings
    breaking_change_hold: BreakingChangeHoldSettings
    hash: str


def load_config(path: str | Path | None = None) -> OmniFlowConfig:
    raw, source = load_raw_config(path)
    return _to_config(raw, source)


def _to_config(raw: dict[str, Any], source: Path | None) -> OmniFlowConfig:
    omni_raw = _mapping(raw.get("omni"), "omni")
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
        fail_on_new_only=parse_bool(
            "content_validation.fail_on_new_only",
            os.getenv("OMNI_FAIL_ON_NEW_ONLY", content_raw.get("fail_on_new_only")),
            False,
        ),
        labels=parse_csv(os.getenv("OMNI_LABELS", content_raw.get("labels"))),
    )
    model = ModelValidationSettings(
        enabled=parse_bool("model_validation.enabled", model_raw.get("enabled"), True),
        fail_on_warnings=parse_bool("model_validation.fail_on_warnings", model_raw.get("fail_on_warnings"), False),
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
        omni_yaml_paths=_repo_path_list(
            dbt_impact_raw.get("omni_yaml_paths"), name="checks.dbt_impact.omni_yaml_paths"
        ),
        table_mapping=_table_mapping(dbt_impact_raw.get("table_mapping")),
    )
    reporting = ReportingSettings(
        formats=formats,
        output_dir=output_dir,
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
        reporting=reporting,
        security=security,
        ai_repair=ai_repair,
        dbt_sync=dbt_sync,
        breaking_change_hold=breaking_change_hold,
        hash=config_hash(raw),
    )


def _string_env(env_name: str, configured: Any) -> str | None:
    value = os.getenv(env_name, configured)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"Expected {env_name} to resolve to a string")
    stripped = value.strip()
    return stripped or None


def _redaction_level(value: Any) -> str:
    if value is None:
        return "standard"
    if not isinstance(value, str):
        raise ConfigError("security.redaction_level must be 'standard' or 'strict'")
    normalized = value.strip().lower()
    if normalized not in {"standard", "strict"}:
        raise ConfigError("security.redaction_level must be 'standard' or 'strict'")
    return normalized


def _refresh_mode(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError("deployment.dbt_sync.refresh_mode must be 'hard' or 'soft'")
    normalized = value.strip().lower()
    if normalized not in {"hard", "soft"}:
        raise ConfigError("deployment.dbt_sync.refresh_mode must be 'hard' or 'soft'")
    return normalized


def _hold_action(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError("deployment.breaking_change_hold.action must be 'fail' or 'warn'")
    normalized = value.strip().lower()
    if normalized not in BREAKING_HOLD_ACTIONS:
        raise ConfigError("deployment.breaking_change_hold.action must be 'fail' or 'warn'")
    return normalized


def _dbt_paths(value: Any) -> list[str]:
    if value is None:
        return list(DEFAULT_DBT_PATHS)
    if not isinstance(value, list):
        raise ConfigError("deployment.breaking_change_hold.dbt_paths must be a list of repository paths")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(
                "deployment.breaking_change_hold.dbt_paths entries must be non-empty strings"
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
            raise ConfigError(
                "deployment.breaking_change_hold.dbt_paths entries must be relative repository paths"
            )
        candidate = raw_candidate.strip("/")
        if not candidate or candidate == ".":
            raise ConfigError(
                "deployment.breaking_change_hold.dbt_paths entries must be relative repository paths"
            )
        if candidate not in normalized:
            normalized.append(candidate)
    if not normalized:
        raise ConfigError("deployment.breaking_change_hold.dbt_paths must include at least one path")
    if len(normalized) > MAX_DBT_PATHS:
        raise SecurityPolicyError(
            f"deployment.breaking_change_hold.dbt_paths cannot exceed {MAX_DBT_PATHS} entries"
        )
    return normalized


def _pending_label(value: Any) -> str:
    if value is None:
        return DEFAULT_PENDING_LABEL
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("deployment.breaking_change_hold.pending_label must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > 50 or any(character in normalized for character in ("\x00", "\r", "\n", ",")):
        raise ConfigError(
            "deployment.breaking_change_hold.pending_label must be one line, no commas, and 50 characters or fewer"
        )
    return normalized


def _relative_repo_path(value: Any, *, name: str) -> str | None:
    """Validate a single optional repository-relative path."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string when provided")
    candidate = value.strip()
    path = Path(candidate)
    if (
        path.is_absolute()
        or ".." in path.parts
        or len(candidate) > 1_024
        or any(character in candidate for character in ("\x00", "\r", "\n", "\\"))
    ):
        raise ConfigError(f"{name} must be a relative path inside the repository")
    return candidate


def _repo_path_list(value: Any, *, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{name} must be a list of repository paths")
    normalized: list[str] = []
    for item in value:
        candidate = _relative_repo_path(item, name=name)
        if candidate is None:
            raise ConfigError(f"{name} entries must be non-empty strings")
        stripped = candidate.strip("/")
        if not stripped or stripped == ".":
            raise ConfigError(f"{name} entries must be relative repository paths")
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
        raise ConfigError("checks.dbt_impact.table_mapping must be a list of mappings")
    if len(value) > MAX_TABLE_MAPPINGS:
        raise SecurityPolicyError(
            f"checks.dbt_impact.table_mapping cannot exceed {MAX_TABLE_MAPPINGS} entries"
        )
    mappings: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ConfigError("checks.dbt_impact.table_mapping entries must be mappings")
        _reject_unknown_keys(item, TABLE_MAPPING_KEYS, "checks.dbt_impact.table_mapping entry")
        if not item.get("dbt_model"):
            raise ConfigError("checks.dbt_impact.table_mapping entries require dbt_model")
        entry: dict[str, str] = {}
        for key in sorted(TABLE_MAPPING_KEYS):
            raw = item.get(key)
            if raw is None:
                continue
            if not isinstance(raw, str) or not raw.strip() or len(raw.strip()) > 512:
                raise ConfigError(
                    f"checks.dbt_impact.table_mapping {key} must be a non-empty string no longer than 512 characters"
                )
            entry[key] = raw.strip()
        mappings.append(entry)
    return mappings


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping")
    return value


def _validate_config_schema(raw: dict[str, Any]) -> None:
    _reject_unknown_keys(raw, CONFIG_KEYS, "config")
    omni = _mapping(raw.get("omni"), "omni")
    checks = _mapping(raw.get("checks"), "checks")
    reporting = _mapping(raw.get("reporting"), "reporting")
    security = _mapping(raw.get("security"), "security")
    contracts = _mapping(raw.get("contracts"), "contracts")
    repairs = _mapping(raw.get("repairs"), "repairs")
    deployment = _mapping(raw.get("deployment"), "deployment")
    _reject_unknown_keys(omni, OMNI_KEYS, "omni")
    _reject_unknown_keys(checks, CHECK_KEYS, "checks")
    _reject_unknown_keys(reporting, REPORTING_KEYS, "reporting")
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
        _mapping(contracts.get("fail_on"), "contracts.fail_on"),
        CONTRACT_FAIL_KEYS,
        "contracts.fail_on",
    )


def _reject_unknown_keys(payload: dict[str, Any], allowed: set[str], source: str) -> None:
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown:
        raise ConfigError(f"{source} contains unsupported key(s): {', '.join(unknown)}")


def _bounded_int(name: str, value: Any, *, minimum: int, maximum: int, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _lint_rules(value: Any) -> dict[str, str]:
    rules = _mapping(value, "checks.semantic_lint.rules")
    normalized: dict[str, str] = {}
    for key, severity in rules.items():
        rule_id = str(key)
        level = str(severity).strip().lower()
        if rule_id not in SEMANTIC_LINT_RULES:
            raise ConfigError(f"Unknown semantic lint rule: {rule_id}")
        if level not in RULE_SEVERITIES:
            raise ConfigError(f"Invalid severity for {rule_id}: {severity!r}")
        normalized[rule_id] = level
    return normalized


def _report_formats(value: Any) -> list[str]:
    formats = [item.lower() for item in parse_csv(value)] or list(DEFAULT_REPORT_FORMATS)
    unknown = sorted(set(formats) - REPORT_FORMATS)
    if unknown:
        raise ConfigError(f"Unknown reporting format(s): {', '.join(unknown)}")
    return formats


def _output_dir(value: Any) -> str:
    output = str(value or ".omniflow").strip()
    path = Path(output)
    if not output or path.is_absolute() or ".." in path.parts:
        raise ConfigError("reporting.output_dir must be a relative path inside the repository")
    validate_repo_output_path(path)
    return output


def require_api_key() -> str:
    value = os.getenv("OMNI_API_KEY")
    if not value:
        raise ConfigError("Missing OMNI_API_KEY. API keys are only read from environment variables.")
    return value


def require_repair_api_key() -> str:
    value = os.getenv("OMNIFLOW_REPAIR_API_KEY")
    if not value:
        raise ConfigError(
            "Missing OMNIFLOW_REPAIR_API_KEY. AI repair requires a separate write-capable Omni token."
        )
    return value


def require_sync_api_key() -> str:
    value = os.getenv("OMNIFLOW_SYNC_API_KEY")
    if not value:
        raise ConfigError(
            "Missing OMNIFLOW_SYNC_API_KEY. dbt synchronization requires a dedicated deployment token."
        )
    return value
