import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from omniflow.cli import cmd_dbt_sync, main
from omniflow.config import load_config
from omniflow.dbt_sync import run_dbt_sync, validate_dbt_sync_environment
from omniflow.discovery import ModelContext
from omniflow.exceptions import ConfigError, OmniAPIError, SecurityPolicyError


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class FakeRefreshClient:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.start_calls = []
        self.status_calls = []

    def start_schema_refresh(self, model_id, *, branch_id=None, hard_refresh=True):
        self.start_calls.append((model_id, branch_id, hard_refresh))
        return {"job_id": "job-1", "model_id": model_id, "status": "RUNNING"}

    def get_schema_refresh_job_status(self, job_id):
        self.status_calls.append(job_id)
        return {"job_id": job_id, "job_type": "refresh_schema", "status": self.statuses.pop(0)}


def enabled_config(path: Path):
    path.write_text(
        "deployment:\n"
        "  dbt_sync:\n"
        "    enabled: true\n"
        "    refresh_mode: hard\n"
        "    poll_interval_seconds: 2\n"
        "    timeout_seconds: 30\n"
        "    post_sync_validation: true\n",
        encoding="utf-8",
    )
    config = load_config(path.resolve())
    config.semantic_lint.enabled = False
    config.contracts.enabled = False
    return config


class DbtSyncTests(unittest.TestCase):
    def test_refresh_polls_to_completion_without_retaining_response_payloads(self):
        clock = FakeClock()
        client = FakeRefreshClient(["RUNNING", "COMPLETED"])
        report, exit_code = run_dbt_sync(
            client=client,
            model_id="model-1",
            branch_id="branch-1",
            refresh_mode="soft",
            poll_interval_seconds=2,
            timeout_seconds=30,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["polls"], 2)
        self.assertFalse(report["raw_query_results_stored"])
        self.assertEqual(client.start_calls, [("model-1", "branch-1", False)])

    def test_failed_and_timed_out_refreshes_exit_as_omni_api_errors(self):
        failed_clock = FakeClock()
        failed, failed_exit = run_dbt_sync(
            client=FakeRefreshClient(["FAILED"]),
            model_id="model-1",
            branch_id=None,
            refresh_mode="hard",
            poll_interval_seconds=2,
            timeout_seconds=30,
            sleep=failed_clock.sleep,
            monotonic=failed_clock.monotonic,
        )
        self.assertEqual(failed_exit, 4)
        self.assertEqual(failed["status"], "failed")

        timeout_clock = FakeClock()
        timed_out, timeout_exit = run_dbt_sync(
            client=FakeRefreshClient(["RUNNING"] * 15),
            model_id="model-1",
            branch_id=None,
            refresh_mode="hard",
            poll_interval_seconds=2,
            timeout_seconds=30,
            sleep=timeout_clock.sleep,
            monotonic=timeout_clock.monotonic,
        )
        self.assertEqual(timeout_exit, 4)
        self.assertEqual(timed_out["status"], "timed_out")

    def test_deployment_guard_accepts_only_the_trusted_base_branch(self):
        context = ModelContext(
            base_url="https://omni.example",
            model_id="model-1",
            model_path="omni/model",
            base_branch="main",
        )
        env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REF_NAME": "main",
            "GITHUB_REF_TYPE": "branch",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(validate_dbt_sync_environment([context]), "main")

        for unsafe_env in (
            {**env, "GITHUB_EVENT_NAME": "pull_request", "GITHUB_HEAD_REF": "feature/a"},
            {**env, "GITHUB_EVENT_NAME": "schedule"},
            {**env, "GITHUB_REF_NAME": "feature/a"},
            {**env, "GITHUB_REF_TYPE": "tag"},
        ):
            with self.subTest(unsafe_env=unsafe_env):
                with mock.patch.dict(os.environ, unsafe_env, clear=True):
                    with self.assertRaises(SecurityPolicyError):
                        validate_dbt_sync_environment([context])

    def test_cli_runs_refresh_then_validation_and_writes_public_evidence(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                config = enabled_config(Path(".omniflow.yml"))
                config.semantic_lint.enabled = True
                context = ModelContext(
                    base_url="https://omni.example",
                    model_id="model-1",
                    model_path="omni/model",
                    base_branch="main",
                )
                args = SimpleNamespace(
                    config=".omniflow.yml",
                    auto=True,
                    base_url=None,
                    model_id=None,
                    model_path=None,
                    branch_id=None,
                    branch_name=None,
                    base_branch=None,
                    refresh_mode=None,
                    user_id=None,
                    include_personal_folders=None,
                )
                refresh_report = {
                    "tool": "omniflow",
                    "operation": "dbt_sync",
                    "model_id": "model-1",
                    "job_id": "job-1",
                    "status": "completed",
                    "issues": [],
                    "raw_query_results_stored": False,
                }
                client = mock.Mock()
                client.get_model_metadata.return_value = {
                    "model_id": "model-1",
                    "connection_id": "connection-1",
                }
                client.list_refresh_affected_model_ids.return_value = ["model-1"]
                with mock.patch("omniflow.cli.load_config", return_value=config):
                    with mock.patch("omniflow.cli.discover_deployment_contexts", return_value=[context]):
                        with mock.patch("omniflow.cli.validate_dbt_sync_environment", return_value="main"):
                            with mock.patch("omniflow.cli.require_sync_api_key", return_value="sync-token"):
                                with mock.patch(
                                    "omniflow.cli._client_and_branch_for_context",
                                    return_value=(client, None),
                                ):
                                    with mock.patch(
                                        "omniflow.cli.run_dbt_sync",
                                        return_value=(refresh_report, 0),
                                    ):
                                        with mock.patch("omniflow.cli.pull_yaml") as snapshot:
                                            with mock.patch(
                                                "omniflow.cli._run_context",
                                                return_value=({"issues": [], "exit_code": 0}, 0),
                                            ) as validation:
                                                exit_code = cmd_dbt_sync(args)
                self.assertEqual(exit_code, 0)
                snapshot.assert_called_once()
                validation.assert_called_once()
                comparison_dir = validation.call_args.kwargs["comparison_base_yaml_dir"]
                self.assertEqual(
                    comparison_dir,
                    Path(".omniflow/restricted/model-1/pre-sync-yaml"),
                )
                report = json.loads(Path(".omniflow/public/report.json").read_text(encoding="utf-8"))
                evidence = json.loads(Path(".omniflow/public/evidence.json").read_text(encoding="utf-8"))
                sync = json.loads(Path(".omniflow/public/dbt-sync.json").read_text(encoding="utf-8"))
                self.assertEqual(report["operation"], "dbt_sync")
                self.assertEqual(evidence["policy_decision"], "pass")
                self.assertFalse(sync["raw_query_results_stored"])
                self.assertFalse(Path(".omniflow/restricted").exists())
                public_text = "\n".join(
                    path.read_text(encoding="utf-8") for path in Path(".omniflow/public").glob("*") if path.is_file()
                )
                self.assertNotIn("sync-token", public_text)
            finally:
                os.chdir(original)

    def test_cli_refreshes_a_shared_connection_once_and_keeps_per_model_evidence(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                config = enabled_config(Path(".omniflow.yml"))
                config.dbt_sync.post_sync_validation = False
                contexts = [
                    ModelContext(
                        base_url="https://omni.example",
                        model_id=model_id,
                        model_path=f"omni/{model_id}",
                        base_branch="main",
                    )
                    for model_id in ("model-1", "model-2")
                ]
                clients = []
                for context in contexts:
                    client = mock.Mock()
                    client.get_model_metadata.return_value = {
                        "model_id": context.model_id,
                        "connection_id": "connection-1",
                    }
                    client.list_refresh_affected_model_ids.return_value = ["model-1", "model-2"]
                    clients.append(client)
                args = SimpleNamespace(
                    config=".omniflow.yml",
                    auto=True,
                    base_url=None,
                    model_id=None,
                    model_path=None,
                    branch_id=None,
                    branch_name=None,
                    base_branch=None,
                    refresh_mode=None,
                    user_id=None,
                    include_personal_folders=None,
                )
                refresh_report = {
                    "tool": "omniflow",
                    "operation": "dbt_sync",
                    "model_id": "model-1",
                    "job_id": "job-1",
                    "status": "completed",
                    "issues": [],
                    "raw_query_results_stored": False,
                }
                with mock.patch("omniflow.cli.load_config", return_value=config):
                    with mock.patch("omniflow.cli.discover_deployment_contexts", return_value=contexts):
                        with mock.patch("omniflow.cli.validate_dbt_sync_environment", return_value="main"):
                            with mock.patch("omniflow.cli.require_sync_api_key", return_value="sync-token"):
                                with mock.patch(
                                    "omniflow.cli._client_and_branch_for_context",
                                    side_effect=[(clients[0], None), (clients[1], None)],
                                ):
                                    with mock.patch(
                                        "omniflow.cli.run_dbt_sync",
                                        return_value=(refresh_report, 0),
                                    ) as refresh:
                                        exit_code = cmd_dbt_sync(args)
                self.assertEqual(exit_code, 0)
                refresh.assert_called_once()
                report = json.loads(Path(".omniflow/public/report.json").read_text(encoding="utf-8"))
                sync = json.loads(Path(".omniflow/public/dbt-sync.json").read_text(encoding="utf-8"))
                self.assertEqual(report["summary"]["models_requested"], 2)
                self.assertEqual(report["summary"]["refreshes_completed"], 1)
                self.assertEqual(len(report["model_reports"]), 2)
                self.assertEqual(sync["models"][0]["affected_model_ids"], ["model-1", "model-2"])
            finally:
                os.chdir(original)

    def test_cli_maps_refresh_and_post_validation_failures_to_documented_exit_codes(self):
        cases = (
            (OmniAPIError("refresh failed"), None, 4, "not_run"),
            (
                ({"status": "completed", "issues": [], "raw_query_results_stored": False}, 0),
                ({"issues": [{"validator": "model", "severity": "error", "message": "invalid"}]}, 1),
                1,
                "failed",
            ),
        )
        original = os.getcwd()
        for refresh_outcome, validation_outcome, expected_exit, validation_status in cases:
            with self.subTest(expected_exit=expected_exit):
                with tempfile.TemporaryDirectory() as tmp:
                    os.chdir(tmp)
                    try:
                        config = enabled_config(Path(".omniflow.yml"))
                        context = ModelContext(
                            base_url="https://omni.example",
                            model_id="model-1",
                            model_path="omni/model",
                            base_branch="main",
                        )
                        client = mock.Mock()
                        client.get_model_metadata.return_value = {
                            "model_id": "model-1",
                            "connection_id": "connection-1",
                        }
                        client.list_refresh_affected_model_ids.return_value = ["model-1"]
                        args = SimpleNamespace(
                            config=".omniflow.yml",
                            auto=True,
                            base_url=None,
                            model_id=None,
                            model_path=None,
                            branch_id=None,
                            branch_name=None,
                            base_branch=None,
                            refresh_mode=None,
                            user_id=None,
                            include_personal_folders=None,
                        )
                        refresh_patch = (
                            mock.patch("omniflow.cli.run_dbt_sync", side_effect=refresh_outcome)
                            if isinstance(refresh_outcome, Exception)
                            else mock.patch("omniflow.cli.run_dbt_sync", return_value=refresh_outcome)
                        )
                        with mock.patch("omniflow.cli.load_config", return_value=config):
                            with mock.patch("omniflow.cli.discover_deployment_contexts", return_value=[context]):
                                with mock.patch("omniflow.cli.validate_dbt_sync_environment", return_value="main"):
                                    with mock.patch("omniflow.cli.require_sync_api_key", return_value="sync-token"):
                                        with mock.patch(
                                            "omniflow.cli._client_and_branch_for_context",
                                            return_value=(client, None),
                                        ):
                                            with refresh_patch:
                                                with mock.patch(
                                                    "omniflow.cli._run_context",
                                                    return_value=validation_outcome,
                                                ) as validation:
                                                    exit_code = cmd_dbt_sync(args)
                        self.assertEqual(exit_code, expected_exit)
                        report = json.loads(Path(".omniflow/public/report.json").read_text(encoding="utf-8"))
                        self.assertEqual(report["exit_code"], expected_exit)
                        self.assertEqual(report["model_reports"][0]["post_sync_validation_status"], validation_status)
                        if expected_exit == 4:
                            validation.assert_not_called()
                    finally:
                        os.chdir(original)

    def test_dbt_sync_defaults_off_and_mixed_connection_branches_fail_before_refresh(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                self.assertEqual(main(["dbt", "sync", "--auto"]), 2)
                report = json.loads(Path(".omniflow/public/report.json").read_text(encoding="utf-8"))
                self.assertEqual(report["exit_code"], 2)
            finally:
                os.chdir(original)

        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                config = enabled_config(Path(".omniflow.yml"))
                contexts = [
                    ModelContext(
                        base_url="https://omni.example",
                        model_id=model_id,
                        model_path=f"omni/{model_id}",
                        base_branch="main",
                    )
                    for model_id in ("model-1", "model-2")
                ]
                clients = [mock.Mock(), mock.Mock()]
                for index, client in enumerate(clients):
                    client.get_model_metadata.return_value = {
                        "model_id": contexts[index].model_id,
                        "connection_id": "connection-1",
                    }
                    client.list_refresh_affected_model_ids.return_value = ["model-1", "model-2"]
                args = SimpleNamespace(
                    config=".omniflow.yml",
                    auto=True,
                    base_url=None,
                    model_id=None,
                    model_path=None,
                    branch_id=None,
                    branch_name=None,
                    base_branch=None,
                    refresh_mode=None,
                    user_id=None,
                    include_personal_folders=None,
                )
                with mock.patch("omniflow.cli.load_config", return_value=config):
                    with mock.patch("omniflow.cli.discover_deployment_contexts", return_value=contexts):
                        with mock.patch("omniflow.cli.validate_dbt_sync_environment", return_value="main"):
                            with mock.patch("omniflow.cli.require_sync_api_key", return_value="sync-token"):
                                with mock.patch(
                                    "omniflow.cli._client_and_branch_for_context",
                                    side_effect=[(clients[0], "branch-1"), (clients[1], "branch-2")],
                                ):
                                    with mock.patch("omniflow.cli.run_dbt_sync") as refresh:
                                        with self.assertRaises(ConfigError):
                                            cmd_dbt_sync(args)
                refresh.assert_not_called()
            finally:
                os.chdir(original)

    def test_unregistered_shared_model_blocks_connection_refresh(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                config = enabled_config(Path(".omniflow.yml"))
                context = ModelContext(
                    base_url="https://omni.example",
                    model_id="model-1",
                    model_path="omni/model-1",
                    base_branch="main",
                )
                client = mock.Mock()
                client.get_model_metadata.return_value = {
                    "model_id": "model-1",
                    "connection_id": "connection-1",
                }
                client.list_refresh_affected_model_ids.return_value = ["model-1", "model-2"]
                args = SimpleNamespace(
                    config=".omniflow.yml",
                    auto=True,
                    base_url=None,
                    model_id=None,
                    model_path=None,
                    branch_id=None,
                    branch_name=None,
                    base_branch=None,
                    refresh_mode=None,
                    user_id=None,
                    include_personal_folders=None,
                )
                with mock.patch("omniflow.cli.load_config", return_value=config):
                    with mock.patch("omniflow.cli.discover_deployment_contexts", return_value=[context]):
                        with mock.patch("omniflow.cli.validate_dbt_sync_environment", return_value="main"):
                            with mock.patch("omniflow.cli.require_sync_api_key", return_value="sync-token"):
                                with mock.patch(
                                    "omniflow.cli._client_and_branch_for_context",
                                    return_value=(client, None),
                                ):
                                    with mock.patch("omniflow.cli.run_dbt_sync") as refresh:
                                        with self.assertRaises(ConfigError):
                                            cmd_dbt_sync(args)
                refresh.assert_not_called()
                report = json.loads(Path(".omniflow/public/report.json").read_text(encoding="utf-8"))
                self.assertEqual(report["exit_code"], 2)
                self.assertIn("every shared model", report["issues"][0]["message"])
            finally:
                os.chdir(original)


if __name__ == "__main__":
    unittest.main()
