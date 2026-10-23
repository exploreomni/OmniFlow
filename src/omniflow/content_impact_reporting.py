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
        lines = ["# dbt column-to-content impact", "", "Repository-only metadata. Do not forward externally.", "",
                 content_impact_line(updated) or "Impact unavailable.", "",
                 "Advisory published-content snapshot; not proof of a regression or complete draft coverage.", "",
                 "Detailed dependency paths, document references and coverage gaps are in column-impact.json.", ""]
        # JSON is canonical; bounded, escaped readable details never render raw HTML.
        impacts = private.get("impacts", [])
        for item in impacts[:100]:
            if not isinstance(item, dict):
                continue
            path = item.get("dependency_path", [])
            if isinstance(path, list):
                text = " → ".join(str(part) for part in path)
                text = _escape(text)
                lines.append(f"- {text[:2000]}")
                for document in item.get("documents", [])[:20]:
                    title = _escape(document.get("name") or document.get("document_id") or "Unnamed document")
                    url = document.get("url")
                    link = f"[{title}]({url})" if url and url != "[REDACTED_URL]" else title
                    owner = document.get("owner") or {}
                    owner_name = _escape(owner.get("name") or owner.get("id") or "unavailable")
                    lines.append(f"  - {link}; owner: {owner_name}")
                    for query in document.get("queries", [])[:20]:
                        title = _escape(query.get("name") or query.get("query_key") or "Unnamed query")
                        url = query.get("url")
                        link = f"[{title}]({url})" if url and url != "[REDACTED_URL]" else title
                        lines.append(f"    - {link} — {_escape(query.get('usage', 'unverified'))}")
                    if document.get("dashboard_filters"):
                        lines.append(f"    - Dashboard filter controls: {len(document['dashboard_filters'])}")
        if len(impacts) > 100:
            lines.append("\nMarkdown details are sampled; column-impact.json retains the full bounded impact inventory.")
        lines.extend(["", "## Coverage gaps", ""])
        lines.extend(f"- `{reason}`" for reason in safe["coverage_reasons"])
        secure_write_text(root / "repository" / "column-impact.md", "\n".join(lines) + "\n")
        ready = True
    seal_validation_report(config)
    return ready


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
