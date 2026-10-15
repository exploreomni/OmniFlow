"""Loopback-only fixture for the enabled composite Action path; no Omni tenant calls."""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

HOST, PORT = "127.0.0.1", 18765
PROMPT = "CONFIDENTIAL-SYNTHETIC-EVAL-PROMPT"


class Handler(BaseHTTPRequestHandler):
    runs: dict[str, dict] = {}
    requests: list[dict] = []

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
        if self.path == "/health":
            self.reply({"ready": True})
        elif self.path == "/fixture-evidence":
            self.reply({"requests": self.requests, "runs": self.runs})
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
        root = Path(".omniflow")
        report = json.loads((root / "public/report.json").read_text())
        check = report["model_reports"][0]["check_reports"][0]
        if report.get("diagnostic_schema_version") != 2:
            raise SystemExit("Action did not emit minimal diagnostic schema 2")
        if report["policy_decision"] != "fail" or check["regressed_count"] != 1 or report["exit_code"] != 1:
            raise SystemExit("Enabled Action did not gate on the complete regression count")
        if check.get("validator") != "ai_eval" or not check.get("issues"):
            raise SystemExit("Enabled Action omitted the AI eval finding")
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
        private_markers = (
            "CONFIDENTIAL", "private-id", "eval-smoke-model", "eval-smoke-branch",
            "eval-smoke-set", "synthetic-prompt-id", '"prompt_ids"', '"prompt_set_id"',
            '"model_id"', '"branch_id"', '"conversation_id"',
        )
        for name in ("report.json", "report.md", "report.sarif", "junit.xml"):
            for path in (root / name, root / "public" / name):
                if any(marker in path.read_text() for marker in private_markers):
                    raise SystemExit("Private eval content escaped to public output")
        if (root / "restricted").exists():
            raise SystemExit("Restricted eval detail was not cleaned")
        print("Enabled eval Action gated correctly and emitted private-safe public artifacts")
    else:
        raise SystemExit("Expected serve, wait, or verify")


if __name__ == "__main__":
    main()
