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
import subprocess
from pathlib import Path


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def prepare(root: Path, event_path: Path):
    if (root / ".git").exists() or (root / "models").exists() or (root / ".omniflow.yml").exists():
        raise SystemExit("Fixture preparation requires an empty consumer workspace; checkout tooling into a subdirectory")
    git(root, "init", "-b", "main")
    (root / ".git/info/exclude").write_text(".omniflow-tooling/\n.omniflow/\n.fixture-state.json\n")
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
    (root / ".fixture-state.json").write_text(json.dumps({"base": base, "head": head}))
    output = os.getenv("GITHUB_OUTPUT")
    values = f"base_sha={base}\nhead_sha={head}\nevent_path={event_path}\n"
    if output:
        with Path(output).open("a") as stream:
            stream.write(values)
    print(values)


def verify(root: Path):
    state = json.loads((root / ".fixture-state.json").read_text())
    report = json.loads((root / ".omniflow/public/report.json").read_text())
    assert report["policy_decision"] == "fail"
    assert report["operation"] == "dbt_impact"
    assert report["git_sha"] == state["head"]
    assert any(issue.get("column") == "customer_id" for issue in report["issues"])
    assert git(root, "rev-parse", "HEAD") == state["base"]
    assert not (root / "untrusted.py").exists()
    assert "enabled: true" in (root / ".omniflow.yml").read_text()
    assert not os.getenv("OMNI_API_KEY")
    print("Composite dbt fixture passed: exact proposed head, trusted base policy, no Omni credential")


if __name__ == "__main__":
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
