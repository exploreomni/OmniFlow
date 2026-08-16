from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from ..artifacts import public_dir, write_public_json
from ..security import public_safe, secure_write_text


def write_repair_artifacts(
    report: dict[str, Any], *, output_dir: str | Path, redaction_level: str
) -> dict[str, Any]:
    root = Path(output_dir)
    safe_report = write_public_json(root / "repair.json", report, redaction_level=redaction_level)
    write_public_json(public_dir(root) / "repair.json", report, redaction_level=redaction_level)
    markdown = render_repair_markdown(safe_report)
    secure_write_text(root / "repair.md", markdown)
    secure_write_text(public_dir(root) / "repair.md", markdown)
    evidence = {
        key: safe_report.get(key)
        for key in (
            "tool",
            "tool_version",
            "operation",
            "generated_at",
            "status",
            "model_id",
            "branch_id",
            "branch_name",
            "pull_request_number",
            "head_sha",
            "config_hash",
            "query_execution_acknowledged",
            "raw_query_results_stored",
            "change_summary",
            "full_validation",
            "rollback",
            "commit",
            "manual_review_required",
            "policy_decision",
            "exit_code",
            "exit_code_reason",
        )
    }
    write_public_json(root / "evidence.json", evidence, redaction_level=redaction_level)
    write_public_json(public_dir(root) / "evidence.json", evidence, redaction_level=redaction_level)
    return safe_report


def render_repair_markdown(report: dict[str, Any]) -> str:
    status = _safe_text(report.get("status") or "unknown")
    message = _safe_text(report.get("message") or "")
    lines = [
        "# OmniFlow AI Repair",
        "",
        f"**Status:** `{status}`",
        f"**Policy decision:** `{_safe_text(report.get('policy_decision') or 'fail')}`",
        f"**Model:** `{_safe_text(report.get('model_id') or 'unknown')}`",
        f"**Branch:** `{_safe_text(report.get('branch_name') or 'unknown')}`",
        f"**Head SHA:** `{_safe_text(report.get('head_sha') or 'unknown')}`",
        "",
        message,
        "",
    ]
    change = report.get("change_summary") if isinstance(report.get("change_summary"), dict) else {}
    modified = change.get("modified_files") if isinstance(change.get("modified_files"), list) else []
    lines.extend(["## Reviewed Changes", ""])
    if modified:
        lines.extend(f"- `{_safe_text(file_name)}`" for file_name in modified)
        lines.append(f"- Changed lines: `{int(change.get('changed_lines') or 0)}`")
    else:
        lines.append("No authored-YAML change was accepted.")
    lines.extend(["", "## Safety", ""])
    lines.append("- Raw query results stored by OmniFlow: `false`")
    lines.append(f"- Rollback verified: `{bool((report.get('rollback') or {}).get('verified'))}`")
    lines.append(f"- Manual review required: `{bool(report.get('manual_review_required'))}`")
    lines.append("")
    lines.append("OmniFlow does not approve, merge, or deploy AI repairs. Review the pull request diff before merge.")
    return "\n".join(lines) + "\n"


def _safe_text(value: Any) -> str:
    safe = public_safe(str(value), redaction_level="standard")
    return html.escape(str(safe), quote=True).replace("`", "'").replace("@", "&#64;")
