from __future__ import annotations

import argparse
import json
import os
import platform
import shutil

# Git is invoked without a shell and with bounded arguments.
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .artifacts import (
    public_dir,
    restricted_dir,
    write_artifact_manifest,
    write_emergency_failure,
    write_public_json,
    write_public_reports,
)
from .breaking_hold import evaluate_breaking_hold, hold_triggered
from .config import (
    DEFAULT_REPORT_FORMATS,
    load_config,
    require_api_key,
    require_repair_api_key,
    require_sync_api_key,
)
from .contracts import evaluate_contracts
from .dbt_impact import evaluate_dbt_impact
from .dbt_sync import run_dbt_sync, validate_dbt_sync_environment
from .diagnostics import diagnostic_session, public_diagnostic, safe_exception_message
from .diff.diff_engine import diff_graphs
from .diff.semantic_graph import has_inheritance, load_yaml_graph
from .diff.yaml_loader import load_yaml_files
from .discovery import (
    ModelContext,
    discover_contexts,
    discover_deployment_contexts,
    get_changed_files,
    load_flow_metadata,
)
from .downstream import generate_downstream_dependencies
from .exceptions import (
    ConfigError,
    ExitCodes,
    OmniAuthError,
    OmniFlowError,
    OmniResponseError,
    SecurityPolicyError,
    exception_diagnostic,
)
from .execution import ValidationProgress, scope_findings
from .exposures import run_dbt_exposure_enrichment
from .git import current_branch, current_sha, event_name, git_executable, git_value, is_pull_request_event, pr_number
from .github.annotations import public_annotation_lines
from .github.repair_attempt import GitHubRepairAttemptGuard, load_repair_event
from .logging import configure_logging
from .nonsemantic import classify_pr_change
from .omni_client import OmniClient
from .query_validation import QueryProvenance
from .repair.orchestrator import run_ai_repair, validate_ai_repair_policy
from .repair.reporting import write_repair_artifacts
from .reporting.issue_policy import is_blocking
from .reporting.json_report import write_json_report
from .repository_diagnostics import (
    REPORT_NAMES as REPOSITORY_REPORT_NAMES,
)
from .repository_diagnostics import (
    clear_repository_reports,
    finish_repository_context,
    publish_repository_output,
    record_candidate_diagnostics,
    repository_reporting,
    start_repository_context,
)
from .revision_data import pull_request_changed_files, pull_request_revision
from .security import redact, validate_path_segment, validate_repo_output_path
from .timestamps import utc_now_iso
from .validation_run import SYNC_REASONS, mark_health_only, may_advise, verify_with_retry
from .validation_scope import ValidationEvidence, plan_content_scope
from .validators.ai_eval import run_ai_eval_validation
from .validators.content import ContentEvidenceError, run_content_validation
from .validators.model import run_model_validation
from .validators.query_validation import run_query_validation
from .validators.yaml_lint import compare_lint_findings, has_error, lint_graph
from .view_identity import ViewNamesMetadataError
from .yaml_pull import pull_yaml


def main(argv: list[str] | None = None, *, changed_files: list[str] | None = None) -> int:
    if os.name == "posix":
        os.umask(0o077)
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging("DEBUG" if getattr(args, "verbose", False) else "INFO")
    try:
        if changed_files is not None:
            if args.func is not cmd_run:
                raise ConfigError("An internal revision inventory is only supported for run")
            return cmd_run(args, changed_files=changed_files)
        return args.func(args)
    except OmniFlowError as exc:
        print(safe_exception_message(exc), file=sys.stderr)
        return exc.exit_code
    except Exception as exc:
        print(safe_exception_message(exc), file=sys.stderr)
        return ExitCodes.INTERNAL_ERROR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="omniflow", description="OmniFlow semantic-layer CI/CD orchestrator")
    parser.add_argument("--version", action="version", version=f"omniflow {__version__}")
    parser.add_argument("--verbose", action="store_true")
    subcommands = parser.add_subparsers(required=True)

    run_parser = subcommands.add_parser("run", help="Run enabled configured checks")
    _add_config_arg(run_parser)
    _add_common_omni_args(run_parser)
    run_parser.add_argument(
        "--auto", action="store_true", help="Discover Omni model context from Omni-managed metadata"
    )
    run_parser.add_argument("--skip-reason", help=argparse.SUPPRESS)
    run_parser.set_defaults(func=cmd_run)

    route_parser = subcommands.add_parser(
        "route",
        help="Determine whether an automatic run contains Omni semantic-layer changes",
    )
    _add_config_arg(route_parser)
    _add_common_omni_args(route_parser)
    route_parser.add_argument(
        "--auto", action="store_true", help="Discover Omni model context from Omni-managed metadata"
    )
    route_parser.add_argument("--format", choices=("text", "json", "github"), default="text")
    route_parser.set_defaults(func=cmd_route)

    content = subcommands.add_parser("content", help="Content validation commands")
    content_sub = content.add_subparsers(required=True)
    content_validate = content_sub.add_parser("validate", help="Validate Omni content")
    _add_config_arg(content_validate)
    _add_common_omni_args(content_validate)
    content_validate.add_argument("--history-in", default=None)
    content_validate.add_argument("--history-out", default=None)
    content_validate.add_argument("--report-out", default=None)
    content_validate.add_argument("--label", action="append", default=[])
    content_validate.add_argument("--labels", action="append", default=[])
    content_validate.add_argument("--fail-on-new-only", action=argparse.BooleanOptionalAction, default=None)
    content_validate.add_argument(
        "--unsafe-raw-output",
        action="store_true",
        help="Include raw issue objects for an explicit local debugging run",
    )
    content_validate.set_defaults(func=cmd_content_validate)

    model = subcommands.add_parser("model", help="Model validation commands")
    model_sub = model.add_subparsers(required=True)
    model_validate = model_sub.add_parser("validate", help="Validate Omni model YAML")
    _add_config_arg(model_validate)
    _add_common_omni_args(model_validate)
    model_validate.add_argument("--fail-on-warnings", action=argparse.BooleanOptionalAction, default=None)
    model_validate.set_defaults(func=cmd_model_validate)

    query = subcommands.add_parser("query", help="Optional reviewed query-result checks")
    query_sub = query.add_subparsers(required=True)
    query_propose = query_sub.add_parser("propose", help="Generate a private AI proposal without executing it")
    _add_config_arg(query_propose)
    _add_common_omni_args(query_propose)
    query_propose.add_argument("--topic", required=True)
    query_propose.add_argument("--prompt", required=True)
    query_propose.add_argument("--output", required=True)
    query_propose.set_defaults(func=cmd_query_propose)

    yaml_parser = subcommands.add_parser("yaml", help="YAML commands")
    yaml_sub = yaml_parser.add_subparsers(required=True)
    yaml_pull = yaml_sub.add_parser("pull", help="Fetch Omni model YAML")
    _add_config_arg(yaml_pull)
    _add_common_omni_args(yaml_pull)
    yaml_pull.add_argument("--out", default=None)
    yaml_pull.add_argument("--mode", default="combined")
    yaml_pull.add_argument("--fully-resolved", action="store_true")
    yaml_pull.set_defaults(func=cmd_yaml_pull)

    exposures = subcommands.add_parser("exposures", help="dbt exposure commands")
    exposures_sub = exposures.add_subparsers(required=True)
    exposures_pull = exposures_sub.add_parser("pull", help="Fetch Omni dbt exposure metadata")
    _add_config_arg(exposures_pull)
    _add_common_omni_args(exposures_pull)
    exposures_pull.add_argument("--out", default=None)
    exposures_pull.set_defaults(func=cmd_exposures_pull)

    dbt = subcommands.add_parser("dbt", help="dbt deployment commands")
    dbt_sub = dbt.add_subparsers(required=True)
    dbt_sync = dbt_sub.add_parser("sync", help="Refresh Omni after a successful production dbt deployment")
    _add_config_arg(dbt_sync)
    _add_common_omni_args(dbt_sync)
    dbt_sync.add_argument("--auto", action="store_true", help="Select deployment models from trusted metadata")
    dbt_sync.add_argument("--base-branch", help="Trusted deployment branch for explicit local debugging")
    dbt_sync.add_argument("--refresh-mode", choices=("hard", "soft"), default=None)
    dbt_sync.set_defaults(func=cmd_dbt_sync)

    diff_parser = subcommands.add_parser("diff", help="Compare semantic YAML")
    diff_parser.add_argument("--base", required=True, help="Directory containing base YAML")
    diff_parser.add_argument("--head", required=True, help="Directory containing head YAML")
    diff_parser.add_argument("--report-out", default=None)
    diff_parser.set_defaults(func=cmd_diff)

    report_parser = subcommands.add_parser("report", help="Render reports from report.json")
    report_parser.add_argument("--input", default=".omniflow/report.json")
    report_parser.add_argument("--output-dir", default=".omniflow")
    report_parser.add_argument("--format", action="append", default=["markdown"])
    report_parser.set_defaults(func=cmd_report)

    doctor = subcommands.add_parser("doctor", help="Check local configuration and environment")
    _add_config_arg(doctor)
    _add_common_omni_args(doctor)
    doctor.add_argument("--auto", action="store_true", help="Validate Omni-managed metadata discovery")
    doctor.set_defaults(func=cmd_doctor)

    repair = subcommands.add_parser("repair", help="Human-authorized repair commands")
    repair_sub = repair.add_subparsers(required=True)
    repair_ai = repair_sub.add_parser("ai", help="Run the opt-in Omni AI model repair workflow")
    _add_config_arg(repair_ai)
    _add_common_omni_args(repair_ai)
    repair_ai.add_argument("--auto", action="store_true", help="Discover the exact pull request model context")
    repair_ai.set_defaults(func=cmd_repair_ai)
    return parser


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config", default=None,
        help="Required policy file (default: .omniflow.yml); PRs read the trusted base branch",
    )


def _add_common_omni_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url")
    parser.add_argument("--model-id")
    parser.add_argument("--model-path")
    parser.add_argument("--branch-id")
    parser.add_argument("--branch-name")
    parser.add_argument("--user-id")
    parser.add_argument("--include-personal-folders", action=argparse.BooleanOptionalAction, default=None)


@diagnostic_session()
def cmd_run(args: argparse.Namespace, *, changed_files: list[str] | None = None) -> int:
    try:
        config = _override_config(load_config(args.config), args)
    except OmniFlowError as exc:
        _write_unconfigured_failure_artifacts(output_dir=Path(".omniflow"), exc=exc)
        raise
    output_dir = Path(config.reporting.output_dir)
    _validate_run_output_layout(output_dir)
    try:
        with repository_reporting(config, output_dir=output_dir):
            return _execute_run(args, config=config, output_dir=output_dir, changed_files=changed_files)
    except OmniFlowError:
        raise
    except Exception:
        # Do not repeat an unsafe formatter or echo an arbitrary exception.
        write_emergency_failure(output_dir=output_dir)
        return ExitCodes.INTERNAL_ERROR


def _execute_run(args, *, config, output_dir: Path, changed_files: list[str] | None) -> int:
    if not config.security.retain_restricted_artifacts:
        _purge_restricted_path(restricted_dir(output_dir))
    if args.skip_reason:
        _write_skipped_artifacts(config=config, output_dir=output_dir, reason=args.skip_reason)
        print("OmniFlow skipped under the trusted workflow policy; see the sanitized report.")
        return 0
    try:
        contexts = discover_contexts(
            auto=args.auto,
            base_url=config.omni.base_url,
            model_id=config.omni.model_id,
            model_path=getattr(args, "model_path", None),
            branch_name=config.omni.branch_name,
            branch_id=config.omni.branch_id,
            allow_skip=True,
            changed_files=changed_files,
        )
    except OmniFlowError as exc:
        _write_setup_failure_artifacts(config=config, output_dir=output_dir, exc=exc)
        raise
    if not contexts:
        # A pull request with no Omni model changes can still break Omni by
        # removing a warehouse column or model the committed YAML references.
        impact_exit = _run_dbt_impact_check(config=config, output_dir=output_dir, changed_files=changed_files)
        if impact_exit is not None:
            return impact_exit
        _write_skipped_artifacts(config=config, output_dir=output_dir)
        print("OmniFlow skipped: no Omni PR context or changed Omni model files detected")
        return 0
    all_reports = []
    all_issues: list[dict[str, Any]] = []
    exit_code = 0
    classification = classify_pr_change(contexts) if config.content_validation.classify_comment_only else None
    if changed_files is None:
        changed_files = get_changed_files() if config.breaking_change_hold.enabled else []
    for context in contexts:
        context_output_dir = restricted_dir(output_dir) / _safe_context_dir(context)
        _validate_context_output_layout(context_output_dir)
        try:
            try:
                context_report, context_exit = _run_context(
                    config=config,
                    context=context,
                    output_dir=context_output_dir,
                    changed_files=changed_files,
                    enforce_breaking_hold=True,
                )
            except Exception as exc:
                context_report, context_exit = _write_context_failure_artifacts(
                    config=config,
                    context=context,
                    output_dir=context_output_dir,
                    exc=exc,
                )
        finally:
            if not config.security.retain_restricted_artifacts:
                _purge_restricted_path(restricted_dir(output_dir))
        all_reports.append(context_report)
        all_issues.extend(context_report.get("issues", []))
        exit_code = max(exit_code, context_exit)

    summary = _summarize(all_issues)
    report = _aggregate_report(config, contexts, exit_code, all_issues, summary, all_reports)
    if classification is not None:
        # Classification is explanatory only. Git cannot prove the absence of
        # extra Omni drafts; no classification may suppress an enabled check.
        report["change_classification"] = classification
    public_report = write_public_reports(
        report,
        output_dir=output_dir,
        formats=config.reporting.formats,
        redaction_level=config.security.redaction_level,
    )
    _emit_github_annotations(public_report.get("issues", []), limit=config.security.max_report_samples)
    evidence = {
        "tool": "omniflow",
        "tool_version": __version__,
        "config_hash": config.hash,
        "git_sha": current_sha(),
        "git_branch": current_branch(),
        "pr_number": pr_number(),
        "event_type": event_name(),
        "runner": _runner_metadata(),
        "models": [_context_dict(context) for context in contexts],
        "validation_status": (
            "incomplete" if not report["validation_complete"] else "failed" if exit_code else "passed"
        ),
        "policy_decision": report["policy_decision"],
        "exit_code": exit_code,
        "exit_code_reason": _exit_code_reason(exit_code),
        "validation_complete": report["validation_complete"],
        "decision_status": report["decision_status"],
        "enforcement_mode": config.validation.mode,
        "timestamp": utc_now_iso(),
    }
    write_public_json(output_dir / "evidence.json", evidence, redaction_level=config.security.redaction_level)
    write_public_json(
        public_dir(output_dir) / "evidence.json", evidence, redaction_level=config.security.redaction_level
    )
    write_artifact_manifest(
        output_dir=output_dir,
        restricted_artifacts_enabled=config.security.retain_restricted_artifacts,
        redaction_level=config.security.redaction_level,
    )
    _emit_hold_output(all_issues, settings=config.breaking_change_hold)
    publish_repository_output()
    print(f"OmniFlow complete: models={len(contexts)} issues={summary['total_issues']} exit_code={exit_code}")
    return exit_code


def cmd_route(args: argparse.Namespace) -> int:
    """Perform trusted discovery before the Omni credential enters a process."""
    try:
        config = _override_config(load_config(args.config), args)
    except OmniFlowError as exc:
        _write_unconfigured_failure_artifacts(output_dir=Path(".omniflow"), exc=exc)
        raise
    output_dir = Path(config.reporting.output_dir)
    _validate_run_output_layout(output_dir)
    if not config.security.retain_restricted_artifacts:
        _purge_restricted_path(restricted_dir(output_dir))
    try:
        contexts = discover_contexts(
            auto=args.auto,
            base_url=config.omni.base_url,
            model_id=config.omni.model_id,
            model_path=getattr(args, "model_path", None),
            branch_name=config.omni.branch_name,
            branch_id=config.omni.branch_id,
            allow_skip=True,
        )
    except OmniFlowError as exc:
        _write_setup_failure_artifacts(config=config, output_dir=output_dir, exc=exc)
        raise

    requires_omni = bool(contexts)
    dbt_impact_needed = config.dbt_impact.enabled and any(
        _path_under_any(path, config.breaking_change_hold.dbt_paths) for path in get_dbt_changed_files()
    )
    should_run = requires_omni or dbt_impact_needed
    reason = "" if should_run else "no Omni PR context or changed Omni model files detected"
    if not should_run:
        _write_skipped_artifacts(config=config, output_dir=output_dir, reason=reason)

    payload = {
        "should_run": should_run,
        "requires_omni": requires_omni,
        "reason": reason,
        "model_count": len(contexts),
    }
    environment = contexts[0].environment if contexts else None
    if environment:
        payload.update(environment=environment, target_branch=contexts[0].base_branch)
    if args.format == "github":
        print(f"should_run={'true' if should_run else 'false'}")
        print(f"requires_omni={'true' if requires_omni else 'false'}")
        print(f"reason={reason}")
        print(f"model_count={len(contexts)}")
        if environment:
            print(f"environment={environment}")
            print(f"target_branch={contexts[0].base_branch}")
    elif args.format == "json":
        print(json.dumps(payload, sort_keys=True))
    else:
        decision = "run" if should_run else "skip"
        print(f"OmniFlow route: {decision} (models={len(contexts)})")
    return 0


def _write_setup_failure_artifacts(*, config, output_dir: Path, exc: OmniFlowError) -> None:
    issue = {
        "severity": "error",
        "validator": "setup",
        "message": redact(str(exc)),
        **exception_diagnostic(exc),
    }
    summary = _summarize([issue])
    report = {
        "tool": "omniflow",
        "tool_version": __version__,
        "generated_at": utc_now_iso(),
        "git_sha": current_sha(),
        "git_branch": current_branch(),
        "pr_number": pr_number(),
        "event_type": event_name(),
        "runner": _runner_metadata(),
        "models": [],
        "config_hash": config.hash,
        "summary": summary,
        "issues": [issue],
        "model_reports": [],
        "policy_decision": "fail",
        "exit_code": exc.exit_code,
        "exit_code_reason": _exit_code_reason(exc.exit_code),
    }
    write_public_reports(
        report,
        output_dir=output_dir,
        formats=config.reporting.formats,
        redaction_level=config.security.redaction_level,
    )
    evidence = {
        "tool": "omniflow",
        "tool_version": __version__,
        "config_hash": config.hash,
        "git_sha": current_sha(),
        "git_branch": current_branch(),
        "pr_number": pr_number(),
        "event_type": event_name(),
        "runner": _runner_metadata(),
        "models": [],
        "validation_status": "failed",
        "policy_decision": "fail",
        "exit_code": exc.exit_code,
        "exit_code_reason": report["exit_code_reason"],
        "timestamp": utc_now_iso(),
    }
    write_public_json(output_dir / "evidence.json", evidence, redaction_level=config.security.redaction_level)
    write_public_json(
        public_dir(output_dir) / "evidence.json", evidence, redaction_level=config.security.redaction_level
    )
    write_artifact_manifest(
        output_dir=output_dir,
        restricted_artifacts_enabled=config.security.retain_restricted_artifacts,
        redaction_level=config.security.redaction_level,
    )


def _write_skipped_artifacts(
    *,
    config,
    output_dir: Path,
    reason: str = "no Omni PR context or changed Omni model files detected",
) -> None:
    summary = _summarize([])
    report = {
        "tool": "omniflow",
        "tool_version": __version__,
        "generated_at": utc_now_iso(),
        "git_sha": current_sha(),
        "git_branch": current_branch(),
        "pr_number": pr_number(),
        "event_type": event_name(),
        "runner": _runner_metadata(),
        "models": [],
        "config_hash": config.hash,
        "summary": summary,
        "issues": [],
        "model_reports": [],
        "policy_decision": "skipped",
        "exit_code": 0,
        "exit_code_reason": reason,
    }
    write_public_reports(
        report,
        output_dir=output_dir,
        formats=config.reporting.formats,
        redaction_level=config.security.redaction_level,
    )
    evidence = {
        "tool": "omniflow",
        "tool_version": __version__,
        "config_hash": config.hash,
        "git_sha": current_sha(),
        "git_branch": current_branch(),
        "pr_number": pr_number(),
        "event_type": event_name(),
        "runner": _runner_metadata(),
        "models": [],
        "validation_status": "skipped",
        "policy_decision": "skipped",
        "exit_code": 0,
        "exit_code_reason": report["exit_code_reason"],
        "timestamp": utc_now_iso(),
    }
    write_public_json(output_dir / "evidence.json", evidence, redaction_level=config.security.redaction_level)
    write_public_json(
        public_dir(output_dir) / "evidence.json", evidence, redaction_level=config.security.redaction_level
    )
    write_artifact_manifest(
        output_dir=output_dir,
        restricted_artifacts_enabled=config.security.retain_restricted_artifacts,
        redaction_level=config.security.redaction_level,
    )


def _run_dbt_impact_check(*, config, output_dir: Path, changed_files: list[str] | None = None) -> int | None:
    """Analyze a non-Omni pull request for dbt changes that orphan Omni references.

    Returns an exit code when the check ran, or None when it was not applicable so
    the caller falls through to the normal skip path. This runs without an Omni
    credential because it only compares committed dbt and Omni files.
    """
    if not config.dbt_impact.enabled:
        return None
    dbt_paths = config.breaking_change_hold.dbt_paths
    changed_files = get_dbt_changed_files() if changed_files is None else changed_files
    if not any(_path_under_any(path, dbt_paths) for path in changed_files):
        return None

    omni_yaml_paths = config.dbt_impact.omni_yaml_paths or _flow_model_paths()
    try:
        report, issues = evaluate_dbt_impact(
            changed_files=changed_files,
            dbt_paths=dbt_paths,
            settings=config.dbt_impact,
            omni_yaml_paths=omni_yaml_paths,
            base_ref=_base_ref(),
        )
    except OmniFlowError as exc:
        _write_setup_failure_artifacts(config=config, output_dir=output_dir, exc=exc)
        raise
    exit_code = (
        ExitCodes.VALIDATION_FAILED
        if any(issue.get("severity") == "error" for issue in issues)
        else ExitCodes.SUCCESS
    )
    aggregate = {
        "tool": "omniflow",
        "tool_version": __version__,
        "operation": "dbt_impact",
        "generated_at": utc_now_iso(),
        "git_sha": current_sha(),
        "git_branch": current_branch(),
        "pr_number": pr_number(),
        "event_type": event_name(),
        "runner": _runner_metadata(),
        "models": [],
        "config_hash": config.hash,
        "summary": _summarize(issues),
        "issues": issues,
        "model_reports": [report],
        "raw_query_results_stored": False,
        "policy_decision": "fail" if exit_code else "pass",
        "exit_code": exit_code,
        "exit_code_reason": _exit_code_reason(exit_code),
    }
    public_report = write_public_reports(
        aggregate,
        output_dir=output_dir,
        formats=config.reporting.formats,
        redaction_level=config.security.redaction_level,
    )
    write_public_json(
        output_dir / "dbt-impact.json", report, redaction_level=config.security.redaction_level
    )
    write_public_json(
        public_dir(output_dir) / "dbt-impact.json",
        report,
        redaction_level=config.security.redaction_level,
    )
    evidence = {
        "tool": "omniflow",
        "tool_version": __version__,
        "operation": "dbt_impact",
        "config_hash": config.hash,
        "git_sha": current_sha(),
        "git_branch": current_branch(),
        "pr_number": pr_number(),
        "event_type": event_name(),
        "runner": _runner_metadata(),
        "models": [],
        "analysis_mode": report["analysis_mode"],
        "validation_status": "failed" if exit_code else "passed",
        "policy_decision": aggregate["policy_decision"],
        "raw_query_results_stored": False,
        "exit_code": exit_code,
        "exit_code_reason": aggregate["exit_code_reason"],
        "timestamp": utc_now_iso(),
    }
    write_public_json(output_dir / "evidence.json", evidence, redaction_level=config.security.redaction_level)
    write_public_json(
        public_dir(output_dir) / "evidence.json",
        evidence,
        redaction_level=config.security.redaction_level,
    )
    write_artifact_manifest(
        output_dir=output_dir,
        restricted_artifacts_enabled=config.security.retain_restricted_artifacts,
        redaction_level=config.security.redaction_level,
    )
    _emit_github_annotations(public_report.get("issues", []), limit=config.security.max_report_samples)
    print(
        "OmniFlow dbt impact analysis complete: "
        f"mode={report['analysis_mode']} issues={len(issues)} exit_code={exit_code}"
    )
    return exit_code


def get_dbt_changed_files() -> list[str]:
    exact = pull_request_changed_files()
    return exact if exact is not None else get_changed_files()


def _flow_model_paths() -> list[str]:
    """Read model paths from trusted base-branch metadata."""
    try:
        flow = load_flow_metadata(missing_ok=True)
    except OmniFlowError:
        return []
    if not flow:
        return []
    paths: list[str] = []
    for model in flow.get("models", []):
        if not isinstance(model, dict):
            continue
        candidate = model.get("model_path")
        if isinstance(candidate, str) and candidate.strip():
            normalized = candidate.strip().strip("/")
            if normalized and normalized not in paths:
                paths.append(normalized)
    return paths


def _base_ref() -> str | None:
    """Resolve a Git ref to compare dbt file history against.

    Different CI checkouts expose the base branch differently, so candidates are
    tried in order and the first one Git can actually resolve is used. Returning
    None means no comparison base is available and the caller reports that rather
    than guessing.
    """
    revision = pull_request_revision("base")
    if revision is not None:
        return revision[0]
    candidates: list[str] = []
    base_branch = os.getenv("GITHUB_BASE_REF")
    if base_branch:
        candidates.extend([f"origin/{base_branch}", base_branch])
    candidates.append("HEAD~1")
    for candidate in candidates:
        if _ref_exists(candidate):
            return git_value("rev-parse", candidate)
    return None


def _ref_exists(ref: str) -> bool:
    try:
        # Arguments are passed directly to Git, never through a shell.
        subprocess.run(  # nosec B603
            [git_executable(), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def _path_under_any(path: str, prefixes: list[str]) -> bool:
    normalized = str(path).strip().strip("/")
    if not normalized:
        return False
    for prefix in prefixes:
        cleaned = str(prefix).strip().strip("/")
        if cleaned and (normalized == cleaned or normalized.startswith(f"{cleaned}/")):
            return True
    return False


def _run_context(
    *,
    config,
    context: ModelContext,
    output_dir: Path,
    api_key: str | None = None,
    comparison_base_yaml_dir: Path | None = None,
    changed_files: list[str] | None = None,
    enforce_breaking_hold: bool = False,
    strict_health: bool = False,
) -> tuple[dict[str, Any], int]:
    progress = ValidationProgress(config, branch_id=context.branch_id, enforce_breaking_hold=enforce_breaking_hold)
    regression_pr = not strict_health and is_pull_request_event() and (
        (config.content_validation.enabled and config.content_validation.fail_on_new_only)
        or (config.model_validation.enabled and config.model_validation.fail_on_new_only)
    )
    query_comparison = not strict_health and is_pull_request_event() and config.query_validation.enabled
    if context.environment or regression_pr or query_comparison:
        progress.states["candidate"] = {"validator": "candidate", "status": "not_run"}
    try:
        return _run_context_steps(
            config=config, context=context, output_dir=output_dir, api_key=api_key,
            comparison_base_yaml_dir=comparison_base_yaml_dir, changed_files=changed_files,
            enforce_breaking_hold=enforce_breaking_hold, progress=progress,
            strict_health=strict_health, regression_pr=regression_pr,
        )
    except Exception as exc:
        progress.fail(exc)
        if progress.current == "candidate":
            # Completed observations are still useful health evidence, but a
            # failed final provenance sample invalidates the PR attribution.
            for check in progress.reports:
                if check.get("validator") in {"content", "model"}:
                    mark_health_only(check)
                elif check.get("validator") == "query_validation":
                    # A later snapshot change invalidates attribution, even if
                    # the warehouse calls themselves completed successfully.
                    check.update(validation_complete=False, execution_status="partial",
                                 query_validation_status="inconclusive", comparison_status="unavailable",
                                 new_issue_count=0, existing_issue_count=0)
                    progress.states["query_validation"]["status"] = "partial"
                    for issue in check.get("issues", []):
                        issue.pop("state", None)
                        issue.update(health_only=True, blocking=False)
        exc.omniflow_progress = progress
        raise


def _run_context_steps(
    *, config, context: ModelContext, output_dir: Path, api_key: str | None,
    comparison_base_yaml_dir: Path | None, changed_files: list[str] | None,
    enforce_breaking_hold: bool, progress: ValidationProgress,
    strict_health: bool, regression_pr: bool,
) -> tuple[dict[str, Any], int]:
    if context.environment and (config.breaking_change_hold.enabled or config.dbt_sync.enabled):
        raise SecurityPolicyError(
            "Version 2 environment targets are validation-only. Disable deployment synchronization and "
            "breaking-change hold/readiness in this validation policy; repository-wide sync state is not "
            "evidence for the selected environment. Keep separate governed deployment gates."
        )
    client, branch_id = _client_and_branch_for_context(context, config.omni.timeout, api_key=api_key)
    progress.branch_id = branch_id
    progress.complete("context")
    all_issues = progress.issues
    reports = progress.reports
    evidence = ValidationEvidence()
    exit_code = 0
    query_comparison = not strict_health and is_pull_request_event() and config.query_validation.enabled
    verify_provenance = bool(context.environment) or regression_pr or query_comparison
    baseline_proof = None
    if verify_provenance:
        from .candidate import CandidateVerificationError

        progress.begin("candidate")
        context.candidate_verification = {"status": "unverified"}
        try:
            proofs, verification_errors = {}, []
            # Inspect both sides before considering advisory handling. A sync
            # mismatch on one side must not conceal an exclusion conflict on
            # the other side.
            for side in (("base", "head") if regression_pr or query_comparison else ("head",)):
                try:
                    proofs[side] = verify_with_retry(
                        client, context, revision_side=side, non_omni_rules=config.files.non_omni,
                    )
                except CandidateVerificationError as exc:
                    verification_errors.append(exc)
            if verification_errors:
                raise next((error for error in verification_errors
                            if error.candidate_reason not in SYNC_REASONS), verification_errors[0])
            context.candidate_verification = proofs["head"]
            # Completion requires the second sample after every enabled check.
            context.candidate_verification["status"] = "pending_recheck"
            baseline_proof = proofs.get("base")
        except CandidateVerificationError as exc:
            if exc.candidate_reason in SYNC_REASONS:
                if (config.omni.include_personal_folders and config.semantic_lint.enabled
                        and config.semantic_lint.rules.get("forbid_personal_folder_validation_scope", "error") == "error"):
                    raise SecurityPolicyError("Configured validation visibility violates the protected scope policy.") from exc
                _collect_candidate_health(
                    config=config, client=client, context=context, branch_id=branch_id,
                    output_dir=output_dir, progress=progress,
                )
            progress.begin("candidate")
            raise

    repository_evidence = start_repository_context(
        context, client, branch_id=branch_id,
        head_proof=context.candidate_verification if verify_provenance else None,
        base_proof=baseline_proof,
        non_omni_rules=config.files.non_omni,
    )

    if config.content_validation.enabled:
        progress.begin("content")
        content_report, content_exit = run_content_validation(
            client=client,
            model_id=context.model_id,
            branch_id=branch_id,
            user_id=config.omni.user_id,
            include_personal_folders=config.omni.include_personal_folders,
            labels=config.content_validation.labels,
            history_in=output_dir / "history.json",
            history_out=output_dir / "history.json",
            report_out=output_dir / "content-report.json",
            fail_on_new_only=config.content_validation.fail_on_new_only and bool(branch_id) and not strict_health,
            force_full_validation=config.content_validation.force_full_validation,
            max_samples=config.security.max_report_samples,
            redact_document_names=config.security.redact_document_names,
            allow_raw_response_output=config.security.allow_raw_response_output,
            evidence=evidence,
            # Git alone cannot prove indirect content dependency coverage or
            # absence of branch drafts. Keep broad validation until an
            # independently qualified coverage provider supplies that evidence.
            scope_plan=plan_content_scope(None, enabled=config.content_validation.targeted),
        )
        reports.append(content_report)
        all_issues.extend(content_report.get("issues", []))
        exit_code = max(exit_code, content_exit)
        progress.complete("content", content_exit, execution_status=content_report.get("execution_status"))

    if config.model_validation.enabled:
        progress.begin("model")
        model_report, model_exit = run_model_validation(
            client=client,
            model_id=context.model_id,
            branch_id=branch_id,
            fail_on_warnings=config.model_validation.fail_on_warnings,
            fail_on_new_only=config.model_validation.fail_on_new_only and not strict_health,
            evidence=evidence,
        )
        reports.append(model_report)
        all_issues.extend(model_report.get("issues", []))
        exit_code = max(exit_code, model_exit)
        progress.complete("model", model_exit, execution_status=model_report.get("execution_status"))

    if config.ai_eval.enabled and config.ai_eval.prompt_sets:
        progress.begin("ai_eval")
        ai_eval_report, ai_eval_detail, ai_eval_exit = run_ai_eval_validation(
            client=client,
            model_id=context.model_id,
            branch_id=branch_id,
            prompt_sets=config.ai_eval.prompt_sets,
            fail_on_regression=config.ai_eval.fail_on_regression,
            poll_interval_seconds=config.ai_eval.poll_interval_seconds,
            timeout_seconds=config.ai_eval.timeout_seconds,
            scoring_grace_seconds=config.ai_eval.scoring_grace_seconds,
            max_samples=config.security.max_report_samples,
            record_runs=lambda journal: write_json_report(output_dir / "ai-eval-runs.json", journal),
        )
        # Full per-prompt detail (cost breakdown, conversation IDs, timing) carries
        # customer prompt/answer content and stays restricted; only the bounded
        # public summary below is appended to the aggregate report.
        write_json_report(output_dir / "ai-eval-detail.json", ai_eval_detail)
        reports.append(ai_eval_report)
        all_issues.extend(ai_eval_report.get("issues", []))
        exit_code = max(exit_code, ai_eval_exit)
        progress.complete("ai_eval", ai_eval_exit, execution_status=ai_eval_report.get("execution_status"),
                          skipped_reason=ai_eval_report.get("skipped_reason"))

    diff_report = None
    head_graph = None
    base_graph = None
    # The breaking-change hold reads the semantic diff, so it must be able to
    # request one even when semantic lint and contracts are both disabled.
    needs_breaking_hold = enforce_breaking_hold and config.breaking_change_hold.enabled
    query_needs_graph = config.query_validation.enabled and any(
        contract.comparison == "absolute" for contract in config.query_validation.contracts
    )
    if config.semantic_lint.enabled or config.contracts.enabled or needs_breaking_hold or query_needs_graph:
        progress.begin("semantic_diff")
        base_yaml_dir = comparison_base_yaml_dir or output_dir / "yaml-base"
        head_yaml_dir = output_dir / "yaml-head"
        if comparison_base_yaml_dir is None:
            base_yaml_dir = _pull_analysis_yaml(
                client=client,
                model_id=context.model_id,
                branch_id=None,
                output_dir=base_yaml_dir,
            )
        head_yaml_dir = _pull_analysis_yaml(
            client=client,
            model_id=context.model_id,
            branch_id=branch_id,
            output_dir=head_yaml_dir,
        )
        base_graph = load_yaml_graph(base_yaml_dir, require_view_names=True)
        head_graph = load_yaml_graph(head_yaml_dir, require_view_names=True)
        diff_report = diff_graphs(base_graph, head_graph)
        write_json_report(output_dir / "semantic-diff.json", diff_report)
        progress.complete("semantic_diff")

    if config.query_validation.enabled:
        progress.begin("query_validation")
        base_revision = pull_request_revision("base") if query_comparison else None
        head_revision = pull_request_revision("head") if query_comparison else None
        newly_added_topics = (
            set(head_graph.topics) - set(base_graph.topics)
            if head_graph is not None and base_graph is not None else set()
        )
        provenance = QueryProvenance(
            model_id=context.model_id, branch_id=branch_id or "",
            base_sha=base_revision[0] if base_revision else "",
            head_sha=head_revision[0] if head_revision else "",
            base_verified=bool(query_comparison and baseline_proof),
            head_verified=bool(query_comparison and context.candidate_verification.get("status") == "pending_recheck"),
            added_entity_contract_ids=frozenset(
                contract.id for contract in config.query_validation.contracts
                if contract.comparison == "absolute"
                and contract.query().get("join_paths_from_topic_name") in newly_added_topics
            ),
        )
        query_report, query_exit = run_query_validation(
            client, context.model_id, branch_id=branch_id,
            settings=config.query_validation, provenance=provenance,
        )
        reports.append(query_report)
        all_issues.extend(query_report.get("issues", []))
        exit_code = max(exit_code, query_exit)
        progress.complete("query_validation", query_exit, execution_status=query_report.get("execution_status"))

    if config.semantic_lint.enabled and head_graph is not None:
        progress.begin("semantic_lint")
        lint_issues = lint_graph(
            head_graph,
            configured_rules=config.semantic_lint.rules,
            include_personal_folders=config.omni.include_personal_folders,
            diff_result=diff_report,
        )
        if regression_pr and base_graph is not None:
            lint_issues = compare_lint_findings(
                lint_issues,
                lint_graph(
                    base_graph, configured_rules=config.semantic_lint.rules,
                    include_personal_folders=config.omni.include_personal_folders,
                ),
                base_graph=base_graph, head_graph=head_graph,
            )
        lint_report = {
            "tool": "omniflow",
            "validator": "semantic_lint",
            "generated_at": utc_now_iso(),
            "model_id": context.model_id,
            "branch_id": branch_id,
            "issues": lint_issues,
            "summary": {
                "total_issues": len(lint_issues),
                "errors": sum(1 for issue in lint_issues if issue.get("severity") == "error"),
                "warnings": sum(1 for issue in lint_issues if issue.get("severity") == "warning"),
            },
        }
        reports.append(lint_report)
        all_issues.extend(lint_issues)
        exit_code = max(exit_code, 1 if has_error(lint_issues) else 0)
        progress.complete("semantic_lint", 1 if has_error(lint_issues) else 0)

    if config.contracts.enabled and diff_report is not None:
        progress.begin("downstream")
        dependencies = generate_downstream_dependencies(
            client=client,
            model_id=context.model_id,
            branch_id=branch_id,
            diff_result=diff_report,
            user_id=config.omni.user_id,
            include_personal_folders=config.omni.include_personal_folders,
        )
        write_json_report(output_dir / "dependencies.json", dependencies)
        progress.complete("downstream", execution_status=dependencies.get("execution_status"))
        progress.begin("contracts")
        contract_report, contract_exit = evaluate_contracts(
            diff_result=diff_report,
            dependencies=dependencies,
            settings=config.contracts,
            model_id=context.model_id,
        )
        write_json_report(output_dir / "contract-impact.json", contract_report)
        reports.append(contract_report)
        all_issues.extend(contract_report.get("issues", []))
        exit_code = max(exit_code, contract_exit)
        progress.complete("contracts", contract_exit, execution_status=contract_report.get("execution_status"))

    if config.dbt_exposures.enabled:
        progress.begin("dbt_exposures")
        exposure_report, exposure_exit = run_dbt_exposure_enrichment(
            client=client,
            model_id=context.model_id,
            branch_id=branch_id,
            settings=config.dbt_exposures,
        )
        write_json_report(output_dir / "dbt-exposures.json", exposure_report)
        reports.append(exposure_report)
        all_issues.extend(exposure_report.get("issues", []))
        exit_code = max(exit_code, exposure_exit)
        progress.complete("dbt_exposures", exposure_exit, execution_status=exposure_report.get("execution_status"))

    if enforce_breaking_hold and config.breaking_change_hold.enabled:
        progress.begin("breaking_change_hold")
        hold_issues = evaluate_breaking_hold(
            diff_result=diff_report,
            changed_files=changed_files or [],
            last_sync_sha=os.getenv("OMNIFLOW_LAST_SYNC_SHA"),
            settings=config.breaking_change_hold,
        )
        if hold_issues:
            hold_report = {
                "tool": "omniflow",
                "validator": "breaking_change_hold",
                "generated_at": utc_now_iso(),
                "model_id": context.model_id,
                "branch_id": branch_id,
                "action": config.breaking_change_hold.action,
                "pending_label": config.breaking_change_hold.pending_label,
                "issues": hold_issues,
                "summary": {
                    "total_issues": len(hold_issues),
                    "errors": sum(1 for issue in hold_issues if issue.get("severity") == "error"),
                    "warnings": sum(1 for issue in hold_issues if issue.get("severity") == "warning"),
                },
            }
            write_json_report(output_dir / "breaking-change-hold.json", hold_report)
            reports.append(hold_report)
            all_issues.extend(hold_issues)
            if config.breaking_change_hold.action == "fail":
                exit_code = max(exit_code, ExitCodes.VALIDATION_FAILED)
        progress.complete("breaking_change_hold", 1 if any(issue.get("severity") == "error" for issue in hold_issues) else 0)

    if verify_provenance:
        progress.begin("candidate")
        before = dict(context.candidate_verification, status="verified")
        final_proofs, final_errors = {}, []
        for side, prior in (("head", before), ("base", baseline_proof)):
            if prior is None:
                continue
            try:
                final_proofs[side] = verify_with_retry(
                    client, context, revision_side=side, non_omni_rules=config.files.non_omni,
                    retry=False,  # Observed final drift must never be retried away.
                )
                if prior != final_proofs[side]:
                    error = CandidateVerificationError("changed_during_validation")
                    error.validation_scope = "branch" if side == "head" else "base"
                    final_errors.append(error)
            except CandidateVerificationError as exc:
                final_errors.append(exc)
        if final_errors:
            raise next((error for error in final_errors
                        if error.candidate_reason not in SYNC_REASONS), final_errors[0])
        context.candidate_verification = final_proofs["head"]
        progress.complete("candidate")
    finish_repository_context(
        repository_evidence, context, client, all_issues, branch_id=branch_id,
        head_proof=context.candidate_verification if verify_provenance else None,
        base_proof=baseline_proof,
    )
    progress.begin("reporting")
    progress.complete("reporting")
    scope_findings(all_issues, model_id=context.model_id, branch_id=branch_id, branch_name=context.branch_name)
    summary = _summarize(all_issues)
    report = _base_report(config, context, branch_id, exit_code, all_issues, summary)
    report["check_reports"] = reports
    report.update(progress.evidence())
    if not report["validation_complete"] and exit_code == 0:
        report.update(policy_decision="advisory", decision_status="incomplete")
    write_json_report(output_dir / "report.json", report)
    return report, exit_code


def _collect_candidate_health(*, config, client, context, branch_id, output_dir, progress) -> None:
    """Read-only diagnostics after ordinary sync failure; never PR approval."""
    checks = []
    if config.model_validation.enabled:
        checks.append(("model", lambda: run_model_validation(
            client=client, model_id=context.model_id, branch_id=branch_id,
            fail_on_warnings=config.model_validation.fail_on_warnings, fail_on_new_only=False,
        )))
    if config.content_validation.enabled:
        checks.append(("content", lambda: run_content_validation(
            client=client, model_id=context.model_id, branch_id=branch_id,
            user_id=config.omni.user_id, include_personal_folders=config.omni.include_personal_folders,
            labels=config.content_validation.labels, history_in=output_dir / "history.json",
            history_out=output_dir / "history.json", report_out=output_dir / "content-report.json",
            fail_on_new_only=False, force_full_validation=config.content_validation.force_full_validation,
            max_samples=config.security.max_report_samples,
            redact_document_names=config.security.redact_document_names,
            allow_raw_response_output=False,
        )))
    for name, run_check in checks:
        progress.begin(name)
        print(f"OmniFlow: collecting {name} health only; PR impact remains unverified.", flush=True)
        try:
            health, code = run_check()
            mark_health_only(health)
            progress.reports.append(health)
            progress.issues.extend(health.get("issues", []))
            progress.complete(name, code, execution_status=health.get("execution_status"))
        except Exception as exc:
            progress.fail(exc)
            issue = {
                "validator": name, "severity": "error", "type": "validation_execution_failed",
                "message": "Health evidence could not be collected.",
                "operational_failure": True, "coverage_complete": False, "blocking": True,
            }
            if isinstance(exc, OmniResponseError):
                issue.update(exc.report_issue(validator=name))
            issue.update(exception_diagnostic(exc))
            progress.issues.append(issue)
            progress.reports.append({"validator": name, "validation_complete": False, "issues": [issue]})


def _pull_analysis_yaml(*, client, model_id: str, branch_id: str | None, output_dir: Path) -> Path:
    """Retain authored evidence; use Omni's resolved representation for inheritance."""
    pull_yaml(client=client, model_id=model_id, branch_id=branch_id, output_dir=output_dir)
    if has_inheritance(load_yaml_files(output_dir)):
        # Keep derived evidence outside the authored namespace: an authored
        # file may legitimately live at resolved/orders.view.
        resolved_dir = output_dir.with_name(f"{output_dir.name}-resolved")
        pull_yaml(
            client=client, model_id=model_id, branch_id=branch_id,
            output_dir=resolved_dir, fully_resolved=True,
        )
        return resolved_dir
    return output_dir


def _write_context_failure_artifacts(
    *,
    config,
    context: ModelContext,
    output_dir: Path,
    exc: Exception,
) -> tuple[dict[str, Any], int]:
    progress = getattr(exc, "omniflow_progress", None)
    if not isinstance(progress, ValidationProgress):
        progress = ValidationProgress(config, branch_id=context.branch_id, enforce_breaking_hold=False)
        progress.fail(exc)
    code = exc.exit_code if isinstance(exc, OmniFlowError) else ExitCodes.INTERNAL_ERROR
    issue = {
        "severity": "error",
        "validator": progress.current,
        "type": "validation_execution_failed",
        "message": redact(str(exc)) if isinstance(exc, OmniFlowError) else "An internal validation error interrupted this context.",
    }
    if isinstance(exc, ContentEvidenceError):
        issue = exc.report_issue(redact_document_names=config.security.redact_document_names)
    if isinstance(exc, OmniResponseError):
        issue = exc.report_issue(validator=progress.current)
    issue.update(exception_diagnostic(exc))
    if issue.get("validator") == "content":
        issue["requested_validation_depth"] = (
            "full" if config.content_validation.force_full_validation else "api_default"
        )
    if isinstance(exc, ViewNamesMetadataError):
        issue.update(metadata_stage=exc.metadata_stage, metadata_reason=exc.metadata_reason)
    if progress.current == "candidate":
        from .candidate import CANDIDATE_COUNT_FIELDS, CandidateVerificationError

        reason = getattr(exc, "candidate_reason", "verification_unavailable")
        issue.update(type="candidate_verification_failed", candidate_reason=reason, coverage_complete=False)
        if isinstance(exc, CandidateVerificationError):
            issue.update({key: value for key, value in exc.candidate_counts.items()
                          if key in CANDIDATE_COUNT_FIELDS and type(value) is int and 0 <= value <= 2**31 - 1})
            issue["contents_comparison"] = getattr(exc, "contents_comparison", "not_compared")
        if getattr(exc, "validation_scope", None) in {"base", "branch"}:
            issue["validation_scope"] = exc.validation_scope
        context.candidate_verification = {"status": "unverified", "reason": reason}
    advisory = may_advise(config, exc, progress.issues)
    issue["blocking"] = not advisory
    issues = [*progress.issues, issue]
    scope_findings(issues, model_id=context.model_id, branch_id=progress.branch_id, branch_name=context.branch_name)
    if progress.current == "candidate":
        record_candidate_diagnostics(context, exc, issue)
    summary = _summarize(issues)
    report = _base_report(config, context, progress.branch_id, code, issues, summary)
    report["check_reports"] = [*progress.reports, {
        "validator": issue["validator"], "coverage_complete": False, "issues": [issue],
        **({"requested_validation_depth": issue["requested_validation_depth"]}
           if "requested_validation_depth" in issue else {}),
    }]
    report.update(progress.evidence())
    report["validation_complete"] = False
    report["decision_status"] = "incomplete"
    if advisory:
        code = 0
        report.update(policy_decision="advisory", exit_code=0, enforcement_mode="advisory")
    report["exit_code_reason"] = _exit_code_reason(code)
    write_json_report(output_dir / "report.json", report)
    return report, code


@diagnostic_session()
def cmd_content_validate(args: argparse.Namespace) -> int:
    config = _override_config(load_config(args.config), args)
    output_dir = Path(config.reporting.output_dir)
    labels = _labels_from_args(args) or config.content_validation.labels
    fail_on_new_only = (
        config.content_validation.fail_on_new_only if args.fail_on_new_only is None else args.fail_on_new_only
    )
    _validate_run_output_layout(output_dir)
    private = restricted_dir(output_dir) / "content-validation"
    if args.unsafe_raw_output and (
        os.getenv("GITHUB_ACTIONS") == "true" or not config.security.retain_restricted_artifacts
    ):
        raise SecurityPolicyError("Raw diagnostics require explicit local restricted retention and cannot run in CI")
    history_out = Path(args.history_out) if args.history_out else private / "history.json"
    if args.history_out:
        if not config.security.retain_restricted_artifacts or not history_out.resolve().is_relative_to(
            restricted_dir(output_dir).resolve()
        ):
            raise SecurityPolicyError("History output requires explicit retention inside the restricted directory")
        validate_repo_output_path(history_out)
    try:
        if fail_on_new_only and is_pull_request_event():
            raise ConfigError("PR regression validation requires omniflow run --auto for trusted Git snapshot verification")
        client, branch_id = _client_and_branch(config)
        fail_on_new_only = fail_on_new_only and bool(branch_id or args.history_in or args.fail_on_new_only is True)
        report, exit_code = run_content_validation(
            client=client,
            model_id=_required(config.omni.model_id, "omni.model_id"),
            branch_id=branch_id,
            user_id=config.omni.user_id,
            include_personal_folders=config.omni.include_personal_folders,
            labels=labels,
            history_in=args.history_in or private / "history.json",
            history_out=history_out,
            report_out=private / "report.json",
            fail_on_new_only=fail_on_new_only,
            force_full_validation=config.content_validation.force_full_validation,
            max_samples=config.security.max_report_samples,
            redact_document_names=config.security.redact_document_names,
            allow_raw_response_output=args.unsafe_raw_output,
        )
        report.update(exit_code=exit_code, policy_decision="fail" if exit_code else "pass",
                      validation_complete=report.get("validation_complete", True))
        write_public_reports(report, output_dir=output_dir, formats=config.reporting.formats,
                             redaction_level=config.security.redaction_level)
        if args.report_out:
            write_public_json(args.report_out, report, redaction_level=config.security.redaction_level)
    except Exception as exc:
        issue = exc.report_issue() if isinstance(exc, (ContentEvidenceError, OmniResponseError)) else {
            "validator": "content", "type": "validation_execution_failed", "severity": "error",
            "message": safe_exception_message(exc), "comparison_status": "unavailable",
            "content_health": "unknown", "regression_status": "unknown",
        }
        issue.update(exception_diagnostic(exc))
        failure = {"validator": "content", "exit_code": exc.exit_code if isinstance(exc, OmniFlowError) else 6,
                              "policy_decision": "fail", "validation_complete": False,
                              "requested_validation_depth": (
                                  "full" if config.content_validation.force_full_validation else "api_default"
                              ),
                              "issues": [issue]}
        issue["requested_validation_depth"] = failure["requested_validation_depth"]
        # A rejected filesystem target must never be retried by a fallback writer.
        if not isinstance(exc, SecurityPolicyError):
            write_public_reports(failure, output_dir=output_dir, formats=config.reporting.formats,
                                 redaction_level=config.security.redaction_level)
            if args.report_out:
                write_public_json(args.report_out, failure, redaction_level=config.security.redaction_level)
        raise
    finally:
        if not config.security.retain_restricted_artifacts:
            _purge_restricted_path(private)
    print(
        "Content validator results: "
        f"total={report['total_issues']} new={report['new_issues']} "
        f"existing={report['existing_issues']} resolved={report['resolved_issues']}"
    )
    return exit_code


@diagnostic_session()
def cmd_model_validate(args: argparse.Namespace) -> int:
    config = _override_config(load_config(args.config), args)
    output_dir = Path(config.reporting.output_dir)
    _validate_run_output_layout(output_dir)
    fail_on_warnings = (
        config.model_validation.fail_on_warnings if args.fail_on_warnings is None else args.fail_on_warnings
    )
    try:
        if config.model_validation.fail_on_new_only and is_pull_request_event():
            raise ConfigError("PR regression validation requires omniflow run --auto for trusted Git snapshot verification")
        client, branch_id = _client_and_branch(config)
        report, exit_code = run_model_validation(
            client=client,
            model_id=_required(config.omni.model_id, "omni.model_id"),
            branch_id=branch_id,
            fail_on_warnings=fail_on_warnings,
            fail_on_new_only=config.model_validation.fail_on_new_only,
        )
        report.update(exit_code=exit_code, policy_decision="fail" if exit_code else "pass",
                      validation_complete=report.get("validation_complete", True))
        write_public_reports(report, output_dir=output_dir, formats=config.reporting.formats,
                             redaction_level=config.security.redaction_level)
        write_public_json(output_dir / "model-report.json", report, redaction_level=config.security.redaction_level)
    except Exception as exc:
        issue = exc.report_issue(validator="model") if isinstance(exc, OmniResponseError) else {
            "validator": "model", "type": "validation_execution_failed", "severity": "error",
            "message": safe_exception_message(exc),
        }
        issue.update(exception_diagnostic(exc))
        failure = {"validator": "model", "exit_code": exc.exit_code if isinstance(exc, OmniFlowError) else 6,
                   "policy_decision": "fail", "validation_complete": False, "issues": [issue]}
        if not isinstance(exc, SecurityPolicyError):
            write_public_reports(failure, output_dir=output_dir, formats=config.reporting.formats,
                                 redaction_level=config.security.redaction_level)
            write_public_json(output_dir / "model-report.json", failure, redaction_level=config.security.redaction_level)
        raise
    print(f"Model validation: errors={report['summary']['errors']} warnings={report['summary']['warnings']}")
    return exit_code


def cmd_yaml_pull(args: argparse.Namespace) -> int:
    config = _override_config(load_config(args.config), args)
    client, branch_id = _client_and_branch(config)
    out = args.out or str(Path(config.reporting.output_dir) / "yaml")
    manifest = pull_yaml(
        client=client,
        model_id=_required(config.omni.model_id, "omni.model_id"),
        branch_id=branch_id,
        output_dir=out,
        mode=args.mode,
        fully_resolved=args.fully_resolved,
    )
    print(f"Pulled {len(manifest['files'])} YAML file(s) to {out}")
    return 0


def cmd_exposures_pull(args: argparse.Namespace) -> int:
    config = _override_config(load_config(args.config), args)
    client, branch_id = _client_and_branch(config)
    report, exit_code = run_dbt_exposure_enrichment(
        client=client,
        model_id=_required(config.omni.model_id, "omni.model_id"),
        branch_id=branch_id,
        settings=config.dbt_exposures,
    )
    output_path = Path(args.out or Path(config.reporting.output_dir) / "dbt-exposures.json")
    write_json_report(output_path, report)
    print(f"Pulled {report['summary']['total_exposures']} dbt exposure record(s) to {output_path}")
    return exit_code


@diagnostic_session()
def cmd_dbt_sync(args: argparse.Namespace) -> int:
    try:
        config = _override_config(load_config(args.config), args)
    except OmniFlowError as exc:
        _write_unconfigured_failure_artifacts(output_dir=Path(".omniflow"), exc=exc)
        raise
    output_dir = Path(config.reporting.output_dir)
    _validate_run_output_layout(output_dir)
    if not config.security.retain_restricted_artifacts:
        _purge_restricted_path(restricted_dir(output_dir))

    try:
        if not config.dbt_sync.enabled:
            raise ConfigError(
                "dbt synchronization is disabled. Set deployment.dbt_sync.enabled: true in trusted policy."
            )
        contexts = discover_deployment_contexts(
            auto=args.auto,
            base_url=config.omni.base_url,
            model_id=config.omni.model_id,
            model_path=getattr(args, "model_path", None),
            branch_name=config.omni.branch_name,
            branch_id=config.omni.branch_id,
            base_branch=getattr(args, "base_branch", None),
        )
        deployment_branch = validate_dbt_sync_environment(contexts)
        sync_api_key = require_sync_api_key()
    except OmniFlowError as exc:
        _write_setup_failure_artifacts(config=config, output_dir=output_dir, exc=exc)
        raise

    refresh_mode = args.refresh_mode or config.dbt_sync.refresh_mode
    prepared_contexts: list[dict[str, Any]] = []
    try:
        connection_branches: dict[tuple[str, str], str | None] = {}
        for context in contexts:
            client, branch_id = _client_and_branch_for_context(
                context,
                config.omni.timeout,
                api_key=sync_api_key,
            )
            metadata = client.get_model_metadata(context.model_id)
            connection_key = (context.base_url, metadata["connection_id"])
            if connection_key in connection_branches and connection_branches[connection_key] != branch_id:
                raise ConfigError(
                    "Models on the same Omni connection resolved to different refresh branches; "
                    "no schema refresh was started"
                )
            connection_branches[connection_key] = branch_id
            prepared_contexts.append(
                {
                    "context": context,
                    "client": client,
                    "branch_id": branch_id,
                    "connection_key": connection_key,
                    "connection_id": metadata["connection_id"],
                }
            )
        for connection_key in connection_branches:
            connection_contexts = [
                item for item in prepared_contexts if item["connection_key"] == connection_key
            ]
            configured_model_ids = {item["context"].model_id for item in connection_contexts}
            affected_model_ids = set(
                connection_contexts[0]["client"].list_refresh_affected_model_ids(
                    connection_contexts[0]["connection_id"]
                )
            )
            if configured_model_ids != affected_model_ids:
                raise ConfigError(
                    "dbt synchronization requires every shared model affected by an Omni connection refresh "
                    "to be registered in trusted .omni/flow.json; no schema refresh was started"
                )
        if config.semantic_lint.enabled or config.contracts.enabled:
            for prepared in prepared_contexts:
                context = prepared["context"]
                snapshot_dir = restricted_dir(output_dir) / _safe_context_dir(context) / "pre-sync-yaml"
                _validate_context_output_layout(snapshot_dir)
                analysis_dir = _pull_analysis_yaml(
                    client=prepared["client"],
                    model_id=context.model_id,
                    branch_id=prepared["branch_id"],
                    output_dir=snapshot_dir,
                )
                prepared["comparison_base_yaml_dir"] = analysis_dir
    except OmniFlowError as exc:
        if not config.security.retain_restricted_artifacts:
            _purge_restricted_path(restricted_dir(output_dir))
        _write_setup_failure_artifacts(config=config, output_dir=output_dir, exc=exc)
        raise

    model_reports: list[dict[str, Any]] = []
    sync_reports: list[dict[str, Any]] = []
    all_issues: list[dict[str, Any]] = []
    exit_code = ExitCodes.SUCCESS
    refresh_outcomes: dict[tuple[str, str], tuple[dict[str, Any], int]] = {}
    try:
        for prepared in prepared_contexts:
            context = prepared["context"]
            client = prepared["client"]
            branch_id = prepared["branch_id"]
            connection_key = prepared["connection_key"]
            connection_id = prepared["connection_id"]
            context_output_dir = restricted_dir(output_dir) / _safe_context_dir(context)
            validation_output_dir = context_output_dir / "post-sync-validation"
            _validate_context_output_layout(context_output_dir)
            _validate_context_output_layout(validation_output_dir)
            if connection_key not in refresh_outcomes:
                affected_model_ids = [
                    item["context"].model_id
                    for item in prepared_contexts
                    if item["connection_key"] == connection_key
                ]
                try:
                    sync_report, sync_exit = run_dbt_sync(
                        client=client,
                        model_id=context.model_id,
                        branch_id=branch_id,
                        refresh_mode=refresh_mode,
                        poll_interval_seconds=config.dbt_sync.poll_interval_seconds,
                        timeout_seconds=config.dbt_sync.timeout_seconds,
                    )
                except OmniFlowError as exc:
                    sync_report = _dbt_sync_failure_report(
                        context=context,
                        refresh_mode=refresh_mode,
                        exc=exc,
                    )
                    sync_exit = exc.exit_code
                sync_report["connection_id"] = connection_id
                sync_report["affected_model_ids"] = affected_model_ids
                refresh_outcomes[connection_key] = (sync_report, sync_exit)
                sync_reports.append(sync_report)
                all_issues.extend(sync_report.get("issues", []))
            else:
                sync_report, sync_exit = refresh_outcomes[connection_key]

            validation_report = None
            validation_exit = ExitCodes.SUCCESS
            if sync_exit == ExitCodes.SUCCESS and config.dbt_sync.post_sync_validation:
                try:
                    validation_report, validation_exit = _run_context(
                        config=config,
                        context=context,
                        output_dir=validation_output_dir,
                        api_key=sync_api_key,
                        comparison_base_yaml_dir=prepared.get("comparison_base_yaml_dir"),
                        strict_health=True,
                    )
                except Exception as exc:
                    validation_report, validation_exit = _write_context_failure_artifacts(
                        config=config,
                        context=context,
                        output_dir=validation_output_dir,
                        exc=exc,
                    )

            context_issues = list(sync_report.get("issues", []))
            if validation_report:
                context_issues.extend(validation_report.get("issues", []))
            context_exit = max(sync_exit, validation_exit)
            context_report = {
                "model_id": context.model_id,
                "model_path": context.model_path,
                "base_branch": context.base_branch,
                "connection_id": connection_id,
                "refresh": sync_report,
                "post_sync_validation": validation_report,
                "post_sync_validation_status": (
                    "not_run"
                    if not config.dbt_sync.post_sync_validation or sync_exit != ExitCodes.SUCCESS
                    else "failed"
                    if validation_exit
                    else "passed"
                ),
                "issues": context_issues,
                "exit_code": context_exit,
                "exit_code_reason": _exit_code_reason(context_exit),
            }
            model_reports.append(context_report)
            if validation_report:
                all_issues.extend(validation_report.get("issues", []))
            exit_code = max(exit_code, context_exit)
    finally:
        if not config.security.retain_restricted_artifacts:
            _purge_restricted_path(restricted_dir(output_dir))

    summary = _summarize(all_issues)
    summary.update(
        {
            "models_requested": len(contexts),
            "refreshes_completed": sum(1 for report in sync_reports if report.get("status") == "completed"),
            "refreshes_failed": sum(1 for report in sync_reports if report.get("status") != "completed"),
        }
    )
    report = {
        "tool": "omniflow",
        "tool_version": __version__,
        "operation": "dbt_sync",
        "generated_at": utc_now_iso(),
        "git_sha": current_sha(),
        "git_branch": deployment_branch,
        "pr_number": pr_number(),
        "event_type": event_name(),
        "runner": _runner_metadata(),
        "models": [_context_dict(context) for context in contexts],
        "config_hash": config.hash,
        "refresh_mode": refresh_mode,
        "post_sync_validation_enabled": config.dbt_sync.post_sync_validation,
        "raw_query_results_stored": False,
        "summary": summary,
        "issues": all_issues,
        "model_reports": model_reports,
        "policy_decision": "fail" if exit_code else "pass",
        "exit_code": exit_code,
        "exit_code_reason": _exit_code_reason(exit_code),
    }
    public_report = write_public_reports(
        report,
        output_dir=output_dir,
        formats=config.reporting.formats,
        redaction_level=config.security.redaction_level,
    )
    sync_artifact = {
        "tool": "omniflow",
        "tool_version": __version__,
        "operation": "dbt_sync",
        "generated_at": utc_now_iso(),
        "models": sync_reports,
        "raw_query_results_stored": False,
        "policy_decision": report["policy_decision"],
        "exit_code": exit_code,
    }
    write_public_json(output_dir / "dbt-sync.json", sync_artifact, redaction_level=config.security.redaction_level)
    write_public_json(
        public_dir(output_dir) / "dbt-sync.json",
        sync_artifact,
        redaction_level=config.security.redaction_level,
    )
    evidence = {
        "tool": "omniflow",
        "tool_version": __version__,
        "operation": "dbt_sync",
        "config_hash": config.hash,
        "git_sha": current_sha(),
        "git_branch": deployment_branch,
        "pr_number": pr_number(),
        "event_type": event_name(),
        "runner": _runner_metadata(),
        "models": [_context_dict(context) for context in contexts],
        "refresh_mode": refresh_mode,
        "refresh_statuses": [report.get("status") for report in sync_reports],
        "validation_status": "failed" if exit_code else "passed",
        "policy_decision": "fail" if exit_code else "pass",
        "raw_query_results_stored": False,
        "exit_code": exit_code,
        "exit_code_reason": _exit_code_reason(exit_code),
        "timestamp": utc_now_iso(),
    }
    write_public_json(output_dir / "evidence.json", evidence, redaction_level=config.security.redaction_level)
    write_public_json(
        public_dir(output_dir) / "evidence.json",
        evidence,
        redaction_level=config.security.redaction_level,
    )
    write_artifact_manifest(
        output_dir=output_dir,
        restricted_artifacts_enabled=config.security.retain_restricted_artifacts,
        redaction_level=config.security.redaction_level,
    )
    _emit_github_annotations(public_report.get("issues", []), limit=config.security.max_report_samples)
    print(
        "OmniFlow dbt sync complete: "
        f"models={len(contexts)} refreshed={summary['refreshes_completed']} exit_code={exit_code}"
    )
    return exit_code


def cmd_diff(args: argparse.Namespace) -> int:
    base_graph = load_yaml_graph(args.base)
    head_graph = load_yaml_graph(args.head)
    report = diff_graphs(base_graph, head_graph)
    if args.report_out:
        write_public_json(args.report_out, report, redaction_level="strict")
    else:
        print(json.dumps(public_diagnostic(report), sort_keys=True))
    return 1 if report["risk_level"] in {"breaking", "security_sensitive"} else 0


def cmd_report(args: argparse.Namespace) -> int:
    import json

    report = json.loads(Path(args.input).read_text(encoding="utf-8"))
    write_public_reports(report, output_dir=args.output_dir, formats=args.format, redaction_level="strict")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = _override_config(load_config(args.config), args)
    if args.auto:
        contexts = discover_contexts(
            auto=True,
            base_url=config.omni.base_url,
            model_id=config.omni.model_id,
            model_path=getattr(args, "model_path", None),
            branch_name=config.omni.branch_name,
            branch_id=config.omni.branch_id,
        )
        if not contexts:
            raise ConfigError("Missing .omni/flow.json model context")
    else:
        contexts = [
            ModelContext(
                base_url=_required(config.omni.base_url, "--base-url or OMNI_BASE_URL"),
                model_id=_required(config.omni.model_id, "--model-id or OMNI_MODEL_ID"),
                model_path=getattr(args, "model_path", None) or "",
                branch_name=config.omni.branch_name,
                branch_id=config.omni.branch_id,
            )
        ]
    issues = []
    warnings = []
    for context in contexts:
        client, branch_id = _client_and_branch_for_context(context, config.omni.timeout)
        client.get_model_yaml(
            context.model_id,
            branch_id=branch_id,
            include_checksums=False,
        )
        try:
            git_config = client.get_git_configuration(context.model_id)
        except OmniAuthError as exc:
            warnings.append(safe_exception_message(exc))
        else:
            issues.extend(_git_configuration_issues(context, git_config))
    if issues:
        raise ConfigError("Omni Git configuration mismatch: " + "; ".join(issues),
                          reason_code="git_configuration_mismatch")
    for warning in warnings:
        print(f"omniflow doctor partial: Git metadata verification unavailable. {warning}", file=sys.stderr)
    if warnings:
        print(f"omniflow doctor incomplete: model access succeeded for {len(contexts)} context(s); "
              "Git configuration is not fully verified.")
    else:
        print(f"omniflow doctor passed: {len(contexts)} model context(s) ready")
    if any(context.environment for context in contexts):
        print("Target access/settings verified only; run PR validation to establish candidate coverage.")
    return 0


@diagnostic_session()
def cmd_repair_ai(args: argparse.Namespace) -> int:
    try:
        config = _override_config(load_config(args.config), args)
    except OmniFlowError as exc:
        _write_repair_setup_failure_artifacts(output_dir=Path(".omniflow"), exc=exc)
        raise
    output_dir = Path(config.reporting.output_dir)
    _validate_run_output_layout(output_dir)
    try:
        if not args.auto:
            raise ConfigError("AI repair requires --auto trusted pull request discovery")
        validate_ai_repair_policy(config)
        event = load_repair_event()
        github_token = os.getenv("OMNIFLOW_GITHUB_TOKEN")
        if not github_token:
            raise ConfigError("OMNIFLOW_GITHUB_TOKEN is required for AI repair pull request safeguards")
        contexts = discover_contexts(
            auto=True,
            base_url=config.omni.base_url,
            model_id=config.omni.model_id,
            model_path=getattr(args, "model_path", None),
            branch_name=config.omni.branch_name,
            branch_id=config.omni.branch_id,
            allow_skip=False,
        )
        if len(contexts) != 1:
            raise ConfigError("AI repair requires exactly one unambiguous Omni model context")
        context = contexts[0]
        if context.environment:
            raise SecurityPolicyError("Version 2 environment targets are validation-only; AI repair is not supported")
        repair_key = require_repair_api_key()
        client, branch_id = _client_and_branch_for_context(context, config.omni.timeout, api_key=repair_key)
        if not branch_id:
            raise SecurityPolicyError("AI repair cannot run without an existing Omni development branch")
        guard = GitHubRepairAttemptGuard(token=github_token)
        validation_output = restricted_dir(output_dir) / _safe_context_dir(context) / "repair-validation"
        _validate_context_output_layout(validation_output)
        outcome = run_ai_repair(
            config=config,
            context=context,
            event=event,
            client=client,
            guard=guard,
            validation_runner=lambda: _run_context(
                config=config,
                context=context,
                output_dir=validation_output,
                api_key=repair_key,
                strict_health=True,
            ),
        )
        write_repair_artifacts(
            outcome.report,
            output_dir=output_dir,
            redaction_level=config.security.redaction_level,
        )
        write_artifact_manifest(
            output_dir=output_dir,
            restricted_artifacts_enabled=config.security.retain_restricted_artifacts,
            redaction_level=config.security.redaction_level,
        )
        print(f"OmniFlow AI repair: status={outcome.report['status']} exit_code={outcome.exit_code}")
        return outcome.exit_code
    except OmniFlowError as exc:
        report = _repair_failure_report(config=config, exc=exc)
        write_repair_artifacts(
            report,
            output_dir=output_dir,
            redaction_level=config.security.redaction_level,
        )
        write_artifact_manifest(
            output_dir=output_dir,
            restricted_artifacts_enabled=config.security.retain_restricted_artifacts,
            redaction_level=config.security.redaction_level,
        )
        raise
    finally:
        if not config.security.retain_restricted_artifacts:
            _purge_restricted_path(restricted_dir(output_dir))

        write_artifact_manifest(
            output_dir=output_dir,
            restricted_artifacts_enabled=config.security.retain_restricted_artifacts,
            redaction_level=config.security.redaction_level,
        )


def cmd_query_propose(args: argparse.Namespace) -> int:
    """Local opt-in only: proposal output is not an approved runnable contract."""
    from .query_transport import propose_query

    if os.getenv("GITHUB_ACTIONS") == "true" or is_pull_request_event():
        raise SecurityPolicyError("AI query proposals are local, human-reviewed preparation only; not a CI step.")
    target = Path(args.output)
    validate_repo_output_path(target)
    if target.exists() or target.is_symlink():
        raise ConfigError("Choose a new private proposal file; existing files are never overwritten.")
    config = _override_config(load_config(args.config), args)
    client, branch_id = _client_and_branch(config)
    proposal = propose_query(
        client, _required(config.omni.model_id, "model_id"),
        topic=args.topic, prompt=args.prompt, branch_id=branch_id,
    )
    # Create exclusively after the network call as well, preventing races.
    validate_repo_output_path(target)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(proposal, handle, indent=2, allow_nan=False)
        handle.write("\n")
    print("Private query proposal saved. Review and qualify it before adding a contract; no query was executed.")
    return 0


def _client_and_branch(config):
    context = ModelContext(
        base_url=_required(config.omni.base_url, "base_url"),
        model_id=_required(config.omni.model_id, "model_id"),
        model_path="",
        branch_name=config.omni.branch_name,
        branch_id=config.omni.branch_id,
    )
    return _client_and_branch_for_context(context, config.omni.timeout)


def _client_and_branch_for_context(context: ModelContext, timeout: int, *, api_key: str | None = None):
    if context.environment and os.getenv("OMNIFLOW_ENVIRONMENT") != context.environment:
        raise SecurityPolicyError(
            f"Selected target requires OMNIFLOW_ENVIRONMENT={context.environment}. "
            "Supply only that environment's dedicated validation credential from the trusted workflow."
        )
    client = OmniClient(
        base_url=context.base_url,
        api_key=api_key or require_api_key(),
        timeout=timeout,
    )
    if context.environment:
        _verify_target_git_configuration(context, client.get_git_configuration(context.model_id))
        matching = []
        if context.branch_name:
            matching = [record for record in client.list_models(
                model_kind="BRANCH", base_model_id=context.model_id, name=context.branch_name,
            ) if record.get("modelKind") == "BRANCH" and record.get("baseModelId") == context.model_id
                and record.get("name") == context.branch_name]
            if len(matching) != 1 or not isinstance(matching[0].get("id"), str):
                reason = "branch_not_found" if not matching else "branch_ambiguous" if len(matching) > 1 else "branch_identity_invalid"
                raise ConfigError(
                    "Selected target must have exactly one corresponding Omni candidate branch. "
                    "Enable Always create branches on the follower and rerun after Git synchronization; "
                    "OmniFlow will not fall back to the production base model.", reason_code=reason,
                )
        branch_id = validate_path_segment(matching[0]["id"], name="branch_id") if matching else None
        if context.branch_id and context.branch_id != branch_id:
            raise SecurityPolicyError("Candidate branch ID no longer matches the selected target branch")
    else:
        branch_id = context.branch_id or client.resolve_branch_id(context.model_id, context.branch_name)
    if context.branch_name and not branch_id:
        raise ConfigError(
            f"Could not resolve Omni branch '{context.branch_name}' for model {context.model_id}. "
            "Verify the Omni PR branch exists and the API key can list model branches.", reason_code="branch_not_found",
        )
    context.branch_id = branch_id
    return client, branch_id


def _override_config(config, args: argparse.Namespace):
    for attr in ("base_url", "model_id", "branch_id", "branch_name", "user_id"):
        value = getattr(args, attr, None)
        if value:
            setattr(config.omni, attr, value)
    if getattr(args, "include_personal_folders", None) is not None:
        config.omni.include_personal_folders = args.include_personal_folders
    return config


def _labels_from_args(args: argparse.Namespace) -> list[str]:
    values = [*(getattr(args, "labels", []) or []), *(getattr(args, "label", []) or [])]
    labels: list[str] = []
    for value in values:
        for name in value.split(","):
            stripped = name.strip()
            if stripped and stripped not in labels:
                labels.append(stripped)
    return labels


def _base_report(
    config,
    context: ModelContext,
    branch_id: str | None,
    exit_code: int,
    issues: list[dict[str, Any]],
    summary: dict[str, Any],
) -> dict[str, Any]:
    return {
        "tool": "omniflow",
        "tool_version": __version__,
        "generated_at": utc_now_iso(),
        "git_sha": current_sha(),
        "git_branch": current_branch(),
        "pr_number": pr_number(),
        "event_type": event_name(),
        "runner": _runner_metadata(),
        "omni_base_url": context.base_url,
        "model_id": context.model_id,
        "model_path": context.model_path,
        "branch_id": branch_id,
        "branch_name": context.branch_name,
        "environment": context.environment,
        "target_branch": context.base_branch,
        "candidate_verification": context.candidate_verification or {"status": "not_applicable" if not context.environment else "unverified"},
        "config_hash": config.hash,
        "summary": summary,
        "issues": issues,
        "policy_decision": "fail" if exit_code else "pass",
        "exit_code": exit_code,
        "exit_code_reason": _exit_code_reason(exit_code),
    }


def _aggregate_report(config, contexts, exit_code, issues, summary, reports):
    complete = bool(reports) and all(report.get("validation_complete") is True for report in reports)
    advisory = not exit_code and any(report.get("policy_decision") == "advisory" for report in reports)
    return {
        "tool": "omniflow",
        "tool_version": __version__,
        "generated_at": utc_now_iso(),
        "git_sha": current_sha(),
        "git_branch": current_branch(),
        "pr_number": pr_number(),
        "event_type": event_name(),
        "runner": _runner_metadata(),
        "models": [_context_dict(context) for context in contexts],
        "config_hash": config.hash,
        "summary": summary,
        "issues": issues,
        "model_reports": reports,
        "validation_complete": complete,
        "decision_status": "complete" if complete else "incomplete",
        "enforcement_mode": config.validation.mode,
        "policy_decision": "fail" if exit_code else "advisory" if advisory else "pass",
        "exit_code": exit_code,
        "exit_code_reason": _exit_code_reason(exit_code),
    }


def _summarize(issues: list[dict[str, Any]]) -> dict[str, Any]:
    active = [issue for issue in issues if issue.get("active", True)]
    risk_order = ["info", "warning", "governance_sensitive", "security_sensitive", "breaking"]
    risks = [issue.get("risk", "info") for issue in active]
    return {
        "total_issues": len(active),
        "errors": sum(1 for issue in active if issue.get("severity") == "error"),
        "warnings": sum(1 for issue in active if issue.get("severity") in {"warning", "warn"}),
        "new_issues": sum(1 for issue in issues if issue.get("state") == "new"),
        "existing_issues": sum(1 for issue in issues if issue.get("state") == "existing"),
        "resolved_issues": sum(1 for issue in issues if issue.get("state") == "resolved"),
        "improved_issues": sum(1 for issue in issues if issue.get("state") == "improved"),
        "blocking_issues": sum(is_blocking(issue) for issue in issues),
        "risk_level": max(
            risks or ["info"],
            key=lambda risk: risk_order.index(risk) if risk in risk_order else 0,
        ),
    }


def _exit_code_reason(exit_code: int) -> str:
    return {
        ExitCodes.SUCCESS: "success",
        ExitCodes.VALIDATION_FAILED: "validation failed",
        ExitCodes.CONFIGURATION_ERROR: "configuration error",
        ExitCodes.AUTHORIZATION_ERROR: "authentication or authorization error",
        ExitCodes.OMNI_API_ERROR: "Omni API error",
        ExitCodes.SECURITY_POLICY_VIOLATION: "security policy violation",
        ExitCodes.INTERNAL_ERROR: "internal tool error",
    }.get(exit_code, "unknown error")


def _required(value: Any, name: str) -> Any:
    if not value:
        raise ConfigError(f"Missing required value: {name}")
    return value


def _safe_context_dir(context: ModelContext) -> str:
    return context.model_id.replace("/", "_")


def _context_dict(context: ModelContext) -> dict[str, Any]:
    return {
        "base_url": context.base_url,
        "model_id": context.model_id,
        "model_path": context.model_path,
        "branch_name": context.branch_name,
        "branch_id": context.branch_id,
        "base_branch": context.base_branch,
        "git_provider": context.git_provider,
        "web_url": context.web_url,
        "environment": context.environment,
        "target_branch": context.base_branch,
        "candidate_verification": context.candidate_verification or {"status": "not_applicable" if not context.environment else "unverified"},
    }


def _verify_target_git_configuration(context: ModelContext, git_config: dict[str, Any]) -> None:
    """Version 2 cannot silently skip missing or unauthorized identity evidence."""
    expected = {
        "baseBranch": context.base_branch, "modelPath": context.model_path,
        "gitServiceProvider": "github", "gitFollower": context.git_follower,
    }
    for key, value in expected.items():
        actual = git_config.get(key)
        if key == "modelPath" and context.model_path == "." and actual in {None, ""}:
            actual = "."
        if actual != value or (key == "gitFollower" and type(actual) is not bool):
            raise ConfigError(f"Selected environment's Omni Git configuration does not match trusted {key}",
                              reason_code="git_configuration_mismatch")
    if context.git_follower and git_config.get("branchPerPullRequest") is not True:
        raise ConfigError("Follower validation requires Always create branches (branchPerPullRequest=true)",
                          reason_code="follower_branch_creation_disabled")
    expected_url = str(context.web_url).removesuffix(".git").rstrip("/")
    actual_url = git_config.get("webUrl")
    clone_url = git_config.get("cloneUrl")
    urls = [value.removesuffix(".git").rstrip("/") for value in (actual_url, clone_url) if isinstance(value, str)]
    # SSH clone URLs are documented; compare repository identity without executing them.
    for value in (clone_url,):
        if isinstance(value, str) and value.startswith("git@") and ":" in value:
            host, path = value[4:].split(":", 1)
            urls.append(f"https://{host}/{path}".removesuffix(".git").rstrip("/"))
    if expected_url not in urls:
        raise ConfigError("Selected environment's Omni Git repository does not match trusted web_url",
                          reason_code="git_configuration_mismatch")
    if is_pull_request_event():
        repository = os.getenv("GITHUB_REPOSITORY")
        server = os.getenv("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
        if not repository or expected_url != f"{server}/{repository}":
            raise ConfigError("Trusted target repository does not match the GitHub PR repository",
                              reason_code="git_configuration_mismatch")


def _git_configuration_issues(context: ModelContext, git_config: dict[str, Any]) -> list[str]:
    checks = [
        ("model_path", context.model_path, git_config.get("modelPath")),
        ("base_branch", context.base_branch, git_config.get("baseBranch")),
        ("git_provider", context.git_provider, git_config.get("gitServiceProvider")),
        ("web_url", context.web_url, git_config.get("webUrl")),
    ]
    issues = []
    for label, expected, actual in checks:
        if not expected:
            continue
        normalize = (lambda value: str(value).rstrip("/")) if label == "model_path" else (
            str if label == "base_branch" else _normalize_git_config_value
        )
        if normalize(expected) != normalize(actual):
            issues.append(f"{label} expected {expected!r} but Omni reports {actual!r}")
    return issues


def _normalize_git_config_value(value: Any) -> str:
    return str(value).strip().strip("/").lower()


def _runner_metadata() -> dict[str, Any]:
    return {
        "os": platform.platform(),
        "python": platform.python_version(),
        "github_actions": os.getenv("GITHUB_ACTIONS") == "true",
    }


def _emit_github_annotations(issues: list[dict[str, Any]], *, limit: int) -> None:
    if os.getenv("GITHUB_ACTIONS") != "true":
        return
    for line in public_annotation_lines(issues, limit=limit):
        print(line)


def _emit_hold_output(issues: list[dict[str, Any]], *, settings) -> None:
    """Publish the hold decision so a protected workflow can label the pull request."""
    if not settings.enabled:
        return
    output_path = os.getenv("GITHUB_OUTPUT")
    if not output_path:
        return
    triggered = hold_triggered(issues)
    try:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(f"hold_triggered={'true' if triggered else 'false'}\n")
            handle.write(f"hold_pending_label={settings.pending_label}\n")
    except OSError:
        print("Could not write the OmniFlow hold decision output; path and details withheld.", file=sys.stderr)


def _validate_run_output_layout(output_dir: Path) -> None:
    report_names = (
        "report.json",
        "report.md",
        "report.sarif",
        "junit.xml",
        "evidence.json",
        "dbt-sync.json",
        "repair.json",
        "repair.md",
    )
    for path in (output_dir, public_dir(output_dir), restricted_dir(output_dir), output_dir / "repository"):
        validate_repo_output_path(path)
    for name in report_names:
        validate_repo_output_path(output_dir / name)
        validate_repo_output_path(public_dir(output_dir) / name)
    validate_repo_output_path(output_dir / "artifact-manifest.json")
    for name in (*REPOSITORY_REPORT_NAMES, "status.json"):
        path = output_dir / "repository" / name
        validate_repo_output_path(path)
        if path.exists() and not path.is_file():
            raise SecurityPolicyError("Repository diagnostic output must be a regular file")
    # Routing, repair, and early standalone guards may never reach the common
    # report writer. Preflight every expected path before clearing old detail.
    clear_repository_reports(output_dir)


def _validate_context_output_layout(output_dir: Path) -> None:
    validate_repo_output_path(output_dir)
    for name in (
        "report.json",
        "history.json",
        "content-report.json",
        "semantic-diff.json",
        "dependencies.json",
        "contract-impact.json",
        "dbt-exposures.json",
        "dbt-sync.json",
        "breaking-change-hold.json",
    ):
        validate_repo_output_path(output_dir / name)
    for name in ("yaml-base", "yaml-head"):
        validate_repo_output_path(output_dir / name)
        validate_repo_output_path(output_dir / name / "manifest.json")


def _purge_restricted_path(path: Path) -> None:
    validate_repo_output_path(path)
    if path.is_symlink():
        raise SecurityPolicyError("Restricted artifact paths must not be symbolic links")
    if path.exists():
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def _write_unconfigured_failure_artifacts(*, output_dir: Path, exc: OmniFlowError) -> None:
    _validate_run_output_layout(output_dir)
    _purge_restricted_path(restricted_dir(output_dir))
    issue = {"severity": "error", "validator": "setup", "message": redact(str(exc)), **exception_diagnostic(exc)}
    if getattr(exc, "reason_code", None) == "missing_policy_config":
        issue["reason_code"] = "missing_policy_config"
    report = {
        "tool": "omniflow",
        "tool_version": __version__,
        "generated_at": utc_now_iso(),
        "git_sha": current_sha(),
        "git_branch": current_branch(),
        "pr_number": pr_number(),
        "event_type": event_name(),
        "runner": _runner_metadata(),
        "config_hash": None,
        "summary": _summarize([issue]),
        "issues": [issue],
        "model_reports": [],
        "policy_decision": "fail",
        "exit_code": exc.exit_code,
        "exit_code_reason": _exit_code_reason(exc.exit_code),
    }
    write_public_reports(
        report,
        output_dir=output_dir,
        formats=DEFAULT_REPORT_FORMATS,
        redaction_level="standard",
    )
    evidence = {
        "tool": "omniflow",
        "tool_version": __version__,
        "config_hash": None,
        "git_sha": current_sha(),
        "git_branch": current_branch(),
        "pr_number": pr_number(),
        "event_type": event_name(),
        "runner": _runner_metadata(),
        "models": [],
        "validation_status": "failed",
        "policy_decision": "fail",
        "exit_code": exc.exit_code,
        "exit_code_reason": _exit_code_reason(exc.exit_code),
        "timestamp": utc_now_iso(),
    }
    write_public_json(output_dir / "evidence.json", evidence, redaction_level="standard")
    write_public_json(public_dir(output_dir) / "evidence.json", evidence, redaction_level="standard")
    write_artifact_manifest(
        output_dir=output_dir,
        restricted_artifacts_enabled=False,
        redaction_level="standard",
    )


def _repair_failure_report(*, config, exc: OmniFlowError) -> dict[str, Any]:
    return {
        "tool": "omniflow",
        "tool_version": __version__,
        "operation": "ai_repair_beta",
        "generated_at": utc_now_iso(),
        "status": "failed",
        "message": redact(str(exc)),
        "model_id": config.omni.model_id,
        "branch_id": config.omni.branch_id,
        "branch_name": config.omni.branch_name,
        "config_hash": config.hash,
        "query_execution_acknowledged": config.ai_repair.allow_query_execution,
        "raw_query_results_stored": False,
        "manual_review_required": False,
        "policy_decision": "fail",
        "exit_code": exc.exit_code,
        "exit_code_reason": _exit_code_reason(exc.exit_code),
        "issues": [{"validator": "ai_repair", "severity": "error", "message": redact(str(exc)), **exception_diagnostic(exc)}],
    }


def _dbt_sync_failure_report(
    *,
    context: ModelContext,
    refresh_mode: str,
    exc: OmniFlowError,
) -> dict[str, Any]:
    return {
        "tool": "omniflow",
        "tool_version": __version__,
        "operation": "dbt_sync",
        "generated_at": utc_now_iso(),
        "model_id": context.model_id,
        "branch_id": context.branch_id,
        "refresh_mode": refresh_mode,
        "status": "failed",
        "raw_query_results_stored": False,
        "issues": [{"validator": "dbt_sync", "severity": "error", "message": redact(str(exc)), **exception_diagnostic(exc)}],
        "summary": {"total_issues": 1, "errors": 1, "warnings": 0},
        "exit_code": exc.exit_code,
        "exit_code_reason": _exit_code_reason(exc.exit_code),
    }


def _write_repair_setup_failure_artifacts(*, output_dir: Path, exc: OmniFlowError) -> None:
    _validate_run_output_layout(output_dir)
    _purge_restricted_path(restricted_dir(output_dir))
    report = {
        "tool": "omniflow",
        "tool_version": __version__,
        "operation": "ai_repair_beta",
        "generated_at": utc_now_iso(),
        "status": "failed",
        "message": redact(str(exc)),
        "query_execution_acknowledged": False,
        "raw_query_results_stored": False,
        "manual_review_required": False,
        "policy_decision": "fail",
        "exit_code": exc.exit_code,
        "exit_code_reason": _exit_code_reason(exc.exit_code),
        "issues": [{"validator": "ai_repair", "severity": "error", "message": redact(str(exc)), **exception_diagnostic(exc)}],
    }
    write_repair_artifacts(report, output_dir=output_dir, redaction_level="standard")
    write_artifact_manifest(
        output_dir=output_dir,
        restricted_artifacts_enabled=False,
        redaction_level="standard",
    )


if __name__ == "__main__":
    raise SystemExit(main())
