"""Advisory impact output, bound to this run without changing validation policy."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

from .exceptions import ConfigError, SecurityPolicyError
from .git import current_sha
from .repository_policy import resolve_repository_policy
from .revision_data import pull_request_revision
from .security import redact, secure_write_text, validate_repo_output_path

COUNTS = ("affected_fields", "dashboards", "workbooks", "queries", "dashboard_filters",
          "searches", "unverified_documents")
PRIVATE_FILES = ("column-impact.json", "column-impact.md")
MAX_REPORT_BYTES = 32 * 1024 * 1024
GAP_REASONS = frozenset({
    "content_impact_disabled", "content_impact_static_invalid", "content_impact_static_incomplete",
    "content_impact_budget_invalid", "content_impact_budget_exhausted", "content_impact_model_scope_ambiguous",
    "content_impact_field_identity_unavailable", "content_impact_search_unavailable",
    "content_impact_document_identity_unavailable", "content_impact_model_unavailable",
    "content_impact_queries_unavailable", "content_impact_layout_unavailable", "content_impact_reference_unverified",
    "content_impact_document_scope_mismatch", "content_impact_app_unsupported", "content_impact_invalid_document",
    "content_impact_layout_unsupported", "content_impact_invalid_queries", "content_impact_query_identity_unavailable",
    "content_impact_query_type_unsupported", "content_impact_query_shape_unsupported",
    "content_impact_query_inventory_incomplete", "content_impact_controls_unsupported",
    "content_impact_query_expression_unverified", "content_impact_timeframe_identity_unverified",
    "content_impact_execution_unavailable", "content_impact_credentials_unavailable", "content_impact_actor_scope_unverified",
    "content_impact_snapshot_changed", "content_impact_query_scope_unverified",
    "content_impact_document_changed", "content_impact_document_scope_unavailable",
    "content_impact_draft_document", "content_impact_timeout",
    "content_impact_environment_mismatch", "content_impact_target_mismatch",
    "dbt_base_revision_unavailable", "dbt_manifest_unavailable", "dbt_manifest_only_change",
    "dbt_model_identity_ambiguous", "dbt_relation_ambiguous", "dbt_relation_context_unavailable",
    "dbt_relation_case_ambiguous", "dbt_relation_identity_unavailable", "dbt_source_unsupported",
    "dbt_explicit_view_mapping_unresolved", "dbt_downstream_models_not_analyzed", "dbt_downstream_models_unavailable",
    "dbt_sql_unsupported", "dbt_sql_dynamic_projection", "dbt_sql_wildcard", "dbt_sql_output_identity_unavailable",
    "dbt_sql_duplicate_output", "dbt_manifest_contract_unavailable", "dbt_source_unavailable",
    "omni_root_unavailable", "omni_model_scope_ambiguous", "omni_query_view_unsupported",
    "omni_field_identity_ambiguous", "omni_field_expression_unsupported", "omni_field_filter_unsupported",
    "omni_field_reference_ambiguous", "omni_field_reference_unresolved", "omni_field_dependency_cycle",
    "omni_field_dependency_depth", "omni_view_evidence_unavailable", "omni_inheritance_unsupported",
    "omni_relation_identity_unavailable", "omni_view_identity_unavailable",
    "impact_evidence_limit",
})


def public_content_impact(report: dict) -> dict:
    """No arbitrary names, URLs, SQL, owner identities or error messages."""
    status = report.get("status")
    if status not in {"complete", "partial", "unavailable", "disabled"}:
        status = "unavailable"
    counts = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    if status == "complete" and not all(type(counts.get(key)) is int and 0 <= counts[key] <= 2**31 - 1
                                         for key in COUNTS):
        status = "unavailable"
    safe: dict[str, Any] = {
        "status": status, "advisory": True, "scope": "published",
        "coverage_complete": status == "complete" and report.get("coverage_complete") is True,
        "summary": {key: counts[key] for key in COUNTS
                    if type(counts.get(key)) is int and 0 <= counts[key] <= 2**31 - 1},
    }
    gaps = report.get("coverage_gaps")
    safe["coverage_gap_count"] = len(gaps) if isinstance(gaps, list) else max(
        0, min(report.get("coverage_gap_count", 0), 2**31 - 1)
    ) if type(report.get("coverage_gap_count", 0)) is int else 0
    reasons = [item.get("reason_code") for item in gaps if isinstance(item, dict)] if isinstance(gaps, list) \
        else report.get("coverage_reasons", [])
    if not isinstance(reasons, list):
        reasons = []
    safe["coverage_reasons"] = sorted({reason for reason in reasons
                                        if isinstance(reason, str) and reason in GAP_REASONS})
    from .exceptions import TRANSPORT_REASON_CODES
    allowed_causes = GAP_REASONS | TRANSPORT_REASON_CODES | {
        "content_pat_impersonation_unsupported", "content_actor_unresolved", "content_personal_actor_unresolved",
        "content_personal_user_required", "validation_identity_changed", "content_identity_invalid",
    }
    causes = [item.get("cause_code") for item in gaps if isinstance(item, dict)] if isinstance(gaps, list) \
        else report.get("coverage_causes", [])
    if not isinstance(causes, list):
        causes = []
    safe["coverage_causes"] = sorted({cause for cause in causes
                                       if isinstance(cause, str) and cause in allowed_causes})
    if safe["status"] == "complete" and (not safe["coverage_complete"] or safe["coverage_gap_count"]
                                            or safe["coverage_reasons"] or safe["coverage_causes"]):
        safe.update(status="partial", coverage_complete=False)
    return safe


def content_impact_line(report: dict) -> str | None:
    impact = report.get("content_impact")
    if not isinstance(impact, dict):
        return None
    safe = public_content_impact(impact)
    if safe["status"] == "disabled":
        return None
    counts = safe["summary"]
    if safe["status"] == "unavailable":
        return "dbt content impact: unavailable — affected content is unknown (advisory)."
    coverage = "complete for supported published scope" if safe["coverage_complete"] else "partial; totals are lower bounds"
    return (f"dbt content impact: {counts.get('dashboards', 0)} dashboard(s), "
            f"{counts.get('workbooks', 0)} workbook(s), {counts.get('queries', 0)} query(s) observed "
            f"— {coverage} (advisory).")


def coverage_explanations(report: dict) -> list[str]:
    """Fixed recovery prose only; never render API messages or source identifiers."""
    safe = public_content_impact(report)
    reasons = set(safe["coverage_reasons"]) | set(safe["coverage_causes"])
    groups = (
        ({"dbt_explicit_view_mapping_unresolved"},
         "A configured dbt-to-Omni view mapping could not be resolved. Impact coverage is incomplete. "
         "Check checks.dbt_impact.table_mapping against the authored views for the recorded revision; "
         "no other view is substituted."),
        ({"dbt_downstream_models_not_analyzed", "dbt_downstream_models_unavailable"},
         "A changed dbt model feeds other dbt models, or that inventory could not be read. Column lineage through "
         "downstream models is not analyzed, so Omni content built on them is not counted. Treat the totals as a "
         "lower bound and check the downstream marts' Omni views."),
        ({"content_impact_field_identity_unavailable", "omni_view_identity_unavailable",
          "omni_field_reference_ambiguous", "omni_field_reference_unresolved",
          "dbt_relation_ambiguous", "dbt_relation_identity_unavailable", "dbt_relation_case_ambiguous"},
         "A source field could not be matched uniquely to a published Omni view. Verify database/schema identity "
         "and published model synchronization; unresolved fields are not attributed to another view's content."),
        ({"content_impact_query_expression_unverified", "content_impact_timeframe_identity_unverified",
          "content_impact_query_type_unsupported", "content_impact_app_unsupported", "omni_query_view_unsupported"},
         "Saved calculations, timeframe references, or SQL/App queries have unsupported lineage. Direct supported "
         "references may be listed, but inspect the affected queries in Omni for additional dependencies."),
        ({"dbt_sql_dynamic_projection", "dbt_sql_unsupported", "dbt_sql_wildcard", "dbt_source_unsupported",
          "dbt_manifest_only_change", "dbt_sql_output_identity_unavailable", "dbt_sql_duplicate_output"},
         "The changed dbt source cannot be fully analyzed. Dynamic macros, wildcard outputs, and compiled-SQL-only "
         "changes are not resolved by this feature. Review the source and generated SQL separately; no dbt code is executed."),
        ({"omni_inheritance_unsupported", "omni_field_dependency_cycle", "omni_field_expression_unsupported",
          "omni_field_filter_unsupported"},
         "An authored Omni dependency uses unsupported inheritance, an expression/filter, or a cycle. Inspect its "
         "dependency path in the private detail report and validate the same scope in Omni."),
        ({"content_impact_budget_exhausted", "content_impact_timeout", "impact_evidence_limit",
          "omni_field_dependency_depth", "omni_timeout"},
         "A bounded lookup or analysis limit was reached. Narrow the scope or adjust supported limits, then rerun; "
         "omitted content is not confirmed unaffected."),
        ({"content_impact_search_unavailable", "content_impact_queries_unavailable", "content_impact_model_unavailable",
          "content_impact_credentials_unavailable", "content_impact_actor_scope_unverified",
          "content_pat_impersonation_unsupported", "content_actor_unresolved", "omni_http_error",
          "omni_unauthorized", "omni_forbidden", "omni_rate_limited", "omni_service_unavailable",
          "omni_tls_error", "omni_connection_error", "omni_request_failed", "omni_response_interrupted",
          "empty_api_response", "invalid_json_response"},
         "Required metadata could not be read. Check the API credential, acting-user permissions, and connection "
         "for the registered model, then rerun. An unavailable response is not zero impact."),
        ({"content_impact_layout_unavailable", "content_impact_layout_unsupported",
          "content_impact_document_changed", "content_impact_snapshot_changed",
          "content_impact_document_scope_mismatch", "content_impact_draft_document",
          "content_impact_query_inventory_incomplete", "content_impact_reference_unverified"},
         "Document placement, scope, or snapshot evidence is incomplete. Inspect the published document and rerun "
         "after it is stable; a saved tab is not assumed to be a dashboard tile."),
    )
    messages = [message for codes, message in groups if reasons & codes]
    covered = set().union(*(codes for codes, _ in groups))
    if reasons - covered or (safe["status"] in {"partial", "unavailable"} and not messages):
        messages.append("Impact coverage remains incomplete. Review the reason categories and private source locations "
                        "when available; do not interpret missing evidence as no affected content.")
    return messages


def _binding(config) -> dict:
    base = pull_request_revision("base")
    head = pull_request_revision("head")
    return {"config_hash": config.hash, "base": base, "head": head,
            "sha": current_sha(), "run_id": os.getenv("GITHUB_RUN_ID"),
            "run_attempt": os.getenv("GITHUB_RUN_ATTEMPT")}


def _read(path: Path) -> bytes:
    validate_repo_output_path(path)
    with path.open("rb") as stream:
        value = stream.read(MAX_REPORT_BYTES + 1)
    if len(value) > MAX_REPORT_BYTES:
        raise ConfigError("Impact report exceeds its size limit")
    return value


def seal_validation_report(config) -> None:
    """Local receipt is never an upload artifact or authority for private data."""
    root = Path(config.reporting.output_dir)
    body = _read(root / "public" / "report.json")
    receipt = {"binding": _binding(config), "sha256": hashlib.sha256(body).hexdigest()}
    secure_write_text(root / ".impact-receipt.json", json.dumps(receipt))


def current_validation_report(config) -> dict:
    root = Path(config.reporting.output_dir)
    receipt = json.loads(_read(root / ".impact-receipt.json"))
    expected = json.loads(json.dumps(_binding(config)))
    body = _read(root / "public" / "report.json")
    if receipt.get("binding") != expected or receipt.get("sha256") != hashlib.sha256(body).hexdigest():
        raise ConfigError("Impact enrichment requires this run's unchanged validation report")
    report = json.loads(body)
    if not isinstance(report, dict) or report.get("diagnostic_schema_version") != 2:
        raise ConfigError("Impact enrichment requires a current public validation report")
    return report


def clear_content_impact(root: Path, *, private_only: bool = False) -> None:
    # Like the low-level public report writer, allow a caller-selected absolute
    # destination, but never follow generated-file symlinks. CLI entry points
    # enforce their stricter checkout boundary before calling this helper.
    if root.is_symlink():
        raise SecurityPolicyError("Impact output must not traverse symbolic links")
    root = root.resolve()
    paths = [root / "repository" / name for name in PRIVATE_FILES]
    if not private_only:
        paths.extend([root / ".impact-receipt.json", root / "public" / "content-impact.json"])
    for path in paths:
        if path.resolve() != path:
            raise SecurityPolicyError("Impact output must not traverse symbolic links")
        if path.exists() and not path.is_file():
            raise SecurityPolicyError("Impact output must be a regular file")
    for path in paths:
        if path.exists():
            path.unlink()


def publish_content_impact(config, detail: dict, original: dict) -> bool:
    """Append advisory evidence; never edit findings, check states or exit code."""
    from .reporting.writer import write_reports

    root = Path(config.reporting.output_dir)
    clear_content_impact(root, private_only=True)
    safe = public_content_impact(detail)
    updated = {**original, "content_impact": safe}
    for directory in (root, root / "public"):
        write_reports(updated, output_dir=directory, formats=list({"json", *config.reporting.formats}))
    secure_write_text(root / "public" / "content-impact.json", json.dumps(safe, indent=2) + "\n")
    # A saved report cannot authorize disclosure: re-check the live private-repo
    # policy independently, without deleting existing validator diagnostics.
    policy = resolve_repository_policy(config)
    ready = False
    if policy.enabled:
        # The internal enricher accepts returned URLs only from a trusted model
        # origin. Re-verify against the registry; generic redact otherwise strips
        # all links, defeating navigation in this private artifact.
        from .discovery import discover_published_contexts
        try:
            origins = {urlsplit(context.base_url).netloc for context in discover_published_contexts()}
        except Exception:
            origins = set()
        private = _private_detail(detail, origins)
        secure_write_text(root / "repository" / "column-impact.json", json.dumps(private, indent=2) + "\n")
        secure_write_text(root / "repository" / "column-impact.md", render_private_impact(private))
        ready = True
    seal_validation_report(config)
    return ready


def _metadata_link(item: dict, fallback: str) -> str:
    title = _escape(item.get("name") or fallback)
    url = item.get("url")
    return f"[{title}]({url})" if url and url != "[REDACTED_URL]" else title


def _impact_description(item: dict) -> str:
    kind = "Potential impact" if item.get("confidence") == "potential" else "Confirmed source dependency"
    source = "/".join(str(item.get(key, "")) for key in ("model_path", "view_file"))
    column = ".".join(str(item.get(key, "")) for key in ("relation", "column")).rstrip(".")
    path = item.get("dependency_path") or [item.get("field_name", "unresolved field")]
    return (f"{kind}; {_escape(item.get('source_revision', 'unknown'))} / "
            f"{_escape(item.get('change_kind', 'unknown'))}; column: {_escape(column or 'unknown')}; "
            f"source: {_escape(source)}; dependency: {_escape(' → '.join(str(part) for part in path))}")


def render_private_impact(private: dict) -> str:
    """Render already-redacted metadata, grouping content once across field impacts."""
    lines = ["# dbt column-to-content impact", "", "Repository-only metadata. Do not forward externally.", "",
             content_impact_line({"content_impact": private}) or "Impact unavailable.", "",
             "Confirmed dependency means a supported reference was observed, not that this PR breaks the document. "
             "Potential impact is broader inference. Unresolved coverage is not proof of no impact.", "",
             "## Affected published content", ""]
    impacts = [item for item in private.get("impacts", []) if isinstance(item, dict)]
    documents = {}
    unresolved = []
    for index, item in enumerate(impacts):
        if not item.get("documents"):
            unresolved.append(item)
        for document in item.get("documents", []):
            key = (item.get("model_path", ""), document.get("document_id") or document.get("url") or f"unknown-{index}")
            record = documents.setdefault(key, {"document": document, "queries": {}, "filters": set(), "sources": {}})
            for query in document.get("queries", []):
                record["queries"].setdefault(query.get("query_key"), query)
            record["filters"].update(control.get("control_id") for control in document.get("dashboard_filters", []))
            description = _impact_description(item)
            record["sources"].setdefault(description, None)
    for (_, identity), record in list(documents.items())[:100]:
        document = record["document"]
        owner = document.get("owner") or {}
        lines.extend([f"### {_metadata_link(document, identity)}", "",
                      f"Owner: {_escape(owner.get('name') or owner.get('id') or 'unavailable from returned metadata')}.", ""])
        for query in list(record["queries"].values())[:20]:
            lines.append(f"- {_metadata_link(query, query.get('query_key') or 'Unnamed query')} "
                         f"— {_escape(query.get('usage', 'unverified'))}")
        if record["filters"]:
            lines.append(f"- Referenced dashboard filter controls: {len(record['filters'])}")
        if not record["queries"] and not record["filters"]:
            lines.append("- Search candidate only; no supported query/filter reference was confirmed.")
        lines.extend(["", "Changed columns and dependency paths:", ""])
        lines.extend(f"- {value}" for value in list(record["sources"])[:20])
        lines.append("")
    if not documents:
        lines.append("No supported published content references were recorded. Check coverage before treating this as zero impact.")
    if unresolved:
        lines.extend(["", "## Source impacts without confirmed content", ""])
        lines.extend(f"- {_impact_description(item)}" for item in unresolved[:30])
    lines.extend(["", "## Coverage gaps and recovery", ""])
    explanations = coverage_explanations(private)
    lines.extend(f"- {message}" for message in explanations)
    if not explanations:
        lines.append("No coverage gaps were recorded for the supported published scope.")
    for gap in private.get("coverage_gaps", [])[:30]:
        if not isinstance(gap, dict):
            continue
        index = gap.get("impact_index")
        impact = impacts[index] if type(index) is int and 0 <= index < len(impacts) else {}
        location = " / ".join(str(gap.get(key) or impact.get(key)) for key in
                              ("model_path", "file", "view_file", "field_name", "source_revision")
                              if gap.get(key) or impact.get(key))
        mapping = gap.get("configured_view") if gap.get("reason_code") == "dbt_explicit_view_mapping_unresolved" else None
        lines.append(f"- `{_escape(gap.get('reason_code', 'unknown'))}`" +
                     (f" — mapping: `checks.dbt_impact.table_mapping[].omni_view` = {_escape(mapping)}" if mapping else "") +
                     (f" — location: {_escape(location)}" if location else ""))
    lines.extend(["", "Readable lists are bounded (100 documents, 20 queries/dependencies per document, "
                  "30 unresolved impacts/gaps). column-impact.json retains the full bounded evidence inventory."])
    return "\n".join(lines) + "\n"


def _escape(value: Any) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    return re.sub(r"([\\`*_{}\[\]<>()#!|])", r"\\\1", text)[:2000]


def _private_detail(value: Any, origins: set[str], key: str = "") -> Any:
    if isinstance(value, dict):
        return {name: _private_detail(item, origins, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_private_detail(item, origins) for item in value]
    if key == "url" and isinstance(value, str):
        try:
            url = urlsplit(value)
            if (url.scheme == "https" and url.netloc in origins and not url.username and not url.password
                    and not any(ord(char) < 32 for char in value)):
                params = parse_qs(url.query)
                query = urlencode({"key": params["key"][0]}) if len(params.get("key", [])) == 1 else ""
                return urlunsplit(("https", url.netloc, quote(url.path, safe="/%:@+~.-"), query, ""))
        except ValueError:
            pass
    return redact(value)
