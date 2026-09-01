import unittest

from omniflow.config import load_config
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
        self._next_id = 0

    def start_ai_eval_run(self, *, prompt_set_id, description, branch_id=None):
        self._next_id += 1
        run_id = f"run-{self._next_id}"
        self.started.append({"prompt_set_id": prompt_set_id, "description": description, "branch_id": branch_id})
        # Deterministically map the Nth start call to "main" then "branch" run fixtures.
        key = "main" if branch_id is None else "branch"
        self.runs_by_id[run_id] = self.runs_by_id.pop(key)
        return run_id

    def get_ai_eval_run(self, run_id):
        self.polled.append(run_id)
        return self.runs_by_id[run_id]


def result(prompt, score, **overrides):
    return {"prompt": prompt, "score": score, **overrides}


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
                        result("q1", 0, error_reason="wrong answer"),
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
        self.assertEqual(issue["prompt"], "q1")
        self.assertNotIn("branch_conversation_id", issue)
        summary = report["prompt_set_summaries"][0]
        self.assertEqual(summary["regressed_count"], 1)
        self.assertAlmostEqual(summary["accuracy_delta_pts"], -50.0)
        self.assertEqual(client.started[1]["branch_id"], "branch-1")
        # Detail carries full per-prompt rows that never reach the public report.
        self.assertEqual(len(detail["prompt_set_results"][0]["comparison"]), 2)

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
        self.assertEqual(exit_code, 0)
        self.assertGreaterEqual(clock.value, 3)

    def test_timeout_raises_operational_error(self):
        client = FakeEvalClient(
            {
                "main": {"status": "RUNNING", "results": []},
                "branch": {"status": "RUNNING", "results": []},
            }
        )
        clock = FakeClock()
        with self.assertRaises(OmniAPIError):
            run_ai_eval_validation(
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


if __name__ == "__main__":
    unittest.main()
