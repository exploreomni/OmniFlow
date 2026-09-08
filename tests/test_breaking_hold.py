import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from omniflow.breaking_hold import (
    PENDING_RULE,
    SAME_PR_RULE,
    STATE_RULE,
    VALIDATOR,
    evaluate_breaking_hold,
    hold_triggered,
)
from omniflow.cli import cmd_run
from omniflow.config import BreakingChangeHoldSettings, load_config
from omniflow.discovery import ModelContext
from omniflow.exceptions import ConfigError, SecurityPolicyError

BREAKING_DIFF = {
    "risk_level": "breaking",
    "changes": [
        {
            "type": "field_renamed",
            "file": "views/orders.view",
            "risk": "breaking",
            "message": "Renamed field may break content references.",
            "previous_field": "orders.customer_id",
            "field": "orders.customer_key",
        }
    ],
}
ADDITIVE_DIFF = {
    "risk_level": "info",
    "changes": [
        {
            "type": "field_added",
            "file": "views/orders.view",
            "risk": "info",
            "message": "Added field orders.discount_amount.",
            "field": "orders.discount_amount",
        }
    ],
}


def settings(**overrides) -> BreakingChangeHoldSettings:
    base = {
        "enabled": True,
        "action": "fail",
        "dbt_paths": ["models/", "seeds/"],
        "pending_label": "omniflow/awaiting-deploy",
    }
    base.update(overrides)
    return BreakingChangeHoldSettings(**base)


class SamePullRequestDetectionTests(unittest.TestCase):
    def test_breaking_change_with_dbt_file_in_same_pull_request_blocks(self):
        issues = evaluate_breaking_hold(
            diff_result=BREAKING_DIFF,
            changed_files=["models/marts/orders.sql", "omni/my_model/views/orders.view"],
            last_sync_sha=None,
            settings=settings(),
        )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["rule"], SAME_PR_RULE)
        self.assertEqual(issues[0]["severity"], "error")
        self.assertEqual(issues[0]["validator"], VALIDATOR)
        self.assertEqual(issues[0]["dbt_paths"], ["models/"])
        self.assertEqual(issues[0]["breaking_change_count"], 1)
        self.assertTrue(hold_triggered(issues))

    def test_additive_change_with_dbt_file_passes(self):
        issues = evaluate_breaking_hold(
            diff_result=ADDITIVE_DIFF,
            changed_files=["models/marts/orders.sql", "omni/my_model/views/orders.view"],
            last_sync_sha=None,
            settings=settings(),
        )
        self.assertEqual(issues, [])
        self.assertFalse(hold_triggered(issues))

    def test_breaking_change_without_sync_state_blocks(self):
        issues = evaluate_breaking_hold(
            diff_result=BREAKING_DIFF,
            changed_files=["omni/my_model/views/orders.view"],
            last_sync_sha=None,
            settings=settings(),
        )
        self.assertEqual(issues[0]["rule"], STATE_RULE)
        self.assertEqual(issues[0]["severity"], "error")

    def test_warn_action_reports_without_blocking_severity(self):
        issues = evaluate_breaking_hold(
            diff_result=BREAKING_DIFF,
            changed_files=["models/marts/orders.sql"],
            last_sync_sha=None,
            settings=settings(action="warn"),
        )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["severity"], "warning")

    def test_disabled_policy_never_fires(self):
        issues = evaluate_breaking_hold(
            diff_result=BREAKING_DIFF,
            changed_files=["models/marts/orders.sql"],
            last_sync_sha="a" * 40,
            settings=settings(enabled=False),
        )
        self.assertEqual(issues, [])

    def test_missing_diff_result_never_fires(self):
        self.assertEqual(
            evaluate_breaking_hold(
                diff_result=None,
                changed_files=["models/marts/orders.sql"],
                last_sync_sha=None,
                settings=settings(),
            ),
            [],
        )

    def test_unrelated_changed_files_do_not_match_dbt_paths(self):
        issues = evaluate_breaking_hold(
            diff_result=BREAKING_DIFF,
            changed_files=["hightouch/syncs/crm.yml", "docs/models/readme.md"],
            last_sync_sha=None,
            settings=settings(),
        )
        self.assertEqual(issues[0]["rule"], STATE_RULE)
        self.assertEqual(issues[0]["dbt_paths"], [])

    def test_dbt_path_match_requires_a_directory_boundary(self):
        issues = evaluate_breaking_hold(
            diff_result=BREAKING_DIFF,
            changed_files=["models_archive/orders.sql"],
            last_sync_sha=None,
            settings=settings(),
        )
        self.assertEqual(issues[0]["rule"], STATE_RULE)
        self.assertEqual(issues[0]["dbt_paths"], [])

    def test_sample_changes_are_bounded(self):
        many_changes = {
            "risk_level": "breaking",
            "changes": [
                {"type": "field_deleted", "file": f"views/v{index}.view", "risk": "breaking"}
                for index in range(25)
            ],
        }
        issues = evaluate_breaking_hold(
            diff_result=many_changes,
            changed_files=["models/marts/orders.sql"],
            last_sync_sha=None,
            settings=settings(),
        )
        self.assertEqual(issues[0]["breaking_change_count"], 25)
        self.assertEqual(len(issues[0]["breaking_changes"]), 10)


class PendingDeploymentDetectionTests(unittest.TestCase):
    def test_stale_sync_sha_with_pending_dbt_changes_blocks(self):
        with mock.patch(
            "omniflow.breaking_hold._git_changed_files_since",
            return_value=["models/marts/orders.sql", "README.md"],
        ):
            issues = evaluate_breaking_hold(
                diff_result=BREAKING_DIFF,
                changed_files=["omni/my_model/views/orders.view"],
                last_sync_sha="abc1234",
                settings=settings(),
            )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["rule"], PENDING_RULE)
        self.assertEqual(issues[0]["severity"], "error")
        self.assertEqual(issues[0]["dbt_paths"], ["models/"])
        self.assertEqual(issues[0]["last_sync_sha"], "abc1234")

    def test_current_sync_sha_without_pending_dbt_changes_passes(self):
        with mock.patch("omniflow.breaking_hold._git_changed_files_since", return_value=["README.md"]):
            issues = evaluate_breaking_hold(
                diff_result=BREAKING_DIFF,
                changed_files=["omni/my_model/views/orders.view"],
                last_sync_sha="abc1234",
                settings=settings(),
            )
        self.assertEqual(issues, [])

    def test_unreachable_sync_commit_blocks(self):
        """Incomplete history cannot establish readiness for a breaking change."""
        with mock.patch("omniflow.breaking_hold._git_changed_files_since", return_value=None):
            issues = evaluate_breaking_hold(
                diff_result=BREAKING_DIFF,
                changed_files=["omni/my_model/views/orders.view"],
                last_sync_sha="abc1234",
                settings=settings(),
            )
        self.assertEqual(issues[0]["rule"], STATE_RULE)
        self.assertEqual(issues[0]["severity"], "error")

    def test_same_pull_request_detection_takes_precedence(self):
        with mock.patch(
            "omniflow.breaking_hold._git_changed_files_since",
            return_value=["models/marts/orders.sql"],
        ) as git_lookup:
            issues = evaluate_breaking_hold(
                diff_result=BREAKING_DIFF,
                changed_files=["models/marts/orders.sql", "omni/my_model/views/orders.view"],
                last_sync_sha="abc1234",
                settings=settings(),
            )
        self.assertEqual(issues[0]["rule"], SAME_PR_RULE)
        git_lookup.assert_not_called()

    def test_malformed_sync_sha_is_rejected(self):
        for value in ("not-a-sha", "abc", "$(whoami)", "a" * 65, "abc123; rm -rf /"):
            with self.subTest(value=value), self.assertRaises(SecurityPolicyError):
                evaluate_breaking_hold(
                    diff_result=BREAKING_DIFF,
                    changed_files=["omni/my_model/views/orders.view"],
                    last_sync_sha=value,
                    settings=settings(),
                )

    def test_blank_sync_sha_is_treated_as_unset(self):
        issues = evaluate_breaking_hold(
            diff_result=BREAKING_DIFF,
            changed_files=["omni/my_model/views/orders.view"],
            last_sync_sha="   ",
            settings=settings(),
        )
        self.assertEqual(issues[0]["rule"], STATE_RULE)


class GitLookupTests(unittest.TestCase):
    def test_unreachable_commit_returns_none(self):
        from omniflow.breaking_hold import _git_changed_files_since

        with mock.patch(
            "omniflow.breaking_hold.subprocess.run",
            side_effect=subprocess.CalledProcessError(128, "git"),
        ):
            self.assertIsNone(_git_changed_files_since("abc1234"))

    def test_traversal_paths_are_discarded(self):
        from omniflow.breaking_hold import _git_changed_files_since

        completed = mock.Mock(stdout="models/a.sql\n../escape.sql\n/etc/passwd\nmodels/a.sql\n")
        with mock.patch("omniflow.breaking_hold.subprocess.run", return_value=completed):
            self.assertEqual(_git_changed_files_since("abc1234"), ["models/a.sql"])

    def test_git_is_invoked_without_a_shell(self):
        from omniflow.breaking_hold import _git_changed_files_since

        completed = mock.Mock(stdout="")
        with mock.patch("omniflow.breaking_hold.subprocess.run", return_value=completed) as run:
            _git_changed_files_since("abc1234")
        args, kwargs = run.call_args
        self.assertIsInstance(args[0], list)
        self.assertNotIn("shell", kwargs)
        self.assertEqual(args[0][-1], "abc1234..HEAD")
        self.assertIn("--is-ancestor", run.call_args_list[0].args[0])


class HoldConfigTests(unittest.TestCase):
    def test_defaults_are_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / ".omniflow.yml"
            config_path.write_text("checks:\n  model_validation:\n    enabled: true\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                config = load_config(config_path)
        self.assertFalse(config.breaking_change_hold.enabled)
        self.assertEqual(config.breaking_change_hold.action, "fail")
        self.assertEqual(config.breaking_change_hold.pending_label, "omniflow/awaiting-deploy")
        self.assertEqual(
            config.breaking_change_hold.dbt_paths,
            ["models/", "seeds/", "snapshots/", "macros/"],
        )

    def test_policy_is_parsed_from_deployment_section(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / ".omniflow.yml"
            config_path.write_text(
                "deployment:\n"
                "  breaking_change_hold:\n"
                "    enabled: true\n"
                "    action: warn\n"
                "    dbt_paths:\n"
                "      - transform/models\n"
                "      - transform/seeds\n"
                "    pending_label: needs-deploy\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                config = load_config(config_path)
        self.assertTrue(config.breaking_change_hold.enabled)
        self.assertEqual(config.breaking_change_hold.action, "warn")
        self.assertEqual(config.breaking_change_hold.dbt_paths, ["transform/models", "transform/seeds"])
        self.assertEqual(config.breaking_change_hold.pending_label, "needs-deploy")

    def test_invalid_values_are_rejected(self):
        cases = {
            "unknown key": "deployment:\n  breaking_change_hold:\n    unexpected: true\n",
            "bad action": "deployment:\n  breaking_change_hold:\n    action: block\n",
            "absolute path": "deployment:\n  breaking_change_hold:\n    dbt_paths:\n      - /etc\n",
            "traversal path": "deployment:\n  breaking_change_hold:\n    dbt_paths:\n      - ../outside\n",
            "empty paths": "deployment:\n  breaking_change_hold:\n    dbt_paths: []\n",
            "paths not a list": "deployment:\n  breaking_change_hold:\n    dbt_paths: models/\n",
            "label with comma": "deployment:\n  breaking_change_hold:\n    pending_label: 'a,b'\n",
        }
        for name, body in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                config_path = Path(directory) / ".omniflow.yml"
                config_path.write_text(body, encoding="utf-8")
                with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(ConfigError):
                    load_config(config_path)

    def test_excess_dbt_paths_are_rejected(self):
        entries = "".join(f"      - path{index}\n" for index in range(51))
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / ".omniflow.yml"
            config_path.write_text(
                f"deployment:\n  breaking_change_hold:\n    dbt_paths:\n{entries}",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(SecurityPolicyError):
                load_config(config_path)

    def test_duplicate_dbt_paths_are_collapsed(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / ".omniflow.yml"
            config_path.write_text(
                "deployment:\n"
                "  breaking_change_hold:\n"
                "    dbt_paths:\n"
                "      - models\n"
                "      - models/\n"
                "      - 'models  '\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                config = load_config(config_path)
        self.assertEqual(config.breaking_change_hold.dbt_paths, ["models"])


class HoldOrchestrationTests(unittest.TestCase):
    """End-to-end coverage of the hold policy inside the run command."""

    def _args(self):
        return SimpleNamespace(
            config=".omniflow.yml",
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

    def _write_policy(self, *, enabled=True, action="fail"):
        Path(".omniflow.yml").write_text(
            "checks:\n"
            "  content_validation:\n"
            "    enabled: false\n"
            "  model_validation:\n"
            "    enabled: false\n"
            "  semantic_lint:\n"
            "    enabled: false\n"
            "contracts:\n"
            "  enabled: false\n"
            "deployment:\n"
            "  breaking_change_hold:\n"
            f"    enabled: {'true' if enabled else 'false'}\n"
            f"    action: {action}\n"
            "    dbt_paths:\n"
            "      - models\n",
            encoding="utf-8",
        )

    def _run(self, *, changed_files, hold_issues, enabled=True, action="fail", github_output=None):
        context = ModelContext(
            base_url="https://omni.example",
            model_id="model-1",
            model_path="omni/model-1",
        )
        env = {"GITHUB_ACTIONS": "true"}
        if github_output:
            env["GITHUB_OUTPUT"] = github_output
        with mock.patch.dict(os.environ, env, clear=True):
            self._write_policy(enabled=enabled, action=action)
            with mock.patch("omniflow.cli.discover_contexts", return_value=[context]):
                with mock.patch("omniflow.cli.get_changed_files", return_value=changed_files) as changed:
                    with mock.patch(
                        "omniflow.cli.evaluate_breaking_hold", return_value=hold_issues
                    ) as evaluate:
                        with mock.patch(
                            "omniflow.cli._client_and_branch_for_context",
                            return_value=(mock.Mock(), None),
                        ):
                            # An enabled hold requires a semantic diff, so the YAML
                            # pull and diff are stubbed for these orchestration tests.
                            with mock.patch("omniflow.cli.pull_yaml"):
                                with mock.patch(
                                    "omniflow.cli.diff_graphs",
                                    return_value={"risk_level": "info", "changes": []},
                                ):
                                    exit_code = cmd_run(self._args())
        return exit_code, changed, evaluate

    def test_hold_failure_blocks_the_run_and_records_the_issue(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                hold_issue = {
                    "validator": VALIDATOR,
                    "rule": SAME_PR_RULE,
                    "severity": "error",
                    "message": "Split the change.",
                }
                exit_code, _, evaluate = self._run(
                    changed_files=["models/orders.sql"],
                    hold_issues=[hold_issue],
                )
                self.assertEqual(exit_code, 1)
                evaluate.assert_called_once()
                self.assertEqual(evaluate.call_args.kwargs["changed_files"], ["models/orders.sql"])
                report = json.loads(Path(".omniflow/public/report.json").read_text(encoding="utf-8"))
                self.assertEqual(report["policy_decision"], "fail")
                self.assertTrue(
                    any(issue.get("validator") == VALIDATOR for issue in report["issues"]),
                    msg="hold issue must reach the public reviewer summary",
                )
            finally:
                os.chdir(original)

    def test_warn_action_does_not_change_the_exit_code(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                hold_issue = {
                    "validator": VALIDATOR,
                    "rule": SAME_PR_RULE,
                    "severity": "warning",
                    "message": "Split the change.",
                }
                exit_code, _, _ = self._run(
                    changed_files=["models/orders.sql"],
                    hold_issues=[hold_issue],
                    action="warn",
                )
                self.assertEqual(exit_code, 0)
            finally:
                os.chdir(original)

    def test_disabled_policy_skips_changed_file_discovery(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                exit_code, changed, evaluate = self._run(
                    changed_files=["models/orders.sql"],
                    hold_issues=[],
                    enabled=False,
                )
                self.assertEqual(exit_code, 0)
                changed.assert_not_called()
                evaluate.assert_not_called()
            finally:
                os.chdir(original)

    def test_hold_decision_is_published_for_the_workflow(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                output_file = Path(tmp) / "github_output"
                output_file.write_text("", encoding="utf-8")
                hold_issue = {
                    "validator": VALIDATOR,
                    "rule": SAME_PR_RULE,
                    "severity": "error",
                    "message": "Split the change.",
                }
                self._run(
                    changed_files=["models/orders.sql"],
                    hold_issues=[hold_issue],
                    github_output=str(output_file),
                )
                published = output_file.read_text(encoding="utf-8")
                self.assertIn("hold_triggered=true", published)
                self.assertIn("hold_pending_label=omniflow/awaiting-deploy", published)
            finally:
                os.chdir(original)

    def test_passing_run_publishes_a_cleared_hold_decision(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                output_file = Path(tmp) / "github_output"
                output_file.write_text("", encoding="utf-8")
                self._run(
                    changed_files=["models/orders.sql"],
                    hold_issues=[],
                    github_output=str(output_file),
                )
                published = output_file.read_text(encoding="utf-8")
                self.assertIn("hold_triggered=false", published)
            finally:
                os.chdir(original)

    def test_hold_receives_a_diff_when_lint_and_contracts_are_disabled(self):
        """The hold reads the semantic diff, so it must be able to request one.

        Regression guard: the diff used to be computed only for semantic lint or
        contracts, which silently disabled the policy for a repository that
        turned both off.
        """
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                context = ModelContext(
                    base_url="https://omni.example",
                    model_id="model-1",
                    model_path="omni/model-1",
                )
                # Policy written by _write_policy disables lint and contracts.
                self._write_policy()
                observed = {}

                def capture(*, diff_result, changed_files, last_sync_sha, settings):
                    observed["diff_result"] = diff_result
                    return []

                with mock.patch.dict(os.environ, {}, clear=True):
                    with mock.patch("omniflow.cli.discover_contexts", return_value=[context]):
                        with mock.patch("omniflow.cli.get_changed_files", return_value=["models/o.sql"]):
                            with mock.patch("omniflow.cli.evaluate_breaking_hold", side_effect=capture):
                                with mock.patch(
                                    "omniflow.cli._client_and_branch_for_context",
                                    return_value=(mock.Mock(), None),
                                ):
                                    with mock.patch("omniflow.cli.pull_yaml") as pull:
                                        with mock.patch(
                                            "omniflow.cli.diff_graphs",
                                            return_value={"risk_level": "info", "changes": []},
                                        ):
                                            cmd_run(self._args())
                self.assertIsNotNone(
                    observed.get("diff_result"),
                    msg="hold must receive a computed diff even with lint and contracts disabled",
                )
                self.assertTrue(pull.called, msg="a diff requires YAML to be pulled")
            finally:
                os.chdir(original)


if __name__ == "__main__":
    unittest.main()
