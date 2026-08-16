from __future__ import annotations

from typing import Any

from .config import DbtExposureSettings
from .exceptions import OmniFlowError
from .omni_client import OmniClient
from .security import redact
from .timestamps import utc_now_iso


def run_dbt_exposure_enrichment(
    *,
    client: OmniClient,
    model_id: str,
    branch_id: str | None,
    settings: DbtExposureSettings,
) -> tuple[dict[str, Any], int]:
    try:
        payload = client.get_dbt_exposures(model_id, branch_id=branch_id)
        records = _exposure_records(payload)
        exposures = _normalize_exposures(records)
        unmapped_dashboards = sum(1 for record in records if _is_unmapped_exposure_record(record))
        coverage_status = "partial" if unmapped_dashboards else "available"
        coverage_gaps = []
        issues = []
        if unmapped_dashboards:
            message = (
                f"{unmapped_dashboards} published dashboard record(s) did not map to dbt model dependencies."
            )
            coverage_gaps.append(
                {
                    "type": "dbt_exposures",
                    "name": model_id,
                    "message": message,
                }
            )
            issues.append(
                {
                    "validator": "dbt_exposures",
                    "severity": "warning",
                    "message": message,
                }
            )
        report = {
            "tool": "omniflow",
            "validator": "dbt_exposures",
            "generated_at": utc_now_iso(),
            "model_id": model_id,
            "branch_id": branch_id,
            "summary": {
                "total_records": len(records),
                "total_exposures": len(exposures),
                "unmapped_dashboards": unmapped_dashboards,
                "coverage_status": coverage_status,
                "coverage_scope": "published_shared_dashboards",
            },
            "exposures": exposures,
            "coverage_gaps": coverage_gaps,
            "issues": issues,
        }
        return report, 0
    except OmniFlowError as exc:
        issue = {
            "validator": "dbt_exposures",
            "severity": "error" if settings.fail_on_unavailable else "warning",
            "message": f"dbt exposure enrichment unavailable: {redact(str(exc))}",
        }
        report = {
            "tool": "omniflow",
            "validator": "dbt_exposures",
            "generated_at": utc_now_iso(),
            "model_id": model_id,
            "branch_id": branch_id,
            "summary": {
                "total_records": 0,
                "total_exposures": 0,
                "unmapped_dashboards": 0,
                "coverage_status": "unavailable",
                "coverage_scope": "published_shared_dashboards",
            },
            "coverage_gaps": [
                {
                    "type": "dbt_exposures",
                    "name": model_id,
                    "message": issue["message"],
                }
            ],
            "issues": [issue],
        }
        return report, 1 if settings.fail_on_unavailable else 0


def _normalize_exposures(payload: Any) -> list[dict[str, Any]]:
    records = _exposure_records(payload)
    exposures = []
    for record in records:
        if not isinstance(record, dict):
            continue
        nested = record.get("exposure")
        if nested is None and "exposure" in record:
            continue
        exposure = nested if isinstance(nested, dict) else record
        exposures.append(
            {
                "id": _text(
                    record.get("dashboard_identifier")
                    or exposure.get("id")
                    or exposure.get("content_id")
                    or exposure.get("dashboard_id")
                ),
                "deduplication_name": _text(record.get("deduplication_name")),
                "name": _text(
                    exposure.get("label")
                    or exposure.get("name")
                    or exposure.get("dashboard_name")
                    or exposure.get("content_name")
                ),
                "type": _text(exposure.get("type") or exposure.get("content_type")) or "dashboard",
                "url": _text(exposure.get("url") or exposure.get("content_url"), maximum=2_048),
                "owner": _owner(exposure.get("owner")),
                "depends_on": _depends_on(exposure),
                "maturity": _text(exposure.get("maturity")),
            }
        )
    return exposures


def _is_unmapped_exposure_record(record: Any) -> bool:
    return isinstance(record, dict) and "exposure" in record and record.get("exposure") is None


def _exposure_records(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("exposures", "records", "items", "dashboards", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _depends_on(record: dict[str, Any]) -> list[str]:
    value = record.get("depends_on") or record.get("dependsOn") or record.get("dependencies")
    if not isinstance(value, list):
        return []
    depends_on = []
    for item in value:
        if isinstance(item, str):
            name = _text(item, maximum=1_024)
            if name:
                depends_on.append(name)
        elif isinstance(item, dict):
            name = item.get("name") or item.get("unique_id") or item.get("id")
            normalized = _text(name, maximum=1_024)
            if normalized:
                depends_on.append(normalized)
        if len(depends_on) >= 10_000:
            break
    return depends_on


def _owner(value: Any) -> dict[str, str] | None:
    if isinstance(value, str):
        name = _text(value)
        return {"name": name} if name else None
    if not isinstance(value, dict):
        return None
    owner = {}
    for key in ("id", "name"):
        normalized = _text(value.get(key))
        if normalized:
            owner[key] = normalized
    return owner or None


def _text(value: Any, *, maximum: int = 500) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized[:maximum] if normalized else None
