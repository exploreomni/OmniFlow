from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import __version__
from .diagnostics import public_diagnostic, record_private_report
from .exceptions import OmniFlowError, SecurityPolicyError
from .git import tool_revision
from .reporting.writer import write_reports
from .security import secure_write_text
from .timestamps import utc_now_iso

PUBLIC_DIR = "public"
RESTRICTED_DIR = "restricted"


def public_dir(output_dir: str | Path) -> Path:
    return Path(output_dir) / PUBLIC_DIR


def restricted_dir(output_dir: str | Path) -> Path:
    return Path(output_dir) / RESTRICTED_DIR


def write_public_reports(
    report: dict[str, Any],
    *,
    output_dir: str | Path,
    formats: list[str],
    redaction_level: str,
) -> dict[str, Any]:
    report = dict(report)
    report.setdefault("tool_revision", tool_revision())
    try:
        safe_report = public_diagnostic(report)
        # A changed format preference must not leave a previous, possibly
        # identity-rich report available for the workflow to upload.
        current_formats = set(formats)
        for directory in (Path(output_dir), public_dir(output_dir)):
            for name, output_format in STANDARD_REPORT_FORMATS.items():
                path = directory / name
                if path.exists() or path.is_symlink():
                    current_formats.add(output_format)
        write_reports(safe_report, output_dir=output_dir, formats=sorted(current_formats))
        write_reports(safe_report, output_dir=public_dir(output_dir), formats=sorted(current_formats))
        for directory in (Path(output_dir), public_dir(output_dir)):
            evidence_path = directory / "evidence.json"
            if evidence_path.exists() or evidence_path.is_symlink():
                # Normal orchestration replaces this with its current evidence
                # envelope next. Until then, only current safe evidence remains.
                secure_write_text(evidence_path, json.dumps(safe_report, indent=2) + "\n")
        record_private_report(report)
    except SecurityPolicyError:
        # Never retry an output path rejected by the filesystem safety checks.
        raise
    except Exception as exc:
        write_emergency_failure(output_dir=output_dir)
        raise OmniFlowError("Report generation failed; current validation evidence is incomplete.") from exc
    return safe_report


def write_emergency_failure(*, output_dir: str | Path) -> None:
    """Replace stale standard outputs without invoking the failed report pipeline.

    Deliberately fixed text: arbitrary exceptions, payloads and partial model
    metadata must never be serialized by this last-resort public path.
    """
    message = "OmniFlow could not complete this run. Current validation evidence is incomplete; do not merge."
    issue = {"validator": "execution", "type": "internal_execution_failure", "severity": "error", "message": message}
    report = {
        "tool": "omniflow", "diagnostic_schema_version": 2,
        "policy_decision": "fail", "validation_complete": False,
        "exit_code": 6, "exit_code_reason": "internal tool error", "issues": [issue],
        "summary": {"total_issues": 1, "errors": 1, "warnings": 0},
        "check_states": [{"validator": "execution", "status": "failed", "exit_code": 6}],
    }
    markdown = f"# OmniFlow\n\n## Decision\n\n**Fail: validation incomplete.**\n\n{message}\n"
    sarif = {
        "version": "2.1.0", "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [{"tool": {"driver": {"name": "omniflow", "version": __version__}},
                  "results": [{"ruleId": "internal_execution_failure", "level": "error", "message": {"text": message}}]}],
    }
    junit = (
        '<testsuite name="omniflow" tests="1" failures="1"><testcase name="internal_execution_failure">'
        f'<failure message="Validation incomplete">{message}</failure></testcase></testsuite>\n'
    )
    contents = {
        "report.json": json.dumps(report, indent=2) + "\n", "report.md": markdown,
        "report.sarif": json.dumps(sarif, indent=2) + "\n", "junit.xml": junit,
        "evidence.json": json.dumps({**report, "validation_status": "failed"}, indent=2) + "\n",
    }
    for directory in (Path(output_dir), public_dir(output_dir)):
        for name, content in contents.items():
            secure_write_text(directory / name, content)


def write_public_json(
    path: str | Path,
    payload: dict[str, Any],
    *,
    redaction_level: str,
) -> dict[str, Any]:
    if payload.get("tool") == "omniflow":
        payload = dict(payload)
        payload.setdefault("tool_revision", tool_revision())
    safe_payload = public_diagnostic(payload)
    target = Path(path)
    secure_write_text(target, json.dumps(safe_payload, indent=2, sort_keys=True) + "\n")
    return safe_payload


def write_artifact_manifest(
    *,
    output_dir: str | Path,
    restricted_artifacts_enabled: bool,
    redaction_level: str,
) -> None:
    root = Path(output_dir)
    public_files = _inventory_files(root / PUBLIC_DIR)
    restricted_files = _inventory_files(root / RESTRICTED_DIR)
    restricted_present = (root / RESTRICTED_DIR).exists()
    categories: dict[str, int] = {}
    for path in restricted_files:
        category = next(
            (name for name in ("yaml-base", "yaml-head", "pre-sync-yaml") if name in path.parts),
            path.name if path.name in RESTRICTED_REPORT_NAMES else "other",
        )
        categories[category] = categories.get(category, 0) + 1
    manifest = {
        "version": 2,
        "inventory_observed_at": utc_now_iso(),
        "public_dir": PUBLIC_DIR,
        "restricted_dir": RESTRICTED_DIR,
        "restricted_artifacts_enabled": restricted_artifacts_enabled,
        "redaction_level": redaction_level if redaction_level in {"standard", "strict"} else "unknown",
        "diagnostic_schema_version": 2,
        "public_artifacts": [f"{PUBLIC_DIR}/{path.as_posix()}" for path in public_files
                             if path.as_posix() in PUBLIC_REPORT_NAMES],
        "unrecognized_public_file_count": sum(path.as_posix() not in PUBLIC_REPORT_NAMES for path in public_files),
        # This manifest is public. Count all restricted files, but never publish
        # customer model IDs, authored filenames, or content-derived paths.
        "restricted_inventory": {
            "directory_present": restricted_present,
            "file_count": len(restricted_files),
            "categories": dict(sorted(categories.items())),
        },
        "restricted_cleanup": {
            "requested": not restricted_artifacts_enabled,
            "status": "not_requested"
            if restricted_artifacts_enabled
            else ("pending" if restricted_present else "absent"),
        },
        "restricted_artifacts": [
            f"{RESTRICTED_DIR}/<model_id>/yaml-base/",
            f"{RESTRICTED_DIR}/<model_id>/yaml-head/",
            f"{RESTRICTED_DIR}/<model_id>/pre-sync-yaml/",
            f"{RESTRICTED_DIR}/<model_id>/post-sync-validation/",
            f"{RESTRICTED_DIR}/<model_id>/repair-validation/",
            *[f"{RESTRICTED_DIR}/<model_id>/{name}" for name in RESTRICTED_REPORT_NAMES],
        ],
    }
    secure_write_text(root / "artifact-manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")


RESTRICTED_REPORT_NAMES = (
    "ai-eval-detail.json",
    "ai-eval-runs.json",
    "breaking-change-hold.json",
    "content-report.json",
    "contract-impact.json",
    "dbt-exposures.json",
    "dependencies.json",
    "history.json",
    "report.json",
    "semantic-diff.json",
)

PUBLIC_REPORT_NAMES = frozenset({
    "report.json", "report.md", "report.sarif", "junit.xml", "evidence.json", "repair.json", "repair.md",
    "dbt-impact.json", "dbt-sync.json",
})

STANDARD_REPORT_FORMATS = {
    "report.json": "json", "report.md": "markdown", "report.sarif": "sarif", "junit.xml": "junit",
}


def _inventory_files(directory: Path) -> list[Path]:
    if directory.is_symlink():
        raise SecurityPolicyError("Artifact inventory paths must not be symbolic links")
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise SecurityPolicyError("Artifact inventory path must be a directory")
    files = []
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise SecurityPolicyError("Artifact inventory paths must not be symbolic links")
        if path.is_file():
            files.append(path.relative_to(directory))
    return sorted(files)
