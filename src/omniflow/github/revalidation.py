"""Fresh, current-head deployment readiness checks; never merge a pull request.

Run only from a protected workflow_dispatch checkout of the current base. GitHub
API state is reread before publication; the event used by normal validation is
assembled from the current PR response, never from an old workflow rerun.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from ..config import load_config
from ..discovery import _is_probable_omni_file, _is_under_model_path, discover_contexts, load_flow_metadata
from ..exceptions import ConfigError, OmniFlowError, SecurityPolicyError
from ..git import git_value
from ..revision_data import FULL_SHA, REPOSITORY, pull_request_changed_files
from ..security import redact

CHECK_NAME = "OmniFlow deployment readiness"
MAX_RESPONSE = 2 * 1024 * 1024


class GitHubRepository:
    def __init__(self, repository: str, token: str):
        if not REPOSITORY.fullmatch(repository) or not token:
            raise ConfigError("Deployment readiness requires a repository and GitHub token")
        api = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        parsed = urlparse(api)
        if parsed.scheme != "https" or not parsed.hostname or any(
            (parsed.username, parsed.password, parsed.query, parsed.fragment)
        ):
            raise SecurityPolicyError("GitHub API must use a trusted HTTPS origin")
        self.url = f"{api}/repos/{repository}"
        self.repository = repository
        self.headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"}

    def request(self, method: str, path: str, body: dict | None = None, *, max_bytes: int = MAX_RESPONSE):
        try:
            with requests.request(
                method, f"{self.url}{path}", headers=self.headers, json=body,
                timeout=30, allow_redirects=False, stream=True,
            ) as response:
                if response.status_code not in {200, 201, 204}:
                    raise ConfigError(f"GitHub readiness operation failed with HTTP {response.status_code}")
                value = bytearray()
                for chunk in response.iter_content(65536):
                    value.extend(chunk)
                    if len(value) > max_bytes:
                        raise SecurityPolicyError("GitHub response exceeds its configured size bound")
                return json.loads(value) if value else None
        except (requests.RequestException, ValueError) as exc:
            raise ConfigError("GitHub readiness operation returned unavailable or invalid data") from exc

    def pull_request(self, number: int) -> dict:
        pr = self.request("GET", f"/pulls/{number}")
        if not isinstance(pr, dict) or pr.get("state") != "open" or pr.get("draft") is not False:
            raise ConfigError("Revalidation requires an open, ready-for-review pull request")
        for side in ("head", "base"):
            data = pr.get(side)
            if not isinstance(data, dict) or not FULL_SHA.fullmatch(str(data.get("sha", ""))):
                raise ConfigError("Pull request must have exact head and base SHAs")
            repository = data.get("repo")
            if not isinstance(repository, dict) or repository.get("full_name") != self.repository:
                raise SecurityPolicyError("Deployment readiness only supports same-repository pull requests")
        if pr["base"].get("ref") != os.getenv("GITHUB_REF_NAME"):
            raise SecurityPolicyError("Revalidation checkout must target the pull request base branch")
        return pr

    def sync_sha(self) -> str:
        value = self.request("GET", "/actions/variables/OMNIFLOW_LAST_SYNC_SHA")
        sha = value.get("value") if isinstance(value, dict) else None
        if not isinstance(sha, str) or not FULL_SHA.fullmatch(sha):
            raise ConfigError("A durable exact OMNIFLOW_LAST_SYNC_SHA is required")
        return sha


def _same_snapshot(api: GitHubRepository, state_api: GitHubRepository, number: int, pr: dict, sync_sha: str):
    latest = api.pull_request(number)
    if any(latest[side]["sha"] != pr[side]["sha"] for side in ("head", "base")):
        raise ConfigError("Pull request head or base changed during validation; dispatch a fresh check")
    if state_api.sync_sha() != sync_sha:
        raise ConfigError("Deployment state changed during validation; dispatch a fresh check")


def _readiness_route(config, changed_files: list[str]) -> tuple[str, set[str], int]:
    """Prove applicability from trusted registrations and one immutable inventory."""
    from ..cli import _path_under_any

    flow = load_flow_metadata(missing_ok=True)
    models = flow["models"] if flow else []
    for path in changed_files:
        if _is_probable_omni_file(path) and not any(
            _is_under_model_path(path, model["model_path"]) for model in models
        ):
            raise ConfigError("Readiness cannot skip Omni files outside registered model paths")
    contexts = discover_contexts(
        auto=True, base_url=config.omni.base_url, model_id=config.omni.model_id,
        branch_name=config.omni.branch_name, branch_id=config.omni.branch_id,
        allow_skip=True, changed_files=changed_files,
    )
    selected = {context.model_id for context in contexts}
    affected = {model["model_id"] for model in models if any(
        _is_under_model_path(path, model["model_path"]) for path in changed_files
    )}
    if not affected.issubset(selected):
        raise ConfigError("Readiness routing does not cover every changed registered Omni model")
    dbt_count = sum(_path_under_any(path, config.breaking_change_hold.dbt_paths) for path in changed_files)
    if contexts:
        return "models", selected, dbt_count
    if dbt_count:
        if not config.dbt_impact.enabled:
            raise ConfigError("dbt-only readiness requires enabled dbt impact validation")
        return "dbt_impact", set(), dbt_count
    return "not_applicable", set(), 0


def _validate_report(report, code: int, head: str, route: str, model_ids: set[str], dbt_count: int):
    if not isinstance(report, dict) or code != 0 or report.get("exit_code") != 0:
        raise ConfigError("Current-head validation did not complete successfully")
    if report.get("git_sha") != head:
        raise ConfigError("Validation report does not identify the current PR head")
    models = report.get("models")
    if route == "models":
        if report.get("policy_decision") != "pass" or not isinstance(models, list) or not models or any(
            not isinstance(model, dict) or not isinstance(model.get("model_id"), str) for model in models
        ) or {model["model_id"] for model in models} != model_ids or report.get("operation") == "dbt_impact":
            raise ConfigError("Current-head validation did not pass every routed Omni model check")
    elif route == "dbt_impact":
        checks = report.get("model_reports")
        if report.get("policy_decision") != "pass" or report.get("operation") != "dbt_impact" or models != [] or (
            not isinstance(checks, list) or len(checks) != 1 or not isinstance(checks[0], dict)
            or checks[0].get("validator") != "dbt_impact" or checks[0].get("coverage_complete") is not True
            or checks[0].get("dbt_file_count") != dbt_count or checks[0].get("issues") != []
            or report.get("issues") != []
        ):
            raise ConfigError("dbt-only readiness requires complete, passing dbt impact evidence without findings")
    elif route != "not_applicable" or report.get("policy_decision") != "skipped" or models != [] or (
        report.get("issues") != [] or report.get("model_reports") != [] or report.get("operation") is not None
    ):
        raise ConfigError("Validation report does not match the proven non-applicable route")


def revalidate(number: int, config_path: str = ".omniflow.yml") -> int:
    if os.getenv("GITHUB_EVENT_NAME") != "workflow_dispatch" or not os.getenv("GITHUB_REF", "").startswith("refs/heads/"):
        raise SecurityPolicyError("Readiness requires an explicit protected-branch workflow dispatch")
    if number < 1:
        raise ConfigError("Pull request number must be positive")
    repository = os.getenv("GITHUB_REPOSITORY", "")
    token = os.getenv("OMNIFLOW_GITHUB_TOKEN", "")
    api = GitHubRepository(repository, token)
    pr = api.pull_request(number)
    check = api.request("POST", "/check-runs", {
        "name": CHECK_NAME, "head_sha": pr["head"]["sha"], "status": "in_progress",
        "output": {"title": "Fresh deployment revalidation", "summary": "Checking current durable deployment state."},
    })
    if not isinstance(check, dict) or not isinstance(check.get("id"), int):
        raise ConfigError("GitHub did not confirm the current-head check ID")
    check_path = f"/check-runs/{check['id']}"
    released_label = None
    try:
        state_api = GitHubRepository(repository, os.getenv("OMNIFLOW_SYNC_STATE_TOKEN", ""))
        if git_value("rev-parse", "HEAD") != pr["base"]["sha"]:
            raise ConfigError("Base checkout is stale; dispatch a new workflow at the current protected base")
        sync_sha = state_api.sync_sha()
        config = load_config(config_path)
        hold = config.breaking_change_hold
        if not hold.enabled or hold.action != "fail":
            raise ConfigError("Deployment readiness requires an enabled, failing breaking-change hold policy")
        event = {"number": number, "pull_request": pr, "repository": {"full_name": repository}}
        prior = os.environ.copy()
        with tempfile.TemporaryDirectory(prefix="omniflow-revalidate-") as directory:
            event_path = Path(directory) / "event.json"
            event_path.write_text(json.dumps(event), encoding="utf-8")
            os.chmod(event_path, 0o600)
            try:
                os.environ.update({
                    "GITHUB_EVENT_NAME": "pull_request_target", "GITHUB_EVENT_PATH": str(event_path),
                    "GITHUB_BASE_REF": pr["base"]["ref"], "GITHUB_HEAD_REF": pr["head"]["ref"],
                    "GITHUB_EVENT_NUMBER": str(number), "GITHUB_SHA": pr["base"]["sha"],
                    "OMNIFLOW_LAST_SYNC_SHA": sync_sha,
                })
                from ..cli import main as run_validation

                changed_files = pull_request_changed_files()
                if changed_files is None:
                    raise ConfigError("Readiness requires a complete immutable pull request inventory")
                route, model_ids, dbt_count = _readiness_route(config, changed_files)
                code = run_validation(["run", "--auto", "--config", config_path], changed_files=changed_files)
            finally:
                os.environ.clear()
                os.environ.update(prior)
        report = json.loads((Path(config.reporting.output_dir) / "public/report.json").read_text())
        _validate_report(report, code, pr["head"]["sha"], route, model_ids, dbt_count)
        _same_snapshot(api, state_api, number, pr, sync_sha)
        label = hold.pending_label
        if label in {item.get("name") for item in pr.get("labels", []) if isinstance(item, dict)}:
            released_label = label
            api.request("DELETE", f"/issues/{number}/labels/{quote(label, safe='')}")
        _same_snapshot(api, state_api, number, pr, sync_sha)
        api.request("PATCH", check_path, {
            "status": "completed", "conclusion": "success",
            "output": {"title": "Current-head deployment validation passed",
                       "summary": f"Validated {route} route at head {pr['head']['sha']} "
                                  f"against synchronized commit {sync_sha}. "
                                  "Manual merge remains subject to all required checks and reviews."},
        })
        return 0
    except Exception as exc:
        try:
            if released_label is not None:
                api.request("POST", f"/issues/{number}/labels", {"labels": [released_label]})
        finally:
            api.request("PATCH", check_path, {
                "status": "completed", "conclusion": "failure",
                "output": {"title": "Deployment readiness not established", "summary": redact(str(exc))[:1000]},
            })
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pull-request", type=int, required=True)
    parser.add_argument("--config", default=".omniflow.yml")
    args = parser.parse_args()
    try:
        return revalidate(args.pull_request, args.config)
    except (OmniFlowError, OSError, ValueError) as exc:
        print(redact(str(exc)))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
