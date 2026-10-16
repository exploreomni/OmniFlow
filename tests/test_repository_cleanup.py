import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from omniflow import cli
from omniflow.discovery import ModelContext
from omniflow.exceptions import ConfigError, SecurityPolicyError
from omniflow.repair.orchestrator import RepairOutcome
from omniflow.repository_diagnostics import REPORT_NAMES


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.startswith(("GITHUB_", "OMNI_", "OMNIFLOW_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("omniflow.github.revalidation.GitHubRepository.request",
                        Mock(side_effect=AssertionError("No live GitHub calls permitted")))
    return tmp_path


def seed(output_dir):
    directory = Path(output_dir) / "repository"
    directory.mkdir(parents=True)
    paths = [directory / name for name in (*REPORT_NAMES, "status.json")]
    for path in paths:
        path.write_text("synthetic-private-prior-run")
    (directory / "user-note.txt").write_text("user-owned file must remain")
    return paths


def policy(root, output_dir, *, repair=False):
    path = root / ".omniflow.yml"
    path.write_text(json.dumps({
        "reporting": {"output_dir": output_dir, "diagnostics": {"mode": "minimal"}},
        "repairs": {"ai": {"enabled": repair, "allow_query_execution": repair}},
    }))
    return path


def assert_cleared(paths):
    assert all(not path.exists() for path in paths)
    assert (paths[0].parent / "user-note.txt").read_text() == "user-owned file must remain"


def context():
    return ModelContext(base_url="https://synthetic.omni.example", model_id="synthetic-model",
                        model_path="omni/model", branch_name="synthetic-change", branch_id="synthetic-branch")


@pytest.mark.parametrize("output_dir", [".omniflow", "custom-evidence"])
def test_reused_minimal_route_clears_detail_when_it_selects_a_run(workspace, monkeypatch, output_dir, capsys):
    paths = seed(output_dir)
    config = policy(workspace, output_dir)
    monkeypatch.setattr(cli, "discover_contexts", Mock(return_value=[context()]))
    assert cli.main(["route", "--auto", "--config", str(config), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["should_run"] is True
    assert_cleared(paths)
    assert not (Path(output_dir) / "public/report.json").exists()


@pytest.mark.parametrize("output_dir", [".omniflow", "custom-evidence"])
def test_successful_repair_clears_prior_detail_without_changing_result(workspace, monkeypatch, output_dir):
    paths = seed(output_dir)
    config = policy(workspace, output_dir, repair=True)
    monkeypatch.setenv("OMNIFLOW_GITHUB_TOKEN", "synthetic-test-token")
    monkeypatch.setattr(cli, "load_repair_event", Mock(return_value=object()))
    monkeypatch.setattr(cli, "discover_contexts", Mock(return_value=[context()]))
    monkeypatch.setattr(cli, "require_repair_api_key", Mock(return_value="synthetic-repair-key"))
    monkeypatch.setattr(cli, "_client_and_branch_for_context", Mock(return_value=(object(), "synthetic-branch")))
    monkeypatch.setattr(cli, "GitHubRepairAttemptGuard", Mock(return_value=object()))
    repair = Mock(return_value=RepairOutcome({
        "tool": "omniflow", "operation": "ai_repair_beta", "status": "not_needed", "issues": [],
        "policy_decision": "pass", "exit_code": 0,
    }, 0))
    monkeypatch.setattr(cli, "run_ai_repair", repair)
    assert cli.main(["repair", "ai", "--auto", "--config", str(config)]) == 0
    repair.assert_called_once()
    assert_cleared(paths)
    report = json.loads((Path(output_dir) / "public/repair.json").read_text())
    assert report["status"] == "not_needed"
    assert report["exit_code"] == 0


@pytest.mark.parametrize("output_dir", [".omniflow", "custom-evidence"])
def test_disabled_repair_clears_prior_detail_on_policy_failure(workspace, output_dir):
    paths = seed(output_dir)
    config = policy(workspace, output_dir)
    assert cli.main(["repair", "ai", "--auto", "--config", str(config)]) == 5
    assert_cleared(paths)
    report = json.loads((Path(output_dir) / "public/repair.json").read_text())
    assert report["status"] == "failed"
    assert report["exit_code"] == 5


@pytest.mark.parametrize("output_dir", [".omniflow", "custom-evidence"])
def test_repair_setup_failure_writer_cleans_custom_output(workspace, output_dir):
    paths = seed(output_dir)
    cli._write_repair_setup_failure_artifacts(output_dir=Path(output_dir), exc=ConfigError("synthetic failure"))
    assert_cleared(paths)
    report = json.loads((Path(output_dir) / "public/repair.json").read_text())
    assert report["exit_code"] == 2


def test_repair_invalid_policy_cli_cleans_default_output(workspace):
    paths = seed(".omniflow")
    config = workspace / ".omniflow.yml"
    config.write_text("unexpected: true\n")
    assert cli.main(["repair", "ai", "--auto", "--config", str(config)]) == 2
    assert_cleared(paths)


@pytest.mark.parametrize("guard", [["--unsafe-raw-output"], ["--history-out", ".omniflow/restricted/history.json"]])
def test_content_early_security_guards_clear_detail_before_raising(workspace, monkeypatch, guard):
    paths = seed(".omniflow")
    config = policy(workspace, ".omniflow")
    client = Mock(side_effect=AssertionError("Guard must fail before any Omni request"))
    monkeypatch.setattr(cli, "_client_and_branch", client)
    assert cli.main(["content", "validate", "--config", str(config), *guard]) == 5
    client.assert_not_called()
    assert_cleared(paths)


@pytest.mark.parametrize("bad_path", ["artifact-manifest.json", "repository/status.json", "public/evidence.json"])
def test_cleanup_preflights_every_expected_path_before_removing_any_detail(workspace, bad_path):
    paths = seed(".omniflow")
    protected = workspace / "must-remain.txt"
    protected.write_text("protected file")
    bad = Path(".omniflow") / bad_path
    bad.parent.mkdir(parents=True, exist_ok=True)
    if bad.exists():
        bad.unlink()
    bad.symlink_to(protected)
    with pytest.raises(SecurityPolicyError):
        cli._validate_run_output_layout(Path(".omniflow"))
    assert protected.read_text() == "protected file"
    assert all(path.exists() for path in paths)
    assert paths[0].read_text() == "synthetic-private-prior-run"


def test_cleanup_preflights_repository_file_types_before_removing_detail(workspace):
    paths = seed(".omniflow")
    paths[-1].unlink()
    paths[-1].mkdir()
    with pytest.raises(SecurityPolicyError):
        cli._validate_run_output_layout(Path(".omniflow"))
    assert paths[0].read_text() == "synthetic-private-prior-run"
