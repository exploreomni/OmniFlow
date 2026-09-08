import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from omniflow.cli import main
from omniflow.config import DbtImpactSettings
from omniflow.dbt_impact import COVERAGE_RULE, evaluate_dbt_impact
from omniflow.dbt_manifest import diff_manifests, parse_manifest
from omniflow.dbt_sql_diff import analyze_output_columns, diff_sql_columns
from omniflow.exceptions import ConfigError, SecurityPolicyError
from omniflow.revision_data import pull_request_changed_files, read_git_text, read_head_text

REPO = Path(__file__).resolve().parents[1]


class ExactRevisionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.previous = Path.cwd()
        os.chdir(self.root)
        self.environment = mock.patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        Path("models").mkdir()
        Path("omni/views").mkdir(parents=True)
        Path(".omni").mkdir()
        Path("models/orders.sql").write_text("select customer_id, order_total from raw.orders")
        Path("omni/views/orders.view").write_text(
            "name: orders\nsql_table_name: orders\nfields:\n  customer_id:\n    sql: ${TABLE}.customer_id\n"
        )
        Path(".omniflow.yml").write_text("checks:\n  dbt_impact:\n    enabled: true\n")
        Path(".omni/flow.json").write_text(json.dumps({"version": 1, "models": [{
            "base_url": "https://omni.example", "model_id": "model-1", "model_path": "omni",
        }]}))
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("switch", "-c", "feature")
        Path("models/orders.sql").write_text("select customer_key, order_total from raw.orders")
        Path(".omniflow.yml").write_text("checks:\n  dbt_impact:\n    enabled: false\n")
        Path("untrusted.py").write_text("raise RuntimeError('never execute the head')")
        self.git("add", ".")
        self.git("commit", "-m", "head")
        self.head = self.git("rev-parse", "HEAD")
        self.git("checkout", "--detach", self.base)
        event = {"number": 4, "repository": {"full_name": "owner/repo"}, "pull_request": {
            "number": 4, "body": "", "changed_files": 3,
            "base": {"sha": self.base, "ref": "main", "repo": {"full_name": "owner/repo"}},
            "head": {"sha": self.head, "ref": "feature", "repo": {"full_name": "owner/repo"}},
        }}
        Path("event.json").write_text(json.dumps(event))
        os.environ.update({"GITHUB_EVENT_NAME": "pull_request_target", "GITHUB_BASE_REF": "main",
                           "GITHUB_EVENT_PATH": str(self.root / "event.json"),
                           "GITHUB_REPOSITORY": "owner/repo", "OMNIFLOW_GITHUB_TOKEN": "github-test-token"})

    def tearDown(self):
        self.environment.stop()
        os.chdir(self.previous)
        self.directory.cleanup()

    def git(self, *args):
        return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()

    def test_trusted_base_checkout_routes_dbt_only_and_reads_exact_head_without_omni_secret(self):
        with mock.patch("omniflow.discovery._github_pull_request_files", return_value=["models/orders.sql"]):
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                self.assertEqual(main(["route", "--auto", "--format", "json"]), 0)
            route = json.loads(captured.getvalue())
            self.assertTrue(route["should_run"])
            self.assertFalse(route["requires_omni"])
            self.assertEqual(main(["run", "--auto"]), 1)
        report = json.loads(Path(".omniflow/public/report.json").read_text())
        self.assertEqual(report["git_sha"], self.head)
        self.assertEqual(report["issues"][0]["column"], "customer_id")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertNotIn("OMNI_API_KEY", os.environ)
        self.assertFalse(Path("untrusted.py").exists())

    def test_changed_file_inventory_is_bound_to_event_head(self):
        self.assertIn("models/orders.sql", pull_request_changed_files())
        self.assertEqual(read_head_text("models/orders.sql", root=self.root, max_bytes=1000),
                         "select customer_key, order_total from raw.orders")

    def test_git_reads_reject_oversized_files_and_missing_revisions(self):
        with self.assertRaises(SecurityPolicyError):
            read_git_text(self.head, "models/orders.sql", root=self.root, max_bytes=3)
        with self.assertRaises(ConfigError):
            read_git_text("f" * 40, "models/orders.sql", root=self.root, max_bytes=1000)

    def test_git_read_rejects_symlink_and_traversal(self):
        self.git("checkout", "feature")
        Path("models/link.sql").symlink_to("orders.sql")
        self.git("add", ".")
        self.git("commit", "-m", "symlink")
        head = self.git("rev-parse", "HEAD")
        with self.assertRaises(SecurityPolicyError):
            read_git_text(head, "models/link.sql", root=self.root, max_bytes=1000)
        with self.assertRaises(ConfigError):
            read_git_text(head, "../outside", root=self.root, max_bytes=1000)


class CoverageTests(unittest.TestCase):
    def test_partial_projections_never_report_certain_removals(self):
        for sql in ("select o.*, id from orders o", "select sum(total), id from orders",
                    "select distinct id from orders", "select 'a,b' as label, id from orders",
                    "select {{ columns() }}, id from orders", "select id from a union select other from b"):
            with self.subTest(sql=sql):
                self.assertFalse(analyze_output_columns(sql)[1])
                self.assertEqual(diff_sql_columns("select customer_id, id from orders", sql), set())

    def test_source_jinja_is_supported_but_projection_jinja_is_not(self):
        self.assertTrue(analyze_output_columns("select id from {{ ref('orders') }}")[1])
        self.assertFalse(analyze_output_columns("select {{ ref('orders') }} as id")[1])

    def test_unsupported_python_and_missing_omni_evidence_block_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            report, issues = evaluate_dbt_impact(
                changed_files=["models/orders.py"], dbt_paths=["models"], settings=DbtImpactSettings(enabled=True),
                omni_yaml_paths=[], base_ref="main", repo_root=Path(directory),
            )
        self.assertFalse(report["coverage_complete"])
        self.assertEqual(issues[0]["rule"], COVERAGE_RULE)
        self.assertEqual(issues[0]["severity"], "error")

    def test_incomplete_coverage_can_be_explicitly_advisory(self):
        with tempfile.TemporaryDirectory() as directory:
            _, issues = evaluate_dbt_impact(
                changed_files=["macros/columns.sql"], dbt_paths=["macros"],
                settings=DbtImpactSettings(enabled=True, fail_on_incomplete_coverage=False),
                omni_yaml_paths=[], base_ref="main", repo_root=Path(directory),
            )
        self.assertEqual(issues[0]["severity"], "warning")

    def test_manifest_same_node_new_relation_removes_the_old_relation(self):
        node = {"resource_type": "model", "name": "orders", "relation_name": "analytics.old.orders",
                "columns": {"id": {}}, "config": {"materialized": "table"}}
        base = parse_manifest(json.dumps({"nodes": {"model.p.orders": node}}))
        node["relation_name"] = "analytics.new.orders"
        head = parse_manifest(json.dumps({"nodes": {"model.p.orders": node}}))
        removed, _ = diff_manifests(base, head)
        self.assertEqual(removed["model.p.orders"].relation_name, "analytics.old.orders")

    def test_action_separates_dbt_analysis_from_omni_credentials(self):
        action = yaml.safe_load((REPO / "action.yml").read_text())
        step = next(step for step in action["runs"]["steps"] if step["name"].startswith("Run dbt impact"))
        self.assertNotIn("OMNI_API_KEY", step["env"])
        self.assertIn("requires_omni == 'false'", step["if"])
