import contextlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omniflow.config import ContractSettings, load_config
from omniflow.contracts import evaluate_contracts
from omniflow.discovery import (
    discover_contexts,
    discover_deployment_contexts,
    get_changed_files,
    load_flow_metadata,
    load_pr_marker,
)
from omniflow.downstream import generate_downstream_dependencies
from omniflow.exceptions import ConfigError, OmniAPIError, SecurityPolicyError


@contextlib.contextmanager
def temporary_workdir():
    original = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            yield Path(tmp)
        finally:
            os.chdir(original)


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class DiscoveryTests(unittest.TestCase):
    def test_single_model_auto_discovery_works_without_policy_config(self):
        with temporary_workdir() as tmp:
            write_json(
                tmp / ".omni/flow.json",
                {
                    "version": 1,
                    "models": [
                        {
                            "base_url": "https://omni.example",
                            "model_id": "model-1",
                            "model_path": "omni/model",
                            "base_branch": "main",
                        }
                    ],
                },
            )
            with mock.patch.dict(os.environ, {"GITHUB_HEAD_REF": "feature/a"}, clear=True):
                contexts = discover_contexts(auto=True)
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0].model_id, "model-1")
        self.assertEqual(contexts[0].branch_name, "feature/a")

    def test_auto_routing_skips_when_no_omni_context_exists(self):
        with temporary_workdir():
            with mock.patch.dict(os.environ, {}, clear=True):
                contexts = discover_contexts(auto=True, allow_skip=True)
        self.assertEqual(contexts, [])

    def test_missing_metadata_fails_closed_for_omni_files(self):
        with temporary_workdir():
            with mock.patch.dict(
                os.environ,
                {"OMNIFLOW_CHANGED_FILES": "omni/model/views/orders.view"},
                clear=True,
            ):
                with self.assertRaises(ConfigError):
                    discover_contexts(auto=True, allow_skip=True)

    def test_auto_routing_skips_non_omni_changed_files(self):
        with temporary_workdir() as tmp:
            write_json(
                tmp / ".omni/flow.json",
                {
                    "version": 1,
                    "models": [
                        {
                            "base_url": "https://omni.example",
                            "model_id": "model-1",
                            "model_path": "omni/model",
                        }
                    ],
                },
            )
            with mock.patch.dict(os.environ, {"OMNIFLOW_CHANGED_FILES": "models/marts/fact_orders.sql"}, clear=True):
                contexts = discover_contexts(auto=True, allow_skip=True)
        self.assertEqual(contexts, [])

    def test_unregistered_omni_files_fail_closed(self):
        with temporary_workdir() as tmp:
            write_json(
                tmp / ".omni/flow.json",
                {
                    "version": 1,
                    "models": [
                        {
                            "base_url": "https://omni.example",
                            "model_id": "model-1",
                            "model_path": "omni/registered",
                        }
                    ],
                },
            )
            with mock.patch.dict(
                os.environ,
                {"OMNIFLOW_CHANGED_FILES": "omni/unregistered/model.yaml"},
                clear=True,
            ):
                with self.assertRaises(ConfigError):
                    discover_contexts(auto=True, allow_skip=True)

    def test_nested_unregistered_omni_files_fail_closed(self):
        with temporary_workdir() as tmp:
            write_json(
                tmp / ".omni/flow.json",
                {
                    "version": 1,
                    "models": [
                        {
                            "base_url": "https://omni.example",
                            "model_id": "model-1",
                            "model_path": "omni/registered",
                        }
                    ],
                },
            )
            with mock.patch.dict(
                os.environ,
                {"OMNIFLOW_CHANGED_FILES": "omni/unregistered/views/orders.yaml"},
                clear=True,
            ):
                with self.assertRaises(ConfigError):
                    discover_contexts(auto=True, allow_skip=True)

    def test_multi_model_selection_by_changed_file_prefix(self):
        with temporary_workdir() as tmp:
            write_json(
                tmp / ".omni/flow.json",
                {
                    "version": 1,
                    "models": [
                        {"base_url": "https://omni.example", "model_id": "a", "model_path": "omni/a"},
                        {"base_url": "https://omni.example", "model_id": "b", "model_path": "omni/b"},
                    ],
                },
            )
            with mock.patch.dict(os.environ, {"OMNIFLOW_CHANGED_FILES": "omni/b/views/orders.view"}, clear=True):
                contexts = discover_contexts(auto=True)
        self.assertEqual([context.model_id for context in contexts], ["b"])

    def test_multiple_changed_model_paths_run_multiple_contexts(self):
        with temporary_workdir() as tmp:
            write_json(
                tmp / ".omni/flow.json",
                {
                    "version": 1,
                    "models": [
                        {"base_url": "https://omni.example", "model_id": "a", "model_path": "omni/a"},
                        {"base_url": "https://omni.example", "model_id": "b", "model_path": "omni/b"},
                    ],
                },
            )
            with mock.patch.dict(
                os.environ, {"OMNIFLOW_CHANGED_FILES": "omni/a/model.yaml\nomni/b/model.yaml"}, clear=True
            ):
                contexts = discover_contexts(auto=True)
        self.assertEqual([context.model_id for context in contexts], ["a", "b"])

    def test_deployment_discovery_selects_every_model_on_the_current_base_branch(self):
        with temporary_workdir() as tmp:
            write_json(
                tmp / ".omni/flow.json",
                {
                    "version": 1,
                    "models": [
                        {
                            "base_url": "https://omni.example",
                            "model_id": "a",
                            "model_path": "omni/a",
                            "base_branch": "main",
                        },
                        {
                            "base_url": "https://omni.example",
                            "model_id": "b",
                            "model_path": "omni/b",
                            "base_branch": "main",
                        },
                        {
                            "base_url": "https://omni.example",
                            "model_id": "release",
                            "model_path": "omni/release",
                            "base_branch": "release",
                        },
                    ],
                },
            )
            with mock.patch.dict(os.environ, {"GITHUB_REF_NAME": "main"}, clear=True):
                contexts = discover_deployment_contexts(auto=True)
        self.assertEqual([context.model_id for context in contexts], ["a", "b"])
        self.assertTrue(all(context.branch_name is None for context in contexts))

    def test_deployment_discovery_requires_complete_trusted_base_branch_metadata(self):
        with temporary_workdir() as tmp:
            write_json(
                tmp / ".omni/flow.json",
                {
                    "version": 1,
                    "models": [
                        {
                            "base_url": "https://omni.example",
                            "model_id": "model-1",
                            "model_path": "omni/model",
                        }
                    ],
                },
            )
            with mock.patch.dict(os.environ, {"GITHUB_REF_NAME": "main"}, clear=True):
                with self.assertRaises(ConfigError):
                    discover_deployment_contexts(auto=True)

    def test_explicit_deployment_discovery_requires_a_base_branch(self):
        with mock.patch.dict(os.environ, {"GITHUB_REF_NAME": "main"}, clear=True):
            with self.assertRaises(ConfigError):
                discover_deployment_contexts(
                    auto=False,
                    base_url="https://omni.example",
                    model_id="model-1",
                )
            contexts = discover_deployment_contexts(
                auto=False,
                base_url="https://omni.example",
                model_id="model-1",
                base_branch="main",
            )
        self.assertEqual(contexts[0].base_branch, "main")

    def test_deployment_discovery_rejects_prs_and_non_base_branches_as_security_violations(self):
        with temporary_workdir() as tmp:
            write_json(
                tmp / ".omni/flow.json",
                {
                    "version": 1,
                    "models": [
                        {
                            "base_url": "https://omni.example",
                            "model_id": "model-1",
                            "model_path": "omni/model",
                            "base_branch": "main",
                        }
                    ],
                },
            )
            for env in (
                {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_HEAD_REF": "feature/a"},
                {"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": "feature/a"},
            ):
                with self.subTest(env=env):
                    with mock.patch.dict(os.environ, env, clear=True):
                        with self.assertRaises(SecurityPolicyError):
                            discover_deployment_contexts(auto=True)

    def test_pr_marker_resolves_ambiguous_content_only_pr(self):
        with temporary_workdir() as tmp:
            write_json(
                tmp / ".omni/flow.json",
                {
                    "version": 1,
                    "models": [
                        {"base_url": "https://omni.example", "model_id": "a", "model_path": "omni/a"},
                        {"base_url": "https://omni.example", "model_id": "b", "model_path": "omni/b"},
                    ],
                },
            )
            event = {
                "pull_request": {"body": '<!-- omniflow-context {"model_id":"b","branch_name":"feature/content"} -->'}
            }
            write_json(tmp / "event.json", event)
            with mock.patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(tmp / "event.json")}, clear=True):
                marker = load_pr_marker()
                contexts = discover_contexts(auto=True)
        self.assertEqual(marker["model_id"], "b")
        self.assertEqual(contexts[0].model_id, "b")
        self.assertEqual(contexts[0].branch_name, "feature/content")

    def test_pr_marker_cannot_redirect_validation_to_a_different_branch(self):
        with temporary_workdir() as tmp:
            write_json(
                tmp / ".omni/flow.json",
                {
                    "version": 1,
                    "models": [
                        {
                            "base_url": "https://omni.example",
                            "model_id": "model-1",
                            "model_path": "omni/model",
                        }
                    ],
                },
            )
            event = {
                "pull_request": {"body": '<!-- omniflow-context {"model_id":"model-1","branch_name":"safe/branch"} -->'}
            }
            write_json(tmp / "event.json", event)
            env = {"GITHUB_EVENT_PATH": str(tmp / "event.json"), "GITHUB_HEAD_REF": "risky/branch"}
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(SecurityPolicyError):
                    discover_contexts(auto=True)

    def test_pull_request_must_target_the_models_trusted_base_branch(self):
        with temporary_workdir() as tmp:
            write_json(
                tmp / ".omni/flow.json",
                {
                    "version": 1,
                    "models": [
                        {
                            "base_url": "https://omni.example",
                            "model_id": "model-1",
                            "model_path": "omni/model",
                            "base_branch": "main",
                        }
                    ],
                },
            )
            env = {
                "GITHUB_EVENT_NAME": "pull_request_target",
                "GITHUB_BASE_REF": "release",
                "GITHUB_HEAD_REF": "feature/omni",
                "OMNIFLOW_CHANGED_FILES": "omni/model/views/orders.view",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(ConfigError):
                    discover_contexts(auto=True)

    def test_pr_marker_cannot_provide_base_url_without_trusted_source(self):
        with temporary_workdir() as tmp:
            event = {
                "pull_request": {
                    "body": '<!-- omniflow-context {"base_url":"https://omni.example","model_id":"model-1","model_path":"omni/model","branch_name":"feature/a"} -->'
                }
            }
            write_json(tmp / "event.json", event)
            with mock.patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(tmp / "event.json")}, clear=True):
                with self.assertRaises(SecurityPolicyError):
                    discover_contexts(auto=True)

    def test_pr_marker_requires_trusted_flow_metadata_even_with_host_override(self):
        with temporary_workdir() as tmp:
            event = {
                "pull_request": {
                    "body": '<!-- omniflow-context {"model_id":"model-1","model_path":"omni/model","branch_name":"feature/a"} -->'
                }
            }
            write_json(tmp / "event.json", event)
            with mock.patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(tmp / "event.json")}, clear=True):
                with self.assertRaises(ConfigError):
                    discover_contexts(auto=True, base_url="https://omni.example")

    def test_pull_request_uses_identity_and_policy_from_trusted_base_branch(self):
        with temporary_workdir() as tmp:
            subprocess.run(["git", "init", "-q", "-b", "main"], check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "config", "user.name", "OmniFlow Tests"], check=True)
            write_json(
                tmp / ".omni/flow.json",
                {
                    "version": 1,
                    "models": [
                        {
                            "base_url": "https://trusted.omniapp.co",
                            "model_id": "model-1",
                            "model_path": "omni/model",
                            "base_branch": "main",
                        }
                    ],
                },
            )
            (tmp / ".omniflow.yml").write_text(
                "checks:\n  model_validation:\n    enabled: true\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "."], check=True)
            subprocess.run(["git", "commit", "-q", "-m", "trusted base"], check=True)
            subprocess.run(["git", "switch", "-q", "-c", "feature/untrusted"], check=True)
            write_json(
                tmp / ".omni/flow.json",
                {
                    "version": 1,
                    "models": [
                        {
                            "base_url": "https://attacker.example",
                            "model_id": "model-1",
                            "model_path": "omni/model",
                            "base_branch": "main",
                        }
                    ],
                },
            )
            (tmp / ".omniflow.yml").write_text(
                "checks:\n  model_validation:\n    enabled: false\n",
                encoding="utf-8",
            )
            env = {
                "GITHUB_EVENT_NAME": "pull_request_target",
                "GITHUB_BASE_REF": "main",
                "GITHUB_HEAD_REF": "feature/untrusted",
                "OMNIFLOW_CHANGED_FILES": "omni/model/views/orders.view",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                contexts = discover_contexts(auto=True)
                config = load_config(".omniflow.yml")
        self.assertEqual(contexts[0].base_url, "https://trusted.omniapp.co")
        self.assertTrue(config.model_validation.enabled)

    def test_pull_request_target_uses_github_api_file_list(self):
        with temporary_workdir() as tmp:
            event = {
                "number": 42,
                "repository": {"full_name": "acme/analytics"},
                "pull_request": {
                    "changed_files": 2,
                    "head": {"ref": "feature/omni", "sha": "a" * 40},
                },
            }
            write_json(tmp / "event.json", event)
            response = mock.Mock()
            response.ok = True
            response.status_code = 200
            response.json.return_value = [
                {"filename": "omni/model/views/orders.view"},
                {
                    "filename": "docs/orders.md",
                    "previous_filename": "omni/model/topics/orders.topic",
                },
            ]
            env = {
                "GITHUB_EVENT_NAME": "pull_request_target",
                "GITHUB_EVENT_PATH": str(tmp / "event.json"),
                "GITHUB_REPOSITORY": "acme/analytics",
                "OMNIFLOW_GITHUB_TOKEN": "github-token",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                with mock.patch("omniflow.discovery.requests.get", return_value=response) as request:
                    files = get_changed_files()
        self.assertEqual(
            files,
            ["omni/model/views/orders.view", "docs/orders.md", "omni/model/topics/orders.topic"],
        )
        self.assertFalse(request.call_args.kwargs["allow_redirects"])
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer github-token")

    def test_pull_request_target_without_github_token_fails_closed(self):
        with temporary_workdir() as tmp:
            event = {"number": 42, "repository": {"full_name": "acme/analytics"}, "pull_request": {}}
            write_json(tmp / "event.json", event)
            env = {
                "GITHUB_EVENT_NAME": "pull_request_target",
                "GITHUB_EVENT_PATH": str(tmp / "event.json"),
            }
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(ConfigError):
                    get_changed_files()

    def test_pull_request_target_rejects_github_api_url_with_query(self):
        with temporary_workdir() as tmp:
            event = {
                "number": 42,
                "repository": {"full_name": "acme/analytics"},
                "pull_request": {"changed_files": 1},
            }
            write_json(tmp / "event.json", event)
            env = {
                "GITHUB_EVENT_NAME": "pull_request_target",
                "GITHUB_EVENT_PATH": str(tmp / "event.json"),
                "OMNIFLOW_GITHUB_TOKEN": "github-token",
                "GITHUB_API_URL": "https://api.github.com?redirect=https://example.com",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(SecurityPolicyError):
                    get_changed_files()

    def test_root_model_path_routes_only_omni_files(self):
        flow = {
            "models": [
                {
                    "base_url": "https://omni.example",
                    "model_id": "model-1",
                    "model_path": ".",
                }
            ]
        }
        from omniflow.discovery import select_model_contexts

        contexts = select_model_contexts(
            flow,
            changed_files=["views/orders.yaml", "models/marts/orders.sql"],
            branch_name="feature/omni",
        )
        self.assertEqual([context.model_id for context in contexts], ["model-1"])

    def test_auto_routing_skips_single_model_when_changed_files_unavailable(self):
        with temporary_workdir() as tmp:
            write_json(
                tmp / ".omni/flow.json",
                {
                    "version": 1,
                    "models": [
                        {"base_url": "https://omni.example", "model_id": "a", "model_path": "omni/a"},
                    ],
                },
            )
            with mock.patch.dict(os.environ, {"GITHUB_HEAD_REF": "feature/dbt"}, clear=True):
                contexts = discover_contexts(auto=True, allow_skip=True)
        self.assertEqual(contexts, [])

    def test_malformed_pr_marker_is_config_error(self):
        with temporary_workdir() as tmp:
            event = {"pull_request": {"body": '<!-- omniflow-context {"model_id":} -->'}}
            write_json(tmp / "event.json", event)
            with mock.patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(tmp / "event.json")}, clear=True):
                with self.assertRaises(ConfigError):
                    load_pr_marker()

    def test_ambiguous_multi_model_without_marker_fails(self):
        with temporary_workdir() as tmp:
            write_json(
                tmp / ".omni/flow.json",
                {
                    "version": 1,
                    "models": [
                        {"base_url": "https://omni.example", "model_id": "a", "model_path": "omni/a"},
                        {"base_url": "https://omni.example", "model_id": "b", "model_path": "omni/b"},
                    ],
                },
            )
            with self.assertRaises(ConfigError):
                discover_contexts(auto=True)

    def test_metadata_rejects_secret_keys(self):
        with temporary_workdir() as tmp:
            path = tmp / ".omni/flow.json"
            write_json(path, {"version": 1, "api_key": "bad", "models": []})  # pragma: allowlist secret
            with self.assertRaises(SecurityPolicyError):
                load_flow_metadata(path)

    def test_metadata_rejects_unknown_top_level_and_model_keys(self):
        payloads = [
            {
                "version": 1,
                "unexpected": True,
                "models": [{"base_url": "https://omni.example", "model_id": "a", "model_path": "omni/a"}],
            },
            {
                "version": 1,
                "models": [
                    {
                        "base_url": "https://omni.example",
                        "model_id": "a",
                        "model_path": "omni/a",
                        "api_version": "v1",
                    }
                ],
            },
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                with temporary_workdir() as tmp:
                    path = tmp / ".omni/flow.json"
                    write_json(path, payload)
                    with self.assertRaises(ConfigError):
                        load_flow_metadata(path)

    def test_push_event_uses_bounded_event_changed_files_without_git_history(self):
        with temporary_workdir() as tmp:
            event = {
                "size": 2,
                "commits": [
                    {"added": ["omni/model/views/orders.view"], "modified": [], "removed": []},
                    {"added": [], "modified": ["README.md"], "removed": ["omni/model/old.topic"]},
                ],
            }
            write_json(tmp / "event.json", event)
            env = {"GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": str(tmp / "event.json")}
            with mock.patch.dict(os.environ, env, clear=True):
                files = get_changed_files()
        self.assertEqual(files, ["omni/model/views/orders.view", "README.md", "omni/model/old.topic"])

    def test_incomplete_push_event_fails_closed(self):
        with temporary_workdir() as tmp:
            write_json(tmp / "event.json", {"size": 2, "commits": [{"added": [], "modified": [], "removed": []}]})
            env = {"GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": str(tmp / "event.json")}
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(ConfigError):
                    get_changed_files()

    def test_multiple_pr_markers_fail_closed(self):
        with temporary_workdir() as tmp:
            marker = '<!-- omniflow-context {"model_id":"a"} -->'
            write_json(tmp / "event.json", {"pull_request": {"body": f"{marker}\n{marker}"}})
            with mock.patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(tmp / "event.json")}, clear=True):
                with self.assertRaises(ConfigError):
                    load_pr_marker()


class ContractImpactTests(unittest.TestCase):
    def dependencies(self):
        return {
            "version": 1,
            "model_id": "model-1",
            "dependencies": [
                {
                    "content_id": "dash-1",
                    "content_type": "dashboard",
                    "content_name": "Executive Revenue",
                    "labels": ["Verified"],
                    "query_id": "query-1",
                    "query_name": "Revenue",
                    "references": [{"type": "field", "name": "orders.revenue"}],
                }
            ],
        }

    def test_deleted_referenced_field_fails(self):
        report, exit_code = evaluate_contracts(
            diff_result={"changes": [{"type": "field_deleted", "field": "orders.revenue", "risk": "breaking"}]},
            dependencies=self.dependencies(),
            settings=ContractSettings(),
            model_id="model-1",
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["issues"][0]["impact_level"], "referenced_breaking")

    def test_deleted_unreferenced_field_reports_without_failure(self):
        report, exit_code = evaluate_contracts(
            diff_result={"changes": [{"type": "field_deleted", "field": "orders.margin", "risk": "breaking"}]},
            dependencies=self.dependencies(),
            settings=ContractSettings(),
            model_id="model-1",
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["issues"][0]["impact_level"], "unreferenced")

    def test_referenced_type_and_cardinality_changes_fail(self):
        cases = (
            (
                {"type": "field_type_changed", "field": "orders.revenue", "risk": "breaking"},
                self.dependencies(),
            ),
            (
                {
                    "type": "relationship_cardinality_changed",
                    "name": "orders_to_items",
                    "affected_views": ["orders", "order_items"],
                    "risk": "breaking",
                },
                {
                    "dependencies": [
                        {
                            "content_id": "dash-1",
                            "query_id": "query-1",
                            "references": [{"type": "view", "name": "orders"}],
                        }
                    ]
                },
            ),
        )
        for change, dependencies in cases:
            with self.subTest(change=change):
                _, exit_code = evaluate_contracts(
                    diff_result={"changes": [change]},
                    dependencies=dependencies,
                    settings=ContractSettings(),
                    model_id="model-1",
                )
                self.assertEqual(exit_code, 1)

    def test_advisory_referenced_change_reports_without_failure(self):
        report, exit_code = evaluate_contracts(
            diff_result={"changes": [{"type": "field_modified", "field": "orders.revenue", "risk": "warning"}]},
            dependencies=self.dependencies(),
            settings=ContractSettings(),
            model_id="model-1",
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["issues"][0]["impact_level"], "referenced_safe")

    def test_reference_type_mismatch_does_not_match_contract_impact(self):
        report, exit_code = evaluate_contracts(
            diff_result={"changes": [{"type": "field_deleted", "field": "orders", "risk": "breaking"}]},
            dependencies={
                "dependencies": [
                    {
                        "content_id": "dash-1",
                        "references": [{"type": "topic", "name": "orders"}],
                    }
                ]
            },
            settings=ContractSettings(),
            model_id="model-1",
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["issues"][0]["impact_level"], "unreferenced")

    def test_contract_report_includes_dependency_coverage(self):
        dependencies = self.dependencies()
        dependencies["generation_mode"] = "targeted_partial"
        dependencies["coverage_gaps"] = [{"type": "field", "name": "orders.margin", "message": "unavailable"}]
        report, _ = evaluate_contracts(
            diff_result={"changes": [{"type": "field_deleted", "field": "orders.revenue", "risk": "breaking"}]},
            dependencies=dependencies,
            settings=ContractSettings(),
            model_id="model-1",
        )
        self.assertEqual(report["dependency_generation_mode"], "targeted_partial")
        self.assertEqual(report["summary"]["dependency_coverage_gaps"], 1)


class FakeDependencyClient:
    def __init__(self, *, fail_targeted=False, fail_after_first=False):
        self.fail_targeted = fail_targeted
        self.fail_after_first = fail_after_first
        self.searches = []

    def search_content_references(self, model_id, *, find, find_type, **kwargs):
        self.searches.append((find_type, find))
        if self.fail_targeted or (self.fail_after_first and len(self.searches) > 1):
            raise OmniAPIError("targeted search unavailable")
        return {
            "content": [
                {
                    "document_id": "dash-1",
                    "identifier": "dashboard-1",
                    "type": "dashboard",
                    "name": "Executive Revenue",
                    "owner": {"name": "Jane Doe", "email": "jane@example.com"},
                    "labels": [{"name": "Verified"}],
                    "queries_and_issues": [{"query_presentation_id": "query-1", "query_name": "Revenue by Month"}],
                }
            ]
        }

    def validate_content(self, model_id, **kwargs):
        return {
            "content": [
                {
                    "document_id": "dash-fallback",
                    "type": "dashboard",
                    "name": "Fallback Dashboard",
                    "queries_and_issues": [{"query_presentation_id": "query-x", "query_name": "Fallback"}],
                }
            ]
        }


class DownstreamGenerationTests(unittest.TestCase):
    def test_generates_dependencies_from_targeted_content_search(self):
        client = FakeDependencyClient()
        dependency_graph = generate_downstream_dependencies(
            client=client,
            model_id="model-1",
            branch_id="branch-1",
            diff_result={"changes": [{"type": "field_deleted", "field": "orders.revenue"}]},
        )
        self.assertEqual(client.searches, [("field", "orders.revenue")])
        self.assertEqual(dependency_graph["generation_mode"], "targeted")
        self.assertEqual(dependency_graph["dependencies"][0]["content_id"], "dash-1")
        self.assertEqual(
            dependency_graph["dependencies"][0]["references"], [{"type": "field", "name": "orders.revenue"}]
        )

    def test_targeted_search_failure_records_a_blocking_coverage_gap(self):
        dependency_graph = generate_downstream_dependencies(
            client=FakeDependencyClient(fail_targeted=True),
            model_id="model-1",
            branch_id="branch-1",
            diff_result={"changes": [{"type": "field_deleted", "field": "orders.revenue"}]},
        )
        self.assertEqual(dependency_graph["generation_mode"], "targeted_unavailable")
        self.assertEqual(dependency_graph["dependencies"], [])
        self.assertEqual(dependency_graph["coverage_gaps"][0]["name"], "orders.revenue")

    def test_partial_targeted_failure_preserves_successful_dependencies(self):
        dependency_graph = generate_downstream_dependencies(
            client=FakeDependencyClient(fail_after_first=True),
            model_id="model-1",
            branch_id="branch-1",
            diff_result={
                "changes": [
                    {"type": "field_deleted", "field": "orders.revenue"},
                    {"type": "field_deleted", "field": "orders.margin"},
                ]
            },
        )
        self.assertEqual(dependency_graph["generation_mode"], "targeted_partial")
        self.assertEqual(dependency_graph["dependencies"][0]["content_id"], "dash-1")
        self.assertEqual(dependency_graph["coverage_gaps"][0]["name"], "orders.margin")

    def test_relationship_changes_record_coverage_gap_without_unsupported_search(self):
        client = FakeDependencyClient()
        dependency_graph = generate_downstream_dependencies(
            client=client,
            model_id="model-1",
            branch_id="branch-1",
            diff_result={"changes": [{"type": "relationship_cardinality_changed", "name": "orders_to_items"}]},
        )
        self.assertEqual(client.searches, [])
        self.assertEqual(dependency_graph["coverage_gaps"][0]["type"], "relationship")
        self.assertIn("did not identify joined views", dependency_graph["coverage_gaps"][0]["message"])


if __name__ == "__main__":
    unittest.main()
