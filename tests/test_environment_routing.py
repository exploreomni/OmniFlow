import contextlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omniflow.discovery import discover_contexts, discover_deployment_contexts, load_flow_metadata
from omniflow.exceptions import ConfigError, SecurityPolicyError


def registry():
    return {
        "version": 2,
        "models": [
            {
                "environment": "development", "base_url": "https://development.omni.example",
                "model_id": "leader-model", "model_path": "omni/shared", "base_branch": "develop",
                "git_follower": False, "web_url": "https://github.com/example/analytics",
            },
            {
                "environment": "production", "base_url": "https://production.omni.example",
                "model_id": "follower-model", "model_path": "omni/shared", "base_branch": "main",
                "git_follower": True, "web_url": "https://github.com/example/analytics",
            },
        ],
    }


@contextlib.contextmanager
def temporary_workdir():
    original = os.getcwd()
    with tempfile.TemporaryDirectory() as directory:
        os.chdir(directory)
        try:
            yield Path(directory)
        finally:
            os.chdir(original)


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


class EnvironmentRoutingTests(unittest.TestCase):
    def load(self, flow):
        with mock.patch("omniflow.discovery.read_trusted_repo_text", return_value=json.dumps(flow)):
            return load_flow_metadata()

    def discover(self, *, flow=None, base="main", head="release/candidate", marker=None,
                 changed_files=None, event_base=None, event_head=None, **kwargs):
        event = {"pull_request": {"base": {"ref": event_base or base}, "head": {"ref": event_head or head}}}
        env = {"GITHUB_EVENT_NAME": "pull_request_target", "GITHUB_BASE_REF": base, "GITHUB_HEAD_REF": head}
        options = {"auto": True, "allow_skip": True, **kwargs}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("omniflow.discovery.read_trusted_repo_text", return_value=json.dumps(flow or registry())), \
             mock.patch("omniflow.discovery.github_event_payload", return_value=event), \
             mock.patch("omniflow.discovery.load_pr_marker", return_value=marker or {}):
            return discover_contexts(
                changed_files=["omni/shared/orders.view"] if changed_files is None else changed_files,
                **options,
            )

    def test_feature_and_release_prs_select_correct_environment_before_shared_path(self):
        cases = (
            ("develop", "feature/orders", "development", "leader-model", False),
            ("main", "release/2026-09-23", "production", "follower-model", True),
        )
        for base, head, environment, model_id, follower in cases:
            with self.subTest(base=base):
                contexts = self.discover(base=base, head=head)
                self.assertEqual(len(contexts), 1)
                context = contexts[0]
                self.assertEqual((context.environment, context.model_id), (environment, model_id))
                self.assertEqual(context.base_url, f"https://{environment}.omni.example")
                self.assertEqual((context.base_branch, context.branch_name), (base, head))
                self.assertEqual(context.git_follower, follower)

    def test_shared_paths_and_same_model_id_are_allowed_only_across_target_branches(self):
        flow = registry()
        flow["models"][1]["model_id"] = flow["models"][0]["model_id"]
        self.assertEqual(self.load(flow), flow)
        self.assertEqual(self.discover(flow=flow)[0].environment, "production")

    def test_duplicate_or_nested_paths_within_target_are_rejected(self):
        for path in ("omni/shared", "omni/shared/submodel", "omni", "."):
            flow = registry()
            other = {**flow["models"][1], "model_id": "other-model", "model_path": path}
            flow["models"].append(other)
            with self.subTest(path=path), self.assertRaisesRegex(ConfigError, "overlapping or duplicate"):
                self.load(flow)

    def test_duplicate_model_id_with_distinct_paths_within_target_is_rejected(self):
        flow = registry()
        flow["models"].append({**flow["models"][1], "model_path": "omni/other"})
        with self.assertRaisesRegex(ConfigError, "duplicate model_id"):
            self.load(flow)

    def test_target_branch_cannot_mix_environments_or_hosts(self):
        for changes in ({"environment": "staging"}, {"base_url": "https://other.omni.example"}):
            flow = registry()
            flow["models"].append({**flow["models"][1], "model_id": "other", "model_path": "omni/other", **changes})
            with self.subTest(changes=changes), self.assertRaisesRegex(ConfigError, "exactly one"):
                self.load(flow)

    def test_environment_cannot_span_multiple_base_branches_or_hosts(self):
        for changes in ({"base_branch": "staging"}, {"base_url": "https://other.omni.example"}):
            flow = registry()
            flow["models"].append({**flow["models"][1], "model_id": "other", "model_path": "omni/other", **changes})
            with self.subTest(changes=changes), self.assertRaisesRegex(ConfigError, "exactly one"):
                self.load(flow)

    def test_environment_metadata_requires_explicit_identity_and_canonical_fields(self):
        for field in ("environment", "base_branch", "web_url", "git_follower"):
            flow = registry()
            flow["models"][1].pop(field)
            with self.subTest(missing=field), self.assertRaises(ConfigError):
                self.load(flow)
        for field, value in (("environment", "Production"), ("environment", "prod/test"),
                             ("git_follower", "true"), ("model_path", "/omni/shared"),
                             ("model_path", "omni/./shared"), ("model_path", "omni//shared")):
            flow = registry()
            flow["models"][1][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ConfigError):
                self.load(flow)

    def test_unknown_base_fails_even_when_only_unrelated_docs_changed(self):
        for paths in ([], ["README.md"], ["omni/shared/orders.view"]):
            with self.subTest(paths=paths), self.assertRaisesRegex(ConfigError, "No trusted version 2"):
                self.discover(base="staging", changed_files=paths)

    def test_unregistered_composite_and_bare_model_files_cannot_skip(self):
        for filename in ("sales.composite_topic", "model", "relationships"):
            with self.subTest(filename=filename), self.assertRaisesRegex(ConfigError, "outside every model_path"):
                self.discover(changed_files=[f"omni/unregistered/{filename}"])

    def test_root_target_routes_generic_yaml_and_other_target_paths_cannot_skip(self):
        flow = registry()
        flow["models"][1]["model_path"] = "."
        self.assertEqual(len(self.discover(flow=flow, changed_files=["orders.yaml"])), 1)
        flow["models"][1]["model_path"] = "omni/production"
        with self.assertRaisesRegex(ConfigError, "outside every model_path"):
            self.discover(flow=flow, changed_files=["omni/shared/orders.yaml"])

    def test_event_and_environment_base_or_head_disagreement_fails_closed(self):
        for changes in ({"event_base": "develop"}, {"event_head": "feature/different"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ConfigError, "GitHub PR"):
                self.discover(**changes)

    def test_explicit_identity_overrides_cannot_bypass_trusted_registry(self):
        cases = ({"auto": False}, {"base_url": "https://other.omni.example"}, {"model_id": "other"},
                 {"model_path": "omni/other"}, {"branch_name": "feature/other"}, {"branch_id": "other"},
                 {"auto": False, "base_url": "https://other.omni.example", "model_id": "other"})
        for options in cases:
            with self.subTest(options=options), self.assertRaisesRegex(SecurityPolicyError, "identity overrides"):
                self.discover(**options)

    def test_marker_cannot_select_another_environment(self):
        with self.assertRaisesRegex(ConfigError, "selected target environment"):
            self.discover(marker={"model_id": "leader-model"})

    def test_marker_cannot_redirect_host_path_or_candidate_branch(self):
        for changes in ({"base_url": "https://other.omni.example"}, {"model_path": "omni/other"},
                        {"branch_name": "feature/safe"}):
            with self.subTest(changes=changes), self.assertRaises(SecurityPolicyError):
                self.discover(marker={"model_id": "follower-model", **changes})

    def test_marker_cannot_narrow_other_affected_models_in_same_environment(self):
        flow = registry()
        flow["models"].append({**flow["models"][1], "model_id": "finance", "model_path": "omni/finance"})
        contexts = self.discover(
            flow=flow, marker={"model_id": "follower-model"},
            changed_files=["omni/shared/orders.view", "omni/finance/revenue.view"],
        )
        self.assertEqual({context.model_id for context in contexts}, {"follower-model", "finance"})
        self.assertTrue(all(context.environment == "production" for context in contexts))

    def test_unregistered_omni_files_fail_even_alongside_a_valid_model_or_marker(self):
        for marker in ({}, {"model_id": "follower-model"}):
            with self.subTest(marker=marker), self.assertRaisesRegex(ConfigError, "outside every model_path"):
                self.discover(marker=marker, changed_files=["omni/shared/orders.view", "other/orders.view"])

    def test_registered_target_can_skip_docs_only_and_route_content_only_marker(self):
        self.assertEqual(self.discover(changed_files=["README.md"]), [])
        contexts = self.discover(changed_files=[], marker={"model_id": "follower-model"})
        self.assertEqual([context.model_id for context in contexts], ["follower-model"])

    def test_local_doctor_can_select_target_without_a_pull_request(self):
        flow = registry()
        flow["models"].append({**flow["models"][1], "model_id": "finance", "model_path": "omni/finance"})
        env = {"OMNIFLOW_TARGET_BRANCH": "main", "GITHUB_REF_NAME": "main"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("omniflow.discovery.read_trusted_repo_text", return_value=json.dumps(flow)):
            contexts = discover_contexts(auto=True, changed_files=[])
        self.assertEqual({context.model_id for context in contexts}, {"follower-model", "finance"})
        self.assertTrue(all(context.branch_name is None for context in contexts))

    def test_trusted_base_registry_survives_pr_head_downgrade_or_deletion(self):
        with temporary_workdir() as directory:
            git("init", "-q", "-b", "main")
            git("config", "user.email", "test@example.com")
            git("config", "user.name", "OmniFlow Tests")
            metadata = directory / ".omni/flow.json"
            metadata.parent.mkdir()
            metadata.write_text(json.dumps(registry()), encoding="utf-8")
            git("add", ".omni/flow.json")
            git("commit", "-q", "-m", "trusted targets")
            git("switch", "-q", "-c", "release/untrusted")
            hostile = {"version": 1, "models": [{
                "base_url": "https://attacker.example", "model_id": "attacker-model", "model_path": "omni/shared",
            }]}
            event = directory / "event.json"
            event.write_text(json.dumps({"pull_request": {
                "base": {"ref": "main"}, "head": {"ref": "release/untrusted"},
            }}), encoding="utf-8")
            env = {
                "GITHUB_EVENT_NAME": "pull_request_target", "GITHUB_BASE_REF": "main",
                "GITHUB_HEAD_REF": "release/untrusted", "GITHUB_EVENT_PATH": str(event),
            }
            for mutation in ("downgrade", "delete"):
                if mutation == "downgrade":
                    metadata.write_text(json.dumps(hostile), encoding="utf-8")
                else:
                    metadata.unlink()
                git("add", ".omni/flow.json")
                git("commit", "-q", "-m", f"untrusted {mutation}")
                with self.subTest(mutation=mutation), mock.patch.dict(os.environ, env, clear=True):
                    contexts = discover_contexts(auto=True, changed_files=["omni/shared/orders.view"])
                    self.assertEqual(contexts[0].model_id, "follower-model")
                    self.assertEqual(contexts[0].base_url, "https://production.omni.example")
                    self.assertEqual(contexts[0].environment, "production")
                    with self.assertRaises(SecurityPolicyError):
                        discover_contexts(auto=False, base_url="https://attacker.example", model_id="attacker-model")

    def test_deployment_discovery_cannot_bypass_environment_registry_with_explicit_identity(self):
        for auto in (True, False):
            with self.subTest(auto=auto), mock.patch.dict(os.environ, {"GITHUB_REF_NAME": "main"}, clear=True), \
                 mock.patch("omniflow.discovery.read_trusted_repo_text", return_value=json.dumps(registry())), \
                 self.assertRaisesRegex(SecurityPolicyError, "validation-only"):
                discover_deployment_contexts(
                    auto=auto, base_url="https://production.omni.example", model_id="follower-model", base_branch="main",
                )


if __name__ == "__main__":
    unittest.main()
