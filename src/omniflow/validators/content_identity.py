"""Private, authoritative document aliases for branch Content Validator evidence."""
from __future__ import annotations

import json
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from ..exceptions import API_OPERATIONS, TRANSPORT_REASON_CODES, OmniAuthError, OmniFlowError
from ..omni_client import OmniClient, OmniReadBudget

CANONICAL_DOCUMENT = "_canonical_document_identifier"
CANONICAL_QUERY = "_canonical_query_map_key"
MAX_DRAFT_LOOKUPS = 200
LOOKUP_BUDGET_SECONDS = 60
IDENTITY_MESSAGES = {
    "document_identity_unavailable": (
        "The required dashboard draft could not be matched to an active published parent. "
        "Check that the draft still exists on the selected branch and rerun. PR impact is unknown."
    ),
    "document_identity_permission_denied": (
        "Omni denied access to a dashboard required for draft comparison. Verify the same API credential's "
        "document and draft read access, then rerun. This does not establish a new content defect."
    ),
    "document_identity_not_found": (
        "A dashboard required for draft comparison was not found or is inaccessible. "
        "Check whether it was deleted, archived, or its access changed, then rerun. PR impact is unknown."
    ),
    "document_identity_transport_unavailable": (
        "The required dashboard identity request could not complete. Review its HTTP status and transport "
        "category, restore API connectivity or availability, then rerun. PR impact is unknown."
    ),
    "document_identity_invalid_response": (
        "The required dashboard identity response was empty, malformed, or exceeded its size limit. "
        "Check the API response contract with Omni support using the run reference. PR impact is unknown."
    ),
    "document_identity_scope_unsupported": (
        "The document identity API does not support the configured acting-user scope. "
        "Use that user's PAT without user_id for the same content scope; do not broaden access to obtain a pass."
    ),
    "document_identity_wrong_branch": (
        "The required dashboard draft is not attached to the selected model branch. "
        "Check the draft's branch in Omni and rerun against the intended branch. PR impact is unknown."
    ),
    "ambiguous_document_identity": (
        "Dashboard identity evidence is conflicting or duplicated; comparison cannot safely determine new errors."
    ),
    "stale_document_draft": (
        "A dashboard draft is out of date with its published document. Update the draft in Omni and rerun validation."
    ),
    "document_identity_changed": (
        "Dashboard or draft evidence changed during validation. Rerun against an unchanged branch and published base."
    ),
    "document_identity_limit_exceeded": (
        "Dashboard draft identity lookup exceeded its safety limit. Comparison is incomplete; no findings were waived."
    ),
    "query_identity_unavailable": (
        "A dashboard draft's query identity could not be compared safely. "
        "Comparison evidence is incomplete; no query findings were waived."
    ),
}


class DocumentIdentityError(Exception):
    def __init__(self, reason: str, *, diagnostic_context: dict[str, Any] | None = None):
        self.reason_code = reason
        self.diagnostic_context = dict(diagnostic_context or {})
        super().__init__(IDENTITY_MESSAGES[reason])


def _lookup_error(exc: Exception, *, operation: str, reference: str) -> DocumentIdentityError:
    """Carry typed, allowlisted evidence, never exception text or resource IDs."""
    source = getattr(exc, "diagnostic_context", {})
    source = source if isinstance(source, dict) else {}
    cause = getattr(exc, "reason_code", None)
    status = source.get("http_status")
    reason = "document_identity_unavailable"
    if cause in IDENTITY_MESSAGES:
        reason = cause
    elif isinstance(exc, OmniAuthError) or status in (401, 403):
        reason = "document_identity_permission_denied"
    elif status == 404:
        reason = "document_identity_not_found"
    elif cause in {"empty_api_response", "invalid_json_response", "omni_response_limit_exceeded"}:
        reason = "document_identity_invalid_response"
    elif cause in {"content_impact_timeout", "content_impact_budget_exhausted"}:
        reason = "document_identity_limit_exceeded"
    elif cause in TRANSPORT_REASON_CODES:
        reason = "document_identity_transport_unavailable"
    actual_operation = source.get("api_operation")
    context = {"service": "omni", "api_operation": (
        actual_operation if actual_operation in API_OPERATIONS else operation
    ), "document_reference": reference}
    for key in ("http_status", "attempt_count", "max_attempts"):
        value = source.get(key)
        if type(value) is int and 0 < value <= 999:
            context[key] = value
    if type(source.get("retryable")) is bool:
        context["retryable"] = source["retryable"]
    if cause in TRANSPORT_REASON_CODES:
        context["transport_reason"] = cause
    if source.get("cause_code") in TRANSPORT_REASON_CODES:
        context["cause_code"] = source["cause_code"]
    return DocumentIdentityError(reason, diagnostic_context=context)


def _identifier(record: dict, key: str = "identifier") -> str | None:
    value = record.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _indexed(documents: list[dict]) -> dict[str, dict]:
    result = {}
    physical = set()
    for doc in documents:
        logical, internal = _identifier(doc), _identifier(doc, "document_id")
        if (logical and logical in result) or (internal and internal in physical):
            raise DocumentIdentityError("ambiguous_document_identity")
        if logical:
            result[logical] = doc
        if internal:
            physical.add(internal)
    return result


def _unordered_items(value: Any) -> Any:
    """Compare validated collections without order, retaining types and duplicates."""
    if not isinstance(value, list):
        return value  # Missing evidence is not equivalent to an explicit empty list.
    return sorted(value, key=lambda item: json.dumps(item, sort_keys=True))


def _filter_evidence(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    return _unordered_items([
        {**item, "issues": _unordered_items(item["issues"])}
        if isinstance(item, dict) and set(item) == {"filter_name", "issues"} else item
        for item in value
    ])


def evidence_signature(documents: list[dict]) -> list[str]:
    """Keep identities and issue occurrences, ignoring only collection ordering.

    The caller validates both responses first. Normalize comparison copies only;
    do not change API payloads, issue fingerprints, or persisted history.
    """
    return sorted(json.dumps({
        "identifier": doc.get("identifier"), "document_id": doc.get("document_id"),
        "filters": _filter_evidence(doc.get("dashboard_filter_issues")),
        "queries": sorted(json.dumps({
            **{key: query.get(key) for key in ("query_id_map_key", "query_presentation_id", "query_id")},
            "issues": _unordered_items(query.get("issues")),
        }, sort_keys=True) for query in doc.get("queries_and_issues", [])),
    }, sort_keys=True) for doc in documents)


@dataclass
class DraftIdentityProof:
    client: Any
    branch_id: str
    user_id: str | None = None
    include_personal_folders: bool = False
    mappings: dict[str, str] = field(default_factory=dict)
    snapshots: dict[str, dict] = field(default_factory=dict)
    references: dict[str, str] = field(default_factory=dict)
    _started: float = 0
    _requests: int = 0

    @contextmanager
    def bounded_lookups(self):
        # Validator calls between discovery and recheck have their own limits.
        # Each identity batch is bounded independently, including network/body reads.
        self._started, self._requests = monotonic(), 0
        # A PAT personal-scope identity read may need one initial whoami call.
        budget = OmniReadBudget(self._started + LOOKUP_BUDGET_SECONDS, 2 * MAX_DRAFT_LOOKUPS + 1)
        boundary = self.client.bounded_reads(budget) if isinstance(self.client, OmniClient) else nullcontext()
        with boundary:
            yield

    def _call(self, operation: str, draft: str, callback):
        reference = self.references.setdefault(draft, f"document-{len(self.references) + 1}")
        context = {"service": "omni", "api_operation": operation, "document_reference": reference}
        if self._requests >= 2 * MAX_DRAFT_LOOKUPS or monotonic() - self._started >= LOOKUP_BUDGET_SECONDS:
            raise DocumentIdentityError("document_identity_limit_exceeded", diagnostic_context=context)
        self._requests += 1
        try:
            result = callback()
        except (OmniFlowError, AttributeError) as exc:
            raise _lookup_error(exc, operation=operation, reference=reference) from None
        if monotonic() - self._started >= LOOKUP_BUDGET_SECONDS:
            raise DocumentIdentityError("document_identity_limit_exceeded", diagnostic_context=context)
        return result

    def parent(self, draft: str) -> str:
        identity = self._call("document_state", draft, lambda: self.client.get_document_identity(
            draft, user_id=self.user_id, include_personal_folders=self.include_personal_folders,
        ))
        if (not isinstance(identity, dict) or identity.get("identifier") != draft
                or not _identifier(identity, "publishedIdentifier")
                or identity["publishedIdentifier"] == draft):
            raise self.error("document_identity_invalid_response", draft, "document_state")
        return identity["publishedIdentifier"]

    def error(self, reason: str, draft: str, operation: str = "document_drafts") -> DocumentIdentityError:
        return DocumentIdentityError(reason, diagnostic_context={
            "service": "omni", "api_operation": operation, "document_reference": self.references[draft],
        })

    def lookup(self, published: str, draft: str, *, recheck: bool = False) -> dict:
        rows = self._call("document_drafts", draft, lambda: self.client.list_document_drafts(published))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise self.error("document_identity_invalid_response", draft)
        selected = [row for row in rows if row.get("identifier") == draft]
        if not selected:
            raise self.error("document_identity_changed" if recheck else "document_identity_unavailable", draft)
        if len(selected) != 1 or any(
            row.get("status") == "active" and row.get("branch") == {"id": self.branch_id}
            and row.get("identifier") != draft for row in rows
        ):
            raise self.error("ambiguous_document_identity", draft)
        row = selected[0]
        if row.get("publishedIdentifier") != published or draft == published:
            raise self.error("ambiguous_document_identity", draft)
        if recheck and row != self.snapshots[draft]:
            raise self.error("document_identity_changed", draft)
        if row.get("branch") != {"id": self.branch_id}:
            raise self.error("document_identity_wrong_branch", draft)
        if row.get("status") != "active":
            raise self.error("document_identity_unavailable", draft)
        if type(row.get("draftOutOfDate")) is not bool or not _identifier(row, "updatedAt"):
            raise self.error("document_identity_invalid_response", draft)
        if row["draftOutOfDate"]:
            raise self.error("stale_document_draft", draft)
        return row

    def recheck(self) -> None:
        with self.bounded_lookups():
            for draft, published in self.mappings.items():
                if self.parent(draft) != published:
                    raise self.error("document_identity_changed", draft, "document_state")
                self.lookup(published, draft, recheck=True)


def resolve_documents(*, client, branch_id: str, current: list[dict], baseline: list[dict],
                      records: list[dict], user_id: str | None = None,
                      include_personal_folders: bool = False) -> tuple[list[dict], list[dict], DraftIdentityProof]:
    """Resolve before label filtering; lookup absence never means a new document."""
    base_index, current_index = _indexed(baseline), _indexed(current)
    published = set(base_index)
    for record in records:
        identifier = _identifier(record)
        if identifier:
            published.add(identifier)
    proof = DraftIdentityProof(client, branch_id, user_id, include_personal_folders)
    unknown = set(current_index) - published
    if len(unknown) > MAX_DRAFT_LOOKUPS:
        raise DocumentIdentityError("document_identity_limit_exceeded")
    # Resolve only the unknown identifiers actually returned by this validator.
    # The direct draftOf relationship avoids scanning unrelated organization content.
    with proof.bounded_lookups():
        for draft in sorted(unknown):
            parent = proof.parent(draft)
            row = proof.lookup(parent, draft)
            proof.mappings[draft] = parent
            proof.snapshots[draft] = dict(row)
    mapped_parents = set(proof.mappings.values())

    def canonicalize(documents):
        result, seen = [], set()
        for doc in documents:
            # Never accept a canonical identity injected by an upstream response.
            copied = {k: v for k, v in doc.items() if k != CANONICAL_DOCUMENT}
            if "queries_and_issues" in copied:
                copied["queries_and_issues"] = [
                    {k: v for k, v in query.items() if k != CANONICAL_QUERY}
                    for query in copied["queries_and_issues"]
                ]
            identifier = _identifier(doc)
            if identifier:
                canonical = proof.mappings.get(identifier, identifier)
                if canonical in seen:
                    raise DocumentIdentityError("ambiguous_document_identity")
                seen.add(canonical)
                if canonical in mapped_parents:
                    copied[CANONICAL_DOCUMENT] = canonical
            result.append(copied)
        return result

    current, baseline = canonicalize(current), canonicalize(baseline)
    for doc in [*current, *baseline]:
        if doc.get(CANONICAL_DOCUMENT) not in mapped_parents:
            continue
        keys = set()
        for query in doc.get("queries_and_issues", []):
            # The documented document-patch address is the tile/map key. Live
            # probes show even unchanged drafts regenerate presentation IDs.
            key = _identifier(query, "query_id_map_key")
            if not key or key in keys:
                raise DocumentIdentityError("query_identity_unavailable")
            keys.add(key)
            query[CANONICAL_QUERY] = key
    return current, baseline, proof
