"""Static, advisory dbt-column to authored-Omni-field evidence (schema version 1).

This module executes no SQL/dbt and has no API client. The default path is
credential-free; an optional caller-supplied canonical-name resolver owns any
metadata API reads, source proof, credential scope, and request bounds. Its private
return value contains ``coverage_complete``, fixed-code ``coverage_gaps`` and an
unsampled ``impacts`` list. Every impact identifies an exact registered
``model_path``, model-relative ``view_file``, ``field_name``, ``source_revision``
(base/head), change kind, dbt model/relation/column, confidence and dependency
path. These are internal identifiers, not permission to publish customer text.

An impact is potential downstream exposure, NOT a validator failure. Independent
legacy orphan checks retain their policy. Missing/unsupported evidence is a gap,
never an empty proof of safety. No API view identity is guessed from these paths.
"""
from __future__ import annotations

import ast
import hashlib
import re
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import Dialect, exp
from sqlglot.errors import SqlglotError

from .config import DbtImpactSettings
from .dbt_impact import (
    MAX_DBT_FILES,
    MAX_SQL_TOTAL_BYTES,
    _clean_relation,
    _field_groups,
    _git_show,
    _is_view_file,
    _load_omni_snapshot,
    _relation_requires_context,
    _under_any,
    _view_name,
    _view_relation,
)
from .dbt_manifest import MAX_MANIFEST_BYTES, DbtModel, parse_manifest
from .dbt_sql_diff import JINJA_RE, MAX_SQL_BYTES, model_name_from_path, sql_reference_text
from .exceptions import SecurityPolicyError
from .revision_data import pull_request_revision, read_head_text

MAX_GRAPH_FIELDS = 20_000
MAX_GRAPH_EDGES = 100_000
MAX_DEPENDENCY_DEPTH = 64
MAX_IMPACTS = 100_000
MAX_COVERAGE_GAPS = 1_000
TEMPLATE_REFERENCE = re.compile(r"\$\{\s*([^{}]+?)\s*\}")
CanonicalViewResolver = Callable[[str, str, dict[str, Any]], dict[str, str]]


@dataclass
class Projection:
    columns: dict[str, str] = field(default_factory=dict)
    context: str = ""
    complete: bool = False
    reason: str = "dbt_sql_unsupported"


def _canonical(node: exp.Expression, dialect: str | None) -> str:
    return node.sql(dialect=dialect, pretty=False, comments=False, normalize=True)


def _identifier_key(identifier: exp.Identifier, dialect: str | None) -> str:
    return Dialect.get_or_raise(dialect).normalize_identifier(identifier.copy()).name


def _reference_text(sql: str, dialect: str | None) -> str:
    escapes = Dialect.get_or_raise(dialect).tokenizer_class.STRING_ESCAPES
    return sql_reference_text(sql, backslash_escapes="\\" in escapes)


def _relation_parts(value: str, dialect: str | None) -> tuple[str, ...] | None:
    try:
        parsed = Dialect.get_or_raise(dialect).parse_into(exp.Table, value)
        if len(parsed) != 1 or not isinstance(parsed[0], exp.Table):
            return None
        parts = parsed[0].parts
        if not 1 <= len(parts) <= 3 or not all(isinstance(part, exp.Identifier) for part in parts):
            return None
        return tuple(_identifier_key(part, dialect) for part in parts)
    except (SqlglotError, ValueError, TypeError):
        return None


def _impact_tokens(model: DbtModel, settings: DbtImpactSettings, dialect: str | None,
                   gaps: list[dict], scope: str) -> set[str]:
    mapped = [entry for entry in settings.table_mapping
              if entry.get("dbt_model", "").strip().lower() == model.name.lower()]
    raw = [entry.get("sql_table_name") for entry in mapped if entry.get("sql_table_name")]
    if not mapped:
        raw = [model.relation_name or ".".join(part for part in
               (model.database, model.schema_name, model.alias or model.name) if part)]
    tokens = set()
    for entry in mapped:
        if not entry.get("sql_table_name") and entry.get("omni_view"):
            # Explicit semantic view identities are not physical SQL identifiers
            # and must not undergo warehouse case folding.
            tokens.add(exp.Identifier(this=entry["omni_view"], quoted=True).sql())
    for value in raw:
        parts = _relation_parts(value, dialect)
        if parts is None:
            _gap(gaps, "dbt_relation_identity_unavailable", dbt_model=model.unique_id, source_revision=scope)
            continue
        # Canonical quoted spelling retains the dialect's resolved case, including
        # case-sensitive quoted names, across before/after metadata comparison.
        tokens.add(".".join(exp.Identifier(this=part, quoted=True).sql() for part in parts))
    return tokens


def analyze_projection(sql: str, *, dialect: str | None = None) -> Projection:
    """Compare supported SELECT expressions, never raw formatting or comments.

    Source-position ref/source calls become opaque stable relation tokens. All
    other Jinja, unknown columns from stars and ambiguous aliases remain gaps.
    Context includes WHERE/JOIN/CTEs/grouping, so changing them cannot hide behind
    an unchanged final select list.
    """
    if len(sql.encode()) > MAX_SQL_BYTES:
        raise SecurityPolicyError("dbt model SQL exceeds the 5 MiB safety limit")
    replacements: dict[int, str] = {}
    reference_text = _reference_text(sql, dialect)
    for match in JINJA_RE.finditer(sql):
        if reference_text[match.start():match.start() + 2] not in {"{{", "{%", "{#"}:
            continue
        if (not re.fullmatch(r"\{\{\s*(?:ref|source)\s*\([^{}]*\)\s*\}\}", match.group(), re.DOTALL)
                or not re.search(r"\b(?:FROM|JOIN)\s*$", sql[:match.start()], re.IGNORECASE)):
            return Projection(reason="dbt_sql_dynamic_projection")
        try:
            call = ast.parse(match.group()[2:-2].strip(), mode="eval").body
            if (not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name)
                    or call.func.id not in {"ref", "source"}
                    or not all(isinstance(item, ast.Constant) and type(item.value) in {str, int}
                               for item in (*call.args, *(keyword.value for keyword in call.keywords)))):
                return Projection(reason="dbt_sql_dynamic_projection")
        except (SyntaxError, ValueError):
            return Projection(reason="dbt_sql_dynamic_projection")
        token = hashlib.sha256(ast.dump(call, include_attributes=False).encode()).hexdigest()
        replacements[match.start()] = f"omniflow_dbt_source_{token}"
    candidate = JINJA_RE.sub(lambda match: replacements.get(match.start(), match.group()), sql)
    try:
        statements = sqlglot.parse(candidate, read=dialect, error_level=sqlglot.ErrorLevel.RAISE)
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            return Projection()
        tree = statements[0]
        # A wildcard in a CTE also hides dependencies. COUNT(*) is not a wildcard
        # output projection and remains supported.
        for select in tree.find_all(exp.Select):
            for item in select.expressions:
                if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and item.is_star):
                    return Projection(reason="dbt_sql_wildcard")
        columns = {}
        for item in tree.expressions:
            if isinstance(item, exp.Alias):
                identifier = item.args.get("alias")
                expression = item.this
            elif isinstance(item, exp.Column) and not item.is_star:
                identifier = item.this
                expression = item
            else:
                return Projection(reason="dbt_sql_output_identity_unavailable")
            if not isinstance(identifier, exp.Identifier):
                return Projection(reason="dbt_sql_output_identity_unavailable")
            name = _identifier_key(identifier, dialect)
            if name in columns:
                return Projection(reason="dbt_sql_duplicate_output")
            columns[name] = _canonical(expression, dialect)
        if not columns:
            return Projection()
        context = tree.copy()
        context.set("expressions", [])
        return Projection(columns, _canonical(context, dialect), True, "")
    except (SqlglotError, ValueError, TypeError):
        return Projection()


@dataclass
class View:
    root: str
    file: str
    name: str
    relation: str
    fields: dict[str, dict[str, Any]]
    raw_relation: str = ""


@dataclass
class FieldGraph:
    views: dict[str, View]
    physical: dict[tuple[str, str], set[tuple[str, str]]]
    reverse: dict[tuple[str, str], set[tuple[str, str]]]
    dialect: str | None = None
    aliases: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    explicit_view_aliases: frozenset[str] = frozenset()


def _gap(gaps: list[dict], reason: str, **context: Any) -> None:
    item = {"reason_code": reason, **context}
    if len(gaps) >= MAX_COVERAGE_GAPS:
        if gaps[-1]["reason_code"] != "impact_evidence_limit":
            gaps.append({"reason_code": "impact_evidence_limit", "omitted_gap_count": 1})
        else:
            gaps[-1]["omitted_gap_count"] += 1
        return
    if item not in gaps:
        gaps.append(item)


def _expression_dependencies(sql: str, dialect: str | None) -> tuple[set[str], set[str], bool]:
    """Physical columns and Omni substitutions, excluding literal/comment text."""
    # This guard preserves quoted identifiers and strips strings/comments. They
    # cannot contain operative Omni references or physical column dependencies.
    reference_text = _reference_text(sql, dialect)
    placeholders: dict[str, str] = {}
    prefix = "omniflow_binding_"
    while prefix in sql:
        prefix += "_"
    table_binding = prefix + "table"

    def replace(match: re.Match) -> str:
        value = match.group(1).strip()
        if value == "TABLE":
            return table_binding
        token = f"{prefix}field_{len(placeholders)}"
        placeholders[token] = value
        return token

    # Literal removal can leave syntactically incomplete expressions (e.g. CASE
    # x = 'literal'). Parse the original expression with only active references
    # substituted, preserving original literal contents for syntax only.
    candidate_parts = []
    cursor = 0
    for match in TEMPLATE_REFERENCE.finditer(reference_text):
        candidate_parts.extend((sql[cursor:match.start()], replace(match)))
        cursor = match.end()
    candidate = "".join((*candidate_parts, sql[cursor:]))
    physical: set[str] = set()
    references: set[str] = set()
    try:
        parsed = sqlglot.parse("SELECT " + candidate, read=dialect, error_level=sqlglot.ErrorLevel.RAISE)
        if len(parsed) != 1 or not isinstance(parsed[0], exp.Select):
            return physical, references, False
        tree = parsed[0]
        if tree.args.get("from_") or tree.find(exp.Subquery) or len(list(tree.find_all(exp.Select))) != 1:
            return physical, references, False
        for column in tree.find_all(exp.Column):
            name = _identifier_key(column.this, dialect)
            if column.name in placeholders and not column.table:
                references.add(placeholders[column.name])
            elif column.table == table_binding:
                physical.add(name)
            elif not column.table:
                physical.add(name)
            else:
                return physical, references, False
        if any(isinstance(item, exp.Star) or (isinstance(item, exp.Column) and item.is_star)
               for item in tree.expressions):
            return physical, references, False
        return physical, references, True
    except (SqlglotError, ValueError, TypeError):
        return physical, references, False


def _has_inheritance(value: Any) -> bool:
    if isinstance(value, dict):
        return any((key in {"extends", "extend", "extension", "inheritance", "include", "includes",
                            "imports", "import", "base_model", "shared_model"} and bool(child))
                   or _has_inheritance(child) for key, child in value.items())
    if isinstance(value, list):
        return any(_has_inheritance(child) for child in value)
    return False


def _graph(files: dict[str, Any], roots: list[str], scope: str, dialect: str | None, gaps: list[dict],
           canonical_view_resolver: CanonicalViewResolver | None = None,
           explicit_view_aliases: frozenset[str] = frozenset()) -> FieldGraph:
    views: dict[str, View] = {}
    aliases: dict[tuple[str, str], set[str]] = defaultdict(set)
    for path, payload in sorted(files.items()):
        if _has_inheritance(payload):
            _gap(gaps, "omni_inheritance_unsupported", file=path, source_revision=scope)
            continue
        if not isinstance(payload, dict) or not _is_view_file(path, payload):
            continue
        matching = [root for root in roots if _under_any(path, [root])]
        if len(matching) != 1:
            _gap(gaps, "omni_model_scope_ambiguous", file=path, source_revision=scope)
            continue
        root = matching[0]
        if "sql" in payload or "query" in payload:
            _gap(gaps, "omni_query_view_unsupported", model_path=root, file=path, source_revision=scope)
            continue
        definitions: dict[str, dict] = {}
        for group in _field_groups(payload):
            for name, definition in group.items():
                if not isinstance(name, str) or not name or name in definitions or not isinstance(definition, (dict, type(None))):
                    _gap(gaps, "omni_field_identity_ambiguous", model_path=root, file=path, source_revision=scope)
                    continue
                definitions[name] = definition or {}
        view_name = _view_name(path, payload)
        raw_relation = payload.get("sql_table_name") or ".".join(str(part) for part in (
            payload.get("catalog") or payload.get("database"), payload.get("schema") or payload.get("schema_name"),
            payload.get("table_name") or view_name) if part)
        if not isinstance(raw_relation, str) or _relation_parts(raw_relation, dialect) is None:
            _gap(gaps, "omni_relation_identity_unavailable", file=path, source_revision=scope)
            continue
        view = View(root, path, view_name, _view_relation(payload, view_name), definitions, raw_relation)
        views[path] = view
        aliases[(root, view.name)].add(path)
        if sum(len(view.fields) for view in views.values()) > MAX_GRAPH_FIELDS:
            raise SecurityPolicyError("dbt content impact exceeds the field graph safety limit")
    if canonical_view_resolver is not None:
        for root in roots:
            scoped_files = {path: payload for path, payload in files.items() if _under_any(path, [root])}
            try:
                names = canonical_view_resolver(scope, root, scoped_files)
                if (not isinstance(names, dict)
                        or any(not isinstance(path, str) or path not in scoped_files
                               or not isinstance(scoped_files[path], dict)
                               or not _is_view_file(path, scoped_files[path])
                               or not isinstance(name, str) or not name or name != name.strip()
                               or len(name) > 1024 or any(ord(char) < 32 for char in name)
                               for path, name in names.items())
                        or len(set(names.values())) != len(names)):
                    raise ValueError("unverified canonical view mapping")
            except SecurityPolicyError:
                raise
            except Exception:
                # Resolver failures are identity evidence gaps, never a reason
                # to expose API payloads or to invent canonical aliases.
                _gap(gaps, "omni_view_identity_unavailable", model_path=root, source_revision=scope)
                continue
            for path, view in views.items():
                if view.root != root:
                    continue
                if path not in names:
                    _gap(gaps, "omni_view_identity_unavailable", model_path=root, file=path, source_revision=scope)
                else:
                    aliases[(root, names[path])].add(path)
    physical: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    reverse: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    edge_count = 0
    for path, view in views.items():
        for name, definition in view.fields.items():
            node = (path, name)
            refs: set[str] = set()
            sql = definition.get("sql")
            if isinstance(sql, str) and sql.strip():
                columns, refs, complete = _expression_dependencies(sql, dialect)
                if not complete:
                    _gap(gaps, "omni_field_expression_unsupported", model_path=view.root, file=path,
                         field=name, source_revision=scope)
                for column in columns:
                    physical[(path, column)].add(node)
            elif sql is not None:
                _gap(gaps, "omni_field_expression_unsupported", model_path=view.root, file=path,
                     field=name, source_revision=scope)
            elif definition.get("aggregate_type") != "count":
                physical[(path, _identifier_key(exp.Identifier(this=name), dialect))].add(node)
            filters = definition.get("filters")
            if isinstance(filters, dict):
                refs.update(key for key in filters if isinstance(key, str))
            elif filters is not None:
                _gap(gaps, "omni_field_filter_unsupported", model_path=view.root, file=path,
                     field=name, source_revision=scope)
            for reference in refs:
                if "." in reference:
                    alias, target = reference.rsplit(".", 1)
                    choices = aliases.get((view.root, alias), set())
                else:
                    target, choices = reference, {path}
                if len(choices) != 1:
                    _gap(gaps, "omni_field_reference_ambiguous", model_path=view.root, file=path,
                         field=name, source_revision=scope)
                    continue
                target_path = next(iter(choices))
                if target not in views[target_path].fields:
                    _gap(gaps, "omni_field_reference_unresolved", model_path=view.root, file=path,
                         field=name, source_revision=scope)
                    continue
                reverse[(target_path, target)].add(node)
                edge_count += 1
                if edge_count > MAX_GRAPH_EDGES:
                    raise SecurityPolicyError("dbt content impact exceeds the dependency graph safety limit")
    return FieldGraph(views, physical, reverse, dialect, dict(aliases), explicit_view_aliases)


def _manifest(settings: DbtImpactSettings, root: Path, base_ref: str | None, gaps: list[dict]) -> tuple[dict, dict]:
    if not settings.manifest_path:
        return {}, {}
    texts = (
        _git_show(base_ref, settings.manifest_path, root=root, max_bytes=MAX_MANIFEST_BYTES) if base_ref else None,
        read_head_text(settings.manifest_path, root=root, max_bytes=MAX_MANIFEST_BYTES),
    )
    models = []
    for scope, text in zip(("base", "head"), texts, strict=True):
        if text is None:
            _gap(gaps, "dbt_manifest_unavailable", source_revision=scope)
            models.append({})
        else:
            models.append(parse_manifest(text, source="dbt impact manifest"))
    return models[0], models[1]


def _relation(path: str, models: dict[str, DbtModel], settings: DbtImpactSettings, gaps: list[dict],
              scope: str, dialect: str | None) -> tuple[str, set[str]]:
    name = model_name_from_path(path) or ""
    if not name:
        _gap(gaps, "dbt_model_identity_ambiguous", file=path, source_revision=scope)
        return name, set()
    exact = [model for model in models.values() if model.original_file_path == path]
    matches = exact or [model for model in models.values() if model.name.lower() == name]
    if len(matches) > 1:
        _gap(gaps, "dbt_model_identity_ambiguous", file=path, source_revision=scope)
        return name, set()
    if matches:
        return matches[0].unique_id, _impact_tokens(matches[0], settings, dialect, gaps, scope)
    model = DbtModel(unique_id=name, name=name)
    return name, _impact_tokens(model, settings, dialect, gaps, scope)


def _matched_views(graph: FieldGraph, relation: str, gaps: list[dict], scope: str) -> list[View]:
    known_parts = _relation_parts(relation, graph.dialect)
    if known_parts and len(known_parts) == 1 and known_parts[0] in graph.explicit_view_aliases:
        selected = []
        found_choices = False
        for root in {view.root for view in graph.views.values()}:
            choices = graph.aliases.get((root, known_parts[0]), set())
            found_choices = found_choices or bool(choices)
            if len(choices) > 1:
                _gap(gaps, "dbt_relation_ambiguous", model_path=root, relation=relation, source_revision=scope)
            elif choices:
                selected.append(graph.views[next(iter(choices))])
        if not found_choices:
            _gap(gaps, "dbt_explicit_view_mapping_unresolved", configured_view=known_parts[0],
                 source_revision=scope)
        return selected
    relation = _clean_relation(relation)
    for view in graph.views.values():
        if _relation_requires_context(relation, view.relation):
            _gap(gaps, "dbt_relation_context_unavailable", model_path=view.root, file=view.file,
                 relation=relation, source_revision=scope)
    # Match only qualified suffixes or an explicit view alias, never broaden a
    # known schema back to its bare table token.
    selected = [view for view in graph.views.values()
                if view.relation == relation or view.relation.endswith("." + relation)
                or ("." not in relation and view.name.lower() == relation)]
    safe_selected = []
    for view in selected:
        candidate_parts = _relation_parts(view.raw_relation, graph.dialect)
        if (known_parts is None or candidate_parts is None
                or tuple(candidate_parts[-len(known_parts):]) != known_parts):
            _gap(gaps, "dbt_relation_case_ambiguous", model_path=view.root, file=view.file,
                 relation=relation, source_revision=scope)
        else:
            safe_selected.append(view)
    selected = safe_selected
    for root in {view.root for view in selected}:
        scoped = [view for view in selected if view.root == root]
        if len({view.relation for view in scoped}) > 1:
            _gap(gaps, "dbt_relation_ambiguous", model_path=root, relation=relation, source_revision=scope)
            selected = [view for view in selected if view.root != root]
    return selected


def _emit(graph: FieldGraph, *, model: str, relation: str, column: str, kind: str,
          scope: str, gaps: list[dict], impacts: list[dict]) -> None:
    for view in _matched_views(graph, relation, gaps, scope):
        seeds = graph.physical.get((view.file, column), set()) if column else {(view.file, field) for field in view.fields}
        queue = deque((node, (node,)) for node in sorted(seeds))
        visited: set[tuple[str, str]] = set()
        while queue:
            node, chain = queue.popleft()
            if node in visited:
                continue
            visited.add(node)
            target = graph.views[node[0]]
            item = {
                "model_path": target.root,
                "view_file": Path(target.file).relative_to(target.root).as_posix(),
                "field_name": node[1], "source_revision": scope, "change_kind": kind,
                "dbt_model": model, "relation": relation, "column": column,
                "confidence": "potential" if kind == "potential" else "exact",
                "dependency_path": [f"{relation}.{column}" if column else relation]
                + [f"{Path(file).relative_to(target.root).as_posix()}#{field}" for file, field in chain],
            }
            impacts.append(item)
            if len(impacts) > MAX_IMPACTS:
                raise SecurityPolicyError("dbt content impact exceeds the impact safety limit")
            for dependent in sorted(graph.reverse.get(node, set())):
                if dependent in chain:
                    _gap(gaps, "omni_field_dependency_cycle", model_path=target.root, source_revision=scope)
                elif len(chain) >= MAX_DEPENDENCY_DEPTH:
                    _gap(gaps, "omni_field_dependency_depth", model_path=target.root, source_revision=scope)
                elif dependent not in visited:
                    queue.append((dependent, (*chain, dependent)))


def _manifest_deltas(manifests: dict[str, dict[str, DbtModel]], settings: DbtImpactSettings,
                     gaps: list[dict], pending: list[dict], dialect: str | None) -> None:
    """Relation/declared schema changes complement, never replace, SQL evidence."""
    for identity in sorted(manifests["base"].keys() | manifests["head"].keys()):
        before, after = manifests["base"].get(identity), manifests["head"].get(identity)
        relations_before = _impact_tokens(before, settings, dialect, gaps, "base") if before else set()
        relations_after = _impact_tokens(after, settings, dialect, gaps, "head") if after else set()
        for scope, removed, kind in (("base", relations_before - relations_after, "removed"),
                                     ("head", relations_after - relations_before, "added")):
            for relation in sorted(removed):
                pending.append(dict(model=identity, relation=relation, column="", kind=kind, scope=scope))
        if not before or not after or before.columns == after.columns:
            continue
        if not before.contract_enforced or not after.contract_enforced or not before.columns or not after.columns:
            _gap(gaps, "dbt_manifest_contract_unavailable", dbt_model=identity)
            for scope, relations in (("base", relations_before), ("head", relations_after)):
                for relation in sorted(relations):
                    pending.append(dict(model=identity, relation=relation, column="", kind="potential", scope=scope))
            continue
        for scope, columns, relations, kind in (
            ("base", before.columns - after.columns, relations_before, "removed"),
            ("head", after.columns - before.columns, relations_after, "added"),
        ):
            for relation in sorted(relations):
                for column in sorted(columns):
                    key = _identifier_key(exp.Identifier(this=column), dialect)
                    pending.append(dict(model=identity, relation=relation, column=key, kind=kind, scope=scope))


def collect_column_impacts(
    *, changed_files: list[str], dbt_paths: list[str], settings: DbtImpactSettings,
    omni_yaml_paths: list[str], base_ref: str | None, repo_root: Path | None = None,
    canonical_view_resolver: CanonicalViewResolver | None = None,
) -> dict[str, Any]:
    """Return bounded private impact evidence without changing validation policy.

    Optional resolver receives (base/head, registered model root, immutable
    authored snapshot) and returns repository-relative view paths -> verified
    canonical names. Its caller owns credential scope and source-identity proof.
    No resolver or Omni snapshot work occurs for proven no-op SQL changes.
    """
    root = repo_root or Path(".")
    gaps: list[dict] = []
    impacts: list[dict] = []
    dbt_files = sorted({path for path in changed_files if _under_any(path, dbt_paths)})
    result = {"schema_version": 1, "coverage_complete": True, "coverage_gaps": gaps, "impacts": impacts}
    if not dbt_files:
        return result
    if len(dbt_files) > MAX_DBT_FILES:
        raise SecurityPolicyError("dbt content impact exceeds the changed-file safety limit")
    base_revision = pull_request_revision("base")
    if base_revision:
        base_ref = base_revision[0]
    if not base_ref:
        _gap(gaps, "dbt_base_revision_unavailable")
        result["coverage_complete"] = False
        return result
    dialect = getattr(getattr(settings, "content_impact", None), "sql_dialect", None)
    pending: list[dict] = []
    manifests = dict(zip(("base", "head"), _manifest(settings, root, base_ref, gaps), strict=True))
    if not any(item["reason_code"] == "dbt_manifest_unavailable" for item in gaps):
        _manifest_deltas(manifests, settings, gaps, pending, dialect)
    total_bytes = 0
    for path in dbt_files:
        if not path.lower().endswith(".sql") or "macros" in Path(path).parts:
            if path != settings.manifest_path:
                _gap(gaps, "dbt_source_unsupported", file=path)
            else:
                _gap(gaps, "dbt_manifest_only_change", file=path)
            continue
        texts = {"base": _git_show(base_ref, path, root=root, max_bytes=MAX_SQL_BYTES),
                 "head": read_head_text(path, root=root, max_bytes=MAX_SQL_BYTES)}
        total_bytes += sum(len(text.encode()) for text in texts.values() if text is not None)
        if total_bytes > MAX_SQL_TOTAL_BYTES:
            raise SecurityPolicyError("dbt content impact SQL exceeds the aggregate safety limit")
        if texts["base"] is None and texts["head"] is None:
            _gap(gaps, "dbt_source_unavailable", file=path)
            continue
        if texts["base"] == texts["head"]:
            continue
        parsed = {scope: analyze_projection(text, dialect=dialect) if text is not None else None
                  for scope, text in texts.items()}
        for scope, projection in parsed.items():
            if projection is not None and not projection.complete:
                _gap(gaps, projection.reason, file=path, source_revision=scope)
        identities = {scope: _relation(path, manifests[scope], settings, gaps, scope, dialect) for scope in ("base", "head")}
        before, after = parsed["base"], parsed["head"]
        deltas: list[tuple[str, str, tuple[str, ...]]] = []
        if before is None:
            if after and after.complete:
                deltas.extend((column, "added", ("head",)) for column in sorted(after.columns))
            else:
                deltas.append(("", "potential", ("head",)))
        elif after is None:
            deltas.append(("", "removed", ("base",)))
        elif not before.complete or not after.complete:
            deltas.append(("", "potential", ("base", "head")))
        else:
            deltas.extend((column, "removed", ("base",)) for column in sorted(before.columns.keys() - after.columns.keys()))
            deltas.extend((column, "added", ("head",)) for column in sorted(after.columns.keys() - before.columns.keys()))
            deltas.extend((column, "modified", ("base", "head")) for column in sorted(before.columns.keys() & after.columns.keys())
                          if before.columns[column] != after.columns[column])
            if before.context != after.context or identities["base"] != identities["head"]:
                deltas.append(("", "potential", ("base", "head")))
        for column, kind, scopes in deltas:
            for scope in scopes:
                model, relations = identities[scope]
                for relation in sorted(relations):
                    pending.append(dict(model=model, relation=relation, column=column, kind=kind, scope=scope))
    # Determine dbt semantic changes first. Unrelated unsupported Omni files must
    # not turn a proven formatting/comment-only change into incomplete evidence.
    # Unsupported dbt SQL still has its explicit gap and potential work above.
    graphs = {}
    for scope, revision in (("base", base_revision), ("head", pull_request_revision("head"))):
        if not any(item["scope"] == scope for item in pending):
            continue
        if scope == "base" and revision is None:
            revision = (base_ref, "")
        files, coverage = _load_omni_snapshot(root, omni_yaml_paths, revision=revision)
        if not coverage:
            _gap(gaps, "omni_root_unavailable", source_revision=scope)
        for item in coverage:
            if item["status"] != "available":
                _gap(gaps, "omni_root_unavailable", model_path=item["path"], source_revision=scope)
        explicit_view_aliases = frozenset(entry["omni_view"] for entry in settings.table_mapping
                                         if entry.get("omni_view") and not entry.get("sql_table_name"))
        graphs[scope] = _graph(files, omni_yaml_paths, scope, dialect, gaps, canonical_view_resolver,
                               explicit_view_aliases)
        for model_path in omni_yaml_paths:
            if not any(view.root == model_path for view in graphs[scope].views.values()):
                _gap(gaps, "omni_view_evidence_unavailable", model_path=model_path, source_revision=scope)
    for item in pending:
        _emit(graphs[item["scope"]], **item, gaps=gaps, impacts=impacts)
    # A field can be reached through several paths. Keep deterministic evidence
    # for each source/column, not duplicate terminal API work or truncated seeds.
    unique = {}
    for item in impacts:
        key = tuple((key, value) for key, value in item.items() if key != "dependency_path")
        if key not in unique:
            unique[key] = item
    result["impacts"] = list(unique.values())
    result["coverage_complete"] = not gaps
    return result
