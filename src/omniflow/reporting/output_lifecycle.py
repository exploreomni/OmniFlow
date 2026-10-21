"""Finalize fresh optional-operation evidence before authorizing any upload.

Preparation happens before package installation in the trusted workflow. This
installed module runs only after the actual operation, never after an install-
only intermediate skip. A step output is a generation receipt, not an upload.
"""
from __future__ import annotations

import argparse
import os

from ..artifacts import write_artifact_manifest, write_public_reports
from ..github.report_publisher import _read_json
from .issue_policy import is_blocking
from .notification import notification_decision
from .output_paths import report_directory


def finalize(report_dir: str, operation_status: str) -> bool:
    root = report_directory(report_dir)
    if operation_status == "skipped":
        return False
    if operation_status not in {"success", "failure", "cancelled"}:
        raise ValueError("No final operation status was provided")
    try:
        report = _read_json(root / "public" / "report.json")
    except FileNotFoundError:
        report = None
    recorded_decision = notification_decision(report) if report is not None else None
    needs_failure = operation_status != "success" and recorded_decision in {"pass", "skipped"}
    if report is None or needs_failure:
        # Keep useful current findings, but invalidate a successful/intermediate
        # policy result when the *final* readiness/operation step did not finish.
        report = dict(report or {})
        report.update(policy_decision="fail", validation_complete=False, decision_status="incomplete",
                      validation_status="failed", exit_code=6)
        report["issues"] = [*report.get("issues", []), {
            "validator": "execution", "type": "validation_execution_failed", "severity": "error",
            "blocking": True, "reason_code": "workflow_operation_incomplete",
        }]
        active = [issue for issue in report["issues"] if issue.get("active", True)]
        report["summary"] = {**report.get("summary", {}), "total_issues": len(active),
                             "errors": sum(issue.get("severity") == "error" for issue in active),
                             "warnings": sum(issue.get("severity") in {"warn", "warning"} for issue in active),
                             "blocking_issues": sum(is_blocking(issue) for issue in report["issues"])}
        try:
            manifest = _read_json(root / "artifact-manifest.json")
        except FileNotFoundError:
            manifest = {}
        write_public_reports(report, output_dir=root, formats=["json", "markdown", "sarif", "junit"],
                             redaction_level="strict")
        write_artifact_manifest(output_dir=root,
                                restricted_artifacts_enabled=manifest.get("restricted_artifacts_enabled") is True,
                                redaction_level="strict")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", default=os.getenv("OMNIFLOW_REPORT_DIR", ".omniflow"))
    parser.add_argument("--operation-status", required=True)
    args = parser.parse_args()
    try:
        ready = finalize(args.report_dir, args.operation_status)
        if ready:
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                output.write("ready=true\n")
        return 0
    except Exception:
        # Exceptions may include sensitive paths or parser source text.
        print("::error::OF-REPORT-FINALIZATION: Current operation evidence could not be finalized. "
              "No upload is authorized. Inspect the final operation and output path; do not reuse earlier artifacts.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
