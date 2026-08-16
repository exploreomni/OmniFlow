from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from ..omni_client import OmniClient
from ..security import redact, secure_write_text
from ..timestamps import utc_now_iso


def load_json(path: str | Path) -> dict[str, Any] | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    secure_write_text(target, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def extract_label_names(record: dict[str, Any]) -> list[str] | None:
    labels = record.get("labels")
    if not isinstance(labels, list):
        return None
    names: list[str] = []
    for item in labels:
        value = item.get("name") if isinstance(item, dict) else item
        if isinstance(value, str) and value.strip():
            names.append(value.strip())
    return names or None


def extract_owner(record: dict[str, Any]) -> dict[str, str] | None:
    owner = record.get("owner")
    if not isinstance(owner, dict):
        return None
    normalized = {}
    if isinstance(owner.get("id"), str) and owner["id"].strip():
        normalized["id"] = owner["id"].strip()
    if isinstance(owner.get("name"), str) and owner["name"].strip():
        normalized["name"] = owner["name"].strip()
    return normalized or None


def collect_content_issues(payload: dict[str, Any]) -> list[Any]:
    issues: list[Any] = []
    content = payload.get("content")
    if not isinstance(content, list):
        return issues
    for document in content:
        if not isinstance(document, dict):
            continue
        doc_context = {
            "document_id": document.get("document_id"),
            "document_identifier": document.get("identifier"),
            "document_name": document.get("name"),
            "document_type": document.get("type"),
            "document_url": document.get("url") if isinstance(document.get("url"), str) else None,
            "folder_name": document.get("folder", {}).get("name") if isinstance(document.get("folder"), dict) else None,
            "folder_path": document.get("folder", {}).get("path") if isinstance(document.get("folder"), dict) else None,
            "document_labels": extract_label_names(document),
            "document_owner": extract_owner(document),
        }
        dashboard_issues = document.get("dashboard_filter_issues")
        if isinstance(dashboard_issues, list):
            for item in dashboard_issues:
                issues.append(
                    {
                        "message": item.get("message") if isinstance(item, dict) else item,
                        "raw_issue": item,
                        "issue_type": "dashboard_filter",
                        **doc_context,
                    }
                )
        queries = document.get("queries_and_issues")
        if not isinstance(queries, list):
            continue
        for query in queries:
            if not isinstance(query, dict):
                continue
            query_issues = query.get("issues")
            if not isinstance(query_issues, list):
                continue
            for item in query_issues:
                issues.append(
                    {
                        "message": item.get("message") if isinstance(item, dict) else item,
                        "raw_issue": item,
                        "issue_type": "query",
                        "query_name": query.get("query_name"),
                        "query_presentation_id": query.get("query_presentation_id"),
                        **doc_context,
                    }
                )
    return issues


def extract_issues(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("issues", "validation_issues", "errors"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    if "content" in payload:
        return collect_content_issues(payload)
    for key in ("documents", "items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def issue_identity(issue: Any) -> str:
    if isinstance(issue, str):
        value = issue
    else:
        try:
            comparable = dict(issue) if isinstance(issue, dict) else issue
            if isinstance(comparable, dict):
                for key in (
                    "document_labels",
                    "document_owner",
                    "document_name",
                    "document_type",
                    "document_url",
                    "folder_name",
                    "folder_path",
                    "query_name",
                    "raw_issue",
                ):
                    comparable.pop(key, None)
            value = json.dumps(comparable, sort_keys=True, separators=(",", ":"))
        except TypeError:
            value = str(issue)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def issue_summary(issue: Any) -> str:
    if isinstance(issue, str):
        return issue
    if isinstance(issue, dict):
        message = issue.get("message")
        if message is not None and not isinstance(message, str):
            message = str(message)
        if isinstance(message, str) and message.strip():
            prefix = " / ".join(
                part.strip()
                for part in (issue.get("document_name"), issue.get("query_name"))
                if isinstance(part, str) and part.strip()
            )
            return f"{prefix}: {message}" if prefix else message
        for key in ("title", "name", "path", "field"):
            value = issue.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return json.dumps(issue, sort_keys=True)
    return str(issue)


def normalize_issues(issues: Iterable[Any]) -> list[dict[str, Any]]:
    return [{"id": issue_identity(issue), "summary": issue_summary(issue), "raw": issue} for issue in issues]


def partition_issues(
    current: list[dict[str, Any]],
    previous: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    previous_ids = {item["id"] for item in previous}
    current_ids = {item["id"] for item in current}
    return (
        [item for item in current if item["id"] not in previous_ids],
        [item for item in current if item["id"] in previous_ids],
        [item for item in previous if item["id"] not in current_ids],
    )


def index_content_records(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for record in records:
        for key in ("identifier", "id"):
            identifier = record.get(key)
            if isinstance(identifier, str) and identifier.strip():
                indexed[identifier] = record
    return indexed


def filter_validator_payload(payload: Any, allowed_identifiers: set[str]) -> Any:
    if not isinstance(payload, dict) or not isinstance(payload.get("content"), list):
        return payload
    filtered = dict(payload)
    filtered["content"] = [
        document
        for document in payload["content"]
        if isinstance(document, dict)
        and isinstance(document.get("identifier"), str)
        and document["identifier"] in allowed_identifiers
    ]
    return filtered


def enrich_validator_payload(
    payload: Any,
    content_records_by_identifier: dict[str, dict[str, Any]],
) -> Any:
    if not isinstance(payload, dict) or not isinstance(payload.get("content"), list):
        return payload
    enriched = dict(payload)
    enriched_content = []
    for document in payload["content"]:
        if not isinstance(document, dict):
            enriched_content.append(document)
            continue
        next_document = dict(document)
        identifiers = (next_document.get("identifier"), next_document.get("document_id"))
        content_record = next(
            (
                content_records_by_identifier[value]
                for value in identifiers
                if isinstance(value, str) and value in content_records_by_identifier
            ),
            None,
        )
        if content_record:
            owner = extract_owner(content_record)
            if owner and not isinstance(next_document.get("owner"), dict):
                next_document["owner"] = owner
            labels = content_record.get("labels")
            if isinstance(labels, list) and not isinstance(next_document.get("labels"), list):
                next_document["labels"] = labels
        enriched_content.append(next_document)
    enriched["content"] = enriched_content
    return enriched


def compare_history_labels(payload: dict[str, Any], labels: list[str]) -> list[dict[str, Any]]:
    previous_labels = payload.get("labels") or []
    if isinstance(previous_labels, str):
        previous_labels = [part.strip() for part in previous_labels.split(",") if part.strip()]
    return payload.get("issues", []) if previous_labels == labels else []


def run_content_validation(
    *,
    client: OmniClient,
    model_id: str,
    branch_id: str | None,
    user_id: str | None,
    include_personal_folders: bool,
    labels: list[str],
    history_in: str | Path,
    history_out: str | Path,
    report_out: str | Path,
    fail_on_new_only: bool,
    max_samples: int = 20,
    redact_document_names: bool = False,
    allow_raw_response_output: bool = False,
) -> tuple[dict[str, Any], int]:
    payload = client.validate_content(
        model_id,
        branch_id=branch_id,
        user_id=user_id,
        include_personal_folders=include_personal_folders,
    )
    records = client.list_content(
        labels=labels,
        branch_id=branch_id,
        include_personal_folders=include_personal_folders,
        user_id=user_id,
    )
    records_by_identifier = index_content_records(records)
    payload = _prepare_validator_payload(payload, labels, records_by_identifier)

    safe_issues = _sanitize_issues(
        extract_issues(payload),
        redact_document_names=redact_document_names,
        allow_raw_response_output=allow_raw_response_output,
    )
    normalized = normalize_issues(safe_issues)
    comparison_source = "history"
    if branch_id and fail_on_new_only:
        baseline_payload = client.validate_content(
            model_id,
            branch_id=None,
            user_id=user_id,
            include_personal_folders=include_personal_folders,
        )
        baseline_payload = _prepare_validator_payload(baseline_payload, labels, records_by_identifier)
        previous = normalize_issues(
            _sanitize_issues(
                extract_issues(baseline_payload),
                redact_document_names=redact_document_names,
                allow_raw_response_output=allow_raw_response_output,
            )
        )
        comparison_source = "base_model"
    else:
        previous_payload = load_json(history_in) or {}
        previous = compare_history_labels(previous_payload, labels) if previous_payload else []
    new_items, existing_items, resolved_items = partition_issues(normalized, previous)
    report_new = [_report_issue(item, state="new", severity="error") for item in new_items]
    existing_severity = "info" if fail_on_new_only else "error"
    report_existing = [_report_issue(item, state="existing", severity=existing_severity) for item in existing_items]
    report_resolved = [_report_issue(item, state="resolved", severity="info", active=False) for item in resolved_items]
    generated_at = utc_now_iso()
    report = {
        "tool": "omniflow",
        "validator": "content",
        "generated_at": generated_at,
        "model_id": model_id,
        "branch_id": branch_id,
        "labels": labels,
        "include_personal_folders": include_personal_folders,
        "total_issues": len(normalized),
        "new_issues": len(new_items),
        "existing_issues": len(existing_items),
        "resolved_issues": len(resolved_items),
        "comparison_source": comparison_source,
        "issues": [*report_new, *report_existing, *report_resolved],
        "new_issue_samples": report_new[:max_samples],
        "existing_issue_samples": report_existing[:max_samples],
        "resolved_issue_samples": report_resolved[:max_samples],
        "note": (
            "Omni validates the full model server-side; label filtering is applied locally "
            "after content metadata lookup."
        ),
    }
    write_json(report_out, redact(report))
    write_json(
        history_out,
        {
            "generated_at": generated_at,
            "model_id": model_id,
            "branch_id": branch_id,
            "labels": labels,
            "include_personal_folders": include_personal_folders,
            "issues": normalized,
        },
    )
    exit_code = 1 if (len(new_items) if fail_on_new_only else len(normalized)) > 0 else 0
    return report, exit_code


def _prepare_validator_payload(
    payload: Any,
    labels: list[str],
    records_by_identifier: dict[str, dict[str, Any]],
) -> Any:
    if not isinstance(payload, dict) or not isinstance(payload.get("content"), list):
        return payload
    if labels:
        payload = filter_validator_payload(payload, set(records_by_identifier))
    return enrich_validator_payload(payload, records_by_identifier)


def _report_issue(
    item: dict[str, Any],
    *,
    state: str,
    severity: str,
    active: bool = True,
) -> dict[str, Any]:
    return {
        **item,
        "validator": "content",
        "severity": severity,
        "message": item.get("summary") or "Omni content validation issue",
        "state": state,
        "active": active,
    }


def _sanitize_issues(
    issues: list[Any],
    *,
    redact_document_names: bool,
    allow_raw_response_output: bool,
) -> list[Any]:
    redacted = []
    for issue in issues:
        if isinstance(issue, dict):
            next_issue = dict(issue)
            if not allow_raw_response_output:
                next_issue.pop("raw_issue", None)
            if redact_document_names:
                for key in ("document_name", "query_name"):
                    if key in next_issue:
                        next_issue[key] = "[REDACTED]"
            redacted.append(next_issue)
        else:
            redacted.append(issue)
    return redacted


def _redact_issue_names(issues: list[Any], enabled: bool) -> list[Any]:
    if not enabled:
        return issues
    redacted = []
    for issue in issues:
        if isinstance(issue, dict):
            next_issue = dict(issue)
            for key in ("document_name", "query_name"):
                if key in next_issue:
                    next_issue[key] = "[REDACTED]"
            redacted.append(next_issue)
        else:
            redacted.append(issue)
    return redacted
