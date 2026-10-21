"""Loopback-only fixture for the enabled composite Action path; no Omni tenant calls."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

HOST, PORT = "127.0.0.1", 18765
PROMPT = "CONFIDENTIAL-SYNTHETIC-EVAL-PROMPT"
BASE_YAML = "label: Synthetic eval base\n"
HEAD_YAML = "label: Synthetic eval candidate\n"


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def prepare(parent: Path) -> Path:
    """Create immutable synthetic PR data without touching the tooling checkout."""
    root = Path(tempfile.mkdtemp(prefix="omniflow-ai-consumer-", dir=parent)).resolve()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "OmniFlow fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / ".git/info/exclude").write_text(".omniflow/\n.fixture-*\n")
    (root / "omni").mkdir()
    (root / "omni/model.yaml").write_text(BASE_YAML)
    git(root, "add", "omni/model.yaml")
    git(root, "commit", "-m", "Trusted synthetic eval base")
    base = git(root, "rev-parse", "HEAD")
    git(root, "switch", "-c", "fixture-eval-head")
    (root / "omni/model.yaml").write_text(HEAD_YAML)
    (root / "untrusted.py").write_text("raise RuntimeError('PR head code must never execute')\n")
    git(root, "add", "omni/model.yaml", "untrusted.py")
    git(root, "commit", "-m", "Proposed synthetic eval change")
    head = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "--detach", base)
    repository = "fixture/consumer"
    event_path = root / ".fixture-event.json"
    event_path.write_text(json.dumps({"number": 5, "repository": {"full_name": repository, "private": False},
        "pull_request": {"number": 5, "body": "", "changed_files": 2,
            "base": {"sha": base, "ref": "main", "repo": {"full_name": repository}},
            "head": {"sha": head, "ref": "fixture-eval-head", "repo": {"full_name": repository}},
        }}))
    (root / ".fixture-state.json").write_text(json.dumps({"base": base, "head": head, "event": str(event_path)}))
    # GitHub reserves GITHUB_* variables. Set the synthetic event only inside
    # the test CLI process; the composite Action and installed package stay real.
    launcher_dir = root / ".fixture-bin"
    launcher_dir.mkdir()
    launcher = launcher_dir / "omniflow"
    launcher.write_text(
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "
        f"invoke {shlex.quote(str(root))} \"$@\"\n"
    )
    launcher.chmod(0o700)
    if os.getenv("GITHUB_PATH"):
        with Path(os.environ["GITHUB_PATH"]).open("a") as stream:
            stream.write(str(launcher_dir) + "\n")
    if os.getenv("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
            stream.write(f"workspace={root}\n")
    print(f"Prepared isolated synthetic eval consumer: {root}")
    return root


def invoke(root: Path, arguments: list[str]) -> int:
    state = json.loads((root / ".fixture-state.json").read_text())
    os.chdir(root)
    os.environ.update({
        "GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": state["event"],
        "GITHUB_BASE_REF": "main", "GITHUB_HEAD_REF": "fixture-eval-head", "GITHUB_SHA": state["base"],
        "GITHUB_REPOSITORY": "fixture/consumer",
    })
    from omniflow.cli import main
    from omniflow.diagnostics import capture_private_reports

    with capture_private_reports() as receipts:
        code = main([*arguments, "--model-path", "omni"])
    if receipts:
        fd = os.open(".fixture-private-reports.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(receipts, stream)
    return code


class Handler(BaseHTTPRequestHandler):
    runs: dict[str, dict] = {}
    requests: list[dict] = []
    events: list[dict] = []

    def log_message(self, *args):
        pass

    def reply(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path)
        if self.path == "/health":
            self.reply({"ready": True})
        elif self.path == "/fixture-evidence":
            self.reply({"requests": self.requests, "runs": self.runs, "events": self.events})
        elif path.path == "/api/v1/models/eval-smoke-model/yaml":
            params = parse_qs(path.query)
            branch = params.get("branchId", [None])[0]
            if branch not in (None, "eval-smoke-branch"):
                self.reply({"error": "unexpected synthetic branch"}, 400)
                return
            self.events.append({"kind": "yaml", "branch_id": branch, "params": params})
            self.reply({"files": {"model": HEAD_YAML if branch else BASE_YAML}})
        elif self.path == "/api/v1/ai/eval/prompt-sets/eval-smoke-set":
            self.reply(
                {
                    "prompt_set": {
                        "id": "eval-smoke-set",
                        "model_id": "eval-smoke-model",
                        "is_archived": False,
                        "prompts": [{"id": "synthetic-prompt-id", "prompt_text": PROMPT}],
                    }
                }
            )
        elif self.path.startswith("/api/v1/ai/eval/runs/") and self.path.rsplit("/", 1)[-1] in self.runs:
            self.events.append({"kind": "eval_result"})
            self.reply({"run": self.runs[self.path.rsplit("/", 1)[-1]]})
        else:
            self.reply({"error": "fixture route not found"}, 404)

    def do_POST(self):
        if self.path != "/api/v1/ai/eval/runs":
            self.reply({"error": "fixture route not found"}, 404)
            return
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(payload)
        branch_id = payload.get("run_config", {}).get("branch_id")
        self.events.append({"kind": "eval_start", "branch_id": branch_id})
        run_id = "eval-smoke-branch-run" if branch_id else "eval-smoke-main-run"
        self.runs[run_id] = {
            "id": run_id,
            "model_id": "eval-smoke-model",
            "prompt_set_id": payload["prompt_set_id"],
            "branch_id": branch_id,
            "status": "COMPLETE",
            "results": [
                {
                    "id": run_id + "-result",
                    "prompt": PROMPT,
                    "score": 0 if branch_id else 1,
                    "error_reason": None,
                    "agentic_job": {"state": "COMPLETE", "conversation_id": "private-id"},
                }
            ],
        }
        self.reply({"run": self.runs[run_id]}, 201)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "invoke":
        raise SystemExit(invoke(Path(sys.argv[2]).resolve(), sys.argv[3:]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "serve", "wait", "verify"))
    parser.add_argument("--parent", type=Path, default=Path(tempfile.gettempdir()))
    parser.add_argument("--workspace", type=Path)
    args = parser.parse_args()
    if args.operation == "prepare":
        prepare(args.parent.resolve())
        return
    if sys.argv[1] == "serve":
        HTTPServer((HOST, PORT), Handler).serve_forever()
    elif sys.argv[1] == "wait":
        for _ in range(30):
            try:
                with urlopen(f"http://{HOST}:{PORT}/health", timeout=1) as response:  # nosec B310
                    if response.status == 200:
                        return
            except URLError:
                time.sleep(0.1)
        raise SystemExit("Loopback eval fixture did not start")
    elif sys.argv[1] == "verify":
        if args.workspace is None:
            parser.error("verify requires --workspace")
        consumer = args.workspace.resolve()
        state = json.loads((consumer / ".fixture-state.json").read_text())
        root = consumer / ".omniflow"
        report = json.loads((root / "public/report.json").read_text())
        check = report["model_reports"][0]["check_reports"][0]
        if report.get("diagnostic_schema_version") != 2:
            raise SystemExit("Action did not emit minimal diagnostic schema 2")
        if report["policy_decision"] != "fail" or check["regressed_count"] != 1 or report["exit_code"] != 1:
            raise SystemExit("Enabled Action did not gate on the complete regression count")
        if check.get("validator") != "ai_eval" or not check.get("issues"):
            raise SystemExit("Enabled Action omitted the AI eval finding")
        receipts = json.loads((consumer / ".fixture-private-reports.json").read_text())
        private = receipts[-1]["model_reports"][0]
        proof = private["candidate_verification"]
        if proof.get("status") != "verified" or proof.get("head_sha") != state["head"] or proof.get("file_count") != 1:
            raise SystemExit("Action did not bind the candidate to the immutable synthetic PR head")
        if not report.get("validation_complete"):
            raise SystemExit("Action regression was not based on complete validation evidence")
        # Exact model/prompt-set/branch provenance is checked against actual
        # synthetic API requests, never reconstructed from public diagnostics.
        with urlopen(f"http://{HOST}:{PORT}/fixture-evidence", timeout=2) as response:  # nosec B310
            evidence = json.load(response)
        requests = evidence["requests"]
        if len(requests) != 2 or any(item.get("prompt_set_id") != "eval-smoke-set" for item in requests):
            raise SystemExit("Action did not request exactly the configured baseline/branch prompt set")
        if [item.get("run_config", {}).get("branch_id") for item in requests] != [None, "eval-smoke-branch"]:
            raise SystemExit("Action did not compare the expected main and branch scopes")
        if any(run.get("model_id") != "eval-smoke-model" for run in evidence["runs"].values()):
            raise SystemExit("Fixture returned unexpected model evidence")
        events = evidence["events"]
        yaml_reads = [(index, entry) for index, entry in enumerate(events) if entry["kind"] == "yaml"]
        if [entry["branch_id"] for _, entry in yaml_reads] != [None, "eval-smoke-branch", "eval-smoke-branch", None]:
            raise SystemExit("Action did not verify exact base/head and final head/base snapshots")
        starts = [index for index, entry in enumerate(events) if entry["kind"] == "eval_start"]
        results = [index for index, entry in enumerate(events) if entry["kind"] == "eval_result"]
        if len(starts) != 2 or not results or yaml_reads[1][0] >= starts[0] or yaml_reads[2][0] <= results[-1]:
            raise SystemExit("Source proof did not bracket the completed eval jobs")
        for _, entry in yaml_reads:
            params = entry["params"]
            if any(params.get(key) != [value] for key, value in (
                ("mode", "combined"), ("includeChecksums", "false"), ("fullyResolved", "false"),
            )):
                raise SystemExit("Action did not request complete authored YAML evidence")
        private_markers = (
            "CONFIDENTIAL", "private-id", "eval-smoke-model", "eval-smoke-branch",
            "eval-smoke-set", "synthetic-prompt-id", '"prompt_ids"', '"prompt_set_id"',
            '"model_id"', '"branch_id"', '"conversation_id"',
            state["base"], state["head"], "Synthetic eval base", "Synthetic eval candidate",
        )
        for name in ("report.json", "report.md", "report.sarif", "junit.xml"):
            for path in (root / name, root / "public" / name):
                if any(marker in path.read_text() for marker in private_markers):
                    raise SystemExit("Private eval content escaped to public output")
        if (root / "restricted").exists():
            raise SystemExit("Restricted eval detail was not cleaned")
        if git(consumer, "rev-parse", "HEAD") != state["base"] or (consumer / "untrusted.py").exists():
            raise SystemExit("Action changed the trusted checkout or exposed PR-head executable code")
        print("Enabled eval Action proved immutable sources, gated correctly and emitted private-safe public artifacts")
    else:
        raise SystemExit("Expected serve, wait, or verify")


if __name__ == "__main__":
    main()
