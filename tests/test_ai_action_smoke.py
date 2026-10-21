"""Local loopback qualification for the immutable consumer Action fixture."""
import importlib.util
import json
import os
import subprocess
import sys
from http.server import HTTPServer
from pathlib import Path
from threading import Thread

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("eval_action_smoke", ROOT / "scripts/smoke_ai_eval_action.py")
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def test_loopback_launcher_proves_immutable_pr_before_and_after_eval_jobs(monkeypatch, tmp_path):
    for name in ("GITHUB_PATH", "GITHUB_OUTPUT", "OMNIFLOW_REPORT_DIR", "OMNIFLOW_CHANGED_FILES"):
        monkeypatch.delenv(name, raising=False)
    smoke.Handler.runs = {}
    smoke.Handler.requests = []
    smoke.Handler.events = []
    try:
        server = HTTPServer((smoke.HOST, 0), smoke.Handler)
    except PermissionError:
        pytest.skip("Loopback binding is prohibited in this sandbox; rerun with permitted local networking")
    monkeypatch.setattr(smoke, "PORT", server.server_port)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        consumer = smoke.prepare(tmp_path)
        policy = tmp_path / "action-policy.yml"
        policy.write_text((ROOT / "tests/fixtures/ai_eval/action-policy.yml").read_text().replace(
            "127.0.0.1:18765", f"127.0.0.1:{server.server_port}",
        ))
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), GITHUB_ACTIONS="true",
                   OMNI_API_KEY="synthetic-eval-token", OMNIFLOW_GITHUB_TOKEN="")  # pragma: allowlist secret
        launcher = str(consumer / ".fixture-bin/omniflow")
        route = subprocess.run(
            [launcher, "route", "--auto", "--config", str(policy), "--format", "github"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
        )
        assert route.returncode == 0, route.stderr
        assert "should_run=true" in route.stdout and "requires_omni=true" in route.stdout
        validation = subprocess.run(
            [launcher, "run", "--auto", "--config", str(policy)],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
        )
        assert validation.returncode == 1, validation.stderr + validation.stdout
        monkeypatch.setattr(sys, "argv", [str(SPEC.origin), "verify", "--workspace", str(consumer)])
        smoke.main()
        assert (consumer / ".fixture-private-reports.json").stat().st_mode & 0o777 == 0o600
        # Deliberately weakening the source receipt must make the verifier fail,
        # even though the independent AI accuracy finding still has exit code 1.
        receipt_path = consumer / ".fixture-private-reports.json"
        receipts = json.loads(receipt_path.read_text())
        receipts[-1]["model_reports"][0]["candidate_verification"]["head_sha"] = "0" * 40
        receipt_path.write_text(json.dumps(receipts))
        with pytest.raises(SystemExit, match="immutable synthetic PR head"):
            smoke.main()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
