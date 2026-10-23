"""Detect dbt schema changes that would orphan Omni model references.

The breaking-change hold stops a breaking *Omni* change from merging ahead of its
dbt deployment. This module covers the opposite direction: a dbt pull
request that renames or drops a column, or deletes a model, while the committed
Omni model YAML still references it. Without this check that pull request merges
cleanly and the breakage only surfaces after deployment.

Everything here is static analysis over files already in the repository. No Omni
API call, no warehouse query, and no dbt invocation is required, so the check can
run on both dbt-only and mixed dbt/Omni pull requests without executing PR code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .candidate import MAX_TREE_ENTRIES, _bounded_git, _GitUnavailable, _local_tree, _remote_tree
from .config import DbtImpactSettings
from .dbt_manifest import MAX_MANIFEST_BYTES, DbtModel, diff_manifests, parse_manifest
from .dbt_sql_diff import MAX_SQL_BYTES, analyze_output_columns, model_name_from_path, sql_reference_text
from .diff.yaml_loader import load_yaml_files, parse_yaml_file_map
from .exceptions import ConfigError, OmniFlowError, SecurityPolicyError
from .model_paths import filename_identity, is_repo_control_file, is_semantic_file
from .revision_data import FULL_SHA, pull_request_revision, read_git_text, read_github_text, read_head_text
from .semantic_files import infer_file_kind
from .timestamps import utc_now_iso
from .trust import _safe_repo_path
from .yaml_security import MAX_YAML_FILE_BYTES, MAX_YAML_FILES, MAX_YAML_TOTAL_BYTES

VALIDATOR = "dbt_impact"
ORPHANED_COLUMN_RULE = "dbt_column_removal_orphans_omni_field"
ORPHANED_MODEL_RULE = "dbt_model_removal_orphans_omni_view"
MAX_SAMPLES = 25
COVERAGE_RULE = "dbt_impact_incomplete_coverage"
MAX_DBT_FILES = 1000
MAX_SQL_TOTAL_BYTES = 50 * 1024 * 1024
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
    omni_snapshot=None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Analyze dbt changes against candidate Omni references, including mixed PRs.

    Returns the impact report and its issues. Incomplete coverage is an explicit
    issue and blocks by default; an empty finding list never hides missing evidence.
    """
    root = repo_root or Path(".")
    severity = "error" if settings.fail_on_orphaned_references else "warning"
    dbt_files = [path for path in changed_files if _under_any(path, dbt_paths)]
    if len(dbt_files) > MAX_DBT_FILES:
        raise SecurityPolicyError("dbt impact exceeds the 1,000-file safety limit")

    head_revision = pull_request_revision("head")
    base_revision = pull_request_revision("base")
    graph_files, root_coverage = _load_omni_snapshot(root, omni_yaml_paths, revision=head_revision,
                                                  external_snapshot=omni_snapshot)
    base_graph_files, base_coverage = (
        (graph_files, [dict(item) for item in root_coverage]) if omni_snapshot is not None else
        _load_omni_snapshot(root, omni_yaml_paths, revision=base_revision)
        if base_revision else ({}, [])
    )
    field_refs, view_refs = _index_omni_references(graph_files)
    base_fields, base_views = _index_omni_references(base_graph_files)

    removed_columns_by_model, removed_models, mode, notes = _resolve_dbt_changes(
        dbt_files=dbt_files,
        settings=settings,
        base_ref=base_ref,
        root=root,
    )
    # A precise dbt relation cannot prove that an Omni relation with an implicit
    # database/schema is unrelated. Do not broaden it, or silently call it clean.
    unique_views = {reference.identity: reference for bucket in view_refs.values() for reference in bucket}
    for token in sorted(removed_columns_by_model.keys() | removed_models.keys()):
        if any(_relation_requires_context(token, reference.sql_table_name) for reference in unique_views.values()):
            notes.append(f"Incomplete coverage: dbt relation '{token}' has less-qualified Omni relation candidates; "
                         "supply explicit relation metadata or table_mapping rather than guessing the connection default.")

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

    for scope, coverage in (("candidate", root_coverage), ("base", base_coverage)):
        for entry in coverage:
            if entry["status"] != "available":
                notes.append(f"Incomplete coverage: {scope} Omni root '{entry['path']}' is {entry['status']}.")
    if dbt_files and not graph_files:
        notes.append("Incomplete coverage: no committed Omni YAML was available to index.")
    for entry in root_coverage:
        selected = {path: value for path, value in graph_files.items() if _under_any(path, [entry["path"]])}
        _, indexed = _index_omni_references(selected)
        entry["views_indexed"] = len({ref.identity for bucket in indexed.values() for ref in bucket})
        if entry["status"] == "available" and not indexed:
            prior = {path: value for path, value in base_graph_files.items() if _under_any(path, [entry["path"]])}
            _, prior_views = _index_omni_references(prior)
            # Metadata alone cannot establish absence of schema-layer views.
            # A non-destructive diff needs no dependency gate; an exact deletion
            # of prior authored views can establish no remaining authored refs.
            if _known_zero_view_model(selected) and (
                (not removed_columns_by_model and not removed_models) or (base_revision and prior_views)
            ):
                entry.update(status="not_applicable", reason="no_candidate_references")
            else:
                entry["status"] = "unindexed"
                notes.append(f"Incomplete coverage: Omni root '{entry['path']}' has no indexed view evidence.")
    gaps = [note for note in notes if note.startswith("Incomplete coverage:")]
    if gaps:
        issues.append({
            "validator": VALIDATOR,
            "rule": COVERAGE_RULE,
            "reason_code": "dbt_impact_coverage_incomplete",
            "severity": "error" if settings.fail_on_incomplete_coverage else "warning",
            "message": "dbt impact could not establish complete coverage. Supply complete evidence before gating.",
            "coverage_gaps": gaps[:MAX_SAMPLES],
        })
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
        "coverage_complete": not gaps,
        "execution_status": "partial" if gaps else "completed",
        "validation_complete": not gaps,
        "reference_scope": "candidate" if head_revision else "local",
        "root_coverage": root_coverage,
        "base_root_coverage": base_coverage,
        "base_omni_views_indexed": len({ref.identity for bucket in base_views.values() for ref in bucket}),
        "base_omni_fields_indexed": len(base_fields),
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
    if not dbt_files:
        return {}, {}, "not_applicable", notes
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
    head_text = read_head_text(settings.manifest_path, root=root, max_bytes=MAX_MANIFEST_BYTES)
    if head_text is None:
        notes.append(
            f"Incomplete coverage: configured dbt manifest '{settings.manifest_path}' was not found at the head; "
            "falling back to SQL heuristics."
        )
        return None
    if not base_ref:
        notes.append(
            "Incomplete coverage: no base ref was available to compare the dbt manifest against; "
            "falling back to SQL heuristics."
        )
        return None
    base_text = _git_show(base_ref, settings.manifest_path, root=root, max_bytes=MAX_MANIFEST_BYTES)
    if base_text is None:
        notes.append(
            f"Incomplete coverage: could not read '{settings.manifest_path}' from the base ref; "
            "falling back to SQL heuristics."
        )
        return None

    base_models = parse_manifest(base_text, source=f"{base_ref}:{settings.manifest_path}")
    head_models = parse_manifest(head_text, source=settings.manifest_path)
    if not base_models:
        notes.append("Incomplete coverage: base manifest contains no materialized nodes.")
    if any(not model.contract_enforced or not model.columns for model in (*base_models.values(), *head_models.values())):
        notes.append("Incomplete coverage: manifest columns require enforced, nonempty dbt contracts on both revisions.")
    if base_text == head_text:
        notes.append("Incomplete coverage: the committed manifest is unchanged despite dbt source changes.")
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
        notes.append("Incomplete coverage: no base ref was available, so dbt SQL could not be compared.")
        return removed_columns, removed_models

    total_bytes = 0
    for path in dbt_files:
        model_name = model_name_from_path(path)
        if not model_name or not path.lower().endswith(".sql") or "macros" in Path(path).parts:
            notes.append(f"Incomplete coverage: '{path}' requires manifest analysis.")
            continue
        base_sql = _git_show(base_ref, path, root=root, max_bytes=MAX_SQL_BYTES)
        head_sql = read_head_text(path, root=root, max_bytes=MAX_SQL_BYTES)
        total_bytes += len((base_sql or "").encode()) + len((head_sql or "").encode())
        if total_bytes > MAX_SQL_TOTAL_BYTES:
            raise SecurityPolicyError("dbt SQL comparison exceeds the 50 MiB safety limit")
        if base_sql is None:
            # New model file: nothing existed before, so nothing can be orphaned.
            continue
        if head_sql is None:
            removed_models[model_name] = model_name
            continue
        base_columns, base_complete = analyze_output_columns(base_sql)
        head_columns, head_complete = analyze_output_columns(head_sql)
        if not base_complete or not head_complete:
            notes.append(f"Incomplete coverage: unsupported SQL projection in '{path}'.")
            continue
        missing = base_columns - head_columns
        if missing:
            removed_columns.setdefault(model_name, set()).update(missing)
    if not removed_columns and not removed_models:
        notes.append(
            "SQL heuristics found no removed output columns. Commit a dbt manifest for precise analysis."
        )
    return removed_columns, removed_models


def _model_tokens(model: DbtModel, settings: DbtImpactSettings) -> set[str]:
    tokens: set[str] = set()
    for mapping in settings.table_mapping:
        if mapping.get("dbt_model", "").strip().lower() != model.name.lower():
            continue
        # An explicit physical relation is stronger evidence than an alias.
        value = mapping.get("sql_table_name") or mapping.get("omni_view")
        if value:
            tokens.add(_clean_relation(value))
    return tokens or {model.canonical_relation()}


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
        safe = _safe_repo_path(Path(relative))
        directory = root
        for part in Path(safe).parts:
            directory /= part
            if directory.is_symlink():
                raise SecurityPolicyError("dbt impact Omni roots must not traverse symlinks")
        if root.is_symlink() or not directory.resolve().is_relative_to(root.resolve()):
            raise SecurityPolicyError("dbt impact Omni roots must remain inside the checkout without symlinks")
        if not directory.is_dir():
            continue
        for name, payload in load_yaml_files(directory).items():
            path = (Path(safe) / name).as_posix()
            if not is_repo_control_file(path):
                merged[path] = payload
    return merged


def _load_omni_snapshot(root: Path, paths: list[str], *, revision: tuple[str, str] | None,
                        external_snapshot=None):
    try:
        return _load_omni_snapshot_data(root, paths, revision=revision, external_snapshot=external_snapshot)
    except OmniFlowError as exc:
        if external_snapshot is not None:
            # Preserve the safety/access exit class, but identify which source
            # failed rather than returning an unclassified inventory message.
            exc.reason_code = "omni_repository_snapshot_unavailable"
        raise


def _load_omni_snapshot_data(root: Path, paths: list[str], *, revision: tuple[str, str] | None,
                             external_snapshot=None):
    """Read bounded data, never check out or execute the candidate revision."""
    if revision is None and external_snapshot is None:
        files = _load_omni_files(root, paths)
        coverage = [{"path": path, "status": "available" if (root / path).is_dir() else "missing"}
                    for path in paths]
    else:
        if external_snapshot is not None:
            repository, sha = external_snapshot.identity
            entries = list(external_snapshot.tree.values())
            local = False
        else:
            sha, repository = revision
            try:
                entries = _local_tree(sha, root=root)
                local = True
            except _GitUnavailable:
                entries = _remote_tree(repository, sha)
                local = False
        if len(entries) > MAX_TREE_ENTRIES:
            raise SecurityPolicyError("dbt impact Git tree exceeds the file inventory limit")
        tree = {}
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise ConfigError("dbt impact requires a complete Git inventory")
            path = _safe_repo_path(Path(entry["path"]))
            if path != entry["path"] or path in tree:
                raise ConfigError("dbt impact Git inventory contains ambiguous paths")
            tree[path] = entry
        coverage = []
        raw = {}
        total_bytes = 0
        for path in paths:
            selected = {name: entry for name, entry in tree.items() if _under_any(name, [path])}
            coverage.append({"path": path, "status": "available" if any(
                entry.get("type") == "blob" for entry in selected.values()) else "missing"})
            for name, entry in selected.items():
                if name in raw or is_repo_control_file(name) or not is_semantic_file(name):
                    continue
                if entry.get("type") != "blob" or entry.get("mode") not in {"100644", "100755"}:
                    raise SecurityPolicyError("dbt impact source must be a regular Git file")
                size, blob = entry.get("size"), entry.get("sha")
                if type(size) is not int or size < 0 or size > MAX_YAML_FILE_BYTES or not FULL_SHA.fullmatch(str(blob)):
                    raise SecurityPolicyError("dbt impact source has invalid size or identity")
                total_bytes += size
                if len(raw) >= MAX_YAML_FILES or total_bytes > MAX_YAML_TOTAL_BYTES:
                    raise SecurityPolicyError("dbt impact Omni source exceeds snapshot limits")
                if local:
                    value = _bounded_git(["cat-file", "blob", blob], root=root, limit=MAX_YAML_FILE_BYTES)
                    if len(value) != size:
                        raise ConfigError("dbt impact Git snapshot is incomplete")
                    try:
                        raw[name] = value.decode("utf-8")
                    except UnicodeError as exc:
                        raise ConfigError("dbt impact source must be UTF-8") from exc
                else:
                    raw[name] = (external_snapshot.read_text(name, max_bytes=MAX_YAML_FILE_BYTES)
                                 if external_snapshot is not None else
                                 read_github_text(repository, sha, name, max_bytes=MAX_YAML_FILE_BYTES))
                    if raw[name] is None:
                        raise ConfigError("dbt impact source disappeared from the immutable inventory")
        files = parse_yaml_file_map(raw)
    for entry in coverage:
        selected = {name: payload for name, payload in files.items() if _under_any(name, [entry["path"]])}
        if entry["status"] == "available" and not selected:
            entry["status"] = "empty"
        for name, payload in selected.items():
            identity = filename_identity(name)
            if identity.endswith(".view") and not isinstance(payload, dict):
                entry["status"] = "unsupported"
            if _external_dependencies(payload):
                entry["status"] = "unsupported"
    return files, coverage


def _external_dependencies(value: Any) -> bool:
    if isinstance(value, dict):
        return any((key in {"extends", "include", "includes", "imports", "import", "base_model", "shared_model"}
                    and bool(child)) or _external_dependencies(child) for key, child in value.items())
    if isinstance(value, list):
        return any(_external_dependencies(child) for child in value)
    return False


def _known_zero_view_model(files: dict[str, Any]) -> bool:
    models = 0
    for path, payload in files.items():
        identity = filename_identity(path)
        if identity == "model" and isinstance(payload, dict) and not payload.get("topics"):
            models += 1
        elif identity in {"relationships"} or identity.endswith(".relationships"):
            if payload not in (None, [], {}):
                return False
        else:
            return False
        if _external_dependencies(payload):
            return False
    return models == 1


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
    identity = filename_identity(file_path)
    if infer_file_kind(file_path, payload) != "view":
        return False
    return identity.endswith(".view") or bool(_field_groups(payload)) or bool(
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


def _relation_requires_context(known: str, candidate: str) -> bool:
    """A shorter matching suffix is not proof of the omitted database/schema."""
    known, candidate = _clean_relation(known), _clean_relation(candidate)
    return bool(candidate) and known.count(".") > candidate.count(".") and known.endswith("." + candidate)


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
                    "reason_code": ORPHANED_COLUMN_RULE,
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
                "reason_code": ORPHANED_MODEL_RULE,
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
    reference_text = sql_reference_text(reference.sql)
    if any(token.lower() == target for token in TABLE_TOKEN_RE.findall(reference_text)):
        return True
    # ${field} and ${view.field} identify Omni fields, not physical columns.
    # Their transitive dependencies belong to the field graph. Leaving their
    # words in this direct matcher can incorrectly block an unrelated removal.
    lowered = re.sub(r"\$\{[^{}]*\}", " ", reference_text).lower()
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(target)}(?![A-Za-z0-9_])", lowered) is not None


def _under_any(path: str, prefixes: list[str]) -> bool:
    normalized = path.strip().strip("/")
    if not normalized:
        return False
    for prefix in prefixes:
        cleaned = prefix.strip().strip("/")
        if not cleaned:
            continue
        if cleaned == "." or normalized == cleaned or normalized.startswith(f"{cleaned}/"):
            return True
    return False


def _git_show(ref: str, path: str, *, root: Path = Path("."), max_bytes: int = MAX_SQL_BYTES) -> str | None:
    return read_git_text(ref, path, root=root, max_bytes=max_bytes)
