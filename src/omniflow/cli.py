from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .artifacts import public_dir, restricted_dir, write_artifact_manifest, write_public_json, write_public_reports
from .config import (
    DEFAULT_REPORT_FORMATS,
    load_config,
    require_api_key,
    require_repair_api_key,
    require_sync_api_key,
)
from .contracts import evaluate_contracts
from .dbt_sync import run_dbt_sync, validate_dbt_sync_environment
from .diff.diff_engine import diff_graphs
from .diff.semantic_graph import build_graph
from .diff.yaml_loader import load_yaml_files
from .discovery import ModelContext, discover_contexts, discover_deployment_contexts
from .downstream import generate_downstream_dependencies
from .exceptions import ConfigError, ExitCodes, OmniAuthError, OmniFlowError, SecurityPolicyError
from .exposures import run_dbt_exposure_enrichment
from .git import current_branch, current_sha, event_name, pr_number
from .github.annotations import annotation_lines
from .github.repair_attempt import GitHubRepairAttemptGuard, load_repair_event
from .logging import configure_logging
from .omni_client import OmniClient
from .repair.orchestrator import run_ai_repair, validate_ai_repair_policy
from .repair.reporting import write_repair_artifacts
from .reporting.json_report import write_json_report
from .reporting.writer import write_reports
from .security import redact, validate_repo_output_path
from .timestamps import utc_now_iso
from .validators.content import run_content_validation
from .validators.model import run_model_validation
from .validators.yaml_lint import has_error, lint_graph
from .yaml_pull import pull_yaml


def main(argv: list[str] | None = None) -> int:
    if os.name == "posix":
        os.umask(0o077)
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging("DEBUG" if getattr(args, "verbose", False) else "INFO")
    try:
        return args.func(args)
    except OmniFlowError as exc:
        print(redact(str(exc)), file=sys.stderr)
        return exc.exit_code
    except Exception as exc:
        print(redact(f"Internal omniflow error: {exc}"), file=sys.stderr)
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
    parser.add_argument("--config", default=None)


def _add_common_omni_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url")
    parser.add_argument("--model-id")
    parser.add_argument("--model-path")
    parser.add_argument("--branch-id")
    parser.add_argument("--branch-name")
    parser.add_argument("--user-id")
    parser.add_argument("--include-personal-folders", action=argparse.BooleanOptionalAction, default=None)


def cmd_run(args: argparse.Namespace) -> int:
    try:
        config = _override_config(load_config(args.config), args)
    except OmniFlowError as exc:
        _write_unconfigured_failure_artifacts(output_dir=Path(".omniflow"), exc=exc)
        raise
    output_dir = Path(config.reporting.output_dir)
    _validate_run_output_layout(output_dir)
    if not config.security.retain_restricted_artifacts:
        _purge_restricted_path(restricted_dir(output_dir))
    if args.skip_reason:
        _write_skipped_artifacts(config=config, output_dir=output_dir, reason=args.skip_reason)
        print(f"OmniFlow skipped: {args.skip_reason}")
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
        )
    except OmniFlowError as exc:
        _write_setup_failure_artifacts(config=config, output_dir=output_dir, exc=exc)
        raise
    if not contexts:
        _write_skipped_artifacts(config=config, output_dir=output_dir)
        print("OmniFlow skipped: no Omni PR context or changed Omni model files detected")
        return 0
    all_reports = []
    all_issues: list[dict[str, Any]] = []
    exit_code = 0
    for context in contexts:
        context_output_dir = restricted_dir(output_dir) / _safe_context_dir(context)
        _validate_context_output_layout(context_output_dir)
        try:
            try:
                context_report, context_exit = _run_context(
                    config=config,
                    context=context,
                    output_dir=context_output_dir,
                )
            except OmniFlowError as exc:
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
        "validation_status": "failed" if exit_code else "passed",
        "policy_decision": "fail" if exit_code else "pass",
        "exit_code": exit_code,
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

    should_run = bool(contexts)
    reason = "" if should_run else "no Omni PR context or changed Omni model files detected"
    if not should_run:
        _write_skipped_artifacts(config=config, output_dir=output_dir, reason=reason)

    payload = {
        "should_run": should_run,
        "reason": reason,
        "model_count": len(contexts),
    }
    if args.format == "github":
        print(f"should_run={'true' if should_run else 'false'}")
        print(f"reason={reason}")
        print(f"model_count={len(contexts)}")
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


def _run_context(
    *,
    config,
    context: ModelContext,
    output_dir: Path,
    api_key: str | None = None,
    comparison_base_yaml_dir: Path | None = None,
) -> tuple[dict[str, Any], int]:
    client, branch_id = _client_and_branch_for_context(context, config.omni.timeout, api_key=api_key)
    all_issues: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    exit_code = 0

    if config.content_validation.enabled:
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
            fail_on_new_only=config.content_validation.fail_on_new_only,
            max_samples=config.security.max_report_samples,
            redact_document_names=config.security.redact_document_names,
            allow_raw_response_output=config.security.allow_raw_response_output,
        )
        reports.append(content_report)
        all_issues.extend(content_report.get("issues", []))
        exit_code = max(exit_code, content_exit)

    if config.model_validation.enabled:
        model_report, model_exit = run_model_validation(
            client=client,
            model_id=context.model_id,
            branch_id=branch_id,
            fail_on_warnings=config.model_validation.fail_on_warnings,
        )
        reports.append(model_report)
        all_issues.extend(model_report.get("issues", []))
        exit_code = max(exit_code, model_exit)

    diff_report = None
    head_graph = None
    if config.semantic_lint.enabled or config.contracts.enabled:
        base_yaml_dir = comparison_base_yaml_dir or output_dir / "yaml-base"
        head_yaml_dir = output_dir / "yaml-head"
        if comparison_base_yaml_dir is None:
            pull_yaml(
                client=client,
                model_id=context.model_id,
                branch_id=None,
                output_dir=base_yaml_dir,
            )
        pull_yaml(
            client=client,
            model_id=context.model_id,
            branch_id=branch_id,
            output_dir=head_yaml_dir,
        )
        base_graph = build_graph(load_yaml_files(base_yaml_dir))
        head_graph = build_graph(load_yaml_files(head_yaml_dir))
        diff_report = diff_graphs(base_graph, head_graph)
        write_json_report(output_dir / "semantic-diff.json", diff_report)

    if config.semantic_lint.enabled and head_graph is not None:
        lint_issues = lint_graph(
            head_graph,
            configured_rules=config.semantic_lint.rules,
            include_personal_folders=config.omni.include_personal_folders,
            diff_result=diff_report,
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

    if config.contracts.enabled and diff_report is not None:
        dependencies = generate_downstream_dependencies(
            client=client,
            model_id=context.model_id,
            branch_id=branch_id,
            diff_result=diff_report,
            user_id=config.omni.user_id,
            include_personal_folders=config.omni.include_personal_folders,
        )
        write_json_report(output_dir / "dependencies.json", dependencies)
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

    if config.dbt_exposures.enabled:
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

    summary = _summarize(all_issues)
    report = _base_report(config, context, branch_id, exit_code, all_issues, summary)
    report["check_reports"] = reports
    write_json_report(output_dir / "report.json", report)
    return report, exit_code


def _write_context_failure_artifacts(
    *,
    config,
    context: ModelContext,
    output_dir: Path,
    exc: OmniFlowError,
) -> tuple[dict[str, Any], int]:
    issue = {
        "severity": "error",
        "validator": "context",
        "message": redact(str(exc)),
    }
    summary = _summarize([issue])
    report = _base_report(config, context, context.branch_id, exc.exit_code, [issue], summary)
    report["check_reports"] = []
    report["exit_code_reason"] = _exit_code_reason(exc.exit_code)
    write_json_report(output_dir / "report.json", report)
    return report, exc.exit_code


def cmd_content_validate(args: argparse.Namespace) -> int:
    config = _override_config(load_config(args.config), args)
    client, branch_id = _client_and_branch(config)
    output_dir = Path(config.reporting.output_dir)
    labels = _labels_from_args(args) or config.content_validation.labels
    fail_on_new_only = (
        config.content_validation.fail_on_new_only if args.fail_on_new_only is None else args.fail_on_new_only
    )
    report, exit_code = run_content_validation(
        client=client,
        model_id=_required(config.omni.model_id, "omni.model_id"),
        branch_id=branch_id,
        user_id=config.omni.user_id,
        include_personal_folders=config.omni.include_personal_folders,
        labels=labels,
        history_in=args.history_in or output_dir / "history.json",
        history_out=args.history_out or output_dir / "history.json",
        report_out=args.report_out or output_dir / "report.json",
        fail_on_new_only=fail_on_new_only,
        max_samples=config.security.max_report_samples,
        redact_document_names=config.security.redact_document_names,
        allow_raw_response_output=args.unsafe_raw_output,
    )
    print(
        "Content validator results: "
        f"total={report['total_issues']} new={report['new_issues']} "
        f"existing={report['existing_issues']} resolved={report['resolved_issues']}"
    )
    return exit_code


def cmd_model_validate(args: argparse.Namespace) -> int:
    config = _override_config(load_config(args.config), args)
    client, branch_id = _client_and_branch(config)
    fail_on_warnings = (
        config.model_validation.fail_on_warnings if args.fail_on_warnings is None else args.fail_on_warnings
    )
    report, exit_code = run_model_validation(
        client=client,
        model_id=_required(config.omni.model_id, "omni.model_id"),
        branch_id=branch_id,
        fail_on_warnings=fail_on_warnings,
    )
    write_json_report(Path(config.reporting.output_dir) / "model-report.json", report)
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
                pull_yaml(
                    client=prepared["client"],
                    model_id=context.model_id,
                    branch_id=prepared["branch_id"],
                    output_dir=snapshot_dir,
                )
                prepared["comparison_base_yaml_dir"] = snapshot_dir
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
                    )
                except OmniFlowError as exc:
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
    base_graph = build_graph(load_yaml_files(args.base))
    head_graph = build_graph(load_yaml_files(args.head))
    report = diff_graphs(base_graph, head_graph)
    if args.report_out:
        write_json_report(args.report_out, report)
    else:
        print(redact(report))
    return 1 if report["risk_level"] in {"breaking", "security_sensitive"} else 0


def cmd_report(args: argparse.Namespace) -> int:
    import json

    report = json.loads(Path(args.input).read_text(encoding="utf-8"))
    write_reports(report, output_dir=args.output_dir, formats=args.format)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = _override_config(load_config(args.config), args)
    api_key = require_api_key()
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
        client, branch_id = _client_and_branch_for_context(context, config.omni.timeout, api_key=api_key)
        client.get_model_yaml(
            context.model_id,
            branch_id=branch_id,
            include_checksums=False,
        )
        try:
            git_config = client.get_git_configuration(context.model_id)
        except OmniAuthError:
            warnings.append(
                f"Model {context.model_id}: Git configuration verification was skipped because the token "
                "does not allow this metadata read. Core model access succeeded."
            )
        else:
            issues.extend(_git_configuration_issues(context, git_config))
    if issues:
        raise ConfigError("Omni Git configuration mismatch: " + "; ".join(issues))
    for warning in warnings:
        print(f"omniflow doctor warning: {warning}", file=sys.stderr)
    print(f"omniflow doctor passed: {len(contexts)} model context(s) ready")
    return 0


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
    client = OmniClient(
        base_url=context.base_url,
        api_key=api_key or require_api_key(),
        timeout=timeout,
    )
    branch_id = context.branch_id or client.resolve_branch_id(context.model_id, context.branch_name)
    if context.branch_name and not branch_id:
        raise ConfigError(
            f"Could not resolve Omni branch '{context.branch_name}' for model {context.model_id}. "
            "Verify the Omni PR branch exists and the API key can list model branches."
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
        "config_hash": config.hash,
        "summary": summary,
        "issues": issues,
        "policy_decision": "fail" if exit_code else "pass",
        "exit_code": exit_code,
        "exit_code_reason": "validation failed" if exit_code else "success",
    }


def _aggregate_report(config, contexts, exit_code, issues, summary, reports):
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
        "policy_decision": "fail" if exit_code else "pass",
        "exit_code": exit_code,
        "exit_code_reason": "validation failed" if exit_code else "success",
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
    }


def _git_configuration_issues(context: ModelContext, git_config: dict[str, Any]) -> list[str]:
    checks = [
        ("model_path", context.model_path, git_config.get("modelPath")),
        ("base_branch", context.base_branch, git_config.get("baseBranch")),
        ("git_provider", context.git_provider, git_config.get("gitServiceProvider")),
        ("web_url", context.web_url, git_config.get("webUrl")),
    ]
    issues = []
    for label, expected, actual in checks:
        if not expected or actual is None:
            continue
        if _normalize_git_config_value(expected) != _normalize_git_config_value(actual):
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
    for line in annotation_lines(issues[:limit]):
        print(line)


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
    for path in (output_dir, public_dir(output_dir), restricted_dir(output_dir)):
        validate_repo_output_path(path)
    for name in report_names:
        validate_repo_output_path(output_dir / name)
        validate_repo_output_path(public_dir(output_dir) / name)
    validate_repo_output_path(output_dir / "artifact-manifest.json")


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
    issue = {"severity": "error", "validator": "setup", "message": redact(str(exc))}
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
        "issues": [{"validator": "ai_repair", "severity": "error", "message": redact(str(exc))}],
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
        "issues": [{"validator": "dbt_sync", "severity": "error", "message": redact(str(exc))}],
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
        "issues": [{"validator": "ai_repair", "severity": "error", "message": redact(str(exc))}],
    }
    write_repair_artifacts(report, output_dir=output_dir, redaction_level="standard")
    write_artifact_manifest(
        output_dir=output_dir,
        restricted_artifacts_enabled=False,
        redaction_level="standard",
    )


if __name__ == "__main__":
    raise SystemExit(main())
