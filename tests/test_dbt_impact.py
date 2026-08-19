import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omniflow.config import DbtImpactSettings, load_config
from omniflow.dbt_impact import (
    ORPHANED_COLUMN_RULE,
    ORPHANED_MODEL_RULE,
    VALIDATOR,
    evaluate_dbt_impact,
)
from omniflow.dbt_manifest import diff_manifests, parse_manifest
from omniflow.dbt_sql_diff import diff_sql_columns, extract_output_columns, model_name_from_path
from omniflow.exceptions import ConfigError, SecurityPolicyError

ORDERS_VIEW = """name: orders
sql_table_name: analytics.marts.orders
fields:
  customer_id:
    type: number
    sql: ${TABLE}.customer_id
  order_total:
    type: number
    sql: ${TABLE}.order_total
  computed_margin:
    type: number
    sql: ${TABLE}.order_total - ${TABLE}.order_cost
"""

IMPLICIT_VIEW = """name: shipments
sql_table_name: analytics.marts.shipments
fields:
  tracking_number:
    type: string
"""


def settings(**overrides) -> DbtImpactSettings:
    base = {
        "enabled": True,
        "manifest_path": None,
        "fail_on_orphaned_references": True,
        "omni_yaml_paths": [],
        "table_mapping": [],
    }
    base.update(overrides)
    return DbtImpactSettings(**base)


def manifest(nodes: dict) -> str:
    return json.dumps({"nodes": nodes})


def model_node(name, columns, *, schema="marts", database="analytics", resource_type="model"):
    return {
        "resource_type": resource_type,
        "name": name,
        "database": database,
        "schema": schema,
        "alias": name,
        "relation_name": f'"{database}"."{schema}"."{name}"',
        "config": {"materialized": "table"},
        "columns": {column: {"name": column} for column in columns},
    }


class SqlHeuristicTests(unittest.TestCase):
    def test_aliases_are_extracted(self):
        sql = "select id as customer_id, name, total as order_total from raw.orders"
        self.assertEqual(
            extract_output_columns(sql), {"customer_id", "name", "order_total"}
        )

    def test_select_star_returns_no_columns(self):
        self.assertEqual(extract_output_columns("select * from raw.orders"), set())

    def test_star_among_columns_returns_no_columns(self):
        self.assertEqual(extract_output_columns("select a, *, b from raw.orders"), set())

    def test_jinja_is_stripped(self):
        sql = 'select id as customer_id from {{ ref("orders") }}'
        self.assertEqual(extract_output_columns(sql), {"customer_id"})

    def test_final_select_wins_over_cte(self):
        sql = "with base as (select a as inner_col from t) select b as final_col from base"
        self.assertEqual(extract_output_columns(sql), {"final_col"})

    def test_comments_are_ignored(self):
        sql = "select id as customer_id -- trailing\n, name /* block */ from raw.orders"
        self.assertEqual(extract_output_columns(sql), {"customer_id", "name"})

    def test_qualified_bare_column_uses_final_segment(self):
        self.assertEqual(extract_output_columns("select o.customer_id from orders o"), {"customer_id"})

    def test_expression_without_alias_is_skipped(self):
        columns = extract_output_columns("select sum(amount), id as order_id from raw.orders")
        self.assertEqual(columns, {"order_id"})

    def test_renamed_column_is_reported_as_removed(self):
        base = "select id as customer_id from raw.orders"
        head = "select id as customer_key from raw.orders"
        self.assertEqual(diff_sql_columns(base, head), {"customer_id"})

    def test_unparseable_head_reports_nothing(self):
        base = "select id as customer_id from raw.orders"
        self.assertEqual(diff_sql_columns(base, "select * from raw.orders"), set())

    def test_unparseable_base_reports_nothing(self):
        head = "select id as customer_id from raw.orders"
        self.assertEqual(diff_sql_columns("select * from raw.orders", head), set())

    def test_additive_change_reports_nothing(self):
        base = "select id as customer_id from raw.orders"
        head = "select id as customer_id, email as customer_email from raw.orders"
        self.assertEqual(diff_sql_columns(base, head), set())

    def test_oversized_sql_is_rejected(self):
        with self.assertRaises(SecurityPolicyError):
            extract_output_columns("select " + ("a" * (5 * 1024 * 1024 + 1)))

    def test_model_name_from_path(self):
        self.assertEqual(model_name_from_path("models/marts/orders.sql"), "orders")
        self.assertEqual(model_name_from_path("models/orders.py"), "orders")
        self.assertIsNone(model_name_from_path("models/schema.yml"))


class ManifestTests(unittest.TestCase):
    def test_models_and_columns_are_parsed(self):
        payload = manifest({"model.p.orders": model_node("orders", ["customer_id", "total"])})
        models = parse_manifest(payload)
        self.assertEqual(set(models), {"model.p.orders"})
        self.assertEqual(models["model.p.orders"].columns, {"customer_id", "total"})

    def test_relation_candidates_cover_qualification_levels(self):
        models = parse_manifest(manifest({"model.p.orders": model_node("orders", [])}))
        candidates = models["model.p.orders"].relation_candidates()
        self.assertIn("orders", candidates)
        self.assertIn("marts.orders", candidates)
        self.assertIn("analytics.marts.orders", candidates)

    def test_ephemeral_models_are_excluded(self):
        node = model_node("staging", ["a"])
        node["config"]["materialized"] = "ephemeral"
        self.assertEqual(parse_manifest(manifest({"model.p.staging": node})), {})

    def test_non_relation_resource_types_are_excluded(self):
        node = model_node("my_test", ["a"], resource_type="test")
        self.assertEqual(parse_manifest(manifest({"test.p.my_test": node})), {})

    def test_seeds_and_snapshots_are_included(self):
        payload = manifest(
            {
                "seed.p.countries": model_node("countries", ["code"], resource_type="seed"),
                "snapshot.p.orders_snap": model_node("orders_snap", ["id"], resource_type="snapshot"),
            }
        )
        self.assertEqual(len(parse_manifest(payload)), 2)

    def test_invalid_json_is_rejected(self):
        with self.assertRaises(ConfigError):
            parse_manifest("{not json")

    def test_missing_nodes_is_rejected(self):
        with self.assertRaises(ConfigError):
            parse_manifest(json.dumps({"metadata": {}}))

    def test_diff_reports_removed_columns_and_models(self):
        base = parse_manifest(
            manifest(
                {
                    "model.p.orders": model_node("orders", ["customer_id", "total"]),
                    "model.p.legacy": model_node("legacy", ["id"]),
                }
            )
        )
        head = parse_manifest(manifest({"model.p.orders": model_node("orders", ["customer_key", "total"])}))
        removed_models, removed_columns = diff_manifests(base, head)
        self.assertEqual(set(removed_models), {"model.p.legacy"})
        self.assertEqual(removed_columns, {"model.p.orders": {"customer_id"}})

    def test_undocumented_columns_never_report_removals(self):
        base = parse_manifest(manifest({"model.p.orders": model_node("orders", [])}))
        head = parse_manifest(manifest({"model.p.orders": model_node("orders", [])}))
        _, removed_columns = diff_manifests(base, head)
        self.assertEqual(removed_columns, {})


class CrossReferenceTests(unittest.TestCase):
    def _repo(self, directory, *, views=None, manifest_text=None):
        root = Path(directory)
        model_dir = root / "omni/my_model/views"
        model_dir.mkdir(parents=True)
        for name, body in (views or {"orders.view": ORDERS_VIEW}).items():
            (model_dir / name).write_text(body, encoding="utf-8")
        if manifest_text is not None:
            target = root / "target"
            target.mkdir(parents=True, exist_ok=True)
            (target / "manifest.json").write_text(manifest_text, encoding="utf-8")
        return root

    def test_removed_column_referenced_by_omni_field_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            with mock.patch(
                "omniflow.dbt_impact._sql_changes",
                return_value=({"analytics.marts.orders": {"customer_id"}}, {}, ),
            ):
                report, issues = evaluate_dbt_impact(
                    changed_files=["models/marts/orders.sql"],
                    dbt_paths=["models"],
                    settings=settings(),
                    omni_yaml_paths=["omni/my_model"],
                    base_ref="origin/main",
                    repo_root=root,
                )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["rule"], ORPHANED_COLUMN_RULE)
        self.assertEqual(issues[0]["severity"], "error")
        self.assertEqual(issues[0]["validator"], VALIDATOR)
        self.assertEqual(issues[0]["column"], "customer_id")
        orphaned = {item["field"] for item in issues[0]["orphaned_fields"]}
        self.assertIn("customer_id", orphaned)
        self.assertEqual(report["summary"]["errors"], 1)

    def test_removed_column_used_only_in_an_expression_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            with mock.patch(
                "omniflow.dbt_impact._sql_changes",
                return_value=({"analytics.marts.orders": {"order_cost"}}, {}),
            ):
                _, issues = evaluate_dbt_impact(
                    changed_files=["models/marts/orders.sql"],
                    dbt_paths=["models"],
                    settings=settings(),
                    omni_yaml_paths=["omni/my_model"],
                    base_ref="origin/main",
                    repo_root=root,
                )
        self.assertEqual(len(issues), 1)
        orphaned = {item["field"] for item in issues[0]["orphaned_fields"]}
        self.assertEqual(orphaned, {"computed_margin"})

    def test_field_without_sql_matches_its_own_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory, views={"shipments.view": IMPLICIT_VIEW})
            with mock.patch(
                "omniflow.dbt_impact._sql_changes",
                return_value=({"analytics.marts.shipments": {"tracking_number"}}, {}),
            ):
                _, issues = evaluate_dbt_impact(
                    changed_files=["models/marts/shipments.sql"],
                    dbt_paths=["models"],
                    settings=settings(),
                    omni_yaml_paths=["omni/my_model"],
                    base_ref="origin/main",
                    repo_root=root,
                )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["orphaned_fields"][0]["field"], "tracking_number")

    def test_unreferenced_column_removal_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            with mock.patch(
                "omniflow.dbt_impact._sql_changes",
                return_value=({"analytics.marts.orders": {"internal_audit_flag"}}, {}),
            ):
                _, issues = evaluate_dbt_impact(
                    changed_files=["models/marts/orders.sql"],
                    dbt_paths=["models"],
                    settings=settings(),
                    omni_yaml_paths=["omni/my_model"],
                    base_ref="origin/main",
                    repo_root=root,
                )
        self.assertEqual(issues, [])

    def test_substring_column_names_do_not_false_positive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            with mock.patch(
                "omniflow.dbt_impact._sql_changes",
                return_value=({"analytics.marts.orders": {"customer"}}, {}),
            ):
                _, issues = evaluate_dbt_impact(
                    changed_files=["models/marts/orders.sql"],
                    dbt_paths=["models"],
                    settings=settings(),
                    omni_yaml_paths=["omni/my_model"],
                    base_ref="origin/main",
                    repo_root=root,
                )
        self.assertEqual(issues, [], msg="'customer' must not match 'customer_id'")

    def test_change_to_a_different_table_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            with mock.patch(
                "omniflow.dbt_impact._sql_changes",
                return_value=({"analytics.marts.other_table": {"customer_id"}}, {}),
            ):
                _, issues = evaluate_dbt_impact(
                    changed_files=["models/marts/other_table.sql"],
                    dbt_paths=["models"],
                    settings=settings(),
                    omni_yaml_paths=["omni/my_model"],
                    base_ref="origin/main",
                    repo_root=root,
                )
        self.assertEqual(issues, [])

    def test_removed_model_with_referencing_view_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            with mock.patch(
                "omniflow.dbt_impact._sql_changes",
                return_value=({}, {"analytics.marts.orders": "orders"}),
            ):
                _, issues = evaluate_dbt_impact(
                    changed_files=["models/marts/orders.sql"],
                    dbt_paths=["models"],
                    settings=settings(),
                    omni_yaml_paths=["omni/my_model"],
                    base_ref="origin/main",
                    repo_root=root,
                )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["rule"], ORPHANED_MODEL_RULE)
        self.assertEqual(issues[0]["orphaned_views"][0]["view"], "orders")

    def test_warning_mode_does_not_emit_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            with mock.patch(
                "omniflow.dbt_impact._sql_changes",
                return_value=({"analytics.marts.orders": {"customer_id"}}, {}),
            ):
                report, issues = evaluate_dbt_impact(
                    changed_files=["models/marts/orders.sql"],
                    dbt_paths=["models"],
                    settings=settings(fail_on_orphaned_references=False),
                    omni_yaml_paths=["omni/my_model"],
                    base_ref="origin/main",
                    repo_root=root,
                )
        self.assertEqual(issues[0]["severity"], "warning")
        self.assertEqual(report["summary"]["errors"], 0)

    def test_non_dbt_changed_files_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            report, issues = evaluate_dbt_impact(
                changed_files=["hightouch/syncs/crm.yml", "README.md"],
                dbt_paths=["models"],
                settings=settings(),
                omni_yaml_paths=["omni/my_model"],
                base_ref="origin/main",
                repo_root=root,
            )
        self.assertEqual(issues, [])
        self.assertEqual(report["dbt_file_count"], 0)

    def test_manifest_mode_is_preferred_and_recorded(self):
        base_manifest = manifest({"model.p.orders": model_node("orders", ["customer_id", "order_total"])})
        head_manifest = manifest({"model.p.orders": model_node("orders", ["customer_key", "order_total"])})
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory, manifest_text=head_manifest)
            with mock.patch("omniflow.dbt_impact._git_show", return_value=base_manifest):
                report, issues = evaluate_dbt_impact(
                    changed_files=["models/marts/orders.sql"],
                    dbt_paths=["models"],
                    settings=settings(manifest_path="target/manifest.json"),
                    omni_yaml_paths=["omni/my_model"],
                    base_ref="origin/main",
                    repo_root=root,
                )
        self.assertEqual(report["analysis_mode"], "manifest")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["column"], "customer_id")
        self.assertEqual(issues[0]["analysis_mode"], "manifest")

    def test_missing_manifest_falls_back_to_heuristic_with_a_note(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            with mock.patch("omniflow.dbt_impact._sql_changes", return_value=({}, {})):
                report, issues = evaluate_dbt_impact(
                    changed_files=["models/marts/orders.sql"],
                    dbt_paths=["models"],
                    settings=settings(manifest_path="target/manifest.json"),
                    omni_yaml_paths=["omni/my_model"],
                    base_ref="origin/main",
                    repo_root=root,
                )
        self.assertEqual(report["analysis_mode"], "sql_heuristic")
        self.assertEqual(issues, [])
        self.assertTrue(any("was not found" in note for note in report["notes"]))

    def test_table_mapping_override_links_model_to_view(self):
        base_manifest = manifest(
            {"model.p.orders_v2": model_node("orders_v2", ["customer_id"], schema="staging")}
        )
        head_manifest = manifest({"model.p.orders_v2": model_node("orders_v2", [], schema="staging")})
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory, manifest_text=head_manifest)
            with mock.patch("omniflow.dbt_impact._git_show", return_value=base_manifest):
                _, issues = evaluate_dbt_impact(
                    changed_files=["models/staging/orders_v2.sql"],
                    dbt_paths=["models"],
                    settings=settings(
                        manifest_path="target/manifest.json",
                        table_mapping=[
                            {"dbt_model": "orders_v2", "sql_table_name": "analytics.marts.orders"}
                        ],
                    ),
                    omni_yaml_paths=["omni/my_model"],
                    base_ref="origin/main",
                    repo_root=root,
                )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["column"], "customer_id")

    def test_missing_omni_yaml_directory_produces_no_issues(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch(
                "omniflow.dbt_impact._sql_changes",
                return_value=({"analytics.marts.orders": {"customer_id"}}, {}),
            ):
                report, issues = evaluate_dbt_impact(
                    changed_files=["models/marts/orders.sql"],
                    dbt_paths=["models"],
                    settings=settings(),
                    omni_yaml_paths=["omni/absent"],
                    base_ref="origin/main",
                    repo_root=root,
                )
        self.assertEqual(issues, [])
        self.assertEqual(report["omni_views_indexed"], 0)


    def test_multiple_relation_tokens_produce_one_issue(self):
        """A model matches bare, schema-qualified, and fully qualified tokens.

        Regression guard: each token used to emit its own duplicate issue for the
        same orphaned column.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            with mock.patch(
                "omniflow.dbt_impact._sql_changes",
                return_value=(
                    {
                        "orders": {"customer_id"},
                        "marts.orders": {"customer_id"},
                        "analytics.marts.orders": {"customer_id"},
                    },
                    {},
                ),
            ):
                _, issues = evaluate_dbt_impact(
                    changed_files=["models/marts/orders.sql"],
                    dbt_paths=["models"],
                    settings=settings(),
                    omni_yaml_paths=["omni/my_model"],
                    base_ref="origin/main",
                    repo_root=root,
                )
        self.assertEqual(len(issues), 1)
        # The most qualified relation name is reported.
        self.assertEqual(issues[0]["dbt_relation"], "analytics.marts.orders")
        self.assertIn("analytics.marts.orders", issues[0]["message"])

    def test_removed_model_matching_many_tokens_produces_one_issue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            with mock.patch(
                "omniflow.dbt_impact._sql_changes",
                return_value=(
                    {},
                    {
                        "orders": "orders",
                        "marts.orders": "orders",
                        "analytics.marts.orders": "orders",
                    },
                ),
            ):
                _, issues = evaluate_dbt_impact(
                    changed_files=["models/marts/orders.sql"],
                    dbt_paths=["models"],
                    settings=settings(),
                    omni_yaml_paths=["omni/my_model"],
                    base_ref="origin/main",
                    repo_root=root,
                )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["orphaned_view_count"], 1)


class OmniViewConventionTests(unittest.TestCase):
    """Coverage for the YAML shapes Omni's dbt integration actually writes.

    Real repositories use `dimensions:` rather than `fields:` and the split
    `catalog`/`schema`/`table_name` form rather than a single `sql_table_name`.
    """

    DBT_INTEGRATION_VIEW = """description: Product dimension derived from transaction data.

catalog: coffee_training
schema: analytics_marts
table_name: dim_product

dimensions:
  product_name: {}
  standard_unit_price: {}

  product_id:
    format: ID
    primary_key: true

  product_detail:
    description: Product detail from the source system.

measures:
  count:
    aggregate_type: count
"""

    def _repo(self, directory, views):
        root = Path(directory)
        view_dir = root / "omni/marts"
        view_dir.mkdir(parents=True)
        for name, body in views.items():
            (view_dir / name).write_text(body, encoding="utf-8")
        return root

    def _run(self, root, removed_columns, *, removed_models=None):
        with mock.patch(
            "omniflow.dbt_impact._sql_changes",
            return_value=(removed_columns, removed_models or {}),
        ):
            return evaluate_dbt_impact(
                changed_files=["models/marts/dim_product.sql"],
                dbt_paths=["models"],
                settings=settings(),
                omni_yaml_paths=["omni"],
                base_ref="origin/main",
                repo_root=root,
            )

    def test_dimensions_block_is_indexed_like_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory, {"dim_product.view.yaml": self.DBT_INTEGRATION_VIEW})
            report, _ = self._run(root, {})
        self.assertGreaterEqual(report["omni_fields_indexed"], 4)

    def test_empty_dimension_matches_a_column_of_the_same_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory, {"dim_product.view.yaml": self.DBT_INTEGRATION_VIEW})
            _, issues = self._run(root, {"dim_product": {"standard_unit_price"}})
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["orphaned_fields"][0]["field"], "standard_unit_price")

    def test_catalog_schema_table_name_matches_a_bare_model_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory, {"dim_product.view.yaml": self.DBT_INTEGRATION_VIEW})
            _, issues = self._run(root, {"dim_product": {"product_detail"}})
        self.assertEqual(len(issues), 1)
        self.assertFalse(issues[0].get("ambiguous_relation_match"))

    def test_catalog_schema_table_name_matches_a_qualified_relation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory, {"dim_product.view.yaml": self.DBT_INTEGRATION_VIEW})
            _, issues = self._run(root, {"analytics_marts.dim_product": {"product_detail"}})
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["dbt_relation"], "analytics_marts.dim_product")

    def test_fully_qualified_relation_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory, {"dim_product.view.yaml": self.DBT_INTEGRATION_VIEW})
            _, issues = self._run(
                root, {"coffee_training.analytics_marts.dim_product": {"product_detail"}}
            )
        self.assertEqual(len(issues), 1)

    def test_measures_block_is_indexed(self):
        view = self.DBT_INTEGRATION_VIEW.replace(
            "measures:\n  count:\n    aggregate_type: count\n",
            "measures:\n  total_price:\n    aggregate_type: sum\n    sql: ${TABLE}.standard_unit_price\n",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory, {"dim_product.view.yaml": view})
            _, issues = self._run(root, {"dim_product": {"standard_unit_price"}})
        orphaned = {item["field"] for item in issues[0]["orphaned_fields"]}
        self.assertIn("total_price", orphaned)

    def test_quoted_and_bracketed_relations_are_normalized(self):
        view = self.DBT_INTEGRATION_VIEW.replace(
            "catalog: coffee_training\nschema: analytics_marts\ntable_name: dim_product\n",
            'sql_table_name: \'"coffee_training"."analytics_marts"."dim_product"\'\n',
        )
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory, {"dim_product.view.yaml": view})
            _, issues = self._run(
                root, {"coffee_training.analytics_marts.dim_product": {"product_detail"}}
            )
        self.assertEqual(len(issues), 1)

    def test_bracket_quoted_relations_are_normalized(self):
        view = self.DBT_INTEGRATION_VIEW.replace(
            "catalog: coffee_training\nschema: analytics_marts\ntable_name: dim_product\n",
            "sql_table_name: '[coffee_training].[analytics_marts].[dim_product]'\n",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory, {"dim_product.view.yaml": view})
            _, issues = self._run(
                root, {"coffee_training.analytics_marts.dim_product": {"product_detail"}}
            )
        self.assertEqual(len(issues), 1)


class CrossSchemaCollisionTests(unittest.TestCase):
    """A bare model name can match same-named tables in different schemas."""

    def _view(self, schema):
        return f"""catalog: coffee_training
schema: {schema}
table_name: dim_product

dimensions:
  product_detail: {{}}
"""

    def _repo(self, directory):
        root = Path(directory)
        view_dir = root / "omni/views"
        view_dir.mkdir(parents=True)
        (view_dir / "marts_dim_product.view.yaml").write_text(
            "name: marts_dim_product\n" + self._view("analytics_marts"), encoding="utf-8"
        )
        (view_dir / "staging_dim_product.view.yaml").write_text(
            "name: staging_dim_product\n" + self._view("analytics_staging"), encoding="utf-8"
        )
        return root

    def _run(self, root, removed_columns):
        with mock.patch(
            "omniflow.dbt_impact._sql_changes", return_value=(removed_columns, {})
        ):
            return evaluate_dbt_impact(
                changed_files=["models/marts/dim_product.sql"],
                dbt_paths=["models"],
                settings=settings(),
                omni_yaml_paths=["omni"],
                base_ref="origin/main",
                repo_root=root,
            )

    def test_unqualified_match_is_flagged_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            _, issues = self._run(root, {"dim_product": {"product_detail"}})
        self.assertEqual(len(issues), 1)
        issue = issues[0]
        self.assertTrue(issue["ambiguous_relation_match"])
        self.assertEqual(
            issue["candidate_relations"],
            [
                "coffee_training.analytics_marts.dim_product",
                "coffee_training.analytics_staging.dim_product",
            ],
        )
        self.assertIn("unqualified", issue["message"])
        self.assertIn("table_mapping", issue["message"])
        # Both views are still reported so a reviewer can see the full candidate set.
        orphaned = {item["view"] for item in issue["orphaned_fields"]}
        self.assertEqual(orphaned, {"marts_dim_product", "staging_dim_product"})

    def test_schema_qualified_match_is_unambiguous_and_targeted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            _, issues = self._run(root, {"analytics_marts.dim_product": {"product_detail"}})
        self.assertEqual(len(issues), 1)
        issue = issues[0]
        self.assertFalse(issue.get("ambiguous_relation_match"))
        orphaned = {item["view"] for item in issue["orphaned_fields"]}
        self.assertEqual(
            orphaned, {"marts_dim_product"}, msg="a qualified relation must not implicate the other schema"
        )

    def test_table_mapping_resolves_the_ambiguity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            with mock.patch(
                "omniflow.dbt_impact._sql_changes",
                return_value=({"coffee_training.analytics_staging.dim_product": {"product_detail"}}, {}),
            ):
                _, issues = evaluate_dbt_impact(
                    changed_files=["models/staging/dim_product.sql"],
                    dbt_paths=["models"],
                    settings=settings(
                        table_mapping=[
                            {
                                "dbt_model": "dim_product",
                                "sql_table_name": "coffee_training.analytics_staging.dim_product",
                            }
                        ]
                    ),
                    omni_yaml_paths=["omni"],
                    base_ref="origin/main",
                    repo_root=root,
                )
        self.assertEqual(len(issues), 1)
        orphaned = {item["view"] for item in issues[0]["orphaned_fields"]}
        self.assertEqual(orphaned, {"staging_dim_product"})


class ImpactConfigTests(unittest.TestCase):
    def test_defaults_are_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".omniflow.yml"
            path.write_text("checks:\n  model_validation:\n    enabled: true\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                config = load_config(path)
        self.assertFalse(config.dbt_impact.enabled)
        self.assertIsNone(config.dbt_impact.manifest_path)
        self.assertTrue(config.dbt_impact.fail_on_orphaned_references)
        self.assertEqual(config.dbt_impact.omni_yaml_paths, [])
        self.assertEqual(config.dbt_impact.table_mapping, [])

    def test_policy_is_parsed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".omniflow.yml"
            path.write_text(
                "checks:\n"
                "  dbt_impact:\n"
                "    enabled: true\n"
                "    manifest_path: target/manifest.json\n"
                "    fail_on_orphaned_references: false\n"
                "    omni_yaml_paths:\n"
                "      - omni/my_model\n"
                "    table_mapping:\n"
                "      - dbt_model: orders_v2\n"
                "        omni_view: orders\n"
                "        sql_table_name: analytics.marts.orders\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                config = load_config(path)
        self.assertTrue(config.dbt_impact.enabled)
        self.assertEqual(config.dbt_impact.manifest_path, "target/manifest.json")
        self.assertFalse(config.dbt_impact.fail_on_orphaned_references)
        self.assertEqual(config.dbt_impact.omni_yaml_paths, ["omni/my_model"])
        self.assertEqual(config.dbt_impact.table_mapping[0]["dbt_model"], "orders_v2")

    def test_invalid_values_are_rejected(self):
        cases = {
            "unknown key": "checks:\n  dbt_impact:\n    unexpected: true\n",
            "absolute manifest": "checks:\n  dbt_impact:\n    manifest_path: /etc/manifest.json\n",
            "traversal manifest": "checks:\n  dbt_impact:\n    manifest_path: ../manifest.json\n",
            "yaml paths not list": "checks:\n  dbt_impact:\n    omni_yaml_paths: omni/model\n",
            "absolute yaml path": "checks:\n  dbt_impact:\n    omni_yaml_paths:\n      - /omni\n",
            "mapping not list": "checks:\n  dbt_impact:\n    table_mapping: orders\n",
            "mapping missing model": (
                "checks:\n  dbt_impact:\n    table_mapping:\n      - omni_view: orders\n"
            ),
            "mapping unknown key": (
                "checks:\n  dbt_impact:\n    table_mapping:\n"
                "      - dbt_model: orders\n        bogus: x\n"
            ),
        }
        for name, body in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / ".omniflow.yml"
                path.write_text(body, encoding="utf-8")
                with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(ConfigError):
                    load_config(path)

    def test_excess_table_mappings_are_rejected(self):
        entries = "".join(f"      - dbt_model: model{index}\n" for index in range(501))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".omniflow.yml"
            path.write_text(f"checks:\n  dbt_impact:\n    table_mapping:\n{entries}", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(SecurityPolicyError):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
