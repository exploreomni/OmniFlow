from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..security import secure_write_text


def to_sarif(report: dict[str, Any]) -> dict[str, Any]:
    issues = report.get("issues", [])
    rules = {}
    results = []
    for issue in issues:
        rule_id = issue.get("rule_id") or issue.get("type") or issue.get("validator") or "omniflow"
        rules.setdefault(rule_id, {"id": rule_id, "name": rule_id, "shortDescription": {"text": rule_id}})
        level = _sarif_level(issue.get("severity") or issue.get("risk"))
        file_path = issue.get("file") or "omniflow"
        results.append(
            {
                "ruleId": rule_id,
                "level": level,
                "message": {"text": issue.get("message") or issue.get("summary") or "OmniFlow issue"},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": file_path},
                            "region": {"startLine": 1},
                        }
                    }
                ],
            }
        )
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {"name": "omniflow", "version": report.get("tool_version"), "rules": list(rules.values())}
                },
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
