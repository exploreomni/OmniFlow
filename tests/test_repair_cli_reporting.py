import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omniflow.artifacts import write_artifact_manifest
from omniflow.cli import _run_context, build_parser, main
from omniflow.config import load_config
from omniflow.discovery import ModelContext
from omniflow.repair.reporting import write_repair_artifacts


class RepairCliReportingTests(unittest.TestCase):
    def test_repair_cli_is_explicitly_nested_and_auto_discovered(self):
        args = build_parser().parse_args(["repair", "ai", "--auto"])
        self.assertTrue(args.auto)
        self.assertEqual(args.func.__name__, "cmd_repair_ai")

    def test_disabled_repair_exits_security_policy_and_writes_public_evidence(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                Path(".omniflow.yml").write_text("repairs:\n  ai:\n    enabled: false\n", encoding="utf-8")
                with mock.patch.dict(os.environ, {}, clear=True):
                    exit_code = main(["repair", "ai", "--auto"])
                self.assertEqual(exit_code, 5)
                report = json.loads(Path(".omniflow/public/repair.json").read_text(encoding="utf-8"))
                self.assertEqual(report["status"], "failed")
                self.assertFalse(report["query_execution_acknowledged"])
                self.assertFalse(report["raw_query_results_stored"])
                self.assertTrue(Path(".omniflow/public/repair.md").is_file())
                self.assertTrue(Path(".omniflow/public/evidence.json").is_file())
            finally:
                os.chdir(original)

    def test_repair_artifacts_are_metadata_only_and_manifested(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                report = {
                    "tool": "omniflow",
                    "operation": "ai_repair_beta",
                    "status": "committed",
                    "message": "Safe repair for <tag> @reviewer",
                    "model_id": "model-1",
                    "branch_name": "feature/repair",
                    "head_sha": "a" * 40,
                    "policy_decision": "pass",
                    "exit_code": 0,
                    "raw_query_results_stored": False,
                    "change_summary": {
                        "modified_files": ["orders.view"],
                        "changed_lines": 2,
                    },
                    "rollback": None,
                    "manual_review_required": False,
                }
                write_repair_artifacts(report, output_dir=".omniflow", redaction_level="standard")
                write_artifact_manifest(
                    output_dir=".omniflow",
                    restricted_artifacts_enabled=False,
                    redaction_level="standard",
                )
                markdown = Path(".omniflow/public/repair.md").read_text(encoding="utf-8")
                manifest = json.loads(Path(".omniflow/artifact-manifest.json").read_text(encoding="utf-8"))
                self.assertIn("&lt;tag&gt;", markdown)
                self.assertIn("&#64;reviewer", markdown)
                self.assertIn("public/repair.json", manifest["public_artifacts"])
                self.assertIn("public/repair.md", manifest["public_artifacts"])
                self.assertNotIn("prompt", Path(".omniflow/public/repair.json").read_text(encoding="utf-8"))
            finally:
                os.chdir(original)

    def test_repair_validation_runner_injects_repair_key_into_full_context_gate(self):
        config = load_config()
        config.content_validation.enabled = False
        config.model_validation.enabled = False
        config.semantic_lint.enabled = False
        config.contracts.enabled = False
        config.dbt_exposures.enabled = False
        model_context = ModelContext(
            base_url="https://omni.example",
            model_id="model-1",
            model_path="omni/model",
            branch_name="feature/repair",
            branch_id="branch-1",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "omniflow.cli._client_and_branch_for_context",
                return_value=(object(), "branch-1"),
            ) as factory:
                report, exit_code = _run_context(
                    config=config,
                    context=model_context,
                    output_dir=Path(tmp),
                    api_key="repair-token",  # pragma: allowlist secret
                )
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["policy_decision"], "pass")
        self.assertEqual(factory.call_args.kwargs["api_key"], "repair-token")


if __name__ == "__main__":
    unittest.main()
