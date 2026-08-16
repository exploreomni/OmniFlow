from __future__ import annotations

from typing import Any

from ..reporting.markdown_report import render_markdown_report

MARKER = "<!-- omniflow-report -->"


def render_pr_comment(report: dict[str, Any]) -> str:
    return f"{MARKER}\n{render_markdown_report(report)}"
