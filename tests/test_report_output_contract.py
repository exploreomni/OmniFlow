"""Machine evidence and optional-workflow freshness, without remote writes."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from omniflow.artifacts import write_public_reports
from omniflow.exceptions import ConfigError, SecurityPolicyError
from omniflow.github import report_publisher
from omniflow.reporting.output_lifecycle import finalize
from omniflow.reporting.output_paths import report_directory, require_matching_report_directory

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted((ROOT / ".github/workflow-examples").glob("*.yml"))


def decision(*, policy="pass", complete=True):
    return {"policy_decision": policy, "validation_complete": complete, "exit_code": 0, "issues": [],
            "decision_status": "complete" if complete else "incomplete"}


def write_report(path, report):
    path.mkdir(parents=True, exist_ok=True)
    (path / "report.json").write_text(json.dumps(report))


@pytest.mark.parametrize("formats", [["markdown"], ["sarif"], ["junit"], ["json", "markdown", "sarif", "junit"]])
def test_machine_json_is_required_fresh_and_public_safe(tmp_path, formats):
    report = decision()
    report.update(raw_response="PRIVATE_PAYLOAD", sql="SELECT PRIVATE_VALUES", model_id="PRIVATE_MODEL")
    output = tmp_path / "custom reports"
    write_report(output / "public", {"stale": "STALE_PASS"})
    write_public_reports(report, output_dir=output, formats=formats, redaction_level="strict")
    text = (output / "public/report.json").read_text()
    assert json.loads(text)["policy_decision"] == "pass"
    assert all(sentinel not in text for sentinel in ("PRIVATE_PAYLOAD", "PRIVATE_VALUES", "PRIVATE_MODEL", "STALE_PASS"))


@pytest.mark.parametrize("path", ["", ".", "../outside", "/tmp/out", "reports/../old", "reports/", "reports//x",
                                 ".git/reports", ".github/reports", ".omni/reports", "reports/*", "reports/[a]",
                                 "reports/@(a|b)", "~/reports", " report"])
def test_delivery_path_rejects_unsafe_or_ambiguous_paths(tmp_path, monkeypatch, path):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError):
        report_directory(path)


def test_custom_path_matches_trusted_config_and_rejects_symlink(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMNIFLOW_REPORT_DIR", "ci results/current")
    assert report_directory("ci results/current") == Path("ci results/current")
    require_matching_report_directory("ci results/current")
    with pytest.raises(ConfigError, match="must match"):
        require_matching_report_directory(".omniflow")
    (tmp_path / "alias").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(SecurityPolicyError):
        report_directory("alias/reports")


@pytest.mark.parametrize("path", [".omniflow", "evidence/custom", "ci results/current"])
def test_publisher_reads_only_explicit_report_directory(tmp_path, monkeypatch, capsys, path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DIAGNOSTIC_OUTPUT_READY", "true")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "event.json"))
    (tmp_path / "event.json").write_text("{}")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/analytics")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setattr(sys, "argv", ["publisher", "--summary", "--report-dir", path])
    write_report(tmp_path / path / "public", decision())
    assert report_publisher.main() == 0
    assert "OmniFlow: Validation passed" in capsys.readouterr().out
    (tmp_path / path / "public/report.json").unlink()
    if path != ".omniflow":
        write_report(tmp_path / ".omniflow/public", decision())
    assert report_publisher.main() == 1
    assert "OF-REPORT-MISSING" in capsys.readouterr().out


@pytest.mark.parametrize("payload", ['{"issues":[],"issues":[]}', '{"broken":', '[]'])
def test_malformed_machine_json_never_authorizes_finalization(tmp_path, monkeypatch, payload):
    monkeypatch.chdir(tmp_path)
    output = tmp_path / ".omniflow/public"
    output.mkdir(parents=True)
    (output / "report.json").write_text(payload)
    with pytest.raises(report_publisher.PublicationUnavailable):
        finalize(".omniflow", "success")


@pytest.mark.parametrize("status", ["failure", "cancelled"])
@pytest.mark.parametrize("policy", ["pass", "skipped"])
def test_final_operation_failure_invalidates_intermediate_pass_or_skip(tmp_path, monkeypatch, status, policy):
    monkeypatch.chdir(tmp_path)
    write_report(tmp_path / "results/public", decision(policy=policy))
    assert finalize("results", status)
    result = json.loads((tmp_path / "results/public/report.json").read_text())
    assert result["policy_decision"] == "fail"
    assert result["validation_complete"] is False
    assert result["decision_status"] == "incomplete" and result["validation_status"] == "failed"
    assert result["summary"]["errors"] == result["summary"]["blocking_issues"] == 1
    assert result["issues"][-1]["reason_code"] == "workflow_operation_incomplete"
    assert "Pass:" not in (tmp_path / "results/public/report.md").read_text()
    manifest = json.loads((tmp_path / "results/artifact-manifest.json").read_text())
    assert manifest["publication_status"] == "unconfirmed"
    assert {"public/report.json", "public/report.md", "public/report.sarif", "public/junit.xml"} <= set(
        manifest["public_artifacts"])


@pytest.mark.parametrize(("policy", "complete"), [("fail", True), ("advisory", False), ("pass", False)])
def test_final_failure_preserves_useful_current_failed_or_incomplete_evidence(tmp_path, monkeypatch, policy, complete):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "results/public"
    write_report(path, decision(policy=policy, complete=complete))
    original = (path / "report.json").read_bytes()
    assert finalize("results", "failure")
    assert (path / "report.json").read_bytes() == original


def test_missing_final_evidence_is_incomplete_and_skipped_operation_is_not_uploadable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert not finalize("results", "skipped")
    assert not (tmp_path / "results").exists()
    assert finalize("results", "success")
    result = json.loads((tmp_path / "results/public/report.json").read_text())
    assert result["validation_complete"] is False and result["exit_code"] != 0


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda item: item.name)
@pytest.mark.parametrize("directory", [".omniflow", "ci results/current"])
def test_every_example_prepares_exact_selected_directory_before_operations(tmp_path, example, directory):
    workflow = yaml.safe_load(example.read_text())
    steps = next(iter(workflow["jobs"].values()))["steps"]
    prepare = next(step for step in steps if step.get("id") == "diagnostic_output")
    assert steps.index(prepare) < next(i for i, step in enumerate(steps) if "OmniFlow@" in step.get("uses", ""))
    root = tmp_path / directory
    stale = []
    for folder, filename in [("", "report.json"), ("", "dbt-sync.json"), ("", "repair.json"),
                             ("public", "report.json"), ("public", "report.md"), ("repository", "status.json")]:
        file = root / folder / filename
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("STALE_PASS")
        stale.append(file)
    user_file = root / "keep.txt"
    user_file.write_text("keep")
    result = subprocess.run(["bash", "-c", prepare["run"]], cwd=tmp_path, capture_output=True, text=True,
                            env={"PATH": os.environ["PATH"], "OMNIFLOW_REPORT_DIR": directory,
                                 "GITHUB_OUTPUT": str(tmp_path / "prepared")})
    assert result.returncode == 0, result.stderr
    assert all(not file.exists() for file in stale)
    assert user_file.read_text() == "keep"
    uploads = [step for step in steps if "actions/upload-artifact@" in step.get("uses", "")]
    assert uploads and all("${{ env.OMNIFLOW_REPORT_DIR }}" in step["with"]["path"] for step in uploads)
    if "operation" in {step.get("id") for step in steps}:
        finalizer = next(step for step in steps if step.get("id") == "report_finalized")
        assert steps.index(finalizer) > next(i for i, step in enumerate(steps) if step.get("id") == "operation")
        assert all("steps.report_finalized.outputs.ready == 'true'" in step["if"] for step in uploads)
        assert "python -I -m omniflow.reporting.output_lifecycle" in finalizer["run"]
    if example.name == "omniflow-revalidate-held.yml":
        clean = next(step for step in steps if step.get("id") == "diagnostic_operation")
        assert prepare["run"] == clean["run"]
        assert steps.index(clean) > next(i for i, step in enumerate(steps) if step.get("id") == "bootstrap")


def test_sync_state_records_completed_shared_refresh_not_a_validation_pass():
    workflow = yaml.safe_load((ROOT / ".github/workflow-examples/omniflow-dbt-sync-with-release.yml").read_text())
    steps = next(iter(workflow["jobs"].values()))["steps"]
    record = next(step for step in steps if step.get("id") == "synchronized_state")
    assert record["if"] == ("always() && steps.deployment.outcome == 'success' && "
                            "steps.operation.outputs.dbt-sync-state-eligible == 'true'")
    assert "post-sync-validation-complete" not in record["if"]
