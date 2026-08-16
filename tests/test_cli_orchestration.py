import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from omniflow.cli import (
    _client_and_branch_for_context,
    _git_configuration_issues,
    _write_context_failure_artifacts,
    _write_setup_failure_artifacts,
    _write_unconfigured_failure_artifacts,
    cmd_route,
    cmd_run,
)
from omniflow.config import load_config
from omniflow.discovery import ModelContext
from omniflow.exceptions import ConfigError, OmniAPIError, SecurityPolicyError
from omniflow.security import secure_write_text


class FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    def resolve_branch_id(self, model_id, branch_name):
        return None


class CliOrchestrationTests(unittest.TestCase):
    def test_route_skips_and_writes_evidence_without_an_omni_api_key(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                args = SimpleNamespace(
                    config=None,
                    auto=True,
                    format="github",
                    base_url=None,
                    model_id=None,
                    model_path=None,
                    branch_id=None,
                    branch_name=None,
                    user_id=None,
                    include_personal_folders=None,
                )
                with mock.patch("omniflow.cli.discover_contexts", return_value=[]):
                    with mock.patch("omniflow.cli.require_api_key") as require_key:
                        with mock.patch("builtins.print") as output:
                            exit_code = cmd_route(args)
                self.assertEqual(exit_code, 0)
                require_key.assert_not_called()
                output.assert_any_call("should_run=false")
                report = json.loads(Path(".omniflow/public/report.json").read_text(encoding="utf-8"))
                self.assertEqual(report["policy_decision"], "skipped")
            finally:
                os.chdir(original)

    def test_route_allows_relevant_context_without_writing_skip_artifacts(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                args = SimpleNamespace(
                    config=None,
                    auto=True,
                    format="json",
                    base_url=None,
                    model_id=None,
                    model_path=None,
                    branch_id=None,
                    branch_name=None,
                    user_id=None,
                    include_personal_folders=None,
                )
                context = ModelContext(
                    base_url="https://omni.example",
                    model_id="model-1",
                    model_path="omni/model",
                )
                with mock.patch("omniflow.cli.discover_contexts", return_value=[context]):
                    with mock.patch("builtins.print") as output:
                        exit_code = cmd_route(args)
                self.assertEqual(exit_code, 0)
                payload = json.loads(output.call_args.args[0])
                self.assertEqual(payload, {"model_count": 1, "reason": "", "should_run": True})
                self.assertFalse(Path(".omniflow/public/report.json").exists())
            finally:
                os.chdir(original)

    def test_multi_context_run_aggregates_partial_failure_and_cleans_restricted_data(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                contexts = [
                    ModelContext(
                        base_url="https://omni.example",
                        model_id="model-1",
                        model_path="omni/model-1",
                    ),
                    ModelContext(
                        base_url="https://omni.example",
                        model_id="model-2",
                        model_path="omni/model-2",
                    ),
                ]
                args = SimpleNamespace(
                    config=None,
                    auto=True,
                    skip_reason=None,
                    base_url=None,
                    model_id=None,
                    model_path=None,
                    branch_id=None,
                    branch_name=None,
                    user_id=None,
                    include_personal_folders=None,
                )
                with mock.patch("omniflow.cli.discover_contexts", return_value=contexts):
                    with mock.patch(
                        "omniflow.cli._run_context",
                        side_effect=[
                            ({"model_id": "model-1", "issues": []}, 0),
                            OmniAPIError("Omni API request failed"),
                        ],
                    ):
                        exit_code = cmd_run(args)
                report = json.loads(Path(".omniflow/public/report.json").read_text(encoding="utf-8"))
                self.assertEqual(exit_code, 4)
                self.assertEqual(report["policy_decision"], "fail")
                self.assertEqual(len(report["model_reports"]), 2)
                self.assertFalse(Path(".omniflow/restricted").exists())
            finally:
                os.chdir(original)

    def test_invalid_config_failure_purges_stale_restricted_artifacts(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                stale = Path(".omniflow/restricted/stale/private.json")
                stale.parent.mkdir(parents=True)
                stale.write_text("private", encoding="utf-8")
                _write_unconfigured_failure_artifacts(
                    output_dir=Path(".omniflow"),
                    exc=ConfigError("invalid policy"),
                )
                self.assertFalse(Path(".omniflow/restricted").exists())
            finally:
                os.chdir(original)

    def test_restricted_artifacts_are_purged_on_unexpected_context_failure(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                config = load_config(None)
                stale = Path(".omniflow/restricted/stale/private.json")
                stale.parent.mkdir(parents=True)
                stale.write_text("private", encoding="utf-8")
                context = ModelContext(
                    base_url="https://omni.example",
                    model_id="model-1",
                    model_path="omni/model",
                )

                def fail_with_restricted_file(*, output_dir, **kwargs):
                    secure_write_text(output_dir / "private.json", "private")
                    raise RuntimeError("unexpected failure")

                args = SimpleNamespace(
                    config=None,
                    auto=True,
                    skip_reason=None,
                    base_url=None,
                    model_id=None,
                    model_path=None,
                    branch_id=None,
                    branch_name=None,
                    user_id=None,
                    include_personal_folders=None,
                )
                with mock.patch("omniflow.cli.load_config", return_value=config):
                    with mock.patch("omniflow.cli.discover_contexts", return_value=[context]):
                        with mock.patch("omniflow.cli._run_context", side_effect=fail_with_restricted_file):
                            with self.assertRaises(RuntimeError):
                                cmd_run(args)
                restricted = Path(".omniflow/restricted")
                self.assertFalse(stale.exists())
                self.assertFalse(any(restricted.rglob("*")) if restricted.exists() else False)
            finally:
                os.chdir(original)

    def test_branch_name_without_resolved_branch_id_fails_closed(self):
        context = ModelContext(
            base_url="https://omni.example",
            model_id="model-1",
            model_path="omni/model",
            branch_name="feature/a",
        )
        with mock.patch("omniflow.cli.require_api_key", return_value="secret"):
            with mock.patch("omniflow.cli.OmniClient", FakeClient):
                with self.assertRaises(ConfigError):
                    _client_and_branch_for_context(context, timeout=60)

    def test_git_configuration_issues_detect_metadata_drift(self):
        context = ModelContext(
            base_url="https://omni.example",
            model_id="model-1",
            model_path="omni/model",
            base_branch="main",
            git_provider="github",
            web_url="https://github.com/acme/repo",
        )
        issues = _git_configuration_issues(
            context,
            {
                "modelPath": "omni/other",
                "baseBranch": "main",
                "gitServiceProvider": "github",
                "webUrl": "https://github.com/acme/repo",
            },
        )
        self.assertEqual(issues, ["model_path expected 'omni/model' but Omni reports 'omni/other'"])

    def test_git_configuration_issues_pass_when_metadata_matches(self):
        context = ModelContext(
            base_url="https://omni.example",
            model_id="model-1",
            model_path="omni/model/",
            base_branch="main",
            git_provider="github",
            web_url="https://github.com/acme/repo/",
        )
        issues = _git_configuration_issues(
            context,
            {
                "modelPath": "omni/model",
                "baseBranch": "main",
                "gitServiceProvider": "github",
                "webUrl": "https://github.com/acme/repo",
            },
        )
        self.assertEqual(issues, [])

    def test_context_failure_writes_report_artifact(self):
        context = ModelContext(
            base_url="https://omni.example",
            model_id="model-1",
            model_path="omni/model",
            branch_name="feature/a",
        )
        with tempfile.TemporaryDirectory() as tmp:
            report, exit_code = _write_context_failure_artifacts(
                config=load_config(None),
                context=context,
                output_dir=Path(tmp),
                exc=OmniAPIError("Omni API request failed"),
            )
            self.assertTrue((Path(tmp) / "report.json").exists())
        self.assertEqual(exit_code, 4)
        self.assertEqual(report["exit_code_reason"], "Omni API error")
        self.assertEqual(report["issues"][0]["validator"], "context")

    def test_setup_failure_writes_security_policy_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_setup_failure_artifacts(
                config=load_config(None),
                output_dir=Path(tmp),
                exc=SecurityPolicyError("unsafe marker"),
            )
            report = (Path(tmp) / "public/report.json").read_text(encoding="utf-8")
        self.assertIn("security policy violation", report)
        self.assertIn("unsafe marker", report)


if __name__ == "__main__":
    unittest.main()
