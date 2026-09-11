from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Iterable

from ..exceptions import OmniAPIError
from ..omni_client import OmniClient
from ..security import redact, secure_write_text
from ..timestamps import utc_now_iso

HISTORY_SCHEMA_VERSION = 1
QUERY_IDENTITY_KEYS = ("query_presentation_id", "query_id_map_key", "query_id")


def load_json(path: str | Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise OmniAPIError("Content validation history has an invalid envelope")
        return payload
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError):
        raise OmniAPIError("Content validation history could not be read as valid JSON; comparison evidence is unavailable") from None


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


class ContentEvidenceError(OmniAPIError):
    """Unreadable API evidence, with allowlisted context and no raw response."""

    def __init__(
        self, *, issue_type: str = "content", document: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> None:
        category = {"dashboard_filter": "dashboard-filter", "query": "query"}.get(issue_type, "content")
        super().__init__(
            f"Content Validator response contains unreadable {category} issue details. "
            "Validation evidence is incomplete; a specific content defect cannot be determined."
        )
        self.issue_type = issue_type if issue_type in {"dashboard_filter", "query"} else "content"
        self.validation_scope: str | None = None
        self.context: dict[str, str] = {}
        for source, mapping in (
            (document or {}, {"document_id": "document_id", "identifier": "document_identifier", "name": "document_name"}),
            (query or {}, {key: key for key in (*QUERY_IDENTITY_KEYS, "query_name")}),
        ):
            for key, target in mapping.items():
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    self.context[target] = redact(value.strip())[:512]

    def report_issue(self, *, redact_document_names: bool = False) -> dict[str, Any]:
        context = dict(self.context)
        if redact_document_names:
            for key in ("document_name", "query_name"):
                if key in context:
                    context[key] = "[REDACTED]"
        return {
            **context,
            "validator": "content",
            "type": "content_evidence_unavailable",
            "issue_type": self.issue_type,
            "validation_scope": self.validation_scope,
            "severity": "error",
            "message": str(self),
        }


def _issue_list(
    value: Any, *, issue_type: str = "content", document: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
) -> list[Any]:
    if not isinstance(value, list):
        raise ContentEvidenceError(issue_type=issue_type, document=document, query=query)
    for item in value:
        # The published API uses strings; retain the existing message-object
        # compatibility, but never guess undocumented nested error structures.
        message = item.get("message") if isinstance(item, dict) else item
        if not isinstance(message, str) or not message.strip():
            raise ContentEvidenceError(issue_type=issue_type, document=document, query=query)
    return value


def content_documents(payload: Any) -> list[dict[str, Any]]:
    """Validate evidence-bearing members before filtering or interpreting emptiness."""
    if not isinstance(payload, dict) or not isinstance(payload.get("content"), list):
        raise OmniAPIError("Content Validator response has an unexpected content envelope")
    for document in payload["content"]:
        if not isinstance(document, dict):
            raise OmniAPIError("Content Validator response contains an invalid document")
        for key in ("document_id", "identifier"):
            if key in document and (not isinstance(document[key], str) or not document[key].strip()):
                raise OmniAPIError("Content Validator response contains an invalid document identity")
        if "dashboard_filter_issues" in document:
            _issue_list(document["dashboard_filter_issues"], issue_type="dashboard_filter", document=document)
        if "queries_and_issues" in document:
            queries = document["queries_and_issues"]
            if not isinstance(queries, list) or any(not isinstance(query, dict) for query in queries):
                raise OmniAPIError("Content Validator response contains an invalid query list")
            for query in queries:
                for key in ("query_presentation_id", "query_id", "query_id_map_key"):
                    if key in query and (not isinstance(query[key], str) or not query[key].strip()):
                        raise OmniAPIError("Content Validator response contains an invalid query identity")
                if "issues" in query:
                    _issue_list(query["issues"], issue_type="query", document=document, query=query)
    return payload["content"]


def collect_content_issues(payload: dict[str, Any]) -> list[Any]:
    issues: list[Any] = []
    for document in content_documents(payload):
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
        query_keys = Counter(_query_identity(query) for query in queries)
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
                        **{key: query.get(key) for key in QUERY_IDENTITY_KEYS},
                        "comparison_eligible": query_keys[_query_identity(query)] == 1,
                        **doc_context,
                    }
                )
    return issues


def extract_issues(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return _issue_list(payload)
    if not isinstance(payload, dict):
        raise OmniAPIError("Content Validator response has an unexpected envelope")
    if "content" in payload:
        return collect_content_issues(payload)
    # Retain explicitly supported legacy issue envelopes, including valid empty lists.
    for key in ("issues", "validation_issues", "errors", "documents", "items", "results"):
        if key in payload:
            return _issue_list(payload[key])
    raise OmniAPIError("Content Validator response has an unexpected envelope")


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
                    "comparison_eligible",
                ):
                    comparable.pop(key, None)
                # Prefer the strongest available query identity. Optional auxiliary
                # identifiers must not make the same presentation appear new.
                query_key = _query_identity(comparable)
                for key in QUERY_IDENTITY_KEYS:
                    comparable.pop(key, None)
                if query_key is not None:
                    comparable[query_key[0]] = query_key[1]
            value = json.dumps(comparable, sort_keys=True, separators=(",", ":"))
        except TypeError:
            value = str(issue)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def issue_summary(issue: Any) -> str:
    if isinstance(issue, str):
        return issue
    if isinstance(issue, dict):
        message = issue.get("message")
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
        return "Content Validator issue details are unavailable; inspect the same model and branch in Omni."
    return "Content Validator issue details are unavailable; inspect the same model and branch in Omni."


def _query_identity(issue: dict[str, Any]) -> tuple[str, str] | None:
    return next(
        ((key, issue[key]) for key in QUERY_IDENTITY_KEYS if isinstance(issue.get(key), str) and issue[key].strip()),
        None,
    )


def _comparison_eligible(issue: Any) -> bool:
    if not isinstance(issue, dict) or issue.get("comparison_eligible") is False:
        return False
    has_document = any(
        isinstance(issue.get(key), str) and issue[key].strip()
        for key in ("document_id", "document_identifier")
    )
    return bool(has_document and (
        issue.get("issue_type") == "dashboard_filter"
        or (issue.get("issue_type") == "query" and _query_identity(issue) is not None)
    ))


def normalize_issues(issues: Iterable[Any]) -> list[dict[str, Any]]:
    return [
        {"id": issue_identity(issue), "summary": issue_summary(issue), "raw": issue,
         "comparison_eligible": _comparison_eligible(issue)}
        for issue in issues
    ]


def partition_issues(
    current: list[dict[str, Any]],
    previous: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    available: dict[str, deque[int]] = defaultdict(deque)
    for index, item in enumerate(previous):
        if item["comparison_eligible"]:
            available[item["id"]].append(index)
    new, existing, matched = [], [], set()
    for item in current:
        if item["comparison_eligible"] and available[item["id"]]:
            matched.add(available[item["id"]].popleft())
            existing.append(item)
        else:
            new.append(item)
    resolved = [
        item for index, item in enumerate(previous)
        if item["comparison_eligible"] and index not in matched
    ]
    return new, existing, resolved


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
    documents = content_documents(payload)
    if any(not isinstance(document.get("identifier"), str) for document in documents):
        raise OmniAPIError("Content Validator response is missing an identity required for label filtering")
    filtered["content"] = [
        document
        for document in documents
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


def compare_history(
    payload: Any, context: dict[str, Any],
) -> tuple[list[dict[str, Any]], str]:
    if payload is None:
        return [], "missing"
    if not isinstance(payload, dict):
        raise OmniAPIError("Content validation history has an invalid envelope")
    if "schema_version" not in payload:
        return [], "invalidated_legacy"
    if type(payload["schema_version"]) is not int:
        raise OmniAPIError("Content validation history has an invalid schema version")
    if payload["schema_version"] != HISTORY_SCHEMA_VERSION:
        return [], "invalidated_version"
    if any(key not in payload for key in context):
        raise OmniAPIError("Content validation history is missing comparison context")
    if (
        not isinstance(payload["model_id"], str) or not payload["model_id"].strip()
        or any(payload[key] is not None and (not isinstance(payload[key], str) or not payload[key].strip())
               for key in ("branch_id", "user_id"))
        or type(payload["include_personal_folders"]) is not bool
        or not isinstance(payload["labels"], list)
        or any(not isinstance(label, str) or not label.strip() for label in payload["labels"])
    ):
        raise OmniAPIError("Content validation history has invalid comparison context")
    issues = payload.get("issues")
    if not isinstance(issues, list):
        raise OmniAPIError("Content validation history has an invalid issue list")
    for item in issues:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("summary"), str) or not item["summary"].strip()
            or not isinstance(item.get("raw"), (str, dict))
            or type(item.get("comparison_eligible")) is not bool
            or item.get("id") != issue_identity(item["raw"])
            or item["comparison_eligible"] != _comparison_eligible(item["raw"])
        ):
            raise OmniAPIError("Content validation history contains invalid issue evidence")
    actual_context = {key: payload[key] for key in context}
    actual_context["labels"] = sorted(set(actual_context["labels"]))
    if actual_context != context:
        return [], "invalidated_context"
    # A missing user ID denotes the current credential's principal, not a stable
    # actor identity. Reusing its history across credential changes is unsafe.
    if context["user_id"] is None:
        return [], "invalidated_unscoped_user"
    return issues, "accepted"


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
    if labels and any(
        not isinstance(record, dict) or not isinstance(record.get("identifier"), str) or not record["identifier"].strip()
        for record in records
    ):
        raise OmniAPIError("Content metadata is missing an identity required for label filtering")
    records_by_identifier = index_content_records(records)
    payload = _prepare_validator_payload(
        payload, labels, records_by_identifier, validation_scope="branch" if branch_id else "base",
    )

    safe_issues = _sanitize_issues(
        extract_issues(payload),
        redact_document_names=redact_document_names,
        allow_raw_response_output=allow_raw_response_output,
    )
    normalized = normalize_issues(safe_issues)
    comparison_source = "history"
    history_context = {
        "model_id": model_id, "branch_id": branch_id, "user_id": user_id,
        "include_personal_folders": include_personal_folders, "labels": sorted(set(labels)),
    }
    history_status = "not_used_live_base"
    if branch_id and fail_on_new_only:
        baseline_payload = client.validate_content(
            model_id,
            branch_id=None,
            user_id=user_id,
            include_personal_folders=include_personal_folders,
        )
        baseline_payload = _prepare_validator_payload(
            baseline_payload, labels, records_by_identifier, validation_scope="base",
        )
        previous = normalize_issues(
            _sanitize_issues(
                extract_issues(baseline_payload),
                redact_document_names=redact_document_names,
                allow_raw_response_output=allow_raw_response_output,
            )
        )
        comparison_source = "base_model"
    else:
        previous, history_status = compare_history(load_json(history_in), history_context)
        # History may have been written under a less restrictive output policy.
        # Rebuild summaries from evidence under the current policy before reporting.
        previous = normalize_issues(_sanitize_issues(
            [item["raw"] for item in previous],
            redact_document_names=redact_document_names,
            allow_raw_response_output=allow_raw_response_output,
        ))
    new_items, existing_items, resolved_items = partition_issues(normalized, previous)
    report_new = [_report_issue(item, state="new", severity="error") for item in new_items]
    existing_severity = "info" if fail_on_new_only else "error"
    report_existing = [_report_issue(item, state="existing", severity=existing_severity) for item in existing_items]
    report_resolved = [_report_issue(item, state="resolved", severity="info", active=False) for item in resolved_items]
    for issue in [*report_new, *report_existing]:
        issue["validation_scope"] = "branch" if branch_id else "base"
    if comparison_source == "base_model":
        for issue in report_resolved:
            issue["validation_scope"] = "base"
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
        "history_status": history_status,
        "comparison_ambiguous_issues": sum(not item["comparison_eligible"] for item in [*normalized, *previous]),
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
            "schema_version": HISTORY_SCHEMA_VERSION,
            "generated_at": generated_at,
            **history_context,
            "issues": normalized,
        },
    )
    exit_code = 1 if (len(new_items) if fail_on_new_only else len(normalized)) > 0 else 0
    return report, exit_code


def _prepare_validator_payload(
    payload: Any,
    labels: list[str],
    records_by_identifier: dict[str, dict[str, Any]],
    *,
    validation_scope: str | None = None,
) -> Any:
    try:
        extract_issues(payload)
    except ContentEvidenceError as exc:
        exc.validation_scope = validation_scope
        raise
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
    raw = item.get("raw")
    context = {
        key: raw[key]
        for key in ("document_id", "document_identifier", "document_name", "query_name", *QUERY_IDENTITY_KEYS)
        if isinstance(raw, dict) and isinstance(raw.get(key), str)
    }
    issue_type = raw.get("issue_type") if isinstance(raw, dict) else None
    return {
        **item,
        **context,
        "validator": "content",
        "type": "content_validation_issue",
        "issue_type": issue_type if issue_type in {"dashboard_filter", "query"} else "content",
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
