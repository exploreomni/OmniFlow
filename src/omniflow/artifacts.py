from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .reporting.writer import write_reports
from .security import public_safe, secure_write_text

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
    safe_report = public_safe(report, redaction_level=redaction_level)
    write_reports(safe_report, output_dir=output_dir, formats=formats)
    write_reports(safe_report, output_dir=public_dir(output_dir), formats=formats)
    return safe_report


def write_public_json(
    path: str | Path,
    payload: dict[str, Any],
    *,
    redaction_level: str,
) -> dict[str, Any]:
    safe_payload = public_safe(payload, redaction_level=redaction_level)
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
    public_artifacts = [
        f"{PUBLIC_DIR}/{name}"
        for name in (
            "report.json",
            "report.md",
            "report.sarif",
            "junit.xml",
            "evidence.json",
            "dbt-sync.json",
            "repair.json",
            "repair.md",
        )
        if (root / PUBLIC_DIR / name).is_file()
    ]
    manifest = {
        "version": 1,
        "public_dir": PUBLIC_DIR,
        "restricted_dir": RESTRICTED_DIR,
        "restricted_artifacts_enabled": restricted_artifacts_enabled,
        "redaction_level": redaction_level,
        "public_artifacts": public_artifacts,
        "restricted_artifacts": [
            f"{RESTRICTED_DIR}/<model_id>/yaml-base/",
            f"{RESTRICTED_DIR}/<model_id>/yaml-head/",
            f"{RESTRICTED_DIR}/<model_id>/pre-sync-yaml/",
            f"{RESTRICTED_DIR}/<model_id>/dependencies.json",
            f"{RESTRICTED_DIR}/<model_id>/semantic-diff.json",
            f"{RESTRICTED_DIR}/<model_id>/contract-impact.json",
            f"{RESTRICTED_DIR}/<model_id>/content-report.json",
            f"{RESTRICTED_DIR}/<model_id>/dbt-exposures.json",
        ],
    }
    secure_write_text(root / "artifact-manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
