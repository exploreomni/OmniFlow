"""Private, authoritative document aliases for branch Content Validator evidence."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from ..exceptions import OmniFlowError

CANONICAL_DOCUMENT = "_canonical_document_identifier"
CANONICAL_QUERY = "_canonical_query_map_key"
MAX_DRAFT_LOOKUPS = 200
LOOKUP_BUDGET_SECONDS = 60
IDENTITY_MESSAGES = {
    "document_identity_unavailable": (
        "Dashboard draft identity could not be verified. Check content and draft read permissions and rerun. "
        "This does not establish a new content defect."
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
    def __init__(self, reason: str):
        self.reason_code = reason
        super().__init__(IDENTITY_MESSAGES[reason])


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


def evidence_signature(documents: list[dict]) -> list[str]:
    """Keep identities and issues, not names, owners, URLs or response timestamps."""
    return sorted(json.dumps({
        "identifier": doc.get("identifier"), "document_id": doc.get("document_id"),
        "filters": doc.get("dashboard_filter_issues"),
        "queries": sorted(json.dumps({key: query.get(key) for key in (
            "query_id_map_key", "query_presentation_id", "query_id", "issues",
        )}, sort_keys=True) for query in doc.get("queries_and_issues", [])),
    }, sort_keys=True) for doc in documents)


@dataclass
class DraftIdentityProof:
    client: Any
    branch_id: str
    mappings: dict[str, str] = field(default_factory=dict)
    snapshots: dict[str, list[dict]] = field(default_factory=dict)

    def lookup(self, published: str) -> list[dict]:
        try:
            rows = self.client.list_document_drafts(published)
        except (OmniFlowError, AttributeError):
            raise DocumentIdentityError("document_identity_unavailable") from None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise DocumentIdentityError("document_identity_unavailable")
        seen, branch_ids = set(), set()
        for row in rows:
            identifier = _identifier(row)
            branch = row.get("branch")
            if (not identifier or row.get("publishedIdentifier") != published
                    or identifier == published or identifier in seen):
                raise DocumentIdentityError("ambiguous_document_identity")
            seen.add(identifier)
            if row.get("status") != "active":
                raise DocumentIdentityError("document_identity_unavailable")
            if branch is not None:
                bid = _identifier(branch, "id") if isinstance(branch, dict) else None
                if not bid or bid in branch_ids:
                    raise DocumentIdentityError("ambiguous_document_identity")
                branch_ids.add(bid)
            if type(row.get("draftOutOfDate")) is not bool or not _identifier(row, "updatedAt"):
                raise DocumentIdentityError("document_identity_unavailable")
        return rows

    def recheck(self) -> None:
        for published, before in self.snapshots.items():
            after = self.lookup(published)
            # Ignore unrelated branch drafts; compare the exact selected mapping,
            # version, and stale/status evidence that justified issue suppression.
            def selected(rows):
                return [row for row in rows if row.get("branch") == {"id": self.branch_id}]
            if selected(before) != selected(after):
                raise DocumentIdentityError("document_identity_changed")


def resolve_documents(*, client, branch_id: str, current: list[dict], baseline: list[dict],
                      records: list[dict]) -> tuple[list[dict], list[dict], DraftIdentityProof]:
    """Resolve before label filtering; lookup absence never means a new document."""
    base_index, current_index = _indexed(baseline), _indexed(current)
    published = set(base_index)
    for record in records:
        identifier = _identifier(record)
        if identifier:
            published.add(identifier)
    proof = DraftIdentityProof(client, branch_id)
    unknown = set(current_index) - published
    # Start with published documents absent from branch output, including healthy
    # documents. Their absence is a search priority, never proof of a relationship.
    search = sorted(published - set(current_index)) + sorted(published & set(current_index))
    started = monotonic()
    for count, parent in enumerate(search):
        if not unknown:
            break
        if count >= MAX_DRAFT_LOOKUPS or monotonic() - started >= LOOKUP_BUDGET_SECONDS:
            raise DocumentIdentityError("document_identity_limit_exceeded")
        rows = proof.lookup(parent)
        for row in rows:
            draft = row["identifier"]
            if draft not in unknown:
                continue
            if row.get("branch") != {"id": branch_id}:
                raise DocumentIdentityError("document_identity_unavailable")
            if row["draftOutOfDate"]:
                raise DocumentIdentityError("stale_document_draft")
            proof.mappings[draft] = parent
            proof.snapshots[parent] = rows
            unknown.remove(draft)
    if unknown:
        raise DocumentIdentityError("document_identity_unavailable")
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
