import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omniflow.diff.diff_engine import diff_graphs
from omniflow.diff.semantic_graph import build_graph
from omniflow.diff.yaml_loader import load_yaml_files
from omniflow.exceptions import ConfigError, SecurityPolicyError
from omniflow.github.annotations import annotation_lines
from omniflow.reporting.junit_report import to_junit
from omniflow.reporting.markdown_report import render_markdown_report
from omniflow.reporting.sarif_report import to_sarif
from omniflow.validators.yaml_lint import has_error, lint_graph
from omniflow.yaml_pull import pull_yaml


class FakeYamlClient:
    def get_model_yaml(self, *args, **kwargs):
        return {
            "files": {"views/orders.view": "name: orders\nfields:\n  id:\n    primary_key: true\n"},
            "checksums": {"views/orders.view": "abc"},
        }


class UnsafeYamlClient:
    def get_model_yaml(self, *args, **kwargs):
        return {"files": {"../escaped.view": "name: escaped\n"}, "checksums": {}}


class MultiFileYamlClient:
    def get_model_yaml(self, *args, **kwargs):
        return {"files": {"views/a.view": "name: a\n", "views/b.view": "name: b\n"}, "checksums": {}}


class DiffLintReportTests(unittest.TestCase):
    def test_loads_composite_topic_files_as_topics(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "revenue.composite_topic"
            path.write_text("name: Revenue Composite\nbase_view: orders\n", encoding="utf-8")
            graph = build_graph(load_yaml_files(tmp))
        self.assertIn("Revenue Composite", graph.topics)

    def test_yaml_pull_writes_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = pull_yaml(
                client=FakeYamlClient(),
                model_id="model-1",
                branch_id="branch-1",
                output_dir=tmp,
            )
            self.assertTrue((Path(tmp) / "views/orders.view").exists())
            self.assertEqual(manifest["files"]["views/orders.view"]["checksum"], "abc")
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE((Path(tmp) / "views/orders.view").stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE((Path(tmp) / "views").stat().st_mode), 0o700)

    def test_yaml_pull_rejects_excessive_file_count_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "yaml"
            with mock.patch("omniflow.yaml_pull.MAX_YAML_FILES", 1):
                with self.assertRaises(SecurityPolicyError):
                    pull_yaml(
                        client=MultiFileYamlClient(),
                        model_id="model-1",
                        branch_id=None,
                        output_dir=root,
                    )
            self.assertFalse(root.exists())

    def test_yaml_loader_rejects_cycles_depth_and_alias_fanout(self):
        cases = {
            "cycle.view": "loop: &loop [*loop]\n",
            "depth.view": "value: " + ("[" * 101) + "0" + ("]" * 101) + "\n",
            "aliases.view": "anchor: &value 1\nvalues: [" + ",".join("*value" for _ in range(51)) + "]\n",
        }
        for name, body in cases.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmp:
                    (Path(tmp) / name).write_text(body, encoding="utf-8")
                    with self.assertRaises(SecurityPolicyError):
                        load_yaml_files(tmp)

    def test_yaml_parse_error_does_not_echo_customer_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orders.view"
            path.write_text("private_customer_value: [unterminated\n", encoding="utf-8")
            with self.assertRaises(ConfigError) as raised:
                load_yaml_files(tmp)
        self.assertNotIn("private_customer_value", str(raised.exception))
        self.assertIn("line", str(raised.exception))

    def test_yaml_pull_rejects_branch_id_for_non_combined_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError):
                pull_yaml(
                    client=FakeYamlClient(),
                    model_id="model-1",
                    branch_id="branch-1",
                    output_dir=tmp,
                    mode="staged",
                )

    def test_yaml_pull_rejects_path_traversal_from_api_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SecurityPolicyError):
                pull_yaml(
                    client=UnsafeYamlClient(),
                    model_id="model-1",
                    branch_id=None,
                    output_dir=Path(tmp) / "yaml",
                )
            self.assertFalse((Path(tmp) / "escaped.view").exists())

    def test_yaml_pull_rejects_symlinked_api_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "yaml"
            root.mkdir()
            outside = Path(tmp) / "outside.view"
            outside.write_text("original\n", encoding="utf-8")
            (root / "views").mkdir()
            (root / "views/orders.view").symlink_to(outside)
            with self.assertRaises(SecurityPolicyError):
                pull_yaml(
                    client=FakeYamlClient(),
                    model_id="model-1",
                    branch_id=None,
                    output_dir=root,
                )
            self.assertEqual(outside.read_text(encoding="utf-8"), "original\n")

    def test_yaml_pull_rejects_symlinked_parent_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            real_parent = Path(tmp) / "real"
            real_parent.mkdir()
            linked_parent = Path(tmp) / "linked"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            with self.assertRaises(SecurityPolicyError):
                pull_yaml(
                    client=FakeYamlClient(),
                    model_id="model-1",
                    branch_id=None,
                    output_dir=linked_parent / "yaml",
                )
            self.assertFalse((real_parent / "yaml").exists())

    def test_yaml_pull_rejects_symlinked_manifest_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "yaml"
            root.mkdir()
            outside = Path(tmp) / "outside.json"
            outside.write_text("original\n", encoding="utf-8")
            (root / "manifest.json").symlink_to(outside)
            with self.assertRaises(SecurityPolicyError):
                pull_yaml(
                    client=FakeYamlClient(),
                    model_id="model-1",
                    branch_id=None,
                    output_dir=root,
                )
            self.assertEqual(outside.read_text(encoding="utf-8"), "original\n")

    def test_semantic_diff_detects_deleted_field_and_type_change(self):
        base = build_graph(
            {
                "views/orders.view": {
                    "name": "orders",
                    "fields": {"id": {"type": "number"}, "revenue": {"type": "number"}},
                }
            }
        )
        head = build_graph({"views/orders.view": {"name": "orders", "fields": {"id": {"type": "string"}}}})
        report = diff_graphs(base, head)
        types = {change["type"] for change in report["changes"]}
        self.assertIn("field_deleted", types)
        self.assertIn("field_type_changed", types)
        self.assertEqual(report["risk_level"], "breaking")

    def test_semantic_diff_detects_relationship_type_change(self):
        base = build_graph(
            {
                "relationships/order_items.relationships": {
                    "relationships": {"orders": {"relationship_type": "many_to_one"}}
                }
            }
        )
        head = build_graph(
            {
                "relationships/order_items.relationships": {
                    "relationships": {"orders": {"relationship_type": "one_to_many"}}
                }
            }
        )
        report = diff_graphs(base, head)
        types = {change["type"] for change in report["changes"]}
        self.assertIn("relationship_cardinality_changed", types)

    def test_semantic_diff_parses_documented_top_level_relationship_list(self):
        base = build_graph(
            {
                "relationships.yaml": [
                    {
                        "join_from_view": "orders",
                        "join_to_view": "order_items",
                        "relationship_type": "many_to_one",
                    }
                ]
            }
        )
        head = build_graph(
            {
                "relationships.yaml": [
                    {
                        "join_from_view": "orders",
                        "join_to_view": "order_items",
                        "relationship_type": "one_to_many",
                    }
                ]
            }
        )
        report = diff_graphs(base, head)
        cardinality = next(
            change for change in report["changes"] if change["type"] == "relationship_cardinality_changed"
        )
        self.assertEqual(cardinality["affected_views"], ["orders", "order_items"])
        self.assertEqual(cardinality["risk"], "breaking")

    def test_semantic_diff_parses_documented_topic_level_relationships(self):
        base = build_graph(
            {
                "topics/orders.topic": {
                    "base_view": "orders",
                    "relationships": [
                        {
                            "join_from_view": "orders",
                            "join_to_view": "users",
                            "join_to_view_as": "buyers",
                            "relationship_type": "many_to_one",
                        }
                    ],
                }
            }
        )
        head = build_graph(
            {
                "topics/orders.topic": {
                    "base_view": "orders",
                    "relationships": [
                        {
                            "join_from_view": "orders",
                            "join_to_view": "users",
                            "join_to_view_as": "buyers",
                            "relationship_type": "one_to_many",
                        }
                    ],
                }
            }
        )
        report = diff_graphs(base, head)
        cardinality = next(
            change for change in report["changes"] if change["type"] == "relationship_cardinality_changed"
        )
        self.assertEqual(cardinality["affected_views"], ["orders", "users"])
        self.assertEqual(cardinality["name"], "orders:->users:buyers")

    def test_topic_join_visibility_tree_is_not_treated_as_a_relationship_definition(self):
        graph = build_graph(
            {
                "topics/orders.topic": {
                    "base_view": "orders",
                    "joins": {"users": {}, "items": {"products": {}}},
                }
            }
        )
        self.assertEqual(graph.relationships, {})

    def test_reordering_relationships_does_not_create_semantic_changes(self):
        first = {
            "join_from_view": "orders",
            "join_to_view": "users",
            "relationship_type": "many_to_one",
        }
        second = {
            "join_from_view": "orders",
            "join_to_view": "items",
            "relationship_type": "one_to_many",
        }
        base = build_graph({"relationships.yaml": [first, second]})
        head = build_graph({"relationships.yaml": [second, first]})
        self.assertEqual(diff_graphs(base, head)["changes"], [])

    def test_topic_and_global_relationships_with_same_endpoints_do_not_overwrite_each_other(self):
        relationship = {
            "join_from_view": "orders",
            "join_to_view": "users",
            "relationship_type": "many_to_one",
        }
        graph = build_graph(
            {
                "relationships.yaml": [relationship],
                "topics/orders.topic": {"base_view": "orders", "relationships": [relationship]},
            }
        )
        self.assertEqual(len(graph.relationships), 2)

    def test_rule_severity_handling(self):
        graph = build_graph({"views/orders.view": {"name": "orders", "fields": {"revenue": {"type": "number"}}}})
        issues = lint_graph(graph, configured_rules={"require_primary_keys": "error"})
        self.assertTrue(has_error(issues))

    def test_default_lint_does_not_block_unreferenced_deletion(self):
        base = build_graph(
            {
                "views/orders.view": {
                    "name": "orders",
                    "dimensions": {
                        "id": {"primary_key": True, "description": "Order ID"},
                        "legacy": {"description": "Legacy value"},
                    },
                }
            }
        )
        head = build_graph(
            {
                "views/orders.view": {
                    "name": "orders",
                    "dimensions": {"id": {"primary_key": True, "description": "Order ID"}},
                }
            }
        )
        issues = lint_graph(head, diff_result=diff_graphs(base, head))
        deletion = next(issue for issue in issues if issue["rule_id"] == "block_deleted_fields")
        self.assertEqual(deletion["severity"], "warning")
        self.assertFalse(has_error(issues))

    def test_many_to_many_lint_reads_documented_relationship_type(self):
        graph = build_graph(
            {
                "relationships.yaml": [
                    {
                        "join_from_view": "orders",
                        "join_to_view": "customers",
                        "relationship_type": "many_to_many",
                    }
                ]
            }
        )
        issues = lint_graph(
            graph,
            configured_rules={"forbid_many_to_many_without_comment": "error"},
        )
        self.assertTrue(any(issue["rule_id"] == "forbid_many_to_many_without_comment" for issue in issues))
        self.assertTrue(has_error(issues))

    def test_sarif_and_junit_output(self):
        report = {
            "tool_version": "0.4.0",
            "issues": [{"rule_id": "x", "severity": "error", "file": "a.yml", "message": "bad"}],
        }
        sarif = to_sarif(report)
        junit = to_junit(report)
        self.assertEqual(sarif["version"], "2.1.0")
        self.assertIn("<failure", junit)

    def test_github_annotations_follow_policy_severity_and_escape_commands(self):
        lines = annotation_lines(
            [
                {
                    "severity": "warning",
                    "risk": "breaking",
                    "file": "views/orders:view,one.view",
                    "message": "line one\nline two%",
                },
                {"severity": "error", "message": "blocked"},
                {"severity": "info", "active": False, "message": "resolved"},
            ]
        )
        self.assertTrue(lines[0].startswith("::warning "))
        self.assertIn("%3A", lines[0])
        self.assertIn("%2C", lines[0])
        self.assertIn("%0A", lines[0])
        self.assertIn("%25", lines[0])
        self.assertTrue(lines[1].startswith("::error "))
        self.assertEqual(len(lines), 2)

    def test_markdown_report_is_reviewer_friendly_for_contract_failure(self):
        report = {
            "tool_version": "0.4.0",
            "generated_at": "2026-08-09T00:00:00Z",
            "git_sha": "abc",
            "git_branch": "feature/a",
            "config_hash": "hash",
            "policy_decision": "fail",
            "exit_code_reason": "validation failed",
            "models": [{"model_id": "model-1", "model_path": "omni/model", "branch_name": "feature/a"}],
            "summary": {"total_issues": 1, "errors": 1, "warnings": 0, "risk_level": "breaking"},
            "issues": [
                {
                    "validator": "contracts",
                    "severity": "error",
                    "impact_level": "referenced_breaking",
                    "field": "orders.revenue",
                    "referenced_content": [{"content_id": "dash-1"}],
                    "message": "Deleted referenced field.",
                }
            ],
            "model_reports": [
                {
                    "model_id": "model-1",
                    "check_reports": [
                        {
                            "coverage_gaps": [
                                {"type": "field", "name": "orders.margin", "message": "targeted search unavailable"}
                            ]
                        },
                        {
                            "validator": "dbt_exposures",
                            "summary": {
                                "total_records": 4,
                                "total_exposures": 3,
                                "unmapped_dashboards": 1,
                                "coverage_status": "partial",
                            },
                        },
                    ]
                }
            ],
        }
        markdown = render_markdown_report(report)
        self.assertIn("## Decision", markdown)
        self.assertIn("Fail: review blocking issues before merge.", markdown)
        self.assertIn("`model-1` path `omni/model` branch `feature/a`", markdown)
        self.assertIn("## Downstream Contract Impact", markdown)
        self.assertIn("referenced content: `1`", markdown)
        self.assertIn("orders.margin", markdown)
        self.assertIn("## dbt Exposure Coverage", markdown)
        self.assertIn("`3` mapped exposure(s) across `4` dashboard record(s)", markdown)
        self.assertIn("unmapped `1`; coverage `partial`", markdown)
        self.assertIn("Resolve blocking validation", markdown)

    def test_markdown_report_escapes_model_controlled_markup_and_mentions(self):
        report = {
            "policy_decision": "fail",
            "issues": [
                {
                    "severity": "error",
                    "message": "[click](https://example.com) @maintainers **urgent** <img src=x>",
                }
            ],
        }
        markdown = render_markdown_report(report)
        self.assertNotIn("[click](", markdown)
        self.assertNotIn("@maintainers", markdown)
        self.assertNotIn("<img", markdown)
        self.assertIn("\\[click\\]", markdown)
        self.assertIn("&#64;maintainers", markdown)

    def test_markdown_report_guides_skipped_non_omni_prs(self):
        markdown = render_markdown_report(
            {
                "tool_version": "0.4.0",
                "policy_decision": "skipped",
                "exit_code_reason": "no Omni PR context or changed Omni model files detected",
                "summary": {},
                "issues": [],
            }
        )
        self.assertIn("Skipped: no Omni semantic-layer changes were detected.", markdown)
        self.assertIn("No reviewer action needed", markdown)
        self.assertIn("Not evaluated because no Omni semantic-layer changes were detected.", markdown)
        self.assertNotIn("Model ID: ``", markdown)
        self.assertNotIn("dbt exposure enrichment was not enabled", markdown)

    def test_markdown_report_uses_deployment_language_for_dbt_sync(self):
        refresh = {
            "connection_id": "connection-1",
            "job_id": "job-1",
            "status": "completed",
            "refresh_mode": "hard",
            "affected_model_ids": ["model-1", "model-2"],
        }
        markdown = render_markdown_report(
            {
                "operation": "dbt_sync",
                "tool_version": "0.4.0",
                "policy_decision": "fail",
                "exit_code_reason": "validation failed",
                "summary": {},
                "issues": [],
                "models": [
                    {"model_id": "model-1", "model_path": "omni/model-1", "base_branch": "main"},
                    {"model_id": "model-2", "model_path": "omni/model-2", "base_branch": "main"},
                ],
                "model_reports": [
                    {
                        "model_id": "model-1",
                        "refresh": refresh,
                        "post_sync_validation_status": "passed",
                    },
                    {
                        "model_id": "model-2",
                        "refresh": refresh,
                        "post_sync_validation_status": "failed",
                    },
                ],
            }
        )
        self.assertIn("Fail: do not mark the dbt deployment complete.", markdown)
        self.assertIn("## dbt Synchronization", markdown)
        self.assertIn("Connection `connection-1` job `job-1`", markdown)
        self.assertIn("affected models `2`; post-sync validation `failed`", markdown)
        self.assertIn("Keep deployment open", markdown)
        self.assertNotIn("before merge", markdown)


if __name__ == "__main__":
    unittest.main()
