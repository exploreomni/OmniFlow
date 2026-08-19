"""Detect dbt schema changes that would orphan Omni model references.

The breaking-change hold stops a breaking *Omni* change from merging ahead of its
dbt deployment. This module covers the opposite direction: a dbt-only pull
request that renames or drops a column, or deletes a model, while the committed
Omni model YAML still references it. Without this check that pull request merges
cleanly and the breakage only surfaces after deployment.

Everything here is static analysis over files already in the repository. No Omni
API call, no warehouse query, and no dbt invocation is required, so the check can
run on pull requests that would otherwise be skipped for having no Omni changes.
"""

from __future__ import annotations

import re

# Git is invoked without a shell and with bounded arguments.
import subprocess  # nosec B603,B404
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DbtImpactSettings
from .dbt_manifest import DbtModel, diff_manifests, parse_manifest
from .dbt_sql_diff import diff_sql_columns, model_name_from_path
from .diff.yaml_loader import load_yaml_files
from .git import git_executable
from .timestamps import utc_now_iso

VALIDATOR = "dbt_impact"
ORPHANED_COLUMN_RULE = "dbt_column_removal_orphans_omni_field"
ORPHANED_MODEL_RULE = "dbt_model_removal_orphans_omni_view"
MAX_SAMPLES = 25
# ${TABLE}.column, "column", or a bare word reference inside a field's SQL.
TABLE_TOKEN_RE = re.compile(r"\$\{\s*TABLE\s*\}\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)")


@dataclass
class OmniFieldRef:
    view: str
    field: str
    file: str
    sql: str
    # File path, so same-named views in different schemas stay distinct.
    identity: str = ""


@dataclass
class OmniViewRef:
    view: str
    file: str
    sql_table_name: str
    identity: str = ""


def evaluate_dbt_impact(
    *,
    changed_files: list[str],
    dbt_paths: list[str],
    settings: DbtImpactSettings,
    omni_yaml_paths: list[str],
    base_ref: str | None,
    repo_root: Path | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Analyze a dbt-only change for Omni reference breakage.

    Returns the impact report and its issues. An empty issue list means either
    nothing was affected or the evidence was insufficient to make a claim.
    """
    root = repo_root or Path(".")
    severity = "error" if settings.fail_on_orphaned_references else "warning"
    dbt_files = [path for path in changed_files if _under_any(path, dbt_paths)]

    graph_files = _load_omni_files(root, omni_yaml_paths)
    field_refs, view_refs = _index_omni_references(graph_files)

    removed_columns_by_model, removed_models, mode, notes = _resolve_dbt_changes(
        dbt_files=dbt_files,
        settings=settings,
        base_ref=base_ref,
        root=root,
    )

    issues: list[dict[str, Any]] = []
    issues.extend(
        _column_issues(
            removed_columns_by_model=removed_columns_by_model,
            field_refs=field_refs,
            view_refs=view_refs,
            severity=severity,
            mode=mode,
        )
    )
    issues.extend(
        _model_issues(
            removed_models=removed_models,
            view_refs=view_refs,
            severity=severity,
            mode=mode,
        )
    )

    report = {
        "tool": "omniflow",
        "validator": VALIDATOR,
        "generated_at": utc_now_iso(),
        "analysis_mode": mode,
        "dbt_files_analyzed": sorted(dbt_files)[:MAX_SAMPLES],
        "dbt_file_count": len(dbt_files),
        "omni_views_indexed": len({reference.identity for bucket in view_refs.values() for reference in bucket}),
        "omni_fields_indexed": len(field_refs),
        "removed_column_models": len(removed_columns_by_model),
        "removed_models": len(removed_models),
        "notes": notes,
        "issues": issues,
        "summary": {
            "total_issues": len(issues),
            "errors": sum(1 for issue in issues if issue.get("severity") == "error"),
            "warnings": sum(1 for issue in issues if issue.get("severity") == "warning"),
        },
    }
    return report, issues


def _resolve_dbt_changes(
    *,
    dbt_files: list[str],
    settings: DbtImpactSettings,
    base_ref: str | None,
    root: Path,
) -> tuple[dict[str, set[str]], dict[str, str], str, list[str]]:
    """Determine removed columns and removed models.

    Prefers a committed manifest, falling back to SQL parsing. Returns column
    removals keyed by a matchable relation token, removed models keyed by that
    same token, the analysis mode, and human-readable notes.
    """
    notes: list[str] = []
    manifest_result = _manifest_changes(settings=settings, base_ref=base_ref, root=root, notes=notes)
    if manifest_result is not None:
        removed_columns, removed_models = manifest_result
        return removed_columns, removed_models, "manifest", notes

    removed_columns, removed_models = _sql_changes(
        dbt_files=dbt_files, base_ref=base_ref, root=root, notes=notes
    )
    return removed_columns, removed_models, "sql_heuristic", notes


def _manifest_changes(
    *,
    settings: DbtImpactSettings,
    base_ref: str | None,
    root: Path,
    notes: list[str],
) -> tuple[dict[str, set[str]], dict[str, str]] | None:
    if not settings.manifest_path:
        return None
    manifest_file = root / settings.manifest_path
    head_text = _read_text(manifest_file)
    if head_text is None:
        notes.append(
            f"Configured dbt manifest '{settings.manifest_path}' was not found in the checkout; "
            "falling back to SQL heuristics."
        )
        return None
    if not base_ref:
        notes.append(
            "No base ref was available to compare the dbt manifest against; "
            "falling back to SQL heuristics."
        )
        return None
    base_text = _git_show(base_ref, settings.manifest_path)
    if base_text is None:
        notes.append(
            f"Could not read '{settings.manifest_path}' from the base ref; falling back to SQL heuristics."
        )
        return None

    base_models = parse_manifest(base_text, source=f"{base_ref}:{settings.manifest_path}")
    head_models = parse_manifest(head_text, source=settings.manifest_path)
    removed_model_nodes, removed_column_nodes = diff_manifests(base_models, head_models)

    removed_columns: dict[str, set[str]] = {}
    for unique_id, columns in removed_column_nodes.items():
        model = base_models[unique_id]
        for token in _model_tokens(model, settings):
            removed_columns.setdefault(token, set()).update(columns)
    removed_models: dict[str, str] = {}
    for model in removed_model_nodes.values():
        for token in _model_tokens(model, settings):
            removed_models[token] = model.name
    return removed_columns, removed_models


def _sql_changes(
    *,
    dbt_files: list[str],
    base_ref: str | None,
    root: Path,
    notes: list[str],
) -> tuple[dict[str, set[str]], dict[str, str]]:
    removed_columns: dict[str, set[str]] = {}
    removed_models: dict[str, str] = {}
    if not base_ref:
        notes.append("No base ref was available, so dbt SQL could not be compared.")
        return removed_columns, removed_models

    for path in dbt_files:
        model_name = model_name_from_path(path)
        if not model_name:
            continue
        base_sql = _git_show(base_ref, path)
        head_sql = _read_text(root / path)
        if base_sql is None:
            # New model file: nothing existed before, so nothing can be orphaned.
            continue
        if head_sql is None:
            removed_models[model_name] = model_name
            continue
        missing = diff_sql_columns(base_sql, head_sql)
        if missing:
            removed_columns.setdefault(model_name, set()).update(missing)
    if not removed_columns and not removed_models:
        notes.append(
            "SQL heuristics found no removed output columns. Commit a dbt manifest for precise analysis."
        )
    return removed_columns, removed_models


def _model_tokens(model: DbtModel, settings: DbtImpactSettings) -> set[str]:
    tokens = model.relation_candidates()
    for mapping in settings.table_mapping:
        if mapping.get("dbt_model", "").strip().lower() != model.name.lower():
            continue
        for key in ("sql_table_name", "omni_view"):
            value = mapping.get(key)
            if value:
                tokens.add(value.strip().lower())
    return tokens


def _load_omni_files(root: Path, omni_yaml_paths: list[str]) -> dict[str, Any]:
    """Load every Omni YAML file, keyed by its repository-relative path.

    Files are kept separate on purpose. ``build_graph`` keys views by name, so a
    repository that materializes the same table into several schemas (a common dbt
    pattern producing ``analytics_marts/dim_product`` and
    ``dbt_austin_marts/dim_product``) would collapse those into one entry and hide
    the others from this check.
    """
    merged: dict[str, Any] = {}
    for relative in omni_yaml_paths:
        directory = root / relative
        if not directory.is_dir():
            continue
        for name, payload in load_yaml_files(directory).items():
            merged[f"{relative}/{name}"] = payload
    return merged


def _index_omni_references(
    files: dict[str, Any],
) -> tuple[list[OmniFieldRef], dict[str, list[OmniViewRef]]]:
    """Index every view file and its fields.

    Views are identified by file path so same-named views in different schemas stay
    distinct, while the reported view label stays the human-readable Omni name.
    """
    field_refs: list[OmniFieldRef] = []
    view_refs: dict[str, list[OmniViewRef]] = {}

    for file_path, payload in sorted(files.items()):
        if not isinstance(payload, dict) or not _is_view_file(file_path, payload):
            continue
        view_name = _view_name(file_path, payload)
        identity = file_path
        relation = _view_relation(payload, view_name)

        for group in _field_groups(payload):
            for field_name, definition in group.items():
                if not isinstance(field_name, str) or not field_name.strip():
                    continue
                sql = definition.get("sql") if isinstance(definition, dict) else None
                field_refs.append(
                    OmniFieldRef(
                        view=view_name,
                        field=field_name.strip(),
                        file=file_path,
                        sql=sql if isinstance(sql, str) else "",
                        identity=identity,
                    )
                )

        reference = OmniViewRef(view=view_name, file=file_path, sql_table_name=relation, identity=identity)
        for token in _table_tokens(relation, view_name):
            bucket = view_refs.setdefault(token, [])
            if not any(existing.identity == identity for existing in bucket):
                bucket.append(reference)
    return field_refs, view_refs


def _is_view_file(file_path: str, payload: dict[str, Any]) -> bool:
    """Identify authored view files, excluding topics, models, and relationships."""
    lowered = file_path.lower()
    basename = lowered.rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0]
    if ".topic" in basename or ".composite_topic" in basename:
        return False
    if stem in {"model", "relationships"} or "relationship" in basename:
        return False
    if payload.get("type") in {"model", "topic"} or "base_view" in payload:
        return False
    return bool(_field_groups(payload)) or bool(
        payload.get("table_name") or payload.get("sql_table_name")
    )


def _field_groups(payload: dict[str, Any]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for key in ("fields", "dimensions", "measures"):
        value = payload.get(key)
        if isinstance(value, dict):
            groups.append(value)
        elif isinstance(value, list):
            groups.append(
                {
                    str(item.get("name")): item
                    for item in value
                    if isinstance(item, dict) and item.get("name")
                }
            )
    return groups


def _view_name(file_path: str, payload: dict[str, Any]) -> str:
    value = payload.get("name") or payload.get("view")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return file_path.rsplit("/", 1)[-1].split(".", 1)[0]


def _view_relation(definition: dict[str, Any], view_name: str) -> str:
    """Build the fully qualified relation an Omni view reads from.

    Omni supports a single ``sql_table_name`` and also the split
    ``catalog``/``schema``/``table_name`` form that its dbt integration writes.
    Both are normalized to a dotted, lowercase relation so schema-qualified
    matching works and two same-named tables in different schemas stay distinct.
    """
    explicit = definition.get("sql_table_name")
    if isinstance(explicit, str) and explicit.strip():
        return _clean_relation(explicit)

    table = definition.get("table_name")
    if not isinstance(table, str) or not table.strip():
        # Omni defaults an undeclared table to the view name.
        table = view_name
    parts = [
        definition.get("catalog") or definition.get("database"),
        definition.get("schema") or definition.get("schema_name"),
        table,
    ]
    qualified = ".".join(
        str(part).strip() for part in parts if isinstance(part, str) and part.strip()
    )
    return _clean_relation(qualified or str(table))


def _clean_relation(value: str) -> str:
    return value.replace('"', "").replace("`", "").replace("[", "").replace("]", "").strip().lower()


def _table_tokens(sql_table_name: str, view_name: str) -> set[str]:
    """Return every relation form a dbt model might be matched against.

    The bare table name is included so a heuristic-mode match (which only knows
    the model file name) still resolves, while the qualified forms let manifest
    mode distinguish same-named tables across schemas.
    """
    tokens = {view_name.strip().lower()}
    if sql_table_name:
        tokens.add(sql_table_name)
        parts = [part for part in sql_table_name.split(".") if part]
        if parts:
            tokens.add(parts[-1])
        if len(parts) >= 2:
            tokens.add(".".join(parts[-2:]))
    return {token for token in tokens if token}


def _column_issues(
    *,
    removed_columns_by_model: dict[str, set[str]],
    field_refs: list[OmniFieldRef],
    view_refs: dict[str, list[OmniViewRef]],
    severity: str,
    mode: str,
) -> list[dict[str, Any]]:
    """Emit one issue per orphaned column.

    A dbt model resolves to several relation tokens (bare, schema-qualified, and
    fully qualified), so the same column can match through more than one token.
    Findings are keyed by the affected fields to collapse those duplicates, and
    the most qualified relation name is kept for the report.
    """
    grouped: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
    for token, columns in sorted(removed_columns_by_model.items()):
        references = view_refs.get(token, [])
        affected = {reference.identity for reference in references}
        if not affected:
            continue
        # An unqualified token can match same-named tables in different schemas.
        # Say so instead of asserting every match is broken.
        distinct_relations = {reference.sql_table_name for reference in references}
        ambiguous = "." not in token and len(distinct_relations) > 1
        for column in sorted(columns):
            orphaned = [
                reference
                for reference in field_refs
                if reference.identity in affected and _references_column(reference, column)
            ]
            if not orphaned:
                continue
            identity = tuple(sorted((reference.file, reference.field) for reference in orphaned))
            key = (column, identity)
            existing = grouped.get(key)
            if existing is None:
                message = (
                    f"This dbt change removes column '{column}' from '{token}', but Omni still "
                    f"references it in {len(orphaned)} field(s). Update the Omni model in the same "
                    "deployment sequence, or the referencing content will break once dbt deploys."
                )
                if ambiguous:
                    message += (
                        f" The relation name '{token}' is unqualified and matches "
                        f"{len(distinct_relations)} Omni relations "
                        f"({', '.join(sorted(distinct_relations))}); commit a dbt manifest or add a "
                        "table_mapping entry to resolve which one changed."
                    )
                issue = {
                    "validator": VALIDATOR,
                    "rule": ORPHANED_COLUMN_RULE,
                    "severity": severity,
                    "analysis_mode": mode,
                    "message": message,
                    "dbt_relation": token,
                    "column": column,
                    "orphaned_fields": [
                        {"view": reference.view, "field": reference.field, "file": reference.file}
                        for reference in orphaned[:MAX_SAMPLES]
                    ],
                    "orphaned_field_count": len(orphaned),
                }
                if ambiguous:
                    issue["ambiguous_relation_match"] = True
                    issue["candidate_relations"] = sorted(distinct_relations)
                grouped[key] = issue
            elif token.count(".") > str(existing["dbt_relation"]).count("."):
                # Prefer the most qualified relation name in the reported message.
                existing["message"] = existing["message"].replace(
                    f"from '{existing['dbt_relation']}'", f"from '{token}'"
                )
                existing["dbt_relation"] = token
    return list(grouped.values())


def _model_issues(
    *,
    removed_models: dict[str, str],
    view_refs: dict[str, list[OmniViewRef]],
    severity: str,
    mode: str,
) -> list[dict[str, Any]]:
    """Emit one issue per removed dbt model, collapsing duplicate relation tokens."""
    grouped: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for token, model_name in sorted(removed_models.items()):
        references = view_refs.get(token, [])
        if not references:
            continue
        identity = tuple(sorted({reference.identity for reference in references}))
        key = (model_name, identity)
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = {
                "validator": VALIDATOR,
                "rule": ORPHANED_MODEL_RULE,
                "severity": severity,
                "analysis_mode": mode,
                "message": (
                    f"This dbt change removes model '{model_name}', but {len(identity)} Omni view(s) "
                    "still reference that relation. Remove or repoint the Omni view in the same "
                    "deployment sequence."
                ),
                "dbt_relation": token,
                "dbt_model": model_name,
                "orphaned_views": [
                    {
                        "view": reference.view,
                        "file": reference.file,
                        "sql_table_name": reference.sql_table_name,
                    }
                    for reference in references[:MAX_SAMPLES]
                ],
                "orphaned_view_count": len(identity),
            }
        elif token.count(".") > str(existing["dbt_relation"]).count("."):
            existing["dbt_relation"] = token
    return list(grouped.values())


def _references_column(reference: OmniFieldRef, column: str) -> bool:
    """Decide whether an Omni field depends on a warehouse column.

    A field with no explicit SQL maps to a column of the same name. A field with
    SQL is matched on ``${TABLE}.column`` tokens first, then on a word-boundary
    search so a substring like ``customer_id_hash`` never matches ``customer_id``.
    """
    target = column.strip().lower()
    if not target:
        return False
    if not reference.sql:
        return reference.field.strip().lower() == target
    lowered = reference.sql.lower()
    if any(token.lower() == target for token in TABLE_TOKEN_RE.findall(reference.sql)):
        return True
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(target)}(?![A-Za-z0-9_])", lowered) is not None


def _under_any(path: str, prefixes: list[str]) -> bool:
    normalized = path.strip().strip("/")
    if not normalized:
        return False
    for prefix in prefixes:
        cleaned = prefix.strip().strip("/")
        if not cleaned:
            continue
        if normalized == cleaned or normalized.startswith(f"{cleaned}/"):
            return True
    return False


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _git_show(ref: str, path: str) -> str | None:
    """Read a file at a Git ref. Returns None when it is unavailable."""
    try:
        # Arguments are passed directly to Git, never through a shell.
        result = subprocess.run(  # nosec B603
            [git_executable(), "show", f"{ref}:{path}"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout
