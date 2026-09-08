import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from omniflow.exceptions import ConfigError
from omniflow.github.revalidation import CHECK_NAME, revalidate

HEAD = "a" * 40
BASE = "b" * 40
SYNC = "c" * 40
ROOT = Path(__file__).resolve().parents[1]


class RevalidationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.output = Path(self.directory.name)
        (self.output / "public").mkdir()
        self.api = mock.Mock()
        self.state = mock.Mock()
        self.pr = {"number": 4, "state": "open", "draft": False,
                   "head": {"sha": HEAD, "ref": "feature", "repo": {"full_name": "owner/repo"}},
                   "base": {"sha": BASE, "ref": "main", "repo": {"full_name": "owner/repo"}},
                   "labels": [{"name": "custom/awaiting-warehouse"}]}
        self.api.pull_request.return_value = self.pr
        self.api.request.return_value = {"id": 27}
        self.state.sync_sha.return_value = SYNC
        self.config = SimpleNamespace(
            breaking_change_hold=SimpleNamespace(enabled=True, action="fail", pending_label="custom/awaiting-warehouse"),
            reporting=SimpleNamespace(output_dir=str(self.output)),
        )

    def tearDown(self):
        self.directory.cleanup()

    def run_check(self, *, code=0, report=None, base=BASE):
        def validate(argv):
            self.assertEqual(os.environ["GITHUB_EVENT_NAME"], "pull_request_target")
            event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
            self.assertEqual(event["pull_request"]["head"]["sha"], HEAD)
            self.assertEqual(os.environ["OMNIFLOW_LAST_SYNC_SHA"], SYNC)
            payload = report if report is not None else {
                "policy_decision": "pass", "models": [{"model_id": "m1"}], "git_sha": HEAD,
            }
            (self.output / "public/report.json").write_text(json.dumps(payload))
            return code

        with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
                                          "GITHUB_REF_NAME": "main", "GITHUB_REPOSITORY": "owner/repo",
                                          "OMNIFLOW_GITHUB_TOKEN": "workflow-token",
                                          "OMNIFLOW_SYNC_STATE_TOKEN": "state-token"}, clear=True), \
             mock.patch("omniflow.github.revalidation.GitHubRepository", side_effect=[self.api, self.state]), \
             mock.patch("omniflow.github.revalidation.git_value", return_value=base), \
             mock.patch("omniflow.github.revalidation.load_config", return_value=self.config), \
             mock.patch("omniflow.cli.main", side_effect=validate):
            return revalidate(4)

    def test_success_sets_check_on_exact_head_and_removes_configured_label_without_merging(self):
        self.assertEqual(self.run_check(), 0)
        calls = self.api.request.call_args_list
        self.assertEqual(calls[0].args[2]["head_sha"], HEAD)
        self.assertEqual(calls[0].args[2]["name"], CHECK_NAME)
        self.assertIn(mock.call("DELETE", "/issues/4/labels/custom%2Fawaiting-warehouse"), calls)
        self.assertEqual(calls[-1].args[2]["conclusion"], "success")
        self.assertFalse(any("merge" in call.args[1] for call in calls))
        self.assertEqual(self.state.sync_sha.call_count, 3)

    def test_changed_head_never_releases_label(self):
        changed = copy.deepcopy(self.pr)
        changed["head"]["sha"] = "d" * 40
        self.api.pull_request.side_effect = [self.pr, changed]
        with self.assertRaisesRegex(ConfigError, "head or base changed"):
            self.run_check()
        self.assertEqual(self.api.request.call_args.args[2]["conclusion"], "failure")
        self.assertFalse(any(call.args[0] == "DELETE" for call in self.api.request.call_args_list))

    def test_changed_sync_state_never_releases_label(self):
        self.state.sync_sha.side_effect = [SYNC, "d" * 40]
        with self.assertRaisesRegex(ConfigError, "Deployment state changed"):
            self.run_check()
        self.assertFalse(any(call.args[0] == "DELETE" for call in self.api.request.call_args_list))

    def test_missing_state_publishes_failure_on_current_head(self):
        self.state.sync_sha.side_effect = ConfigError("state missing")
        with self.assertRaisesRegex(ConfigError, "state missing"):
            self.run_check()
        self.assertEqual(self.api.request.call_args.args[2]["conclusion"], "failure")

    def test_stale_base_checkout_publishes_failure(self):
        with self.assertRaisesRegex(ConfigError, "Base checkout is stale"):
            self.run_check(base="f" * 40)
        self.assertEqual(self.api.request.call_args.args[2]["conclusion"], "failure")

    def test_validation_failure_skip_and_wrong_sha_are_not_ready(self):
        for code, report in ((1, None), (0, {"policy_decision": "skipped"}),
                             (0, {"policy_decision": "pass", "models": [{}], "git_sha": "f" * 40})):
            with self.subTest(code=code, report=report), self.assertRaises(ConfigError):
                self.run_check(code=code, report=report)
        self.assertFalse(any(call.args[0] == "DELETE" for call in self.api.request.call_args_list))

    def test_label_write_failure_keeps_readiness_failed(self):
        def request(method, path, body=None):
            if method == "DELETE":
                raise ConfigError("label write failed")
            return {"id": 27}
        self.api.request.side_effect = request
        with self.assertRaisesRegex(ConfigError, "label write failed"):
            self.run_check()
        self.assertEqual(self.api.request.call_args.args[2]["conclusion"], "failure")

    def test_workflow_durable_write_must_succeed_and_reread_before_revalidation(self):
        path = ROOT / ".github/workflow-examples/omniflow-dbt-sync-with-release.yml"
        text = path.read_text()
        steps = yaml.safe_load(text)["jobs"]["deploy-and-sync"]["steps"]
        record = next(step for step in steps if step["name"] == "Record synchronized commit")
        self.assertIn("exit 1", record["run"])
        self.assertIn('test "$RECORDED_SHA" = "$SYNCED_SHA"', record["run"])
        self.assertNotIn("--auto", text)
        self.assertNotIn("--remove-label", text)
