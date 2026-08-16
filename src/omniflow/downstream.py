from __future__ import annotations

from typing import Any

from .exceptions import OmniAPIError
from .omni_client import OmniClient
from .security import redact
from .timestamps import utc_now_iso


def generate_downstream_dependencies(
    *,
    client: OmniClient,
    model_id: str,
    branch_id: str | None,
    diff_result: dict[str, Any],
    user_id: str | None = None,
    include_personal_folders: bool = False,
) -> dict[str, Any]:
    searches = _searches_from_diff(diff_result)
    dependencies: list[dict[str, Any]] = []
    seen = set()
    mode = "targeted"
    coverage_gaps: list[dict[str, str]] = _coverage_gaps_from_diff(diff_result)
    for search in searches:
        try:
            payload = client.search_content_references(
                model_id,
                find=search["name"],
                find_type=search["type"],
                branch_id=branch_id,
                user_id=user_id,
                include_personal_folders=include_personal_folders,
            )
        except OmniAPIError as exc:
            mode = "targeted_partial" if dependencies else "targeted_unavailable"
            coverage_gaps.append(
                {
                    "type": search["type"],
                    "name": search["name"],
                    "message": redact(str(exc)),
                }
            )
            continue
        for dependency in _dependencies_from_payload(payload, search):
            identity = (
                dependency.get("content_id"),
                dependency.get("query_id"),
                tuple((ref.get("type"), ref.get("name")) for ref in dependency.get("references", [])),
            )
            if identity in seen:
                continue
            seen.add(identity)
            dependencies.append(dependency)
    return {
        "version": 1,
        "model_id": model_id,
        "branch_id": branch_id,
        "generated_at": utc_now_iso(),
        "generation_mode": mode,
        "searches": searches,
        "coverage_gaps": coverage_gaps,
        "dependencies": dependencies,
    }


def _searches_from_diff(diff_result: dict[str, Any]) -> list[dict[str, str]]:
    searches: list[dict[str, str]] = []
    seen = set()
    for change in diff_result.get("changes", []):
        for key in ("field", "previous_field"):
            value = change.get(key)
            if isinstance(value, str) and value:
                _append_search(searches, seen, "field", value)
        if change.get("type", "").startswith("view_") and isinstance(change.get("name"), str):
            _append_search(searches, seen, "view", change["name"])
        if change.get("type", "").startswith("topic_") and isinstance(change.get("name"), str):
            _append_search(searches, seen, "topic", change["name"])
        if str(change.get("type", "")).startswith("relationship_"):
            for view_name in change.get("affected_views", []):
                if isinstance(view_name, str) and view_name:
                    _append_search(searches, seen, "view", view_name)
    return searches


def _coverage_gaps_from_diff(diff_result: dict[str, Any]) -> list[dict[str, str]]:
    gaps = []
    for change in diff_result.get("changes", []):
        if not isinstance(change, dict):
            continue
        if str(change.get("type", "")).startswith("relationship_") and not change.get("affected_views"):
            gaps.append(
                {
                    "type": "relationship",
                    "name": str(change.get("name") or change.get("field") or ""),
                    "message": (
                        "Relationship impact could not be derived because the semantic diff did not identify joined views."
                    ),
                }
            )
    return gaps


def _append_search(searches: list[dict[str, str]], seen: set[tuple[str, str]], find_type: str, name: str) -> None:
    identity = (find_type, name)
    if identity in seen:
        return
    seen.add(identity)
    searches.append({"type": find_type, "name": name})


def _dependencies_from_payload(payload: Any, search: dict[str, str]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("content"), list):
        return []
    dependencies = []
    for document in payload["content"]:
        if not isinstance(document, dict):
            continue
        base = {
            "content_id": document.get("document_id") or document.get("id"),
            "content_identifier": document.get("identifier"),
            "content_type": document.get("type"),
            "content_name": document.get("name"),
            "content_url": document.get("url") if isinstance(document.get("url"), str) else None,
            "folder_name": document.get("folder", {}).get("name") if isinstance(document.get("folder"), dict) else None,
            "folder_path": document.get("folder", {}).get("path") if isinstance(document.get("folder"), dict) else None,
            "owner": _owner(document.get("owner")),
            "labels": _labels(document.get("labels")),
        }
        queries = document.get("queries_and_issues")
        if isinstance(queries, list) and queries:
            for query in queries:
                if not isinstance(query, dict):
                    continue
                dependencies.append(
                    {
                        **base,
                        "query_id": query.get("query_presentation_id") or query.get("query_id"),
                        "query_name": query.get("query_name"),
                        "references": [{"type": search["type"], "name": search["name"]}],
                    }
                )
        else:
            dependencies.append(
                {
                    **base,
                    "query_id": None,
                    "query_name": None,
                    "references": [{"type": search["type"], "name": search["name"]}],
                }
            )
    return dependencies


def _labels(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    labels = []
    for item in value:
        label = item.get("name") if isinstance(item, dict) else item
        if isinstance(label, str) and label:
            labels.append(label)
    return labels


def _owner(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    owner = {}
    for key in ("id", "name", "email"):
        if isinstance(value.get(key), str) and value[key]:
            owner[key] = value[key]
    return owner or None
