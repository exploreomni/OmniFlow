import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from omniflow.cli import cmd_run
from omniflow.config import load_config
from omniflow.discovery import ModelContext
from omniflow.exceptions import ConfigError, OmniAPIError, SecurityPolicyError
from omniflow.validators.ai_eval import accuracy, build_comparison, run_ai_eval_validation


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class FakeEvalClient:
    def __init__(self, runs_by_id):
        self.runs_by_id = runs_by_id
        self.started = []
        self.polled = []
        self.cancelled = []
        self._next_id = 0

    def get_ai_eval_prompt_set(self, prompt_set_id):
        return {
            "id": prompt_set_id,
            "model_id": "model-1",
            "is_archived": False,
            "prompts": [
                {"id": f"prompt-{i}", "prompt_text": row["prompt"]}
                for i, row in enumerate(self.runs_by_id.get("main", {}).get("results", []))
            ],
        }

    def start_ai_eval_run(self, *, prompt_set_id, description, branch_id=None):
        self._next_id += 1
        run_id = f"run-{self._next_id}"
        self.started.append({"prompt_set_id": prompt_set_id, "description": description, "branch_id": branch_id})
        # Deterministically map the Nth start call to "main" then "branch" run fixtures.
        key = "main" if branch_id is None else "branch"
        self.runs_by_id[run_id] = {
            "id": run_id,
            "model_id": "model-1",
            "branch_id": branch_id,
            "prompt_set_id": prompt_set_id,
            **self.runs_by_id.pop(key),
        }
        return run_id

    def get_ai_eval_run(self, run_id):
        self.polled.append(run_id)
        return self.runs_by_id[run_id]

    def cancel_ai_eval_run(self, run_id):
        self.cancelled.append(run_id)
        self.runs_by_id[run_id]["status"] = "CANCELLED"
        return self.runs_by_id[run_id]


def result(prompt, score, **overrides):
    return {"prompt": prompt, "score": score, "agentic_job": {"state": "COMPLETE"}, **overrides}


class ComparisonTests(unittest.TestCase):
    def test_accuracy_ignores_unscored_results(self):
        results = [result("a", 1), result("b", 0), result("c", None)]
        acc, passed, total = accuracy(results)
        self.assertEqual((passed, total), (1, 2))
        self.assertAlmostEqual(acc, 0.5)

    def test_accuracy_with_no_scored_results_is_none(self):
        self.assertEqual(accuracy([result("a", None)]), (None, 0, 0))

    def test_build_comparison_classifies_every_status(self):
        main_run = {"results": [result("regressed", 1), result("improved", 0), result("same", 1), result("gone", 1)]}
        branch_run = {
            "results": [
                result("regressed", 0, error_reason="incomplete"),
                result("improved", 1),
                result("same", 1),
                result("new", 1),
            ]
        }
        rows = {row["prompt"]: row for row in build_comparison(main_run, branch_run)}
        self.assertEqual(rows["regressed"]["status"], "regressed")
        self.assertEqual(rows["regressed"]["branch_error"], "incomplete")
        self.assertEqual(rows["improved"]["status"], "improved")
        self.assertEqual(rows["same"]["status"], "unchanged")
        self.assertEqual(rows["gone"]["status"], "unscored")
        self.assertEqual(rows["new"]["status"], "unscored")


class RunAiEvalValidationTests(unittest.TestCase):
    def test_no_branch_skips_without_calling_the_client(self):
        report, detail, exit_code = run_ai_eval_validation(
            client=FakeEvalClient({}),
            model_id="model-1",
            branch_id=None,
            prompt_sets=[{"id": "set-1", "label": "Set 1"}],
            fail_on_regression=True,
            poll_interval_seconds=1,
            timeout_seconds=10,
            scoring_grace_seconds=1,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["prompt_set_summaries"], [])
        self.assertEqual(detail["prompt_set_results"], [])

    def test_regression_fails_the_check_and_bounds_public_detail(self):
        client = FakeEvalClient(
            {
                "main": {"status": "COMPLETE", "results": [result("q1", 1), result("q2", 1)]},
                "branch": {
                    "status": "COMPLETE",
                    "results": [
                        result("q1", 0),
                        result("q2", 1),
                    ],
                },
            }
        )
        clock = FakeClock()
        report, detail, exit_code = run_ai_eval_validation(
            client=client,
            model_id="model-1",
            branch_id="branch-1",
            prompt_sets=[{"id": "set-1", "label": "Core"}],
            fail_on_regression=True,
            poll_interval_seconds=5,
            timeout_seconds=60,
            scoring_grace_seconds=5,
            max_samples=1,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(len(report["issues"]), 1)
        issue = report["issues"][0]
        self.assertEqual(issue["severity"], "error")
        self.assertEqual(len(issue["prompt_ids"]), 1)
        self.assertNotIn("q1", json.dumps(report))
        self.assertNotIn("branch_conversation_id", issue)
        summary = report["prompt_set_summaries"][0]
        self.assertEqual(summary["regressed_count"], 1)
        self.assertAlmostEqual(summary["accuracy_delta_pts"], -50.0)
        self.assertEqual(client.started[1]["branch_id"], "branch-1")
        # Detail carries full per-prompt rows that never reach the public report.
        self.assertEqual(len(detail["prompt_set_results"][0]["rows"]), 2)

    def test_regression_reports_as_warning_when_not_gating(self):
        client = FakeEvalClient(
            {
                "main": {"status": "COMPLETE", "results": [result("q1", 1)]},
                "branch": {"status": "COMPLETE", "results": [result("q1", 0)]},
            }
        )
        report, _detail, exit_code = run_ai_eval_validation(
            client=client,
            model_id="model-1",
            branch_id="branch-1",
            prompt_sets=[{"id": "set-1", "label": "Core"}],
            fail_on_regression=False,
            poll_interval_seconds=1,
            timeout_seconds=10,
            scoring_grace_seconds=1,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["issues"][0]["severity"], "warning")

    def test_scoring_grace_gives_up_on_stragglers_without_hanging(self):
        client = FakeEvalClient(
            {
                "main": {"status": "COMPLETE", "results": [result("q1", None)]},
                "branch": {"status": "COMPLETE", "results": [result("q1", 1)]},
            }
        )
        clock = FakeClock()
        report, _detail, exit_code = run_ai_eval_validation(
            client=client,
            model_id="model-1",
            branch_id="branch-1",
            prompt_sets=[{"id": "set-1", "label": "Core"}],
            fail_on_regression=True,
            poll_interval_seconds=2,
            timeout_seconds=60,
            scoring_grace_seconds=3,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        self.assertEqual(exit_code, 4)
        self.assertGreaterEqual(clock.value, 3)

    def test_timeout_fails_and_cancels_known_runs(self):
        client = FakeEvalClient(
            {
                "main": {"status": "RUNNING", "results": [result("q1", None)]},
                "branch": {"status": "RUNNING", "results": [result("q1", None)]},
            }
        )
        clock = FakeClock()
        report, detail, exit_code = run_ai_eval_validation(
            client=client,
            model_id="model-1",
            branch_id="branch-1",
            prompt_sets=[{"id": "set-1", "label": "Core"}],
            fail_on_regression=True,
            poll_interval_seconds=5,
            timeout_seconds=10,
            scoring_grace_seconds=1,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        self.assertEqual(exit_code, 4)
        self.assertEqual(report["issues"][0]["reason"], "timeout")
        self.assertEqual(client.cancelled, ["run-1", "run-2"])
        self.assertFalse(detail["lifecycle"]["cleanup_required"])


class AiEvalCliTests(unittest.TestCase):
    """Exercise the actual model orchestration and every final public artifact."""

    def run_cli(self, client, *, max_samples=20, fail_on_regression=True, contexts=None, prompt_sets=None, retain=True):
        config = load_config(None)
        for check in (config.content_validation, config.model_validation, config.semantic_lint, config.contracts):
            check.enabled = False
        config.ai_eval.enabled = True
        config.ai_eval.prompt_sets = prompt_sets or [{"id": "set-1", "label": "CONFIDENTIAL-LABEL"}]
        config.ai_eval.fail_on_regression = fail_on_regression
        config.ai_eval.scoring_grace_seconds = 0
        config.ai_eval.timeout_seconds = 2
        config.ai_eval.poll_interval_seconds = 1
        config.security.max_report_samples = max_samples
        config.security.redaction_level = "strict"
        config.security.retain_restricted_artifacts = retain
        contexts = contexts or [
            ModelContext(
                base_url="https://omni.example", model_id="model-1", model_path="omni/model-1", branch_id="branch-1"
            )
        ]
        clock = FakeClock()

        def evaluate(**kwargs):
            return run_ai_eval_validation(**kwargs, sleep=clock.sleep, monotonic=clock.monotonic)

        def client_for(*args, **kwargs):
            context = args[0]
            return client, context.branch_id

        original = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                with (
                    mock.patch("omniflow.cli.load_config", return_value=config),
                    mock.patch("omniflow.cli.discover_contexts", return_value=contexts),
                    mock.patch("omniflow.cli._client_and_branch_for_context", side_effect=client_for),
                    mock.patch("omniflow.cli.run_ai_eval_validation", side_effect=evaluate),
                    mock.patch("builtins.print"),
                ):
                    exit_code = cmd_run(SimpleNamespace(config=None, auto=True, skip_reason=None))
                files = {
                    str(path.relative_to(".omniflow")): path.read_text()
                    for path in Path(".omniflow").rglob("*")
                    if path.is_file()
                }
                report = json.loads(files["public/report.json"])
                for path, body in files.items():
                    if not path.startswith("restricted/"):
                        self.assertNotIn("CONFIDENTIAL", body, path)
                for name in ("report.json", "report.md", "report.sarif", "junit.xml"):
                    self.assertIn(f"public/{name}", files)
                return exit_code, report, files
            finally:
                os.chdir(original)

    def make_client(self, branch, main=None):
        return FakeEvalClient(
            {
                "main": main or {"status": "COMPLETE", "results": [result("CONFIDENTIAL-PROMPT", 1)]},
                "branch": branch,
            }
        )

    def test_zero_samples_keeps_regression_gate_and_private_detail(self):
        client = self.make_client({"status": "COMPLETE", "results": [result("CONFIDENTIAL-PROMPT", 0)]})
        original_get = client.get_ai_eval_run
        observed_journals = []

        def get_with_journal(run_id):
            journal = json.loads(Path(".omniflow/restricted/model-1/ai-eval-runs.json").read_text())
            self.assertIn(run_id, [entry["run_id"] for entry in journal["owned_runs"]])
            observed_journals.append(journal)
            return original_get(run_id)

        client.get_ai_eval_run = get_with_journal
        exit_code, report, files = self.run_cli(client, max_samples=0)
        self.assertEqual(exit_code, 1)
        check = report["model_reports"][0]["check_reports"][0]
        self.assertEqual(check["regressed_count"], 1)
        self.assertEqual(check["issues"][0]["prompt_ids"], [])
        self.assertTrue(observed_journals)
        self.assertIn("CONFIDENTIAL-PROMPT", files["restricted/model-1/ai-eval-detail.json"])

    def test_operational_failures_cannot_be_downgraded_to_warning(self):
        cases = {
            "cancelled": {"status": "CANCELLED", "results": []},
            "failed": {"status": "FAILED", "results": []},
            "missing_prompt": {"status": "COMPLETE", "results": []},
            "missing_scores": {"status": "COMPLETE", "results": [result("CONFIDENTIAL-PROMPT", None)]},
            "error": {
                "status": "COMPLETE",
                "results": [result("CONFIDENTIAL-PROMPT", 0, error_reason="CONFIDENTIAL-ERROR")],
            },
            "failed_job": {
                "status": "COMPLETE",
                "results": [result("CONFIDENTIAL-PROMPT", 0, agentic_job={"state": "FAILED"})],
            },
            "fraction": {"status": "COMPLETE", "results": [result("CONFIDENTIAL-PROMPT", 0.9)]},
            "boolean": {"status": "COMPLETE", "results": [result("CONFIDENTIAL-PROMPT", True)]},
            "nan": {"status": "COMPLETE", "results": [result("CONFIDENTIAL-PROMPT", float("nan"))]},
            "malformed": {"status": "COMPLETE", "results": ["CONFIDENTIAL-MALFORMED"]},
            "duplicates": {"status": "COMPLETE", "results": [result("CONFIDENTIAL-PROMPT", 0)] * 2},
            "wrong_identity": {"status": "COMPLETE", "model_id": "other-model", "results": []},
        }
        for name, branch in cases.items():
            with self.subTest(name=name):
                exit_code, report, _ = self.run_cli(self.make_client(branch), fail_on_regression=False)
                self.assertEqual(exit_code, 4)
                self.assertEqual(report["policy_decision"], "fail")
                self.assertTrue(report["model_reports"][0]["check_reports"][0]["operational_failure"])

    def test_warning_mode_allows_complete_regression_and_cleans_detail(self):
        client = self.make_client({"status": "COMPLETE", "results": [result("CONFIDENTIAL-PROMPT", 0)]})
        exit_code, report, files = self.run_cli(client, fail_on_regression=False, retain=False)
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["issues"][0]["severity"], "warning")
        self.assertFalse(any(path.startswith("restricted/") for path in files))

    def test_partial_creation_cancels_owned_run_without_retrying_creation(self):
        client = self.make_client(
            {"status": "RUNNING", "results": []},
            main={"status": "RUNNING", "results": [result("CONFIDENTIAL-PROMPT", None)]},
        )
        start = client.start_ai_eval_run
        attempts = []

        def partially_create(**kwargs):
            attempts.append(kwargs)
            if kwargs.get("branch_id"):
                raise OmniAPIError("CONFIDENTIAL-CREATION-ERROR")
            return start(**kwargs)

        client.start_ai_eval_run = partially_create
        exit_code, report, files = self.run_cli(client)
        self.assertEqual(exit_code, 4)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(client.cancelled, ["run-1"])
        journal = json.loads(files["restricted/model-1/ai-eval-runs.json"])
        self.assertEqual(journal["creation_outcome"], "pending_or_ambiguous")
        self.assertTrue(journal["cleanup_required"])
        self.assertTrue(report["model_reports"][0]["check_reports"][0]["cleanup_required"])

    def test_timeout_cancels_and_reconciles_both_runs(self):
        client = self.make_client(
            {"status": "RUNNING", "results": [result("CONFIDENTIAL-PROMPT", None)]},
            main={"status": "RUNNING", "results": [result("CONFIDENTIAL-PROMPT", None)]},
        )
        exit_code, _, files = self.run_cli(client)
        self.assertEqual(exit_code, 4)
        self.assertEqual(client.cancelled, ["run-1", "run-2"])
        journal = json.loads(files["restricted/model-1/ai-eval-runs.json"])
        self.assertEqual([row["cleanup"] for row in journal["owned_runs"]], ["terminal_confirmed"] * 2)

    def test_two_models_route_sets_using_server_metadata(self):
        class TwoModelClient(FakeEvalClient):
            def get_ai_eval_prompt_set(self, prompt_set_id):
                model_id = prompt_set_id.replace("set", "model")
                return {
                    "id": prompt_set_id,
                    "model_id": model_id,
                    "is_archived": False,
                    "prompts": [{"id": "opaque-id", "prompt_text": "CONFIDENTIAL-PROMPT"}],
                }

            def start_ai_eval_run(self, **kwargs):
                model_id = kwargs["prompt_set_id"].replace("set", "model")
                for key in ("main", "branch"):
                    self.runs_by_id[key] = {
                        "model_id": model_id,
                        "status": "COMPLETE",
                        "results": [result("CONFIDENTIAL-PROMPT", 1)],
                    }
                return super().start_ai_eval_run(**kwargs)

        client = TwoModelClient({})
        contexts = [
            ModelContext(
                base_url="https://omni.example",
                model_id=f"model-{i}",
                model_path=f"omni/model-{i}",
                branch_id=f"branch-{i}",
            )
            for i in (1, 2)
        ]
        exit_code, report, _ = self.run_cli(client, contexts=contexts, prompt_sets=[{"id": "set-1"}, {"id": "set-2"}])
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(report["model_reports"]), 2)
        self.assertEqual(
            [(row["prompt_set_id"], row["branch_id"]) for row in client.started],
            [("set-1", None), ("set-1", "branch-1"), ("set-2", None), ("set-2", "branch-2")],
        )

    def test_preflight_rejects_empty_duplicate_and_wrong_model_sets_before_creation(self):
        valid = {
            "id": "set-1",
            "model_id": "model-1",
            "is_archived": False,
            "prompts": [{"id": "p1", "prompt_text": "CONFIDENTIAL-PROMPT"}],
        }
        for override in ({"prompts": []}, {"prompts": valid["prompts"] * 2}, {"model_id": "model-2"}):
            with self.subTest(override=override):
                client = self.make_client({"status": "COMPLETE", "results": []})
                client.get_ai_eval_prompt_set = lambda _, override=override: {**copy.deepcopy(valid), **override}
                exit_code, _, _ = self.run_cli(client, prompt_sets=[{"id": "set-1", "model_id": "model-1"}])
                self.assertEqual(exit_code, 4)
                self.assertEqual(client.started, [])


class AiEvalConfigTests(unittest.TestCase):
    def test_disabled_by_default(self, tmp_path=None):
        config = load_config(None)
        self.assertFalse(config.ai_eval.enabled)
        self.assertEqual(config.ai_eval.prompt_sets, [])
        self.assertTrue(config.ai_eval.fail_on_regression)

    def test_prompt_sets_reject_duplicates_and_unknown_keys(self):
        from omniflow.config import _prompt_sets

        with self.assertRaises(ConfigError):
            _prompt_sets([{"id": "a"}, {"id": "a"}])
        with self.assertRaises(ConfigError):
            _prompt_sets([{"id": "a", "bogus": 1}])
        with self.assertRaises(ConfigError):
            _prompt_sets([{"label": "no id"}])

    def test_prompt_sets_cap_entry_count(self):
        from omniflow.config import MAX_PROMPT_SETS, _prompt_sets

        with self.assertRaises(SecurityPolicyError):
            _prompt_sets([{"id": f"set-{i}"} for i in range(MAX_PROMPT_SETS + 1)])

    def test_prompt_set_label_defaults_to_id(self):
        from omniflow.config import _prompt_sets

        self.assertEqual(_prompt_sets([{"id": "set-1"}]), [{"id": "set-1", "label": "set-1"}])

    def test_optional_model_routing_is_validated(self):
        from omniflow.config import _prompt_sets

        self.assertEqual(_prompt_sets([{"id": "set-1", "model_id": " model-1 "}])[0]["model_id"], "model-1")
        for value in ("", 1, "m" * 129):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                _prompt_sets([{"id": "set-1", "model_id": value}])


if __name__ == "__main__":
    unittest.main()
