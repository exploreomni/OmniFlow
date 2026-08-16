from __future__ import annotations

from pathlib import Path
from typing import Any

from .json_report import write_json_report
from .junit_report import write_junit_report
from .markdown_report import write_markdown_report
from .sarif_report import write_sarif_report


def write_reports(report: dict[str, Any], *, output_dir: str | Path, formats: list[str]) -> None:
    root = Path(output_dir)
    if "json" in formats:
        write_json_report(root / "report.json", report)
    if "markdown" in formats or "md" in formats:
        write_markdown_report(root / "report.md", report)
    if "sarif" in formats:
        write_sarif_report(root / "report.sarif", report)
    if "junit" in formats or "xml" in formats:
        write_junit_report(root / "junit.xml", report)
