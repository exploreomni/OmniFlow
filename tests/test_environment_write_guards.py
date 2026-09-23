import os
import unittest
from types import SimpleNamespace
from unittest import mock

from omniflow.dbt_sync import validate_dbt_sync_environment
from omniflow.discovery import ModelContext
from omniflow.exceptions import SecurityPolicyError
from omniflow.github.revalidation import _readiness_route, revalidate
from omniflow.repair.orchestrator import run_ai_repair


def target(environment=None, base_branch="main"):
    context = ModelContext(
        base_url="https://omni.example",
        model_id="model-1",
        model_path="omni/model",
        branch_name="release/candidate",
        branch_id="branch-1",
        base_branch=base_branch,
    )
    context.environment = environment
    return context


class EnvironmentWriteGuardTests(unittest.TestCase):
    def test_dbt_sync_rejects_each_environment_before_branch_or_refresh_work(self):
        for environment, branch in (("development", "develop"), ("production", "main")):
            with self.subTest(environment=environment), \
                 mock.patch("omniflow.dbt_sync.current_branch") as current_branch, \
                 self.assertRaisesRegex(SecurityPolicyError, "validation-only"):
                validate_dbt_sync_environment([target(environment, branch)])
            current_branch.assert_not_called()

    def test_dbt_sync_rejects_whole_mixed_batch_before_work(self):
        with mock.patch("omniflow.dbt_sync.current_branch") as current_branch, \
             self.assertRaisesRegex(SecurityPolicyError, "per-environment deployment state"):
            validate_dbt_sync_environment([target(), target("production")])
        current_branch.assert_not_called()

    def test_legacy_dbt_sync_still_accepts_matching_protected_branch(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("omniflow.dbt_sync.current_branch", return_value="main"):
            self.assertEqual(validate_dbt_sync_environment([target()]), "main")

    def test_ai_repair_rejects_environment_before_policy_client_or_attempt_guard(self):
        client, guard, validation = mock.Mock(), mock.Mock(), mock.Mock()
        with mock.patch("omniflow.repair.orchestrator.validate_ai_repair_policy") as policy, \
             self.assertRaisesRegex(SecurityPolicyError, "validation-only"):
            run_ai_repair(
                config=mock.Mock(), context=target("production"), event=mock.Mock(),
                client=client, guard=guard, validation_runner=validation,
            )
        policy.assert_not_called()
        self.assertEqual(client.mock_calls, [])
        self.assertEqual(guard.mock_calls, [])
        validation.assert_not_called()

    def test_legacy_ai_repair_retains_noop_validation(self):
        client, guard = mock.Mock(), mock.Mock()
        client.validate_model.return_value = []
        with mock.patch("omniflow.repair.orchestrator.validate_ai_repair_policy") as policy, \
             mock.patch("omniflow.repair.orchestrator._repair_report", return_value={"status": "not_needed"}):
            outcome = run_ai_repair(
                config=mock.Mock(), context=target(),
                event=SimpleNamespace(head_branch="release/candidate", base_branch="main"),
                client=client, guard=guard, validation_runner=mock.Mock(),
            )
        self.assertEqual(outcome.exit_code, 0)
        policy.assert_called_once()
        client.validate_model.assert_called_once_with("model-1", branch_id="branch-1")
        self.assertEqual(guard.mock_calls, [])

    def test_readiness_rejects_environment_before_check_write_or_legacy_sync_read(self):
        env = {
            "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
            "GITHUB_REF_NAME": "main", "OMNIFLOW_LAST_SYNC_SHA": "a" * 40,
        }
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("omniflow.github.revalidation.load_flow_metadata", return_value={"version": 2}) as flow, \
             mock.patch("omniflow.github.revalidation.GitHubRepository") as github, \
             mock.patch("omniflow.github.revalidation.load_config") as config, \
             self.assertRaisesRegex(SecurityPolicyError, "OMNIFLOW_LAST_SYNC_SHA must not authorize"):
            revalidate(25)
        flow.assert_called_once_with(missing_ok=True)
        github.assert_not_called()
        config.assert_not_called()

    def test_readiness_route_rejects_environment_even_when_no_model_files_changed(self):
        for paths in ([], ["README.md"], ["omni/model/orders.view"]):
            with self.subTest(paths=paths), \
                 mock.patch("omniflow.github.revalidation.load_flow_metadata", return_value={"version": 2}), \
                 mock.patch("omniflow.github.revalidation.discover_contexts") as discover, \
                 self.assertRaisesRegex(SecurityPolicyError, "hold release"):
                _readiness_route(mock.Mock(), paths)
            discover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
