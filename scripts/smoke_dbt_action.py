"""Prepare/verify a bounded consumer fixture for the real composite Action.

CI checks OmniFlow out into .omniflow-tooling, then runs --prepare from the empty
workspace root and invokes uses: ./.omniflow-tooling. The synthetic pull_request
event uses local Git discovery; pull_request_target metadata retrieval is covered
separately by contract-mocked tests. No remote operation or Omni key is needed.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def prepare(root: Path, event_path: Path):
    if (root / ".git").exists() or (root / "models").exists() or (root / ".omniflow.yml").exists():
        raise SystemExit("Fixture preparation requires an empty consumer workspace; checkout tooling into a subdirectory")
    git(root, "init", "-b", "main")
    (root / ".git/info/exclude").write_text(
        ".omniflow-tooling/\n.omniflow/\n.fixture-state.json\n.fixture-private-reports.json\n"
    )
    git(root, "config", "user.name", "OmniFlow fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "models").mkdir()
    (root / "omni/views").mkdir(parents=True)
    (root / ".omni").mkdir()
    (root / "models/orders.sql").write_text("select customer_id, order_total from raw.orders\n")
    (root / "omni/views/orders.view").write_text(
        "name: orders\nsql_table_name: orders\nfields:\n  customer_id:\n    sql: ${TABLE}.customer_id\n"
    )
    (root / ".omniflow.yml").write_text("checks:\n  dbt_impact:\n    enabled: true\n")
    (root / ".omni/flow.json").write_text(json.dumps({"version": 1, "models": [{
        "model_id": "fixture-model", "model_path": "omni", "base_url": "https://omni.example",
    }]}))
    git(root, "add", "models", "omni", ".omni", ".omniflow.yml")
    git(root, "commit", "-m", "Trusted fixture base")
    base = git(root, "rev-parse", "HEAD")
    git(root, "switch", "-c", "fixture-head")
    (root / "models/orders.sql").write_text("select customer_key, order_total from raw.orders\n")
    (root / ".omniflow.yml").write_text("checks:\n  dbt_impact:\n    enabled: false\n")
    (root / "untrusted.py").write_text("raise RuntimeError('PR head code must never execute')\n")
    git(root, "add", "models", ".omniflow.yml", "untrusted.py")
    git(root, "commit", "-m", "Proposed dbt rename with untrusted policy")
    head = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "--detach", base)
    repository = os.getenv("GITHUB_REPOSITORY", "fixture/consumer")
    event = {"number": 4, "repository": {"full_name": repository}, "pull_request": {
        "number": 4, "body": "", "changed_files": 3,
        "base": {"sha": base, "ref": "main", "repo": {"full_name": repository}},
        "head": {"sha": head, "ref": "fixture-head", "repo": {"full_name": repository}},
    }}
    event_path.write_text(json.dumps(event))
    (root / ".fixture-state.json").write_text(json.dumps({"base": base, "head": head, "event": str(event_path)}))
    # GitHub reserves GITHUB_* variables: step env is not a reliable event
    # override. This test-only launcher sets synthetic context inside the CLI
    # process; the real composite steps and installed package remain unchanged.
    if os.getenv("GITHUB_PATH"):
        launcher_dir = Path(tempfile.mkdtemp(prefix="omniflow-dbt-fixture-", dir=os.environ["RUNNER_TEMP"]))
        launcher = launcher_dir / "omniflow"
        launcher.write_text(
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} invoke \"$@\"\n"
        )
        launcher.chmod(0o700)
        with Path(os.environ["GITHUB_PATH"]).open("a") as stream:
            stream.write(str(launcher_dir) + "\n")
    output = os.getenv("GITHUB_OUTPUT")
    values = f"base_sha={base}\nhead_sha={head}\nevent_path={event_path}\n"
    if output:
        with Path(output).open("a") as stream:
            stream.write(values)
    print(values)


def verify(root: Path):
    state = json.loads((root / ".fixture-state.json").read_text())
    report = json.loads((root / ".omniflow/public/report.json").read_text())
    impact = json.loads((root / ".omniflow/public/dbt-impact.json").read_text())
    assert report["diagnostic_schema_version"] == 2
    assert report["policy_decision"] == "fail"
    assert report["operation"] == "dbt_impact"
    assert report["exit_code"] == 1 and impact["coverage_complete"] is True
    assert any(issue.get("severity") == "error" for issue in report["issues"])
    # This receipt comes from the actual CLI invocation in this synthetic
    # fixture, not from public output (which is deliberately non-authoritative).
    receipts = json.loads((root / ".fixture-private-reports.json").read_text())
    assert len(receipts) == 1
    private = receipts[0]
    assert private["git_sha"] == state["head"]
    assert private["operation"] == "dbt_impact" and private["exit_code"] == 1
    assert any(issue.get("column") == "customer_id" for issue in private["issues"])
    for path in (root / ".omniflow").rglob("*"):
        if path.is_file():
            text = path.read_text()
            assert not any(marker in text for marker in (
                state["head"], state["base"], "customer_id", "fixture-model", "fixture-head",
                '"git_sha"', '"column"', '"model_id"', '"model_path"',
            )), f"Synthetic identity escaped to {path.name}"
    assert not (root / ".omniflow/restricted").exists()
    assert git(root, "rev-parse", "HEAD") == state["base"]
    assert not (root / "untrusted.py").exists()
    assert "enabled: true" in (root / ".omniflow.yml").read_text()
    assert not os.getenv("OMNI_API_KEY")
    print("Composite dbt fixture passed: exact proposed head, trusted base policy, no Omni credential")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "invoke":
        state = json.loads((Path.cwd() / ".fixture-state.json").read_text())
        os.environ.update({
            "GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": state["event"],
            "GITHUB_BASE_REF": "main", "GITHUB_HEAD_REF": "fixture-head", "GITHUB_SHA": state["base"],
        })
        from omniflow.cli import main
        from omniflow.diagnostics import capture_private_reports

        with capture_private_reports() as receipts:
            exit_code = main(sys.argv[2:])
        if receipts:
            # Test-only evidence, outside .omniflow and not uploaded by the
            # Action. Exclusive creation rejects a stale earlier receipt.
            fd = os.open(".fixture-private-reports.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(receipts, stream)
        raise SystemExit(exit_code)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "verify"))
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--event", type=Path)
    args = parser.parse_args()
    if args.operation == "prepare":
        if args.event is None:
            parser.error("prepare requires --event")
        prepare(args.workspace.resolve(), args.event.resolve())
    else:
        verify(args.workspace.resolve())
