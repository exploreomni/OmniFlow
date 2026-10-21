from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict, deque
from copy import deepcopy
from inspect import getattr_static
from pathlib import Path
from typing import Any, Iterable

from ..exceptions import OmniAPIError, OmniFlowError
from ..omni_client import OmniClient
from ..security import redact, secure_write_text
from ..timestamps import utc_now_iso
from ..validation_scope import MAX_TARGETED_SELECTORS, ContentScopePlan, ValidationEvidence
from .content_identity import (
    CANONICAL_DOCUMENT,
    CANONICAL_QUERY,
    IDENTITY_MESSAGES,
    DocumentIdentityError,
    evidence_signature,
    resolve_documents,
)

HISTORY_SCHEMA_VERSION = 4
QUERY_IDENTITY_KEYS = ("query_presentation_id", "query_id_map_key", "query_id")
ISSUE_ENVELOPE_KEYS = ("content", "issues", "validation_issues", "errors", "documents", "items", "results")
NESTED_ERROR_KEYS = frozenset({"issues", "validation_issues", "errors"})
CONTENT_EVIDENCE_REASON_CODES = frozenset({
    "invalid_issue_container", "invalid_issue_entry", "missing_issue_message",
    "invalid_issue_message", "unsupported_issue_shape",
    "invalid_filter_identity", *IDENTITY_MESSAGES,
})


def load_json(path: str | Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise OmniAPIError("Content validation history has an invalid envelope", reason_code="invalid_content_history")
        return payload
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError):
        raise OmniAPIError("Content validation history could not be read as valid JSON; comparison evidence is unavailable",
                           reason_code="invalid_content_history") from None


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
        query: dict[str, Any] | None = None, reason_code: str = "unsupported_issue_shape",
    ) -> None:
        self.reason_code = (
            reason_code if isinstance(reason_code, str) and reason_code in CONTENT_EVIDENCE_REASON_CODES
            else "unsupported_issue_shape"
        )
        category = {"dashboard_filter": "dashboard-filter", "query": "query"}.get(issue_type, "content")
        super().__init__(IDENTITY_MESSAGES.get(self.reason_code) or (
            f"Content Validator response contains unreadable {category} issue details. "
            "Validation evidence is incomplete; a specific content defect cannot be determined. "
            f"Reason code: {self.reason_code}."
        ))
        self.issue_type = issue_type if issue_type in {"dashboard_filter", "query"} else "content"
        self.validation_scope: str | None = None
        self.comparison_mode = "unknown"
        self.requested_validation_depth = "api_default"
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
            "reason_code": self.reason_code,
            "validation_scope": self.validation_scope,
            "comparison_mode": self.comparison_mode,
            "requested_validation_depth": self.requested_validation_depth,
            "comparison_status": "unavailable",
            "content_health": "unknown",
            "regression_status": "unknown",
            "severity": "error",
            "blocking": True,
            "active": True,
            "coverage_complete": False,
            "message": str(self),
        }


def _issue_list(
    value: Any, *, issue_type: str = "content", document: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
) -> list[Any]:
    if not isinstance(value, list):
        raise ContentEvidenceError(
            issue_type=issue_type, document=document, query=query, reason_code="invalid_issue_container",
        )
    for item in value:
        # The published API uses strings; retain the existing message-object
        # compatibility, but never guess undocumented nested error structures.
        if isinstance(item, list):
            reason_code = "unsupported_issue_shape"
        elif not isinstance(item, (str, dict)):
            reason_code = "invalid_issue_entry"
        elif isinstance(item, dict) and "message" not in item:
            reason_code = "missing_issue_message"
        else:
            reason_code = "invalid_issue_message"
        message = item.get("message") if isinstance(item, dict) else item
        if not isinstance(message, str) or not message.strip():
            raise ContentEvidenceError(
                issue_type=issue_type, document=document, query=query, reason_code=reason_code,
            )
        if isinstance(item, dict) and NESTED_ERROR_KEYS.intersection(item):
            # A readable message cannot make competing nested evidence safe to
            # discard. Ordinary metadata remains supported; do not recurse into it.
            raise ContentEvidenceError(
                issue_type=issue_type, document=document, query=query, reason_code="unsupported_issue_shape",
            )
    return value


def _dashboard_filter_entries(value: Any, *, document: dict[str, Any]) -> list[dict[str, Any]]:
    """Adapt the observed grouped API form without guessing arbitrary nesting.

    A filter name is document-scoped identity, not a safe display field. Retain
    its exact value for comparisons; the public diagnostic projection omits it.
    """
    if not isinstance(value, list):
        raise ContentEvidenceError(
            issue_type="dashboard_filter", document=document, reason_code="invalid_issue_container",
        )
    entries: list[dict[str, Any]] = []
    named_groups: Counter[str] = Counter()
    for item in value:
        if isinstance(item, dict) and "issues" in item:
            # Both a message and nested issues would be competing evidence.
            # The live grouped form has exactly these two keys; do not drop an
            # unfamiliar extra structure that might contain additional errors.
            if set(item) != {"filter_name", "issues"}:
                raise ContentEvidenceError(
                    issue_type="dashboard_filter", document=document, reason_code="unsupported_issue_shape",
                )
            name = item["filter_name"]
            if not isinstance(name, str) or not name.strip():
                raise ContentEvidenceError(
                    issue_type="dashboard_filter", document=document, reason_code="invalid_filter_identity",
                )
            messages = _issue_list(item["issues"], issue_type="dashboard_filter", document=document)
            if any(not isinstance(message, str) for message in messages):
                raise ContentEvidenceError(
                    issue_type="dashboard_filter", document=document, reason_code="unsupported_issue_shape",
                )
            named_groups[name] += 1
            # An explicit empty list contains no issues; null or absent lists
            # are not equivalent and never reach this point.
            entries.extend({"message": message, "raw_issue": message, "filter_name": name} for message in messages)
            continue
        _issue_list([item], issue_type="dashboard_filter", document=document)
        entry = {"message": item.get("message") if isinstance(item, dict) else item, "raw_issue": item}
        if isinstance(item, dict) and "filter_name" in item:
            name = item["filter_name"]
            if not isinstance(name, str) or not name.strip():
                raise ContentEvidenceError(
                    issue_type="dashboard_filter", document=document, reason_code="invalid_filter_identity",
                )
            entry["filter_name"] = name
            named_groups[name] += 1
        entries.append(entry)
    for entry in entries:
        if "filter_name" in entry:
            # Repeated messages within one group remain countable occurrences;
            # multiple groups with the same name cannot establish filter identity.
            entry["comparison_eligible"] = named_groups[entry["filter_name"]] == 1
    return entries


def content_documents(payload: Any) -> list[dict[str, Any]]:
    """Validate evidence-bearing members before filtering or interpreting emptiness."""
    if not isinstance(payload, dict) or not isinstance(payload.get("content"), list):
        raise OmniAPIError("Content Validator response has an unexpected content envelope",
                           reason_code="invalid_content_response_envelope")
    for document in payload["content"]:
        if not isinstance(document, dict):
            raise OmniAPIError("Content Validator response contains an invalid document",
                               reason_code="invalid_content_document")
        for key in ("document_id", "identifier"):
            if key in document and (not isinstance(document[key], str) or not document[key].strip()):
                raise OmniAPIError("Content Validator response contains an invalid document identity",
                                   reason_code="invalid_content_document_identity")
        if "dashboard_filter_issues" in document:
            _dashboard_filter_entries(document["dashboard_filter_issues"], document=document)
        if "queries_and_issues" in document:
            queries = document["queries_and_issues"]
            if not isinstance(queries, list) or any(not isinstance(query, dict) for query in queries):
                raise OmniAPIError("Content Validator response contains an invalid query list",
                                   reason_code="invalid_content_query_list")
            for query in queries:
                if {"errors", "validation_issues"}.intersection(query):
                    raise ContentEvidenceError(
                        issue_type="query", document=document, query=query, reason_code="unsupported_issue_shape",
                    )
                for key in ("query_presentation_id", "query_id", "query_id_map_key"):
                    if key in query and (not isinstance(query[key], str) or not query[key].strip()):
                        raise OmniAPIError("Content Validator response contains an invalid query identity",
                                           reason_code="invalid_content_query_identity")
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
        if CANONICAL_DOCUMENT in document:
            doc_context[CANONICAL_DOCUMENT] = document[CANONICAL_DOCUMENT]
        dashboard_issues = document.get("dashboard_filter_issues")
        if isinstance(dashboard_issues, list):
            for item in _dashboard_filter_entries(dashboard_issues, document=document):
                issues.append(
                    {
                        **item,
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
                        **({CANONICAL_QUERY: query[CANONICAL_QUERY]} if CANONICAL_QUERY in query else {}),
                        "comparison_eligible": query_keys[_query_identity(query)] == 1,
                        **doc_context,
                    }
                )
    return issues


def extract_issues(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return _issue_list(payload)
    if not isinstance(payload, dict):
        raise OmniAPIError("Content Validator response has an unexpected envelope",
                           reason_code="invalid_content_response_envelope")
    if sum(key in payload for key in ISSUE_ENVELOPE_KEYS) > 1:
        # Never choose an empty preferred container while ignoring another
        # recognized source of errors. Unrelated response metadata is allowed.
        raise ContentEvidenceError(reason_code="unsupported_issue_shape")
    if "content" in payload:
        return collect_content_issues(payload)
    # Retain explicitly supported legacy issue envelopes, including valid empty lists.
    for key in ISSUE_ENVELOPE_KEYS[1:]:
        if key in payload:
            return _issue_list(payload[key])
    raise OmniAPIError("Content Validator response has an unexpected envelope",
                       reason_code="invalid_content_response_envelope")


def issue_identity(issue: Any) -> str:
    if isinstance(issue, str):
        value = issue
    else:
        try:
            comparable = dict(issue) if isinstance(issue, dict) else issue
            if isinstance(comparable, dict):
                # filter_name is intentionally not stripped: grouped dashboard
                # messages on different filters must never match as pre-existing.
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
                comparable.pop(CANONICAL_QUERY, None)
                for key in QUERY_IDENTITY_KEYS:
                    comparable.pop(key, None)
                if query_key is not None:
                    comparable[query_key[0]] = query_key[1]
                # A verified draft alias is the same logical document as its
                # published parent. Physical document IDs differ across drafts.
                logical = comparable.pop(CANONICAL_DOCUMENT, None)
                if isinstance(logical, str) and logical.strip():
                    comparable.pop("document_id", None)
                    comparable["document_identifier"] = logical
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
    canonical = issue.get(CANONICAL_QUERY)
    if isinstance(canonical, str) and canonical.strip():
        return "query_id_map_key", canonical
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
        raise OmniAPIError("Content Validator response is missing an identity required for label filtering",
                           reason_code="content_label_identity_unavailable")
    filtered["content"] = [
        document
        for document in documents
        if isinstance(document, dict)
        and isinstance(document.get("identifier"), str)
        and document.get(CANONICAL_DOCUMENT, document["identifier"]) in allowed_identifiers
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
        identifiers = (next_document.get(CANONICAL_DOCUMENT), next_document.get("identifier"),
                       next_document.get("document_id"))
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
        raise OmniAPIError("Content validation history has an invalid envelope", reason_code="invalid_content_history")
    if "diagnostic_schema_version" in payload:
        # Public output intentionally lacks identity evidence. Even a report
        # augmented with history-like fields must never become a trusted baseline.
        return [], "invalidated_public_diagnostic"
    if "schema_version" not in payload:
        return [], "invalidated_legacy"
    if type(payload["schema_version"]) is not int:
        raise OmniAPIError("Content validation history has an invalid schema version", reason_code="invalid_content_history")
    if payload["schema_version"] != HISTORY_SCHEMA_VERSION:
        # Older histories do not bind the origin, credential and acting identity.
        # Neither a schema upgrade nor a clean issue list can prove that scope.
        return [], "invalidated_version"
    for identity_context in (payload, context):
        if (identity_context.get("identity_contract") != "membership_v1"
                or not isinstance(identity_context.get("identity_fingerprint"), str)
                or re.fullmatch(r"[0-9a-f]{64}", identity_context["identity_fingerprint"]) is None):
            return [], "invalidated_identity"
    if any(key not in payload for key in context):
        raise OmniAPIError("Content validation history is missing comparison context", reason_code="invalid_content_history")
    if (
        not isinstance(payload["model_id"], str) or not payload["model_id"].strip()
        or any(payload[key] is not None and (not isinstance(payload[key], str) or not payload[key].strip())
               for key in ("branch_id", "user_id"))
        or type(payload["include_personal_folders"]) is not bool
        or not isinstance(payload["labels"], list)
        or any(not isinstance(label, str) or not label.strip() for label in payload["labels"])
        or not isinstance(payload.get("requested_validation_depth"), str)
        or payload["requested_validation_depth"] not in {"api_default", "full"}
    ):
        raise OmniAPIError("Content validation history has invalid comparison context", reason_code="invalid_content_history")
    issues = payload.get("issues")
    if not isinstance(issues, list):
        raise OmniAPIError("Content validation history has an invalid issue list", reason_code="invalid_content_history")
    for item in issues:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("summary"), str) or not item["summary"].strip()
            or not isinstance(item.get("raw"), (str, dict))
            or type(item.get("comparison_eligible")) is not bool
            or item.get("id") != issue_identity(item["raw"])
            or item["comparison_eligible"] != _comparison_eligible(item["raw"])
        ):
            raise OmniAPIError("Content validation history contains invalid issue evidence", reason_code="invalid_content_history")
    actual_context = {key: payload[key] for key in context}
    actual_context["labels"] = sorted(set(actual_context["labels"]))
    if actual_context != context:
        return [], "invalidated_context"
    # A missing user ID denotes the current credential's principal, not a stable
    # actor identity. Reusing its history across credential changes is unsafe.
    if context["user_id"] is None:
        return [], "invalidated_unscoped_user"
    return issues, "accepted"


def _fetch_validator_payload(
    client: OmniClient, model_id: str, *, branch_id: str | None, user_id: str | None,
    include_personal_folders: bool, comparison_mode: str, force_full_validation: bool = False,
    evidence: ValidationEvidence, refresh: bool = False, selector: tuple[str, str] | None = None,
) -> Any:
    try:
        return evidence.fetch(
            client, "validate_content", model_id, refresh=refresh, branch_id=branch_id, user_id=user_id,
            include_personal_folders=include_personal_folders,
            **({"force_full_validation": True} if force_full_validation else {}),
            **({"find_type": selector[0], "find": selector[1]} if selector else {}),
        )
    except OmniFlowError as exc:
        _attach_content_scope(
            exc, validation_scope="branch" if branch_id else "base", comparison_mode=comparison_mode,
            requested_validation_depth="full" if force_full_validation else "api_default",
        )
        raise


def _attach_content_scope(
    exc: OmniFlowError, *, validation_scope: str | None, comparison_mode: str,
    requested_validation_depth: str,
) -> None:
    """Preserve the failing request scope without changing its cause or policy."""
    exc.validation_scope = validation_scope
    exc.comparison_mode = comparison_mode if comparison_mode in {"all_issues", "new_only"} else "unknown"
    exc.requested_validation_depth = requested_validation_depth
    exc.diagnostic_context.update(
        validation_scope=exc.validation_scope, comparison_mode=exc.comparison_mode,
        requested_validation_depth=exc.requested_validation_depth,
    )


class _TargetedScopeIncomplete(Exception):
    """Valid evidence that cannot establish the experimental targeted scope."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _targeted_inventory(payload: Any) -> tuple:
    # Explicit inventory is required for targeting; optional omissions remain
    # accepted by broad validation. A valid empty search is not coverage proof.
    if not isinstance(payload, dict) or not isinstance(payload.get("content"), list) or not payload["content"]:
        raise _TargetedScopeIncomplete("targeted_inventory_unverified")
    if any(payload.get(key) for key in ("partial", "truncated", "has_more", "next_cursor")):
        raise _TargetedScopeIncomplete("targeted_inventory_unverified")
    inventory = []
    logical, physical = set(), set()
    for document in payload["content"]:
        identity = (document.get("identifier"), document.get("document_id"))
        if any(not isinstance(value, str) or not value.strip() for value in identity):
            raise _TargetedScopeIncomplete("targeted_inventory_unverified")
        if identity[0] in logical or identity[1] in physical:
            raise ContentEvidenceError(reason_code="ambiguous_document_identity")
        logical.add(identity[0])
        physical.add(identity[1])
        if not all(isinstance(document.get(key), list) for key in ("queries_and_issues", "dashboard_filter_issues")):
            raise _TargetedScopeIncomplete("targeted_inventory_unverified")
        queries = []
        for query in document["queries_and_issues"]:
            identity_values = tuple(query.get(key) or "" for key in QUERY_IDENTITY_KEYS)
            if not any(identity_values) or not isinstance(query.get("issues"), list):
                raise _TargetedScopeIncomplete("targeted_inventory_unverified")
            queries.append(identity_values)
        if len(set(queries)) != len(queries):
            raise _TargetedScopeIncomplete("targeted_inventory_unverified")
        filters = []
        for item in document["dashboard_filter_issues"]:
            if not isinstance(item, dict) or not isinstance(item.get("filter_name"), str) or "issues" not in item:
                raise _TargetedScopeIncomplete("targeted_inventory_unverified")
            filters.append(item["filter_name"])
        if len(set(filters)) != len(filters):
            raise _TargetedScopeIncomplete("targeted_inventory_unverified")
        inventory.append((*identity, tuple(sorted(queries)), tuple(sorted(filters))))
    return tuple(sorted(inventory))


def _merge_targeted_payloads(payloads: list[Any]) -> dict:
    documents = {}
    for payload in payloads:
        # Validate each individual response's identities before deduplicating
        # overlap between searches; never erase duplicates within one response.
        _targeted_inventory(payload)
        for document in payload["content"]:
            identity = (document["identifier"], document["document_id"])
            if identity in documents:
                if evidence_signature([documents[identity]]) != evidence_signature([document]):
                    raise _TargetedScopeIncomplete("targeted_overlap_inconsistent")
            else:
                documents[identity] = document
    merged = {"content": list(documents.values())}
    _targeted_inventory(merged)
    return merged


def _content_identity_context(
    client: Any, *, user_id: str | None, include_personal_folders: bool,
) -> dict[str, Any]:
    # Older adapters and lightweight test clients can still perform a live
    # comparison, but cannot manufacture a reusable identity-bound history.
    if (getattr_static(client, "content_identity_context", None) is None
            or getattr_static(client, "validation_evidence_binding", None) is None):
        return {"identity_contract": "membership_v1", "identity_fingerprint": None}
    reader = client.content_identity_context
    value = reader(user_id=user_id, include_personal_folders=include_personal_folders) if callable(reader) else None
    if (not isinstance(value, dict) or value.get("identity_contract") != "membership_v1"
            or not isinstance(value.get("identity_fingerprint"), str)
            or re.fullmatch(r"[0-9a-f]{64}", value["identity_fingerprint"]) is None):
        raise OmniAPIError("Content validation identity could not be verified for this run",
                           reason_code="validation_identity_changed")
    return deepcopy(value)


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
    force_full_validation: bool = False,
    max_samples: int = 20,
    redact_document_names: bool = False,
    allow_raw_response_output: bool = False,
    scope_plan: ContentScopePlan | None = None,
    evidence: ValidationEvidence | None = None,
) -> tuple[dict[str, Any], int]:
    comparison_mode = "new_only" if fail_on_new_only else "all_issues"
    requested_depth = "full" if force_full_validation else "api_default"
    snapshots = evidence if evidence is not None else ValidationEvidence()
    initial_labels = tuple(sorted(set(labels)))
    initial_identity: dict[str, Any] | None = None

    def check_identity(scope=branch_id):
        nonlocal initial_identity
        try:
            snapshots.assert_client_binding(client)
            current = _content_identity_context(
                client, user_id=user_id, include_personal_folders=include_personal_folders,
            )
            snapshots.assert_client_binding(client)
            if initial_identity is None:
                initial_identity = current
            if current != initial_identity or tuple(sorted(set(labels))) != initial_labels:
                raise OmniAPIError("Content validation identity or scope changed during this run; rerun validation",
                                   reason_code="validation_identity_changed")
        except OmniFlowError as exc:
            _attach_content_scope(
                exc, validation_scope="branch" if scope else "base", comparison_mode=comparison_mode,
                requested_validation_depth=requested_depth,
            )
            raise

    check_identity()

    def read_catalog(**params):
        check_identity()
        try:
            return client.list_content(**params)
        finally:
            check_identity()

    plan = scope_plan if scope_plan is not None else ContentScopePlan()
    if plan.mode == "targeted" and not (branch_id and fail_on_new_only):
        plan = ContentScopePlan(reason="strict_health_scope")
    if plan.mode == "targeted" and (
        plan.reason != "qualified_semantic_scope"
        or not 1 <= len(plan.selectors) <= MAX_TARGETED_SELECTORS
        or len(set(plan.selectors)) != len(plan.selectors)
        or any(kind not in {"FIELD", "VIEW", "TOPIC"} or not isinstance(name, str) or not name.strip()
               for kind, name in plan.selectors)
    ):
        plan = ContentScopePlan(reason="semantic_coverage_unverified")

    def read_scope(scope, *, refresh=False):
        check_identity(scope)
        payloads = []
        for selector in plan.selectors if plan.mode == "targeted" else (None,):
            value = _fetch_validator_payload(
                client, model_id, branch_id=scope, user_id=user_id,
                include_personal_folders=include_personal_folders, comparison_mode=comparison_mode,
                force_full_validation=force_full_validation, evidence=snapshots, refresh=refresh, selector=selector,
            )
            check_identity(scope)
            payloads.append(_prepare_validator_payload(
                value, [], {}, validation_scope="branch" if scope else "base", comparison_mode=comparison_mode,
                requested_validation_depth=requested_depth,
            ))
        try:
            return _merge_targeted_payloads(payloads) if plan.mode == "targeted" else payloads[0]
        except ContentEvidenceError as exc:
            exc.validation_scope = "branch" if scope else "base"
            exc.comparison_mode = comparison_mode
            exc.requested_validation_depth = requested_depth
            raise

    baseline_needed = branch_id and (fail_on_new_only or labels)
    try:
        # Capture published debt before evaluating the candidate. No global cache
        # or persisted history substitutes for this same-run baseline.
        baseline_payload = read_scope(None) if baseline_needed else None
        payload = read_scope(branch_id)
        if plan.mode == "targeted" and _targeted_inventory(payload) != _targeted_inventory(baseline_payload):
            raise _TargetedScopeIncomplete("targeted_inventory_changed")
    except _TargetedScopeIncomplete as exc:
        plan = ContentScopePlan(reason=exc.reason)
        # Reacquire a fresh, symmetric broad comparison; never turn missing or
        # partially returned targeted documents into clean/resolved evidence.
        baseline_payload = read_scope(None, refresh=True) if baseline_needed else None
        payload = read_scope(branch_id, refresh=True)
    records = read_catalog(
        labels=labels,
        branch_id=branch_id,
        include_personal_folders=include_personal_folders,
        user_id=user_id,
    )
    if labels and any(
        not isinstance(record, dict) or not isinstance(record.get("identifier"), str) or not record["identifier"].strip()
        for record in records
    ):
        raise OmniAPIError("Content metadata is missing an identity required for label filtering",
                           reason_code="content_label_identity_unavailable")
    records_by_identifier = index_content_records(records)
    if baseline_needed:
        if (isinstance(payload, dict) and "content" in payload
                and isinstance(baseline_payload, dict) and "content" in baseline_payload):
            try:
                current_docs = content_documents(payload)
                base_docs = content_documents(baseline_payload)
                identity_records = records
                # Label-selected metadata alone cannot identify an unselected
                # draft or prove that a previously healthy/new document is new.
                if labels:
                    identity_records = read_catalog(
                        labels=[], branch_id=branch_id, include_personal_folders=include_personal_folders,
                        user_id=user_id,
                    )
                check_identity()
                try:
                    current, base, proof = resolve_documents(
                        client=client, branch_id=branch_id, current=current_docs, baseline=base_docs,
                        records=identity_records,
                    )
                finally:
                    check_identity()
                if proof.mappings:
                    # Bracket the actual validator evidence with mapping/version
                    # reads. A changed draft must never reuse stale error waivers.
                    for scope, before in ((None, base_docs), (branch_id, current_docs)):
                        after = read_scope(scope, refresh=True)
                        if evidence_signature(before) != evidence_signature(content_documents(after)):
                            raise DocumentIdentityError("document_identity_changed")
                    try:
                        proof.recheck()
                    finally:
                        check_identity()
                    if labels:
                        after_records = read_catalog(
                            labels=labels, branch_id=branch_id, include_personal_folders=include_personal_folders,
                            user_id=user_id,
                        )
                        if set(index_content_records(after_records)) != set(records_by_identifier):
                            raise DocumentIdentityError("document_identity_changed")
                payload = {**payload, "content": current}
                baseline_payload = {**baseline_payload, "content": base}
            except DocumentIdentityError as exc:
                error = ContentEvidenceError(reason_code=exc.reason_code)
                error.validation_scope = "branch"
                error.comparison_mode = comparison_mode
                error.requested_validation_depth = requested_depth
                raise error from None
            except ContentEvidenceError as exc:
                if exc.validation_scope is None:
                    exc.validation_scope = "branch"
                exc.comparison_mode = comparison_mode
                exc.requested_validation_depth = requested_depth
                raise
    payload = _prepare_validator_payload(
        payload, labels, records_by_identifier, validation_scope="branch" if branch_id else "base",
        comparison_mode=comparison_mode,
        requested_validation_depth=requested_depth,
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
        "requested_validation_depth": requested_depth,
        "identity_contract": initial_identity["identity_contract"],
        "identity_fingerprint": initial_identity["identity_fingerprint"],
    }
    history_status = "not_used_live_base"
    if branch_id and fail_on_new_only:
        baseline_payload = _prepare_validator_payload(
            baseline_payload, labels, records_by_identifier, validation_scope="base",
            comparison_mode=comparison_mode,
            requested_validation_depth=requested_depth,
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
    baseline_available = comparison_source == "base_model" or history_status == "accepted"
    ambiguous_issues = sum(not item["comparison_eligible"] for item in [*normalized, *previous])
    comparison_status = (
        "baseline_unavailable" if not baseline_available else "ambiguous" if ambiguous_issues else "complete"
    )
    report_new = [_report_issue(item, state="new", severity="error") for item in new_items]
    report_existing = [_report_issue(item, state="existing", severity="error") for item in existing_items]
    report_resolved = [_report_issue(item, state="resolved", severity="info", active=False) for item in resolved_items]
    for issue in report_new:
        issue["blocking"] = True
    for issue in report_existing:
        issue["blocking"] = not fail_on_new_only
    for issue in report_resolved:
        issue["blocking"] = False
    evidence = []
    if fail_on_new_only and comparison_status != "complete":
        evidence.append({
            "validator": "content", "type": "content_evidence_unavailable",
            "reason_code": "content_comparison_unavailable", "comparison_status": comparison_status,
            "comparison_mode": comparison_mode, "regression_status": "unknown",
            "requested_validation_depth": requested_depth,
            "severity": "error", "blocking": True, "active": True, "coverage_complete": False,
            "message": "Content comparison evidence is unavailable or ambiguous; no regression conclusion can be made.",
        })
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
        "current_issues": len(normalized),
        "baseline_issues": len(previous) if baseline_available else None,
        "new_issues": len(new_items),
        "existing_issues": len(existing_items),
        "resolved_issues": len(resolved_items),
        "comparison_source": comparison_source,
        "comparison_mode": comparison_mode,
        "requested_validation_depth": requested_depth,
        "content_scope_mode": plan.mode,
        "content_scope_reason": plan.reason,
        "content_selector_count": len(plan.selectors),
        "comparison_status": comparison_status,
        "content_health": "issues_in_scope" if normalized else "no_issues_in_scope",
        "regression_status": (
            ("new_issues_detected" if new_items else "no_new_issues_detected")
            if comparison_status == "complete" else "unknown"
        ),
        "history_status": history_status,
        "comparison_ambiguous_issues": ambiguous_issues,
        "blocking_issues": len(report_new) + (0 if fail_on_new_only else len(report_existing)) + len(evidence),
        "validation_complete": not evidence,
        "execution_status": "unavailable" if evidence else "completed",
        "issues": [*report_new, *report_existing, *report_resolved, *evidence],
        "new_issue_samples": report_new[:max_samples],
        "existing_issue_samples": report_existing[:max_samples],
        "resolved_issue_samples": report_resolved[:max_samples],
        "note": (
            "Omni validates content for the selected model server-side; label filtering is applied locally "
            "after content metadata lookup. Requested validation depth records the request, not observed "
            "exhaustive coverage. Validation complete means available evidence was processed."
        ),
    }
    check_identity()
    write_json(report_out, redact(report))
    if plan.mode != "targeted":
        # A partial model scope must never become a reusable whole-model history.
        check_identity()
        write_json(
            history_out,
            {
                "schema_version": HISTORY_SCHEMA_VERSION,
                "generated_at": generated_at,
                **history_context,
                "issues": normalized,
            },
        )
    exit_code = 4 if evidence else 1 if (len(new_items) if fail_on_new_only else len(normalized)) > 0 else 0
    return report, exit_code


def _prepare_validator_payload(
    payload: Any,
    labels: list[str],
    records_by_identifier: dict[str, dict[str, Any]],
    *,
    validation_scope: str | None = None,
    comparison_mode: str = "unknown",
    requested_validation_depth: str = "api_default",
) -> Any:
    try:
        extract_issues(payload)
    except OmniFlowError as exc:
        _attach_content_scope(
            exc, validation_scope=validation_scope, comparison_mode=comparison_mode,
            requested_validation_depth=requested_validation_depth,
        )
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
        for key in ("document_id", "document_identifier", "document_name", "query_name", "filter_name", *QUERY_IDENTITY_KEYS)
        if isinstance(raw, dict) and isinstance(raw.get(key), str)
    }
    issue_type = raw.get("issue_type") if isinstance(raw, dict) else None
    return {
        **item,
        **context,
        "validator": "content",
        "type": "content_validation_issue",
        # Categories follow the verified response member, never a guess from
        # untrusted free text. Specific underlying defects remain unknown.
        "reason_code": {
            "dashboard_filter": "content_dashboard_filter_validation_issue",
            "query": "content_query_validation_issue",
        }.get(issue_type, "content_validation_issue"),
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
