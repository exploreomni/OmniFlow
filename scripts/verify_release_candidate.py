"""Require successful main-push workflows and jobs for the exact release commit."""

from __future__ import annotations

import json
import os
import re
import subprocess

REQUIRED_JOBS = {
    "test.yml": {"test (3.11)", "test (3.12)", "test (3.13)", "package", "action-install"},
    "dependency-scan.yml": {"pip-audit"},
    "actions-security.yml": {"zizmor"},
    "sast.yml": {"codeql"},
    "secret-scan.yml": {"gitleaks"},
}


def api(path: str, *, pages: bool = False):
    command = ["gh", "api", path]
    if pages:
        command.extend(["--paginate", "--slurp"])
    return json.loads(subprocess.check_output(command, text=True))


def verify(repo: str, sha: str, fetch=api) -> list[dict]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Expected canonical repository and full candidate commit SHA")
    if fetch(f"repos/{repo}/git/ref/heads/main")["object"]["sha"] != sha:
        raise ValueError("Release candidate must be the current main commit")
    evidence = []
    for workflow, required in REQUIRED_JOBS.items():
        pages = fetch(
            f"repos/{repo}/actions/workflows/{workflow}/runs?head_sha={sha}&event=push&per_page=100",
            pages=True,
        )
        runs = [
            run for page in pages for run in page["workflow_runs"]
            if run.get("head_sha") == sha and run.get("head_branch") == "main" and run.get("event") == "push"
        ]
        if not runs:
            raise ValueError(f"No main-push evidence for {workflow} at {sha}")
        run = max(runs, key=lambda item: item["id"])
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            raise ValueError(f"Latest {workflow} is not successful at the candidate SHA")
        job_pages = fetch(f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100", pages=True)
        jobs = {job["name"]: job for page in job_pages for job in page["jobs"]}
        for name in required:
            job = jobs.get(name, {})
            if job.get("status") != "completed" or job.get("conclusion") != "success":
                raise ValueError(f"Required job {name} is missing, skipped, or failed in {workflow}")
        evidence.append({"workflow": workflow, "run_id": run["id"], "url": run["html_url"], "sha": sha})
    return evidence


if __name__ == "__main__":
    try:
        result = verify(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"])
    except (KeyError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Release blocked: {exc}") from exc
    print(json.dumps({"candidate_sha": os.environ["GITHUB_SHA"], "required_workflows": result}, indent=2))
