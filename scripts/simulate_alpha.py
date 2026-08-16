#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
MODEL_ID = "model-1"
SECOND_MODEL_ID = "model-2"
BRANCH_ID = "branch-1"
BRANCH_NAME = "feature/omniflow-alpha"


BASE_FILES = {
    "views/orders.view": """name: orders
fields:
  id:
    type: number
    primary_key: true
    description: Order ID
  revenue:
    type: number
    description: Revenue
    aggregate_type: sum
""",
    "relationships/relationships.yaml": """- join_from_view: orders
  join_to_view: order_items
  relationship_type: many_to_one
  on_sql: $${orders.id} = $${order_items.order_id}
""",
    "topics/sales.topic": """name: sales
label: Sales
base_view: orders
""",
}


HEAD_FILES = {
    "views/orders.view": """name: orders
fields:
  id:
    type: number
    primary_key: true
    description: Order ID
""",
    "relationships/relationships.yaml": """- join_from_view: orders
  join_to_view: order_items
  relationship_type: one_to_many
  on_sql: $${orders.id} = $${order_items.order_id}
""",
    "topics/sales.topic": """name: sales
label: Sales
base_view: orders
""",
}


SYNC_HEAD_FILES = {
    **BASE_FILES,
    "views/orders.view": BASE_FILES["views/orders.view"]
    + "  dbt_sync_marker:\n    type: string\n    description: Added by simulated dbt deployment\n",
}


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    expected_exit: int
    changed_files: str
    marker: dict[str, Any] | None = None
    server_mode: str = "normal"
    config: str | None = None
    include_api_key: bool = True
    model_ids: tuple[str, ...] = (MODEL_ID,)
    operation: str = "validate"


SCENARIOS = [
    Scenario(
        name="skip_non_omni_pr",
        description="dbt-only PR should skip cleanly",
        expected_exit=0,
        changed_files="models/marts/fct_orders.sql",
        server_mode="unused",
    ),
    Scenario(
        name="fork_non_omni_without_secret",
        description="fork dbt-only PR should skip without receiving the Omni secret",
        expected_exit=0,
        changed_files="models/marts/fct_orders.sql",
        server_mode="unused",
        include_api_key=False,
    ),
    Scenario(
        name="fork_omni_without_secret",
        description="fork Omni PR should fail closed when the Omni secret is withheld",
        expected_exit=2,
        changed_files="omni/model/views/orders.view",
        server_mode="unused",
        include_api_key=False,
    ),
    Scenario(
        name="multi_model_contract_failure",
        description="changes under two model paths should execute and aggregate both model contexts",
        expected_exit=1,
        changed_files="omni/model/views/orders.view\nomni/model-2/views/orders.view",
        model_ids=(MODEL_ID, SECOND_MODEL_ID),
    ),
    Scenario(
        name="contract_failure",
        description="deleted referenced field should fail with public artifacts",
        expected_exit=1,
        changed_files="omni/model/views/orders.view",
    ),
    Scenario(
        name="strict_redaction",
        description="strict public reports should redact content names and owner metadata",
        expected_exit=1,
        changed_files="omni/model/views/orders.view",
        config="""security:
  redaction_level: strict
checks:
  dbt_exposures:
    enabled: true
""",
    ),
    Scenario(
        name="missing_branch",
        description="branch name without branch ID should fail closed and still write artifacts",
        expected_exit=2,
        changed_files="omni/model/views/orders.view",
        server_mode="missing_branch",
    ),
    Scenario(
        name="bad_marker_base_url",
        description="PR marker with base_url should be rejected as a security violation",
        expected_exit=5,
        changed_files="docs/readme.md",
        marker={"base_url": "https://evil.example", "model_id": MODEL_ID, "branch_name": BRANCH_NAME},
    ),
    Scenario(
        name="exposures_available",
        description="dbt exposures should enrich the integrated report and reviewer summary",
        expected_exit=1,
        changed_files="omni/model/views/orders.view",
        config="""checks:
  dbt_exposures:
    enabled: true
    fail_on_unavailable: true
""",
    ),
    Scenario(
        name="exposures_partial",
        description="unmapped dashboard exposure records should produce an advisory coverage gap",
        expected_exit=1,
        changed_files="omni/model/views/orders.view",
        server_mode="exposures_partial",
        config="""checks:
  dbt_exposures:
    enabled: true
    fail_on_unavailable: true
""",
    ),
    Scenario(
        name="exposures_unavailable_warning",
        description="dbt exposures API failure should warn but not change contract failure policy",
        expected_exit=1,
        changed_files="omni/model/views/orders.view",
        server_mode="exposures_403",
        config="""checks:
  dbt_exposures:
    enabled: true
    fail_on_unavailable: false
""",
    ),
    Scenario(
        name="dbt_sync_success",
        description="production dbt deployment should refresh a shared connection once and revalidate every model",
        expected_exit=0,
        changed_files="models/marts/fct_orders.sql",
        model_ids=(MODEL_ID, SECOND_MODEL_ID),
        operation="dbt_sync",
        config="""deployment:
  dbt_sync:
    enabled: true
    poll_interval_seconds: 2
    timeout_seconds: 30
""",
    ),
    Scenario(
        name="dbt_sync_job_failure",
        description="failed Omni schema refresh should block the deployment with API exit code 4",
        expected_exit=4,
        changed_files="models/marts/fct_orders.sql",
        operation="dbt_sync",
        server_mode="sync_failed",
        config="""deployment:
  dbt_sync:
    enabled: true
    poll_interval_seconds: 2
    timeout_seconds: 30
""",
    ),
]


class FakeOmniState:
    def __init__(self, scenario: Scenario) -> None:
        self.mode = scenario.server_mode
        self.operation = scenario.operation
        self.model_ids = scenario.model_ids
        self.requests: list[dict[str, Any]] = []
        self.refreshed = False


def make_handler(state: FakeOmniState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            state.requests.append({"method": "GET", "path": parsed.path, "query": query})
            try:
                payload, status = route_request(state, "GET", parsed.path, query)
            except Exception as exc:  # noqa: BLE001 - simulation server should surface unexpected behavior.
                payload, status = {"error": str(exc)}, 500
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            state.requests.append({"method": "POST", "path": parsed.path, "query": query})
            try:
                payload, status = route_request(state, "POST", parsed.path, query)
            except Exception as exc:  # noqa: BLE001 - simulation server should surface unexpected behavior.
                payload, status = {"error": str(exc)}, 500
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

    return Handler


def route_request(
    state: FakeOmniState,
    method: str,
    path: str,
    query: dict[str, list[str]],
) -> tuple[Any, int]:
    if method == "GET" and path == "/api/v1/models":
        records = []
        requested_model_ids = query.get("modelId", [])
        requested_connection_ids = query.get("connectionId", [])
        if requested_model_ids or requested_connection_ids:
            records.extend(
                {
                    "id": model_id,
                    "baseModelId": model_id,
                    "connectionId": "connection-1",
                    "modelKind": "SHARED",
                    "name": f"Simulated {model_id}",
                }
                for model_id in state.model_ids
                if not requested_model_ids or model_id in requested_model_ids
            )
        elif state.mode != "missing_branch":
            records.extend(
                {
                    "id": branch_id_for(model_id),
                    "modelKind": "BRANCH",
                    "baseModelId": model_id,
                    "name": BRANCH_NAME,
                }
                for model_id in state.model_ids
            )
        return {"records": records, "pageInfo": {}}, 200
    if method == "GET" and path == "/api/v1/jobs/job-1/status":
        status = "FAILED" if state.mode == "sync_failed" else "COMPLETED"
        return {"job_type": "refresh_schema", "job_id": "job-1", "status": status}, 200
    for model_id in state.model_ids:
        if method == "POST" and path == f"/api/v1/models/{model_id}/refresh":
            state.refreshed = True
            return {"jobId": "job-1", "modelId": model_id, "status": "running"}, 200
        if method == "GET" and path == f"/api/v1/models/{model_id}/git":
            return {
                "modelPath": model_path_for(model_id),
                "baseBranch": "main",
                "gitServiceProvider": "github",
                "webUrl": "https://github.com/example-org/simulated",
                "branchPerPullRequest": True,
                "gitFollower": False,
                "requirePullRequest": True,
            }, 200
        if method == "GET" and path == f"/api/v1/models/{model_id}/validate":
            return [], 200
        if method == "GET" and path == f"/api/v1/models/{model_id}/yaml":
            if state.operation == "dbt_sync" and state.refreshed:
                files = SYNC_HEAD_FILES
            else:
                files = HEAD_FILES if query.get("branchId") == [branch_id_for(model_id)] else BASE_FILES
            return {"files": files, "checksums": {name: f"checksum-{name}" for name in files}}, 200
        if method == "GET" and path == f"/api/v1/models/{model_id}/content-validator":
            if query.get("find") == ["orders.revenue"] and query.get("find_type") == ["FIELD"]:
                return content_payload("Executive Revenue", "alice@example.com"), 200
            return {"content": []}, 200
    if method == "GET" and path == "/api/v1/content":
        return {"records": [{"identifier": "dash-1", "labels": [{"name": "Verified"}]}], "pageInfo": {}}, 200
    for model_id in state.model_ids:
        if method == "GET" and path == f"/api/v1/models/{model_id}/dbt-exposures":
            if state.mode == "exposures_403":
                return {"error": "forbidden"}, 403
            records = [
                {
                    "dashboard_identifier": "dash-1",
                    "deduplication_name": "executive_revenue",
                    "exposure": {
                        "name": "executive_revenue",
                        "label": "Executive Revenue",
                        "type": "dashboard",
                        "url": "https://omni.example/dashboards/dash-1",
                        "owner": {"name": "Alice", "email": "alice@example.com"},
                        "depends_on": ["ref('orders')"],
                    },
                }
            ]
            if state.mode == "exposures_partial":
                records.append({"dashboard_identifier": "dash-unmapped", "exposure": None})
            return {
                "records": records,
                "pageInfo": {"hasNextPage": False},
            }, 200
    return {"error": f"Unhandled fake Omni path: {path}"}, 404


def model_path_for(model_id: str) -> str:
    return "omni/model" if model_id == MODEL_ID else f"omni/{model_id}"


def branch_id_for(model_id: str) -> str:
    return BRANCH_ID if model_id == MODEL_ID else f"branch-{model_id.removeprefix('model-')}"


def content_payload(name: str, email: str) -> dict[str, Any]:
    return {
        "content": [
            {
                "document_id": "dash-1",
                "identifier": "dash-1",
                "type": "dashboard",
                "name": name,
                "url": "https://omni.example/dashboards/dash-1",
                "owner": {"name": "Alice", "email": email},
                "folder": {"name": "Leadership", "path": "/Executive/Leadership"},
                "labels": [{"name": "Executive"}],
                "queries_and_issues": [
                    {
                        "query_presentation_id": "query-1",
                        "query_name": "Revenue by Month",
                        "issues": [{"message": "Field orders.revenue was not found"}],
                    }
                ],
            }
        ]
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run OmniFlow alpha simulations against a fake Omni API")
    parser.add_argument("--scenario", choices=[scenario.name for scenario in SCENARIOS], action="append")
    parser.add_argument("--keep-workdirs", action="store_true")
    args = parser.parse_args(argv)
    wanted = set(args.scenario or [scenario.name for scenario in SCENARIOS])
    selected = [scenario for scenario in SCENARIOS if scenario.name in wanted]

    failures = []
    for scenario in selected:
        result = run_scenario(scenario, keep_workdir=args.keep_workdirs)
        print_result(result)
        if not result["passed"]:
            failures.append(result)
    if failures:
        print(f"\n{len(failures)} simulation(s) failed.", file=sys.stderr)
        return 1
    print(f"\nAll {len(selected)} simulation(s) passed.")
    return 0


def run_scenario(scenario: Scenario, *, keep_workdir: bool) -> dict[str, Any]:
    tmp_ctx = tempfile.TemporaryDirectory(prefix=f"omniflow-{scenario.name}-")
    tmp = Path(tmp_ctx.name)
    state = FakeOmniState(scenario)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        repo = tmp / "repo"
        repo.mkdir()
        setup_repo(repo, base_url=base_url, scenario=scenario)
        completed = run_omniflow(repo, scenario)
        artifacts = inspect_artifacts(repo)
        passed, errors = assert_result(scenario, completed.returncode, artifacts, state.requests)
        return {
            "name": scenario.name,
            "description": scenario.description,
            "passed": passed,
            "errors": errors,
            "exit_code": completed.returncode,
            "expected_exit": scenario.expected_exit,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "workdir": str(repo),
            "artifacts": artifacts,
            "requests": state.requests,
        }
    finally:
        server.shutdown()
        server.server_close()
        if keep_workdir:
            print(f"Kept workdir for {scenario.name}: {tmp}")
        else:
            tmp_ctx.cleanup()


def setup_repo(repo: Path, *, base_url: str, scenario: Scenario) -> None:
    run(["git", "init", "-q", "--initial-branch=main"], cwd=repo)
    run(["git", "config", "user.email", "omniflow@example.com"], cwd=repo)
    run(["git", "config", "user.name", "OmniFlow Simulation"], cwd=repo)
    (repo / ".omni").mkdir()
    write_json(
        repo / ".omni/flow.json",
        {
            "version": 1,
            "models": [
                {
                    "base_url": base_url,
                    "model_id": model_id,
                    "model_path": model_path_for(model_id),
                    "base_branch": "main",
                    "git_provider": "github",
                    "web_url": "https://github.com/example-org/simulated",
                }
                for model_id in scenario.model_ids
            ],
        },
    )
    for model_id in scenario.model_ids:
        model_root = repo / model_path_for(model_id)
        (model_root / "views").mkdir(parents=True)
        (model_root / "views/orders.view").write_text(BASE_FILES["views/orders.view"], encoding="utf-8")
    config = (
        scenario.config
        or """reporting:
  formats: [json, markdown, sarif, junit]
security:
  redaction_level: standard
"""
    )
    (repo / ".omniflow.yml").write_text(config, encoding="utf-8")
    run(["git", "add", "."], cwd=repo)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    if scenario.operation == "validate":
        run(["git", "checkout", "-q", "-b", BRANCH_NAME], cwd=repo)
    apply_changed_files(repo, scenario.changed_files)
    if scenario.operation == "dbt_sync":
        run(["git", "add", "."], cwd=repo)
        run(["git", "commit", "-q", "-m", "deploy dbt"], cwd=repo)
        event = {"ref": "refs/heads/main", "commits": []}
    else:
        event = {"pull_request": {"body": marker_body(scenario), "number": 1}}
    write_json(repo / "event.json", event)


def apply_changed_files(repo: Path, changed_files: str) -> None:
    for raw in changed_files.splitlines():
        path = raw.strip()
        if not path:
            continue
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.endswith(".sql"):
            target.write_text("select 1 as id\n", encoding="utf-8")
        elif path.endswith(".md"):
            target.write_text("# docs\n", encoding="utf-8")
        elif path.endswith(".view"):
            target.write_text(HEAD_FILES["views/orders.view"], encoding="utf-8")
        else:
            target.write_text("changed\n", encoding="utf-8")


def marker_body(scenario: Scenario) -> str:
    if not scenario.marker:
        return ""
    return f"<!-- omniflow-context {json.dumps(scenario.marker, separators=(',', ':'))} -->"


def run_omniflow(repo: Path, scenario: Scenario) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in (
        "OMNI_API_KEY",
        "OMNIFLOW_SYNC_API_KEY",
        "GITHUB_ACTIONS",
        "GITHUB_HEAD_REF",
        "GITHUB_BASE_REF",
        "GITHUB_EVENT_NAME",
        "GITHUB_REF_NAME",
        "GITHUB_REF_TYPE",
        "OMNIFLOW_CHANGED_FILES",
    ):
        env.pop(name, None)
    env["PYTHONPATH"] = str(SRC)
    env["GITHUB_EVENT_PATH"] = str(repo / "event.json")
    if scenario.operation == "dbt_sync":
        env.update(
            {
                "GITHUB_ACTIONS": "true",
                "GITHUB_EVENT_NAME": "push",
                "GITHUB_REF_NAME": "main",
                "GITHUB_REF_TYPE": "branch",
                "OMNIFLOW_SYNC_API_KEY": "simulation-sync-secret",  # pragma: allowlist secret
            }
        )
        command = [
            sys.executable,
            "-m",
            "omniflow.cli",
            "dbt",
            "sync",
            "--auto",
            "--config",
            ".omniflow.yml",
        ]
    else:
        env.update(
            {
                "GITHUB_HEAD_REF": BRANCH_NAME,
                "GITHUB_BASE_REF": "main",
                "GITHUB_EVENT_NAME": "pull_request",
                "OMNIFLOW_CHANGED_FILES": scenario.changed_files,
            }
        )
        command = [sys.executable, "-m", "omniflow.cli", "run", "--auto", "--config", ".omniflow.yml"]
    if scenario.include_api_key and scenario.operation == "validate":
        env["OMNI_API_KEY"] = "simulation-secret"  # pragma: allowlist secret
    return subprocess.run(
        command,
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def inspect_artifacts(repo: Path) -> dict[str, Any]:
    root = repo / ".omniflow"
    artifacts: dict[str, Any] = {"exists": root.exists(), "files": []}
    if not root.exists():
        return artifacts
    artifacts["files"] = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())
    for relative in (
        "report.json",
        "public/report.json",
        "public/report.md",
        "public/dbt-sync.json",
        "artifact-manifest.json",
    ):
        path = root / relative
        if path.exists():
            artifacts[relative] = path.read_text(encoding="utf-8")
            if relative.endswith(".json"):
                try:
                    artifacts[f"{relative}:json"] = json.loads(artifacts[relative])
                except json.JSONDecodeError:
                    pass
    return artifacts


def assert_result(
    scenario: Scenario,
    exit_code: int,
    artifacts: dict[str, Any],
    requests: list[dict[str, Any]],
) -> tuple[bool, list[str]]:
    errors = []
    if exit_code != scenario.expected_exit:
        errors.append(f"expected exit {scenario.expected_exit}, got {exit_code}")
    if not artifacts.get("exists"):
        errors.append("missing .omniflow artifacts")
    if "public/report.json" not in artifacts.get("files", []):
        errors.append("missing public/report.json")
    public_text = artifacts.get("public/report.json", "")
    public_blob = public_text + artifacts.get("public/report.md", "") + artifacts.get("public/dbt-sync.json", "")
    if (
        "simulation-secret" in public_blob
        or "simulation-sync-secret" in public_blob
        or "alice@example.com" in public_blob
        or "https://omni.example/dashboards" in public_blob
    ):
        errors.append("public report leaked secret/email/dashboard URL")
    if scenario.name == "strict_redaction" and "Executive Revenue" in public_text:
        errors.append("strict public report leaked content name")
    if scenario.name == "bad_marker_base_url" and "security policy violation" not in public_text.lower():
        errors.append("bad marker scenario did not report security policy violation")
    if scenario.name == "skip_non_omni_pr":
        report = artifacts.get("public/report.json:json", {})
        if report.get("policy_decision") != "skipped":
            errors.append("non-Omni PR did not produce skipped policy decision")
    if scenario.name == "fork_non_omni_without_secret":
        report = artifacts.get("public/report.json:json", {})
        if report.get("policy_decision") != "skipped":
            errors.append("fork non-Omni PR did not skip without the Omni secret")
    if scenario.name == "fork_omni_without_secret":
        report = artifacts.get("public/report.json:json", {})
        if report.get("policy_decision") != "fail" or report.get("exit_code") != 2:
            errors.append("fork Omni PR did not fail closed without the Omni secret")
    if scenario.name == "multi_model_contract_failure":
        report = artifacts.get("public/report.json:json", {})
        if len(report.get("model_reports", [])) != 2 or len(report.get("models", [])) != 2:
            errors.append("multi-model run did not aggregate both selected model contexts")
    if scenario.name in {"exposures_available", "exposures_partial"}:
        report = artifacts.get("public/report.json:json", {})
        check_reports = report.get("model_reports", [{}])[0].get("check_reports", [])
        exposure_report = next(
            (item for item in check_reports if item.get("validator") == "dbt_exposures"),
            {},
        )
        summary = exposure_report.get("summary", {})
        expected_records = 2 if scenario.name == "exposures_partial" else 1
        expected_status = "partial" if scenario.name == "exposures_partial" else "available"
        if summary.get("total_records") != expected_records:
            errors.append("dbt exposure report did not retain the dashboard record count")
        if summary.get("total_exposures") != 1 or summary.get("coverage_status") != expected_status:
            errors.append("dbt exposure report did not record the expected coverage status")
        markdown = artifacts.get("public/report.md", "")
        if "## dbt Exposure Coverage" not in markdown or "mapped exposure(s)" not in markdown:
            errors.append("reviewer Markdown omitted successful dbt exposure coverage")
        if scenario.name == "exposures_partial" and not exposure_report.get("coverage_gaps"):
            errors.append("partial dbt exposure coverage did not produce a coverage gap")
    if scenario.operation == "dbt_sync":
        if "public/dbt-sync.json" not in artifacts.get("files", []):
            errors.append("dbt sync run did not emit public/dbt-sync.json")
        report = artifacts.get("public/report.json:json", {})
        sync = artifacts.get("public/dbt-sync.json:json", {})
        refresh_posts = [
            request
            for request in requests
            if request.get("method") == "POST" and str(request.get("path", "")).endswith("/refresh")
        ]
        if len(refresh_posts) != 1:
            errors.append(f"dbt sync expected one connection refresh, got {len(refresh_posts)}")
        refresh_index = requests.index(refresh_posts[0]) if refresh_posts else len(requests)
        if refresh_posts:
            pre_refresh_yaml_reads = [
                request
                for request in requests[:refresh_index]
                if request.get("method") == "GET" and str(request.get("path", "")).endswith("/yaml")
            ]
            if len(pre_refresh_yaml_reads) != len(scenario.model_ids):
                errors.append("dbt sync did not snapshot every affected model before the refresh POST")
        if scenario.name == "dbt_sync_success":
            if report.get("summary", {}).get("models_requested") != 2:
                errors.append("dbt sync did not retain both configured model validations")
            if report.get("summary", {}).get("refreshes_completed") != 1:
                errors.append("dbt sync did not deduplicate the shared connection refresh")
            if any(
                item.get("post_sync_validation_status") != "passed"
                for item in report.get("model_reports", [])
            ):
                errors.append("dbt sync did not complete post-refresh validation for every model")
            affected = (sync.get("models") or [{}])[0].get("affected_model_ids", [])
            if affected != [MODEL_ID, SECOND_MODEL_ID]:
                errors.append("dbt sync evidence omitted affected models")
            post_refresh_yaml_reads = [
                request
                for request in requests[refresh_index + 1 :]
                if request.get("method") == "GET" and str(request.get("path", "")).endswith("/yaml")
            ]
            if len(post_refresh_yaml_reads) < len(scenario.model_ids):
                errors.append("dbt sync did not compare post-refresh YAML for every affected model")
        if scenario.name == "dbt_sync_job_failure":
            statuses = [item.get("status") for item in sync.get("models", [])]
            if statuses != ["failed"]:
                errors.append("failed schema refresh did not retain normalized failure status")
    return not errors, errors


def print_result(result: dict[str, Any]) -> None:
    status = "PASS" if result["passed"] else "FAIL"
    print(f"[{status}] {result['name']}: {result['description']}")
    print(f"  exit: {result['exit_code']} expected: {result['expected_exit']}")
    print(f"  artifacts: {', '.join(result['artifacts'].get('files', [])[:8])}")
    for error in result["errors"]:
        print(f"  error: {error}")
    if not result["passed"]:
        print(f"  stdout: {result['stdout'][-1000:]}")
        print(f"  stderr: {result['stderr'][-1000:]}")
        print(f"  workdir: {result['workdir']}")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run(cmd: list[str], *, cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)


if __name__ == "__main__":
    raise SystemExit(main())
