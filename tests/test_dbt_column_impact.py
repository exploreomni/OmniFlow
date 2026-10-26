"""Static dbt impact evidence: real immutable Git snapshots, never live APIs."""
import json
import os
import subprocess
from unittest import mock

import pytest

from omniflow.config import DbtContentImpactSettings, DbtImpactSettings
from omniflow.dbt_column_impact import analyze_projection, collect_column_impacts
from omniflow.dbt_impact import COVERAGE_RULE, ORPHANED_COLUMN_RULE, evaluate_dbt_impact
from omniflow.exceptions import SecurityPolicyError

VIEW = """sql_table_name: warehouse.marts.orders
dimensions:
  id: {}
  net: {}
  doubled:
    sql: ${net} * 2
  constant:
    sql: "'net'"
measures:
  filtered_count:
    aggregate_type: count
    filters:
      doubled:
        greater_than: 0
"""


@pytest.fixture(autouse=True)
def isolated_environment():
    with mock.patch.dict(os.environ, {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                     "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}, clear=True):
        yield


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def commit(root):
    git(root, "add", ".")
    git(root, "commit", "-qm", "synthetic fixture")
    return git(root, "rev-parse", "HEAD")


def fixture(root, before="select id, amount * 0.9 as net from raw.orders", view=VIEW, extra=None):
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    files = {"models/orders.sql": before, "omni/orders.view.yaml": view, **(extra or {})}
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return commit(root)


def collect(root, base, *, settings=None, changed=None, resolver=None, omni_paths=None):
    return collect_column_impacts(
        changed_files=changed or ["models/orders.sql"], dbt_paths=["models", "target"],
        settings=settings or DbtImpactSettings(content_impact=DbtContentImpactSettings(enabled=True)),
        omni_yaml_paths=omni_paths or ["omni"], base_ref=base, repo_root=root,
        canonical_view_resolver=resolver,
    )


def test_modified_column_transitive_filtered_measure_and_private_schema(tmp_path):
    base = fixture(tmp_path)
    (tmp_path / "models/orders.sql").write_text("select id, amount * 0.8 as net from raw.orders")
    result = collect(tmp_path, base)
    assert result["schema_version"] == 1
    assert result["coverage_complete"] is True
    assert {(item["field_name"], item["source_revision"]) for item in result["impacts"]} == {
        (field, scope) for field in ("net", "doubled", "filtered_count") for scope in ("base", "head")}
    assert all(item["change_kind"] == "modified" and item["column"] == "net" for item in result["impacts"])
    assert all(item["model_path"] == "omni" and item["view_file"] == "orders.view.yaml" for item in result["impacts"])
    assert any(len(item["dependency_path"]) == 4 for item in result["impacts"])


@pytest.mark.parametrize("candidate", [
    "SELECT id, amount * 0.9 AS net FROM raw.orders -- literal {{ unsupported() }}\n",
    "select id, amount /* comment */ * 0.9 as net from raw.orders",
    "select id, amount * .9 as net\nFROM raw.orders",
])
def test_format_and_comment_only_changes_have_no_impact(tmp_path, candidate):
    base = fixture(tmp_path)
    (tmp_path / "models/orders.sql").write_text(candidate)
    result = collect(tmp_path, base)
    assert result["coverage_complete"] is True
    assert result["impacts"] == []


def test_proven_noop_does_not_scan_unrelated_graph_or_resolve_names(tmp_path):
    base = fixture(tmp_path, extra={
        "omni/query.query.view.yaml": "sql: select * from unknown\ndimensions:\n  net: {}\n",
        "omni/unresolved.view.yaml": "sql_table_name: other\ndimensions:\n  net:\n    sql: ${unknown_view.net}\n",
    })
    (tmp_path / "models/orders.sql").write_text("select id, amount * .9 as net from raw.orders -- only a comment\n")
    resolver = mock.Mock(side_effect=AssertionError("no identity API needed"))
    with mock.patch("omniflow.dbt_column_impact._load_omni_snapshot", side_effect=AssertionError("no graph needed")):
        result = collect(tmp_path, base, resolver=resolver)
    assert result["coverage_complete"] is True
    assert result["coverage_gaps"] == [] and result["impacts"] == []
    resolver.assert_not_called()


@pytest.mark.parametrize("base_sql,head_sql", [
    ("select 'old' as net from orders", "select 'new' as net from orders"),
    ("select cast(amount as int) as net from orders", "select cast(amount as varchar) as net from orders"),
    ("select '{{ literal }}' as net from orders", "select '{{ other }}' as net from orders"),
    ('select \'{{ ref("orders") }}\' as net from {{ ref("orders") }}',
     'select \'{{ref("orders")}}\' as net from {{ref("orders")}}'),
])
def test_literals_and_casts_are_meaningful(base_sql, head_sql):
    before, after = analyze_projection(base_sql), analyze_projection(head_sql)
    assert before.complete and after.complete
    assert before.columns["net"] != after.columns["net"]


@pytest.mark.parametrize("candidate", [
    "select id, amount * .9 as net from raw.orders where id > 1",
    "select id, amount * .9 as net from raw.orders join customers on orders.id = customers.id",
    "with changed as (select amount from raw.orders) select id, amount * .9 as net from raw.orders",
    "select id, amount * .9 as net from raw.orders group by id, amount",
])
def test_context_changes_are_wider_potential_not_clean(tmp_path, candidate):
    base = fixture(tmp_path)
    (tmp_path / "models/orders.sql").write_text(candidate)
    result = collect(tmp_path, base)
    assert result["coverage_complete"] is True
    assert {item["field_name"] for item in result["impacts"]} == {"id", "net", "doubled", "constant", "filtered_count"}
    assert all(item["change_kind"] == "potential" and item["confidence"] == "potential" for item in result["impacts"])


@pytest.mark.parametrize("candidate,reason", [
    ("select * from raw.orders", "dbt_sql_wildcard"),
    ("select o.*, id from orders o", "dbt_sql_wildcard"),
    ("select {{ columns() }}, id from orders", "dbt_sql_dynamic_projection"),
    ("select amount as net, tax as net from orders", "dbt_sql_duplicate_output"),
    ("select ( from orders", "dbt_sql_unsupported"),
])
def test_unknown_projection_is_gap_with_potential_scope(tmp_path, candidate, reason):
    base = fixture(tmp_path)
    (tmp_path / "models/orders.sql").write_text(candidate)
    result = collect(tmp_path, base)
    assert result["coverage_complete"] is False
    assert reason in {gap["reason_code"] for gap in result["coverage_gaps"]}
    assert result["impacts"] and all(item["change_kind"] == "potential" for item in result["impacts"])


def test_rename_preserves_before_and_after_mixed_pr_references(tmp_path):
    base = fixture(tmp_path)
    (tmp_path / "models/orders.sql").write_text("select id, amount * .9 as subtotal from raw.orders")
    (tmp_path / "omni/orders.view.yaml").write_text("sql_table_name: warehouse.marts.orders\ndimensions:\n  subtotal: {}\n")
    result = collect(tmp_path, base)
    assert result["coverage_complete"] is True
    assert {(item["column"], item["change_kind"], item["source_revision"]) for item in result["impacts"]} == {
        ("net", "removed", "base"), ("subtotal", "added", "head")}
    assert any(item["field_name"] == "filtered_count" for item in result["impacts"])
    assert all(item["field_name"] == "subtotal" for item in result["impacts"] if item["source_revision"] == "head")


def test_cross_view_dependencies_are_followed(tmp_path):
    base = fixture(tmp_path, extra={"omni/other.view.yaml": "sql_table_name: other\ndimensions:\n  derived:\n    sql: ${orders.doubled}\n"})
    (tmp_path / "models/orders.sql").write_text("select id, amount as net from raw.orders")
    result = collect(tmp_path, base)
    assert result["coverage_complete"] is True
    assert any(item["view_file"] == "other.view.yaml" and item["field_name"] == "derived" for item in result["impacts"])


def test_verified_canonical_names_resolve_cross_schema_references_and_filters(tmp_path):
    base = fixture(tmp_path, view="sql_table_name: unrelated\ndimensions:\n  id: {}\n", extra={
        "omni/marts/orders.view.yaml": "schema: marts\ntable_name: orders\ndimensions:\n  net: {}\n",
        "omni/staging/orders.view.yaml": "schema: staging\ntable_name: orders\ndimensions:\n  net: {}\n",
        "omni/calculated.view.yaml": "sql_table_name: calculated\ndimensions:\n  derived:\n    sql: ${marts__orders.net}\nmeasures:\n  filtered:\n    aggregate_type: count\n    filters:\n      marts__orders.net:\n        greater_than: 0\n",
    })
    (tmp_path / "models/orders.sql").write_text("select id, amount as net from raw.orders")
    settings = DbtImpactSettings(table_mapping=[{"dbt_model": "orders", "sql_table_name": "marts.orders"}],
        content_impact=DbtContentImpactSettings(enabled=True))
    without = collect(tmp_path, base, settings=settings)
    assert "omni_field_reference_ambiguous" in {item["reason_code"] for item in without["coverage_gaps"]}
    calls = []

    def resolver(scope, root, files):
        calls.append((scope, root, sorted(files)))
        return {"omni/orders.view.yaml": "unrelated", "omni/marts/orders.view.yaml": "marts__orders",
                "omni/staging/orders.view.yaml": "staging__orders", "omni/calculated.view.yaml": "calculated"}

    result = collect(tmp_path, base, settings=settings, resolver=resolver)
    assert result["coverage_complete"] is True
    assert len(calls) == 2 and {call[0] for call in calls} == {"base", "head"}
    assert all(call[1] == "omni" and len(call[2]) == 4 for call in calls)
    assert {(item["view_file"], item["field_name"]) for item in result["impacts"]} == {
        ("marts/orders.view.yaml", "net"), ("calculated.view.yaml", "derived"), ("calculated.view.yaml", "filtered")}


@pytest.mark.parametrize("mapping", [{}, {"omni/absent.view.yaml": "orders"},
    {"omni/orders.view.yaml": "same", "omni/other.view.yaml": "same"}, None])
def test_invalid_canonical_mapping_is_incomplete_not_guessed(tmp_path, mapping):
    base = fixture(tmp_path, extra={"omni/other.view.yaml": "sql_table_name: other\ndimensions:\n  id: {}\n"})
    (tmp_path / "models/orders.sql").write_text("select id, amount as net from raw.orders")

    def resolver(*args):
        if mapping is None:
            raise RuntimeError("private transport error")
        return mapping

    result = collect(tmp_path, base, resolver=resolver)
    assert result["coverage_complete"] is False
    assert "omni_view_identity_unavailable" in {item["reason_code"] for item in result["coverage_gaps"]}
    assert "private transport" not in json.dumps(result)


def test_explicit_semantic_view_mapping_does_not_invent_physical_relation_identity(tmp_path):
    base = fixture(tmp_path, view=VIEW.replace("warehouse.marts.orders", "warehouse.billing.invoice"))
    (tmp_path / "models/orders.sql").write_text("select id, amount as net from raw.orders")
    settings = DbtImpactSettings(table_mapping=[{"dbt_model": "orders", "omni_view": "orders"}],
        content_impact=DbtContentImpactSettings(enabled=True, sql_dialect="snowflake"))
    result = collect(tmp_path, base, settings=settings)
    assert result["coverage_complete"] is True and result["impacts"]
    assert {item["field_name"] for item in result["impacts"]} == {"net", "doubled", "filtered_count"}


def test_nonexistent_explicit_view_mapping_is_not_complete_no_impact(tmp_path):
    base = fixture(tmp_path)
    (tmp_path / "models/orders.sql").write_text("select id, amount * 0.8 as net from raw.orders")
    settings = DbtImpactSettings(table_mapping=[{"dbt_model": "orders", "omni_view": "missing_view"}],
        content_impact=DbtContentImpactSettings(enabled=True))
    result = collect(tmp_path, base, settings=settings)
    assert result["coverage_complete"] is False
    assert result["impacts"] == []  # Do not substitute the physical orders namesake.
    assert result["coverage_gaps"] == [
        {"reason_code": "dbt_explicit_view_mapping_unresolved", "configured_view": "missing_view",
         "source_revision": scope} for scope in ("base", "head")]


@pytest.mark.parametrize("aliases,complete", [(["orders", "missing_view"], False), (["schema.orders"], True)])
def test_explicit_mapping_retains_valid_and_verified_dotted_aliases(tmp_path, aliases, complete):
    base = fixture(tmp_path)
    (tmp_path / "models/orders.sql").write_text("select id, amount * 0.8 as net from raw.orders")
    settings = DbtImpactSettings(table_mapping=[{"dbt_model": "orders", "omni_view": name} for name in aliases],
        content_impact=DbtContentImpactSettings(enabled=True))
    def resolver(scope, root, files):
        return {"omni/orders.view.yaml": "schema.orders"}

    result = collect(tmp_path, base, settings=settings, resolver=resolver)
    assert result["coverage_complete"] is complete
    assert len(result["impacts"]) == 6
    assert {item["field_name"] for item in result["impacts"]} == {"net", "doubled", "filtered_count"}
    if complete:
        assert result["coverage_gaps"] == []
    else:
        assert result["coverage_gaps"] == [
            {"reason_code": "dbt_explicit_view_mapping_unresolved", "configured_view": "missing_view",
             "source_revision": scope} for scope in ("base", "head")]


def test_explicit_mapping_match_in_one_root_does_not_require_every_root(tmp_path):
    base = fixture(tmp_path, extra={"other/stock.view.yaml": "sql_table_name: inventory.stock\ndimensions:\n  id: {}\n"})
    (tmp_path / "models/orders.sql").write_text("select id, amount * 0.8 as net from raw.orders")
    settings = DbtImpactSettings(table_mapping=[{"dbt_model": "orders", "omni_view": "orders"}],
        content_impact=DbtContentImpactSettings(enabled=True))
    result = collect(tmp_path, base, settings=settings, omni_paths=["omni", "other"])
    assert result["coverage_complete"] is True and result["coverage_gaps"] == []
    assert len(result["impacts"]) == 6
    assert {item["model_path"] for item in result["impacts"]} == {"omni"}


def test_ambiguous_explicit_mapping_does_not_get_missing_mapping_reason(tmp_path):
    base = fixture(tmp_path, extra={"omni/other/orders.view.yaml": "sql_table_name: staging.orders\ndimensions:\n  net: {}\n"})
    (tmp_path / "models/orders.sql").write_text("select id, amount * 0.8 as net from raw.orders")
    settings = DbtImpactSettings(table_mapping=[{"dbt_model": "orders", "omni_view": "orders"}],
        content_impact=DbtContentImpactSettings(enabled=True))
    result = collect(tmp_path, base, settings=settings)
    assert result["coverage_complete"] is False and result["impacts"] == []
    assert {item["reason_code"] for item in result["coverage_gaps"]} == {"dbt_relation_ambiguous"}


@pytest.mark.parametrize("before_name,after_name,missing_scope", [
    ("orders", "renamed", "head"), ("renamed", "orders", "base"), ("orders", None, "head"),
])
def test_explicit_mapping_checks_renamed_or_removed_view_per_revision(tmp_path, before_name, after_name, missing_scope):
    base = fixture(tmp_path, view=f"name: {before_name}\n" + VIEW,
        extra={"omni/other.view.yaml": "sql_table_name: other\ndimensions:\n  id: {}\n"})
    (tmp_path / "models/orders.sql").write_text("select id, amount * 0.8 as net from raw.orders")
    if after_name is None:
        (tmp_path / "omni/orders.view.yaml").unlink()
    else:
        (tmp_path / "omni/orders.view.yaml").write_text(f"name: {after_name}\n" + VIEW)
    settings = DbtImpactSettings(table_mapping=[{"dbt_model": "orders", "omni_view": "orders"}],
        content_impact=DbtContentImpactSettings(enabled=True))
    result = collect(tmp_path, base, settings=settings)
    assert result["coverage_complete"] is False
    assert len(result["impacts"]) == 3
    assert all(item["source_revision"] != missing_scope for item in result["impacts"])
    assert result["coverage_gaps"] == [
        {"reason_code": "dbt_explicit_view_mapping_unresolved", "configured_view": "orders",
         "source_revision": missing_scope}]


def test_unmatched_physical_mapping_remains_legitimate_no_impact(tmp_path):
    base = fixture(tmp_path)
    (tmp_path / "models/orders.sql").write_text("select id, amount * 0.8 as net from raw.orders")
    settings = DbtImpactSettings(table_mapping=[{"dbt_model": "orders", "sql_table_name": "warehouse.other.orders"}],
        content_impact=DbtContentImpactSettings(enabled=True))
    result = collect(tmp_path, base, settings=settings)
    assert result["coverage_complete"] is True and result["impacts"] == [] and result["coverage_gaps"] == []


def test_noop_with_invalid_explicit_mapping_still_skips_graph_and_resolver(tmp_path):
    base = fixture(tmp_path)
    (tmp_path / "models/orders.sql").write_text("select id, amount * .9 as net from raw.orders -- comment\n")
    settings = DbtImpactSettings(table_mapping=[{"dbt_model": "orders", "omni_view": "missing_view"}],
        content_impact=DbtContentImpactSettings(enabled=True))
    resolver = mock.Mock(side_effect=AssertionError("no API needed"))
    with mock.patch("omniflow.dbt_column_impact._load_omni_snapshot", side_effect=AssertionError("no graph needed")):
        result = collect(tmp_path, base, settings=settings, resolver=resolver)
    assert result["coverage_complete"] is True and result["impacts"] == [] and result["coverage_gaps"] == []
    resolver.assert_not_called()


@pytest.mark.parametrize("extension,reason", [
    ("  unresolved:\n    sql: ${missing}\n", "omni_field_reference_unresolved"),
    ("  loop_a:\n    sql: ${net} + ${loop_b}\n  loop_b:\n    sql: ${loop_a}\n", "omni_field_dependency_cycle"),
])
def test_dependency_gaps_are_explicit(tmp_path, extension, reason):
    base = fixture(tmp_path, view="sql_table_name: orders\ndimensions:\n  net: {}\n" + extension)
    (tmp_path / "models/orders.sql").write_text("select id, amount as net from raw.orders")
    result = collect(tmp_path, base)
    assert result["coverage_complete"] is False
    assert reason in {gap["reason_code"] for gap in result["coverage_gaps"]}


def test_impacted_fields_are_not_sampled_at_25(tmp_path):
    fields = "".join(f"  dependent_{index}:\n    sql: ${{net}}\n" for index in range(40))
    base = fixture(tmp_path, view="sql_table_name: orders\ndimensions:\n  net: {}\n" + fields)
    (tmp_path / "models/orders.sql").write_text("select id, amount as net from raw.orders")
    result = collect(tmp_path, base)
    assert result["coverage_complete"] is True
    assert len(result["impacts"]) == 82


@pytest.mark.parametrize("extra,reason", [
    ({"omni/query.query.view.yaml": "sql: select net from orders\ndimensions:\n  net: {}\n"}, "omni_query_view_unsupported"),
    ({"omni/model.yaml": "extends: elsewhere\n"}, "omni_root_unavailable"),
])
def test_query_views_and_external_dependencies_are_not_false_complete(tmp_path, extra, reason):
    base = fixture(tmp_path, extra=extra)
    (tmp_path / "models/orders.sql").write_text("select id, amount as net from raw.orders")
    result = collect(tmp_path, base)
    assert result["coverage_complete"] is False
    assert reason in {gap["reason_code"] for gap in result["coverage_gaps"]}


def test_full_manifest_qualified_matching_does_not_broaden_to_other_schema(tmp_path):
    def manifest(columns):
        return json.dumps({"nodes": {"model.fixture.orders": {
            "resource_type": "model", "name": "orders", "relation_name": "warehouse.marts.orders",
            "original_file_path": "models/orders.sql", "config": {"materialized": "table", "contract": {"enforced": True}},
            "columns": {column: {} for column in columns},
        }}})
    base = fixture(tmp_path, extra={"target/manifest.json": manifest(["id", "net"]),
        "omni/staging.view.yaml": "sql_table_name: warehouse.staging.orders\ndimensions:\n  net: {}\n"})
    (tmp_path / "models/orders.sql").write_text("select id from raw.orders")
    (tmp_path / "target/manifest.json").write_text(manifest(["id"]))
    settings = DbtImpactSettings(enabled=True, manifest_path="target/manifest.json",
                                content_impact=DbtContentImpactSettings(enabled=True))
    result = collect(tmp_path, base, settings=settings)
    assert result["coverage_complete"] is True
    assert result["impacts"]
    assert all(item["view_file"] != "staging.view.yaml" for item in result["impacts"])
    report, issues = evaluate_dbt_impact(changed_files=["models/orders.sql"], dbt_paths=["models"], settings=settings,
        omni_yaml_paths=["omni"], base_ref=base, repo_root=tmp_path)
    assert report["coverage_complete"] is True
    orphaned = [issue for issue in issues if issue["rule"] == ORPHANED_COLUMN_RULE]
    assert len(orphaned) == 1
    assert all(item["file"] != "omni/staging.view.yaml" for item in orphaned[0]["orphaned_fields"])


@pytest.mark.parametrize("omni_relation,complete", [
    ("marts.orders", False), ("orders", False),
    ("other.marts.orders", True), ("warehouse.other.orders", True),
])
def test_known_manifest_relation_does_not_invent_implicit_database(tmp_path, omni_relation, complete):
    def manifest(columns):
        return json.dumps({"nodes": {"model.fixture.orders": {
            "resource_type": "model", "name": "orders", "relation_name": "warehouse.marts.orders",
            "original_file_path": "models/orders.sql", "config": {"materialized": "table", "contract": {"enforced": True}},
            "columns": {column: {} for column in columns},
        }}})
    base = fixture(tmp_path, view=VIEW.replace("warehouse.marts.orders", omni_relation),
                   extra={"target/manifest.json": manifest(["id", "net"])})
    (tmp_path / "models/orders.sql").write_text("select id from raw.orders")
    (tmp_path / "target/manifest.json").write_text(manifest(["id"]))
    settings = DbtImpactSettings(enabled=True, manifest_path="target/manifest.json",
                                content_impact=DbtContentImpactSettings(enabled=True))
    result = collect(tmp_path, base, settings=settings)
    assert result["coverage_complete"] is complete
    assert result["impacts"] == []
    if not complete:
        assert "dbt_relation_context_unavailable" in {gap["reason_code"] for gap in result["coverage_gaps"]}
    report, issues = evaluate_dbt_impact(changed_files=["models/orders.sql"], dbt_paths=["models"], settings=settings,
        omni_yaml_paths=["omni"], base_ref=base, repo_root=tmp_path)
    assert report["coverage_complete"] is complete
    assert [issue["rule"] for issue in issues] == ([] if complete else [COVERAGE_RULE])


def test_trusted_base_checkout_uses_immutable_candidate_not_worktree(tmp_path, monkeypatch):
    base = fixture(tmp_path)
    (tmp_path / "models/orders.sql").write_text("select id, amount as net from raw.orders")
    head = commit(tmp_path)
    git(tmp_path, "checkout", "--detach", base)
    event = {"pull_request": {"base": {"sha": base, "repo": {"full_name": "example/fixture"}},
                              "head": {"sha": head, "repo": {"full_name": "example/fixture"}}}}
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    result = collect(tmp_path, base)
    assert result["coverage_complete"] is True and result["impacts"]
    assert git(tmp_path, "rev-parse", "HEAD") == base
    assert "OMNI_API_KEY" not in os.environ


@pytest.mark.parametrize("key", ["extends", "extension", "inheritance"])
def test_inherited_view_evidence_is_explicitly_unsupported(tmp_path, key):
    base = fixture(tmp_path, view=f"{key}: inherited_orders\n" + VIEW)
    (tmp_path / "models/orders.sql").write_text("select id, amount as net from raw.orders")
    result = collect(tmp_path, base)
    assert result["coverage_complete"] is False
    assert "omni_inheritance_unsupported" in {gap["reason_code"] for gap in result["coverage_gaps"]}


def test_dependency_depth_and_impact_budgets_never_silently_truncate(tmp_path, monkeypatch):
    base = fixture(tmp_path)
    (tmp_path / "models/orders.sql").write_text("select id, amount as net from raw.orders")
    monkeypatch.setattr("omniflow.dbt_column_impact.MAX_DEPENDENCY_DEPTH", 2)
    result = collect(tmp_path, base)
    assert result["coverage_complete"] is False
    assert "omni_field_dependency_depth" in {gap["reason_code"] for gap in result["coverage_gaps"]}
    monkeypatch.setattr("omniflow.dbt_column_impact.MAX_IMPACTS", 1)
    with pytest.raises(SecurityPolicyError, match="impact safety limit"):
        collect(tmp_path, base)


@pytest.mark.parametrize("candidate", [
    "SELECT amount AS net FROM {{ref('orders', version = 2)}}",
    "SELECT amount AS net FROM {{- ref ('orders', version = 2) -}}",
    "{# ignored {{ arbitrary_macro() }} #}\nSELECT amount AS net FROM {{ref('orders', version = 2)}}",
])
def test_source_jinja_normalization_does_not_execute_or_erase_literal_changes(candidate):
    before = analyze_projection('select amount as net from {{ ref("orders", version=2) }}')
    after = analyze_projection(candidate)
    assert before.complete and after.complete
    assert before.columns == after.columns and before.context == after.context
    assert not analyze_projection("select amount as net from {{ ref(var('source')) }}").complete


def test_snowflake_identifier_folding_and_quoted_aliases(tmp_path):
    assert not analyze_projection('select amount as net, tax as "NET" from orders', dialect="snowflake").complete
    assert analyze_projection('select amount as net, tax as "Net" from orders', dialect="snowflake").complete
    base = fixture(tmp_path)
    (tmp_path / "models/orders.sql").write_text("select id, amount as net from raw.orders")
    result = collect(tmp_path, base, settings=DbtImpactSettings(content_impact=DbtContentImpactSettings(
        enabled=True, sql_dialect="snowflake")))
    assert result["coverage_complete"] is True
    assert {item["field_name"] for item in result["impacts"]} == {"net", "doubled", "filtered_count"}


@pytest.mark.parametrize("manifest_table,complete", [("Orders", False), ("ORDERS", True)])
def test_snowflake_quoted_relation_case_is_not_collapsed(tmp_path, manifest_table, complete):
    manifest = json.dumps({"nodes": {"model.fixture.orders": {
        "resource_type": "model", "name": "orders",
        "relation_name": f'"WAREHOUSE"."MARTS"."{manifest_table}"',
        "original_file_path": "models/orders.sql", "config": {"materialized": "table"},
    }}})
    base = fixture(tmp_path, extra={"target/manifest.json": manifest})
    (tmp_path / "models/orders.sql").write_text("select id, amount as net from raw.orders")
    result = collect(tmp_path, base, settings=DbtImpactSettings(manifest_path="target/manifest.json",
        content_impact=DbtContentImpactSettings(enabled=True, sql_dialect="snowflake")))
    assert result["coverage_complete"] is complete
    if complete:
        assert result["impacts"] and result["coverage_gaps"] == []
    else:
        assert result["impacts"] == []
        assert "dbt_relation_case_ambiguous" in {item["reason_code"] for item in result["coverage_gaps"]}


def test_same_sql_changed_manifest_relation_preserves_both_scopes(tmp_path):
    def manifest(schema):
        return json.dumps({"nodes": {"model.fixture.orders": {
            "resource_type": "model", "name": "orders", "relation_name": f"warehouse.{schema}.orders",
            "original_file_path": "models/orders.sql", "config": {"materialized": "table", "contract": {"enforced": True}},
            "columns": {"id": {}, "net": {}},
        }}})
    base = fixture(tmp_path, extra={"target/manifest.json": manifest("marts")})
    (tmp_path / "target/manifest.json").write_text(manifest("next"))
    (tmp_path / "omni/orders.view.yaml").write_text(VIEW.replace("warehouse.marts.orders", "warehouse.next.orders"))
    result = collect(tmp_path, base, settings=DbtImpactSettings(manifest_path="target/manifest.json",
        content_impact=DbtContentImpactSettings(enabled=True)), changed=["target/manifest.json"])
    assert {(item["change_kind"], item["source_revision"]) for item in result["impacts"]} == {
        ("removed", "base"), ("added", "head")}
    # Manifest alone cannot establish whether compiled SQL expressions changed.
    assert result["coverage_complete"] is False


def test_model_metadata_alone_cannot_prove_zero_authored_dependencies(tmp_path):
    base = fixture(tmp_path)
    (tmp_path / "omni/orders.view.yaml").unlink()
    (tmp_path / "omni/model.yaml").write_text("label: Model\n")
    (tmp_path / "models/orders.sql").write_text("select id, amount as net from raw.orders")
    result = collect(tmp_path, base)
    assert result["coverage_complete"] is False
    assert "omni_view_evidence_unavailable" in {gap["reason_code"] for gap in result["coverage_gaps"]}


JAFFLE_STYLE = """with orders as (select * from {{ ref('stg_orders') }})
select orders.id, orders.amount * 0.9 as net from orders"""


def test_import_cte_wildcards_keep_column_precision(tmp_path):
    base = fixture(tmp_path, before=JAFFLE_STYLE)
    (tmp_path / "models/orders.sql").write_text(JAFFLE_STYLE.replace("0.9", "0.8"))
    result = collect(tmp_path, base)
    assert result["coverage_complete"] is True
    assert {item["field_name"] for item in result["impacts"]} == {"net", "doubled", "filtered_count"}
    assert all(item["change_kind"] == "modified" for item in result["impacts"])


def test_import_cte_wildcard_comment_change_is_no_impact(tmp_path):
    base = fixture(tmp_path, before=JAFFLE_STYLE)
    (tmp_path / "models/orders.sql").write_text("-- reworded\n" + JAFFLE_STYLE)
    result = collect(tmp_path, base)
    assert result == {"schema_version": 1, "coverage_complete": True, "coverage_gaps": [], "impacts": []}


def test_changed_import_cte_wildcard_is_wider_potential(tmp_path):
    base = fixture(tmp_path, before=JAFFLE_STYLE)
    (tmp_path / "models/orders.sql").write_text(JAFFLE_STYLE.replace("stg_orders", "stg_orders_v2"))
    result = collect(tmp_path, base)
    assert {item["field_name"] for item in result["impacts"]} == {"id", "net", "doubled", "constant", "filtered_count"}
    assert all(item["change_kind"] == "potential" for item in result["impacts"])


@pytest.mark.parametrize("before,after", [
    ("select id, {{ cents_to_dollars('amount') }} as net from raw.orders",
     "select id, {{ cents_to_dollars('subtotal') }} as net from raw.orders"),
    # An unchanged macro can return `id, amount` for 1 and just `amount` for 2.
    # The trailing alias does not establish the number of expanded columns.
    ("select {{ projected_columns(1) }} as net from raw.orders",
     "select {{ projected_columns(2) }} as net from raw.orders"),
])
def test_aliased_constant_macro_has_unknown_expansion(tmp_path, before, after):
    base = fixture(tmp_path, before=before)
    (tmp_path / "models/orders.sql").write_text(after)
    result = collect(tmp_path, base)
    assert result["coverage_complete"] is False
    assert "dbt_sql_dynamic_projection" in {gap["reason_code"] for gap in result["coverage_gaps"]}
    assert {item["field_name"] for item in result["impacts"]} == {"id", "net", "doubled", "constant", "filtered_count"}
    assert all(item["change_kind"] == "potential" for item in result["impacts"])


@pytest.mark.parametrize("candidate", [
    "select {{ dbt_utils.star(from=ref('x')) }} from {{ ref('x') }}",
    "select {{ cents_to_dollars('amount') }}, id from raw.orders",
    "select {{ var('rate') }} as net, id from raw.orders",
    "select id, net from {{ my_relation('orders') }}",
])
def test_macros_that_can_change_output_identity_remain_gaps(tmp_path, candidate):
    base = fixture(tmp_path)
    (tmp_path / "models/orders.sql").write_text(candidate)
    result = collect(tmp_path, base)
    assert "dbt_sql_dynamic_projection" in {gap["reason_code"] for gap in result["coverage_gaps"]}


def test_changed_model_with_dbt_children_reports_downstream_gap(tmp_path):
    base = fixture(tmp_path, extra={"models/fct_orders.sql": "select id from {{ ref('orders') }}",
                                    "models/unrelated.sql": "select id from {{ ref('orders_archive') }}"})
    (tmp_path / "models/orders.sql").write_text("select id, amount * 0.8 as net from raw.orders")
    result = collect(tmp_path, base)
    assert result["coverage_complete"] is False
    gaps = [gap for gap in result["coverage_gaps"] if gap["reason_code"] == "dbt_downstream_models_not_analyzed"]
    assert gaps == [{"reason_code": "dbt_downstream_models_not_analyzed", "file": "models/orders.sql",
                     "downstream_model_count": 1, "downstream_files": ["models/fct_orders.sql"]}]
    assert result["impacts"]


@pytest.mark.parametrize("child,dependent", [
    ("select id from {{ ref ('orders') }}", True),
    ("select id from {{- ref('orders') -}}", True),
    ("select id from {{ ref('package', 'orders', version=2) }}", True),
    ("select id from {{ ref('orders', v=2) }}", True),
    ("-- depends_on: {{ ref('orders') }}\nselect id from raw.orders", True),
    ("{# ignored {{ ref('orders') }} #}\nselect id from raw.orders", False),
    ("select id from {{ ref('orders_archive') }}", False),
])
def test_downstream_refs_use_jinja_syntax_not_raw_substrings(tmp_path, child, dependent):
    base = fixture(tmp_path, extra={"models/fct_orders.sql": child})
    (tmp_path / "models/orders.sql").write_text("select id, amount * 0.8 as net from raw.orders")
    result = collect(tmp_path, base)
    assert result["coverage_complete"] is not dependent
    assert any(gap["reason_code"] == "dbt_downstream_models_not_analyzed"
               for gap in result["coverage_gaps"]) is dependent


@pytest.mark.parametrize("child_sql", [
    "select id * 2 as id, net from {{ ref('orders') }}",
    "select id, net from {{ ref('orders') }} -- comment only",
])
def test_changed_downstream_child_does_not_prove_upstream_coverage(tmp_path, child_sql):
    base = fixture(tmp_path, extra={"models/fct_orders.sql": "select id, net from {{ ref('orders') }}"})
    (tmp_path / "models/orders.sql").write_text("select id, amount * 0.8 as net from raw.orders")
    (tmp_path / "models/fct_orders.sql").write_text(child_sql)
    result = collect(tmp_path, base, changed=["models/orders.sql", "models/fct_orders.sql"])
    assert result["coverage_complete"] is False
    assert any(gap["reason_code"] == "dbt_downstream_models_not_analyzed"
               and gap["file"] == "models/orders.sql" for gap in result["coverage_gaps"])


def test_downstream_unicode_filename_is_not_lost_to_git_quoting(tmp_path):
    child = "models/café orders.sql"
    base = fixture(tmp_path, extra={child: "select id from {{ ref('orders') }}"})
    (tmp_path / "models/orders.sql").write_text("select id, amount * 0.8 as net from raw.orders")
    result = collect(tmp_path, base)
    assert result["coverage_complete"] is False
    assert any(child in gap.get("downstream_files", []) for gap in result["coverage_gaps"])


@pytest.mark.parametrize("child", [
    "select id from {{ ref(var('parent')) }}",
    "select id from {{ ref('orders')",
])
def test_dynamic_downstream_ref_is_unavailable_not_clean_absence(tmp_path, child):
    base = fixture(tmp_path, extra={"models/fct_orders.sql": child})
    (tmp_path / "models/orders.sql").write_text("select id, amount * 0.8 as net from raw.orders")
    result = collect(tmp_path, base)
    assert result["coverage_complete"] is False
    assert "dbt_downstream_models_unavailable" in {gap["reason_code"] for gap in result["coverage_gaps"]}


def test_comment_only_change_with_dbt_children_has_no_downstream_gap(tmp_path):
    base = fixture(tmp_path, extra={"models/fct_orders.sql": "select id from {{ ref('orders') }}"})
    (tmp_path / "models/orders.sql").write_text("-- note\nselect id, amount * 0.9 as net from raw.orders")
    result = collect(tmp_path, base)
    assert result["coverage_complete"] is True and result["coverage_gaps"] == []


def test_unreadable_downstream_inventory_is_a_gap_not_zero(tmp_path):
    base = fixture(tmp_path)
    (tmp_path / "models/orders.sql").write_text("select id, amount * 0.8 as net from raw.orders")
    with mock.patch("omniflow.dbt_column_impact._list_revision_files", return_value=None):
        result = collect(tmp_path, base)
    assert "dbt_downstream_models_unavailable" in {gap["reason_code"] for gap in result["coverage_gaps"]}


@pytest.mark.parametrize("child,dependent", [
    ("{{ config(materialized='table', tags=['daily']) }}\nselect id from raw.other", False),
    ("select {{ dbt.date_trunc('day', 'ordered_at') }} as day from raw.other", False),
    ("select {{ dbt_utils.generate_surrogate_key(['id', 'sku']) }} as k from raw.other", False),
    ("select {{ cents_to_dollars('cost') }} as cost from raw.other", False),
    ("{{ config(materialized='view') }}\nselect {{ cents_to_dollars('net') }} from {{ ref('orders') }}", True),
    ("select {{ dbt_utils.star(ref('orders')) }} from {{ ref('orders') }}", True),
])
def test_ref_free_macros_keep_downstream_inventory_readable(tmp_path, child, dependent):
    macro = "{% macro cents_to_dollars(column_name) -%}\n({{ column_name }} / 100)::numeric(16, 2)\n{%- endmacro %}"
    base = fixture(tmp_path, extra={"models/fct_orders.sql": child, "models/macros/cents.sql": macro})
    (tmp_path / "models/orders.sql").write_text("select id, amount * 0.8 as net from raw.orders")
    result = collect(tmp_path, base)
    codes = {gap["reason_code"] for gap in result["coverage_gaps"]}
    assert "dbt_downstream_models_unavailable" not in codes
    assert ("dbt_downstream_models_not_analyzed" in codes) is dependent
    assert result["coverage_complete"] is not dependent


@pytest.mark.parametrize("child,macro", [
    # A project macro that can reach a model hides dependencies of its callers.
    ("select {{ cents_to_dollars('cost') }} from raw.other",
     "{% macro cents_to_dollars(c) %}(select max(x) from {{ ref('orders') }}){% endmacro %}"),
    ("select {{ cents_to_dollars('cost') }} from raw.other",
     "{% macro cents_to_dollars(c) %}{{ run_query('select 1') }}{% endmacro %}"),
    # Undefined macros and other packages are not known to be ref-free.
    ("select {{ undefined_macro('cost') }} from raw.other", ""),
    ("select {{ other_pkg.helper('cost') }} from raw.other", ""),
    # Dynamic arguments are not literal.
    ("select {{ dbt.date_trunc(var('grain'), 'ordered_at') }} from raw.other", ""),
])
def test_unprovable_macros_keep_downstream_inventory_unavailable(tmp_path, child, macro):
    extra = {"models/fct_orders.sql": child}
    if macro:
        extra["models/macros/cents.sql"] = macro
    base = fixture(tmp_path, extra=extra)
    (tmp_path / "models/orders.sql").write_text("select id, amount * 0.8 as net from raw.orders")
    result = collect(tmp_path, base)
    assert result["coverage_complete"] is False
    assert "dbt_downstream_models_unavailable" in {gap["reason_code"] for gap in result["coverage_gaps"]}
