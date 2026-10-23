"""Bounded, read-only published-content enrichment of static column lineage.

This is advisory discovery, not a PR validator or a promise of complete tenant
lineage. Private identifiers are retained only in ``impacts``; ``summary`` and
fixed coverage reasons are safe for the public report's separate allowlist.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .artifact_identity import ArtifactIdentityError, match_artifacts
from .discovery import ModelContext
from .exceptions import ConfigError, OmniFlowError, exception_diagnostic
from .model_paths import authored_file_identity
from .omni_client import OmniReadBudget
from .validators.content import content_documents
from .view_identity import normalize_api_view_names
from .yaml_pull import _extract_file_map
from .yaml_security import MAX_YAML_FILE_BYTES, MAX_YAML_FILES, MAX_YAML_TOTAL_BYTES

MAX_DOCUMENTS = 500
MAX_QUERY_RECORDS = 10_000
MAX_LAYOUT_NODES = 10_000


class _ImpactSession:
    """Private single-run metadata cache; never reusable permission authority."""

    def __init__(self, contexts, client_factory, settings, client_verifier):
        self.contexts = contexts
        self.client_factory = client_factory
        self.client_verifier = client_verifier
        self.budget = OmniReadBudget(
            time.monotonic() + settings.timeout_seconds,
            settings.max_searches + 2 * MAX_DOCUMENTS + 3 * len(contexts),
        )
        self.inventories = {}
        self.failures = {}
        self.source_files = {}

    def source_key(self, context, path, scope):
        return context.base_url, context.model_id, context.model_path, scope, path

    def source(self, context, path, scope, reader):
        key = self.source_key(context, path, scope)
        self.budget.remaining()
        if key not in self.source_files:
            self.source_files[key] = reader(context, path, scope)
        return self.source_files[key]

    def inventory(self, context):
        key = (context.base_url, context.model_id, context.model_path, context.environment)
        self.budget.remaining()
        if key in self.failures:
            raise self.failures[key]
        if key not in self.inventories:
            try:
                client = self.client_factory(context)
                with client.bounded_reads(self.budget):
                    if self.client_verifier is not None:
                        self.client_verifier(context, client)
                    payload = client.get_model_yaml(context.model_id, branch_id=None, mode="combined",
                                                    include_checksums=False, fully_resolved=False)
                    files, names = _view_inventory(payload)
                self.inventories[key] = (client, files, names)
            except (OmniFlowError, ValueError, TypeError, KeyError) as exc:
                self.failures[key] = exc
                raise
        return self.inventories[key]


def collect_and_enrich_content_impact(
    *, collect_static: Callable, contexts: list[ModelContext], client_factory: Callable,
    settings: Any, user_id: str | None = None, include_personal_folders: bool = False,
    source_file_reader: Callable[[ModelContext, str, str], str] | None = None,
    client_verifier: Callable | None = None,
) -> dict[str, Any]:
    """Resolve authored canonical references and enrich within one read budget.

    ``collect_static(resolver)`` calls ``resolver(scope, model_root, files)`` only
    once a semantic dbt delta needs a graph. Files are a complete immutable
    repository-relative source inventory. When values are parsed YAML, the
    trusted ``source_file_reader`` supplies the original text for typed matching.
    Returned mappings retain original repository paths; no canonical alias is
    synthesized from underscores, basenames or physical-name concatenation.
    """
    if not getattr(settings, "enabled", False):
        return enrich_content_impact(static_report={}, contexts=contexts, client_factory=client_factory,
                                     settings=settings)
    session = _ImpactSession(contexts, client_factory, settings, client_verifier)
    resolver_gaps = []

    def resolver(scope, model_root, files):
        try:
            session.budget.remaining()
            matches = [context for context in contexts if context.model_path == model_root]
            if scope not in {"base", "head"} or len(matches) != 1 or not isinstance(files, dict):
                raise ConfigError("Canonical field scope is unavailable.",
                                  reason_code="content_impact_model_scope_ambiguous")
            context = matches[0]
            source, original_paths = {}, {}
            for path, payload in files.items():
                if not _safe_path(path):
                    raise ConfigError("Authored source identity is unavailable.")
                if model_root == ".":
                    relative = path
                elif path.startswith(model_root + "/"):
                    relative = path[len(model_root) + 1:]
                else:
                    continue
                session.budget.remaining()
                text = (session.source(context, relative, scope, source_file_reader)
                        if source_file_reader is not None else payload)
                if not isinstance(text, str) or len(text.encode()) > MAX_YAML_FILE_BYTES:
                    raise ConfigError("Original immutable authored text is required for canonical identity.")
                source[relative] = text
                session.source_files[session.source_key(context, relative, scope)] = text
                original_paths[relative] = path
            if (not source or len(source) > MAX_YAML_FILES
                    or sum(len(text.encode()) for text in source.values()) > MAX_YAML_TOTAL_BYTES):
                raise ConfigError("Authored source inventory is unavailable.")
            _, published, names = session.inventory(context)
            pairs = match_artifacts(source, published, omni_view_names=names).pairs
            inverse = {path: name for name, path in names.items()}
            return {original_paths[path]: inverse[target] for path, target in pairs.items() if target in inverse}
        except (OmniFlowError, ArtifactIdentityError, ValueError, TypeError, KeyError) as exc:
            _gap(resolver_gaps, "content_impact_field_identity_unavailable", exc=exc)
            # The collector records its own graph-coverage gap; never return an
            # empty mapping that could be misread as a fully resolved graph.
            raise ConfigError("Published canonical view identity is unavailable.",
                              reason_code="content_impact_field_identity_unavailable") from exc

    static = collect_static(resolver)
    result = enrich_content_impact(
        static_report=static, contexts=contexts, client_factory=client_factory, settings=settings,
        user_id=user_id, include_personal_folders=include_personal_folders,
        source_file_reader=source_file_reader, client_verifier=client_verifier, _session=session,
    )
    if resolver_gaps:
        result["coverage_gaps"].extend(resolver_gaps)
        result["coverage_complete"] = False
        result["status"] = "partial" if result["summary"]["searches"] else "unavailable"
    return result


def enrich_content_impact(
    *, static_report: dict[str, Any], contexts: list[ModelContext], client_factory: Callable,
    settings: Any, user_id: str | None = None, include_personal_folders: bool = False,
    source_file_reader: Callable[[ModelContext, str, str], str] | None = None,
    client_verifier: Callable | None = None, _session: _ImpactSession | None = None,
) -> dict[str, Any]:
    """Search individual fields in published scope; never enumerate all content.

    ``source_file_reader(context, relative_path, base_or_head)`` must read the
    trusted immutable Git revision, never the current filesystem or live branch.
    Its text is used only for typed artifact identity and is never returned.
    ``client_factory(context)`` must return an OmniClient-compatible client.
    """
    result = {
        "schema_version": 1, "scope": "published", "advisory": True,
        "status": "unavailable", "coverage_complete": False,
        "coverage_gaps": [], "impacts": [],
        "summary": dict.fromkeys(("affected_fields", "dashboards", "workbooks", "queries",
                                  "dashboard_filters", "searches", "unverified_documents"), 0),
    }
    gaps = result["coverage_gaps"]
    if not getattr(settings, "enabled", False):
        _gap(gaps, "content_impact_disabled")
        return result
    if (not isinstance(static_report, dict) or static_report.get("schema_version") != 1
            or not isinstance(static_report.get("impacts"), list)):
        _gap(gaps, "content_impact_static_invalid")
        return result
    if static_report.get("coverage_complete") is not True or static_report.get("coverage_gaps"):
        _gap(gaps, "content_impact_static_incomplete")
        for gap in static_report.get("coverage_gaps", [])[:1000]:
            if isinstance(gap, dict) and gap.get("reason_code") in _STATIC_REASONS:
                item = {"reason_code": gap["reason_code"]}
                for key in ("model_path", "file", "field", "relation", "dbt_model", "source_revision"):
                    if isinstance(gap.get(key), str):
                        item[key] = gap[key][:1024]
                gaps.append(item)
    max_searches = settings.max_searches
    timeout = settings.timeout_seconds
    if (type(max_searches) is not int or not 1 <= max_searches <= 1000
            or type(timeout) not in {int, float} or not 0 < timeout <= 600):
        _gap(gaps, "content_impact_budget_invalid")
        return result
    # Bound metadata lookups as well as searches, globally across models. Identity
    # lookup and YAML reads consume the same budget; no hidden pagination/retries.
    session = _session or _ImpactSession(contexts, client_factory, settings, client_verifier)
    budget = session.budget
    grouped_contexts: dict[str, list[ModelContext]] = defaultdict(list)
    for context in contexts:
        grouped_contexts[context.model_path].append(context)
    grouped_impacts: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for index, impact in enumerate(static_report["impacts"]):
        if not _valid_impact(impact):
            _gap(gaps, "content_impact_static_invalid", impact_index=index)
            continue
        entry = {**impact, "canonical_field": None, "documents": [], "coverage_complete": False}
        result["impacts"].append(entry)
        grouped_impacts[impact["model_path"]].append((index, entry))
    unique_fields: set[tuple[str, str, str]] = set()
    dashboards, workbooks, queries, filters, unverified = set(), set(), set(), set(), set()
    document_count = 0
    completed_searches = 0
    for model_path, entries in grouped_impacts.items():
        matches = grouped_contexts.get(model_path, [])
        if len(matches) != 1:
            _gap(gaps, "content_impact_model_scope_ambiguous")
            continue
        context = matches[0]
        try:
            budget.remaining()
            client, files, names = session.inventory(context)
            with client.bounded_reads(budget):
                searches: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
                for index, entry in entries:
                    try:
                        budget.remaining()
                        reader = None
                        if (source_file_reader is not None
                                or session.source_key(context, entry["view_file"], entry["source_revision"])
                                in session.source_files):
                            def reader(context, path, scope):
                                return session.source(context, path, scope, source_file_reader)
                        field = _canonical_field(context, entry, files, names, reader)
                    except (OmniFlowError, ArtifactIdentityError, ValueError, KeyError):
                        _gap(gaps, "content_impact_field_identity_unavailable", impact_index=index)
                        continue
                    entry["canonical_field"] = field
                    searches[field].append((index, entry))
                    unique_fields.add((context.base_url, context.model_id, field))
                # The full saved definition/layout is cached only within this run
                # and one model/actor scope. Search evidence is always field-specific.
                document_cache: dict[str, tuple[Any, Any, list[dict[str, Any]]]] = {}
                for field, field_entries in searches.items():
                    index = field_entries[0][0]
                    if result["summary"]["searches"] >= max_searches:
                        _gap(gaps, "content_impact_budget_exhausted", impact_index=index)
                        continue
                    before_gaps = len(gaps)
                    try:
                        budget.remaining()
                        result["summary"]["searches"] += 1
                        found = client.search_content_references(
                            context.model_id, find=field, find_type="FIELD", branch_id=None,
                            user_id=user_id, include_personal_folders=include_personal_folders,
                        )
                        documents = content_documents(found)
                        completed_searches += 1
                    except OmniFlowError as exc:
                        _gap(gaps, "content_impact_search_unavailable", exc=exc, impact_index=index)
                        continue
                    evidence = []
                    seen_documents = set()
                    for document in documents:
                        document_id = _document_id(document)
                        if document_id is None:
                            _gap(gaps, "content_impact_document_identity_unavailable", impact_index=index)
                            continue
                        if document_id in seen_documents:
                            continue
                        seen_documents.add(document_id)
                        identity = (context.base_url, context.model_id, document_id)
                        if str(document.get("type", "")).lower() in {"draft", "document_draft"}:
                            _gap(gaps, "content_impact_draft_document", impact_index=index)
                            unverified.add(identity)
                            continue
                        if document_id not in document_cache:
                            if document_count >= MAX_DOCUMENTS:
                                _gap(gaps, "content_impact_budget_exhausted", impact_index=index)
                                unverified.add(identity)
                                continue
                            document_count += 1
                            document_cache[document_id] = _read_document(
                                client, budget, document_id, user_id, include_personal_folders,
                            )
                        query_payload, state, read_gaps = document_cache[document_id]
                        for gap in read_gaps:
                            gaps.append({**gap, "impact_index": index})
                        item, item_gaps = _document_evidence(
                            document, query_payload, state, field, context.model_id, context.base_url,
                        )
                        evidence.append(item)
                        for reason in item_gaps:
                            _gap(gaps, reason, impact_index=index)
                        if read_gaps or item_gaps:
                            unverified.add(identity)
                        for query in item["queries"]:
                            queries.add((*identity, query["query_key"]))
                            if query["usage"] == "dashboard":
                                dashboards.add(identity)
                            elif query["usage"] == "workbook":
                                workbooks.add(identity)
                        for control in item["dashboard_filters"]:
                            filters.add((*identity, control["control_id"]))
                            dashboards.add(identity)
                    for _, entry in field_entries:
                        entry["documents"] = evidence
                        entry["coverage_complete"] = len(gaps) == before_gaps
        except OmniFlowError as exc:
            _gap(gaps, "content_impact_model_unavailable", exc=exc)
        except (ValueError, TypeError, KeyError):
            _gap(gaps, "content_impact_field_identity_unavailable")
    result["summary"].update(affected_fields=len(unique_fields), dashboards=len(dashboards), workbooks=len(workbooks),
                             queries=len(queries), dashboard_filters=len(filters), unverified_documents=len(unverified))
    result["coverage_complete"] = not gaps
    result["status"] = "complete" if not gaps else "partial" if completed_searches else "unavailable"
    return result


def _gap(gaps, reason, *, exc=None, impact_index=None, cause=None):
    gap = {"reason_code": reason}
    if impact_index is not None:
        gap["impact_index"] = impact_index
    if exc is not None:
        detail = exception_diagnostic(exc)
        # No exception messages, URLs, keys or response bodies enter safe gaps.
        cause = detail.get("reason_code")
        for key in ("http_status", "attempt_count", "max_attempts"):
            if type(detail.get(key)) is int:
                gap[key] = detail[key]
    if isinstance(cause, str) and (cause.startswith("content_impact_") or cause in _SAFE_CAUSES):
        gap["cause_code"] = cause
    gaps.append(gap)


_SAFE_CAUSES = frozenset({
    "omni_unauthorized", "omni_forbidden", "omni_rate_limited", "omni_service_unavailable", "omni_http_error",
    "omni_timeout", "omni_tls_error", "omni_connection_error", "omni_request_failed", "omni_response_interrupted",
    "omni_response_limit_exceeded", "empty_api_response", "invalid_json_response", "content_identity_invalid",
    "content_pat_impersonation_unsupported", "content_actor_unresolved", "content_personal_actor_unresolved",
    "content_personal_user_required", "validation_identity_changed",
})

_STATIC_REASONS = frozenset({
    "dbt_sql_unsupported", "dbt_sql_dynamic_projection", "dbt_sql_wildcard", "dbt_sql_output_identity_unavailable",
    "dbt_sql_duplicate_output", "impact_evidence_limit", "omni_inheritance_unsupported", "omni_model_scope_ambiguous",
    "omni_query_view_unsupported", "omni_field_identity_ambiguous", "omni_field_expression_unsupported",
    "omni_field_filter_unsupported", "omni_field_reference_ambiguous", "omni_field_reference_unresolved",
    "dbt_manifest_unavailable", "dbt_model_identity_ambiguous", "dbt_relation_ambiguous", "omni_field_dependency_cycle",
    "omni_field_dependency_depth", "dbt_manifest_contract_unavailable", "dbt_base_revision_unavailable",
    "omni_root_unavailable", "omni_view_evidence_unavailable", "dbt_source_unsupported", "dbt_manifest_only_change",
    "dbt_source_unavailable", "omni_view_identity_unavailable", "dbt_relation_context_unavailable",
    "dbt_relation_identity_unavailable", "omni_relation_identity_unavailable", "dbt_relation_case_ambiguous",
})


def _safe_path(path):
    return (isinstance(path, str) and bool(path) and len(path) <= 1024 and path == path.strip()
            and not path.startswith("/") and "\\" not in path and ".." not in PurePosixPath(path).parts
            and not any(ord(char) < 32 or ord(char) == 127 for char in path))


def _valid_impact(impact):
    return (isinstance(impact, dict) and _safe_path(impact.get("model_path"))
            and _safe_path(impact.get("view_file")) and isinstance(impact.get("field_name"), str)
            and 0 < len(impact["field_name"]) <= 1024 and "." not in impact["field_name"]
            and not any(ord(char) < 32 for char in impact["field_name"])
            and impact.get("source_revision") in {"base", "head"})


def _view_inventory(payload):
    if not isinstance(payload, dict):
        raise ValueError("Invalid model metadata")
    files = _extract_file_map(payload)
    if (not files or len(files) > MAX_YAML_FILES
            or any(len(text.encode()) > MAX_YAML_FILE_BYTES for text in files.values())
            or sum(len(text.encode()) for text in files.values()) > MAX_YAML_TOTAL_BYTES):
        raise ValueError("Invalid model inventory")
    names = normalize_api_view_names(payload.get("viewNames"), files)
    return files, names


def _canonical_field(context, entry, files, names, reader):
    path = entry["view_file"]
    if reader is not None:
        text = reader(context, path, entry["source_revision"])
        if not isinstance(text, str) or len(text.encode()) > MAX_YAML_FILE_BYTES:
            raise ValueError("Unavailable source")
        pairs = match_artifacts({path: text}, files, omni_view_names=names).pairs
        api_path = pairs.get(path)
    else:
        # Without immutable source evidence, do not infer moved qualified views.
        candidates = [name for name in files if authored_file_identity(name) == authored_file_identity(path)]
        api_path = candidates[0] if len(candidates) == 1 else None
    canonical = [name for name, target in names.items() if target == api_path]
    if len(canonical) != 1:
        raise ValueError("Unavailable canonical identity")
    return f"{canonical[0]}.{entry['field_name']}"


def _document_id(document):
    for key in ("identifier", "document_id", "id"):
        value = document.get(key)
        if isinstance(value, str) and value and len(value) <= 256 and value == value.strip():
            return value
    return None


def _read_document(client, budget, document_id, user_id, include_personal):
    values, gaps = [], []
    for method, reason in ((client.get_document_queries, "content_impact_queries_unavailable"),
                           (client.get_document_state, "content_impact_layout_unavailable")):
        try:
            budget.remaining()
            values.append(method(document_id, user_id=user_id, include_personal_folders=include_personal))
        except OmniFlowError as exc:
            values.append(None)
            _gap(gaps, reason, exc=exc)
    return *values, gaps


def _returned_url(value, base_url, *, query_key=None):
    """Keep only a returned same-origin link, never credentials or opaque tokens."""
    if (not isinstance(value, str) or len(value) > 4096
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        return None
    try:
        url, base = urlsplit(value), urlsplit(base_url)
        if (url.scheme != "https" or url.username is not None or url.password is not None
                or (url.hostname, url.port) != (base.hostname, base.port)):
            return None
        params = parse_qs(url.query)
        safe_query = urlencode({"key": query_key}) if query_key is not None and params.get("key") == [query_key] else ""
        return urlunsplit((url.scheme, url.netloc, url.path, safe_query, ""))
    except ValueError:
        return None


def _private_metadata(document, base_url):
    item = {"document_id": _document_id(document), "queries": [], "dashboard_filters": []}
    for key in ("name", "type"):
        if isinstance(document.get(key), str):
            item[key] = document[key][:512]
    url = _returned_url(document.get("url"), base_url)
    if url is not None:
        item["url"] = url
    owner = document.get("owner")
    if isinstance(owner, dict):
        item["owner"] = {key: owner[key][:512] for key in ("id", "name", "email") if isinstance(owner.get(key), str)}
    return item


def _document_evidence(document, query_payload, state, field, model_id, base_url):
    item, gaps = _private_metadata(document, base_url), []
    if not isinstance(state, dict):
        return item, ["content_impact_reference_unverified"]
    if state.get("draftOf") is not None or ("modelId" in state and state["modelId"] != model_id):
        return item, ["content_impact_document_scope_mismatch"]
    if "app" in state:
        return item, ["content_impact_app_unsupported"]
    presentations = state.get("queryPresentations")
    if (not isinstance(presentations, dict) or not isinstance(presentations.get("data"), dict)
            or not isinstance(presentations.get("order"), list)):
        return item, ["content_impact_invalid_document"]
    tabs = presentations["data"]
    order = presentations["order"]
    if (len(tabs) > MAX_QUERY_RECORDS or any(not isinstance(tab, dict) for tab in tabs.values())
            or any(not isinstance(key, str) for key in tabs)
            or any(not isinstance(key, str) for key in order) or len(set(order)) != len(order)
            or set(order) != set(tabs)):
        return item, ["content_impact_invalid_document"]
    try:
        controls = state.get("controls")
        control_keys = set(controls["data"]) if isinstance(controls, dict) and isinstance(controls.get("data"), dict) else set()
        rendered = _layout_references(state.get("containers"), set(tabs), control_keys)
    except ValueError:
        rendered = None
        gaps.append("content_impact_layout_unsupported")
    saved = query_payload.get("queries") if isinstance(query_payload, dict) else []
    if not isinstance(saved, list) or len(saved) > MAX_QUERY_RECORDS:
        return item, ["content_impact_invalid_queries"]
    seen = set()
    for query in saved:
        if not isinstance(query, dict):
            gaps.append("content_impact_invalid_queries")
            continue
        key = query.get("queryIdentifierMapKey")
        if not isinstance(key, str) or key not in tabs or key in seen:
            gaps.append("content_impact_query_identity_unavailable")
            continue
        seen.add(key)
        tab = tabs[key]
        if tab.get("type") not in {"query", "linked"} or tab.get("isSql") is True:
            gaps.append("content_impact_query_type_unsupported")
            continue
        try:
            definition = query.get("query")
            kinds = _query_reference_kinds(definition, field)
            # Direct references remain useful lower-bound evidence, but workbook
            # expressions/controls need a separately qualified lineage adapter.
            if (definition.get("calculations") or definition.get("controls")
                    or any("." not in value for value in definition.get("fields", []))):
                gaps.append("content_impact_query_expression_unverified")
            refs = [*definition.get("fields", []), *definition.get("filters", {})]
            refs.extend(sort.get("column_name", sort.get("field", "")) for sort in definition.get("sorts", []))
            if any(value.startswith(field + "[") for value in refs):
                gaps.append("content_impact_timeframe_identity_unverified")
            if isinstance(tab.get("query"), dict):
                saved_definition = {key: value for key, value in query["query"].items() if key != "modelId"}
                state_definition = {key: value for key, value in tab["query"].items() if key != "modelId"}
                if saved_definition != state_definition:
                    gaps.append("content_impact_document_changed")
                    continue
            query_model = query.get("query", {}).get("modelId")
            allowed_models = {model_id, state.get("workbookModelId"), tab.get("model_extension_id")}
            if query_model is not None and query_model not in allowed_models:
                gaps.append("content_impact_document_scope_mismatch")
                continue
        except ValueError:
            gaps.append("content_impact_query_shape_unsupported")
            continue
        if not kinds:
            continue
        usage = "unverified" if rendered is None else "dashboard" if key in rendered else "workbook"
        observation = {"query_key": key, "reference_kinds": sorted(kinds), "usage": usage}
        if isinstance(query.get("name"), str):
            observation["name"] = query["name"][:512]
        url = _returned_url(query.get("url"), base_url, query_key=key)
        if url is not None:
            observation["url"] = url
        item["queries"].append(observation)
    # State and saved-query API are separate snapshots. Missing saved definitions
    # are not evidence that a formerly referenced tab disappeared.
    if any(tab.get("type") == "query" and key not in seen for key, tab in tabs.items()):
        gaps.append("content_impact_query_inventory_incomplete")
    try:
        item["dashboard_filters"] = _control_references(state.get("controls"), field)
    except ValueError:
        gaps.append("content_impact_controls_unsupported")
    if not item["queries"] and not item["dashboard_filters"]:
        gaps.append("content_impact_reference_unverified")
    return item, sorted(set(gaps))


def _query_reference_kinds(query, field):
    if not isinstance(query, dict):
        raise ValueError("Unknown query")
    fields, filters, sorts = query.get("fields", []), query.get("filters", {}), query.get("sorts", [])
    if (not isinstance(fields, list) or any(not isinstance(value, str) for value in fields)
            or not isinstance(filters, dict) or not isinstance(sorts, list)):
        raise ValueError("Unknown query references")
    kinds = {"field"} if field in fields else set()
    if field in filters:
        kinds.add("filter")
    for sort in sorts:
        if not isinstance(sort, dict):
            raise ValueError("Unknown sort")
        # Native saved queries use column_name; the structured-query input form
        # also uses field. Conflicting simultaneous spellings are not trusted.
        names = [sort[key] for key in ("field", "column_name") if key in sort]
        if not names or any(not isinstance(name, str) for name in names) or len(set(names)) != 1:
            raise ValueError("Unknown sort")
        if names[0] == field:
            kinds.add("sort")
    # Do not silently treat undocumented composite/filter-expression shapes as
    # flat maps or scan arbitrary values, which can contain sensitive literals.
    if any(not isinstance(key, str) or "." not in key for key in filters):
        raise ValueError("Unknown filter binding")
    return kinds


def _layout_references(containers, query_keys, control_keys):
    if containers is None:
        return set()  # Documented workbook-only state omits layout.
    if not isinstance(containers, list):
        raise ValueError("Unknown layout")
    references, pending, count = set(), list(containers), 0
    while pending:
        node = pending.pop()
        count += 1
        if count > MAX_LAYOUT_NODES or not isinstance(node, dict):
            raise ValueError("Unknown layout")
        types = [node[key] for key in ("type", "containerType") if key in node]
        if not types or any(not isinstance(kind, str) for kind in types) or len(set(types)) != 1:
            raise ValueError("Unknown container type")
        kind = types[0]
        if kind in {"query", "filter"}:
            # Native v2 read responses use an id in the query/control namespace.
            key = node.get("id")
            if not isinstance(key, str) or not key:
                raise ValueError("Unknown reference")
            if kind == "query":
                if key not in query_keys:
                    raise ValueError("Unbound query reference")
                references.add(key)
            elif key not in control_keys:
                raise ValueError("Unbound filter reference")
            continue
        if kind == "reference":
            key = node.get("referenceKey")
            if not isinstance(key, str):
                raise ValueError("Unknown reference")
            if key in query_keys and key in control_keys:
                raise ValueError("Ambiguous reference namespace")
            if key in query_keys:
                references.add(key)
            elif key not in control_keys:
                raise ValueError("Unbound reference")
        elif kind == "page" and "container" in node:
            if "children" in node or "containers" in node or not isinstance(node["container"], dict):
                raise ValueError("Competing page children")
            pending.append(node["container"])
        elif kind in {"grid", "stack", "page"}:
            children = node.get("children", node.get("containers"))
            if not isinstance(children, list) or ("children" in node and "containers" in node):
                raise ValueError("Unknown children")
            pending.extend(children)
        else:
            raise ValueError("Unknown container type")
    return references


def _control_references(controls, field):
    if controls is None:
        return []
    if (not isinstance(controls, dict) or not isinstance(controls.get("data"), dict)
            or not isinstance(controls.get("order"), list) or len(controls["data"]) > MAX_LAYOUT_NODES):
        raise ValueError("Unknown controls")
    references = []
    for key, control in controls["data"].items():
        if not isinstance(key, str) or not isinstance(control, dict):
            raise ValueError("Unsupported control")
        if "config" in control:
            config = control["config"]
            # Observed native v2 simple filters. Composite/multi-field filters
            # need their own qualified adapter, never arbitrary string scans.
            if (not isinstance(config, dict) or not isinstance(config.get("fieldName"), str)
                    or config.get("type") not in {"string", "number", "date", "boolean", "null"}):
                raise ValueError("Unsupported filter")
            if config["fieldName"] == field:
                references.append({"control_id": key, "reference_kind": "filter"})
            continue
        if control.get("type") != "FIELD_SELECTION" or not isinstance(control.get("field"), str):
            raise ValueError("Unsupported control")
        options = control.get("options", [])
        if not isinstance(options, list) or any(not isinstance(option, dict) or not isinstance(option.get("value"), str)
                                               for option in options):
            raise ValueError("Unsupported field options")
        if control["field"] == field or any(option["value"] == field for option in options):
            references.append({"control_id": key, "reference_kind": "field_selection"})
    return references
