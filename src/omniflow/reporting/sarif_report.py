from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..security import secure_write_text
from .issue_policy import is_blocking


def to_sarif(report: dict[str, Any]) -> dict[str, Any]:
    issues = report.get("issues", [])
    rules = {}
    results = []
    for issue in issues:
        rule_id = issue.get("rule_id") or issue.get("type") or issue.get("validator") or "omniflow"
        rules.setdefault(rule_id, {"id": rule_id, "name": rule_id, "shortDescription": {"text": rule_id}})
        level = _sarif_level(issue.get("severity") or issue.get("risk"))
        file_path = issue.get("file") or "omniflow"
        location = {"artifactLocation": {"uri": file_path}}
        if report.get("diagnostic_audience") == "repository":
            if type(issue.get("line")) is int and issue["line"] > 0:
                location["region"] = {"startLine": issue["line"]}
        else:
            location["region"] = {"startLine": 1}
        results.append(
            {
                "ruleId": rule_id,
                "level": level,
                "kind": "fail" if is_blocking(issue) else "informational",
                "properties": {
                    "policy_blocking": is_blocking(issue),
                },
                "message": {"text": issue.get("message") or issue.get("summary") or "OmniFlow issue"},
                "locations": [
                    {
                        "physicalLocation": location
                    }
                ],
            }
        )
    driver = {"name": "omniflow", "rules": list(rules.values())}
    if isinstance(report.get("tool_version"), str):
        driver["version"] = report["tool_version"]
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": driver},
                "results": results,
            }
        ],
    }


def write_sarif_report(path: str | Path, report: dict[str, Any]) -> None:
    target = Path(path)
    secure_write_text(target, json.dumps(to_sarif(report), indent=2, sort_keys=True) + "\n")


def _sarif_level(value: Any) -> str:
    if value in {"error", "breaking", "security_sensitive", "governance_sensitive"}:
        return "error"
    if value in {"warning", "warn"}:
        return "warning"
    return "note"
