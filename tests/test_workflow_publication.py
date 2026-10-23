"""Execute the shipped workflow receipt shell, without GitHub or Omni calls."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
NAMES = ("omniflow.yml", "omniflow-environments.yml")
URL = "https://github.com/example/analytics/actions/runs/123/artifacts/17"


def workflow(name="omniflow.yml"):
    return yaml.safe_load((ROOT / ".github/workflow-examples" / name).read_text())


def steps(name="omniflow.yml"):
    return next(iter(workflow(name)["jobs"].values()))["steps"]


def receipt_step(name="omniflow.yml"):
    return next(step for step in steps(name) if step.get("id") == "publication")


def execute_receipt(tmp_path, *, name="omniflow.yml", overrides=None, report=True):
    workspace = tmp_path / "workspace"
    runner = tmp_path / "runner"
    workspace.mkdir(exist_ok=True)
    runner.mkdir(exist_ok=True)
    public = workspace / ".omniflow" / "public"
    public.mkdir(parents=True, exist_ok=True)
    if report:
        (public / "report.md").write_text("# OmniFlow\nValidation decision: fail; one new model error.\n")
        (public / "report.json").write_text(json.dumps({
            "policy_decision": "fail", "exit_code": 1, "validation_complete": True,
            "summary": {"new_issues": 1},
            "issues": [{"validator": "model", "type": "model_validation_error", "severity": "error", "blocking": True}],
        }))
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"head": {"sha": "a" * 40}}}))
    # Checked-out material must never be used as a publication receipt/detail.
    (workspace / ".omniflow" / "publication.md").write_text("STALE_CHECKOUT_RECEIPT")
    private = workspace / ".omniflow" / "repository"
    private.mkdir(exist_ok=True)
    (private / "report.md").write_text("PRIVATE_SOURCE_IDENTIFIER_SENTINEL")
    env = {
        "PATH": os.environ["PATH"], "RUNNER_TEMP": str(runner),
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "example/analytics",
        "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
        "PUBLIC_UPLOAD_OUTCOME": "success", "PUBLIC_ARTIFACT_ID": "17", "PUBLIC_ARTIFACT_URL": URL,
        "REPOSITORY_UPLOAD_OUTCOME": "success", "REPOSITORY_ARTIFACT_ID": "17",
        "REPOSITORY_ARTIFACT_URL": URL, "REPOSITORY_READY": "true", "PRIVATE_REPOSITORY": "true",
        "DIAGNOSTIC_OUTPUT_READY": "true",
        "OMNIFLOW_REPORT_DIR": workflow(name)["env"]["OMNIFLOW_REPORT_DIR"],
        "GITHUB_EVENT_PATH": str(event), "TEST_PYTHON": sys.executable, "TRUSTED_PACKAGE_SRC": str(ROOT / "src"),
    }
    env.update(overrides or {})
    # Emulate the installed package without installing into the test interpreter.
    # The workflow itself must use isolated mode and never checkout Python code.
    installed_package = '''python() {
      [[ "$1" == -I && "$2" == -m && "$3" == omniflow.github.report_publisher ]] || return 90
      "$TEST_PYTHON" -I -c 'import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); sys.argv=["publisher",*sys.argv[1:]]; runpy.run_module("omniflow.github.report_publisher",run_name="__main__")' "$TRUSTED_PACKAGE_SRC" "${@:4}"
    }
'''
    result = subprocess.run(["bash", "-c", installed_package + receipt_step(name)["run"]], cwd=workspace,
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    output = Path(env["GITHUB_OUTPUT"]).read_text().splitlines()[-1]
    assert output.startswith("receipt_path=")
    receipt = Path(output.split("=", 1)[1])
    assert receipt.parent == runner and not receipt.is_symlink()
    assert receipt.name.startswith("omniflow-publication-123-2.")
    text = receipt.read_text()
    summary = Path(env["GITHUB_STEP_SUMMARY"]).read_text()
    assert "PRIVATE_SOURCE_IDENTIFIER_SENTINEL" not in text + summary
    assert "STALE_CHECKOUT_RECEIPT" not in text + summary
    return text, summary, receipt, env, workspace


@pytest.mark.parametrize("name", NAMES)
def test_receipt_runs_after_uploads_without_changing_privileges_or_failure_policy(name):
    all_steps = steps(name)
    by_id = {step.get("id"): step for step in all_steps}
    assert all_steps.index(by_id["publication"]) > all_steps.index(by_id["repository_upload"])
    assert all_steps.index(by_id["publication"]) > all_steps.index(by_id["public_upload"])
    assert all_steps.index(by_id["publication"]) > all_steps.index(by_id["impact_upload"])
    assert by_id["publication"]["if"] == "always()"
    assert all_steps.index(by_id["diagnostic_output"]) < next(
        i for i, step in enumerate(all_steps) if step["name"] == "Set up Python")
    assert "steps.diagnostic_output.outputs.ready == 'true'" in by_id["public_upload"]["if"]
    for upload in ("public_upload", "repository_upload"):
        assert not by_id[upload].get("continue-on-error", False)
        assert "publication" not in by_id[upload]["with"]["path"]
        assert "RUNNER_TEMP" not in by_id[upload]["with"]["path"]
        assert by_id[upload]["with"]["include-hidden-files"] is True
    private = by_id["repository_upload"]
    assert "repository-diagnostics-ready == 'true'" in private["if"]
    assert "github.event.repository.private == true" in private["if"]
    assert private["with"]["retention-days"] == 7
    assert private["with"]["if-no-files-found"] == "error"
    assert private["with"]["path"].replace("${{ env.OMNIFLOW_REPORT_DIR }}", ".omniflow").splitlines() == [
        ".omniflow/repository/report.json", ".omniflow/repository/report.md",
        ".omniflow/repository/report.sarif", ".omniflow/repository/junit.xml",
        ".omniflow/repository/status.json",
    ]
    impact = by_id["impact_upload"]
    assert impact["continue-on-error"] is True  # Advisory delivery cannot change the gate.
    assert "steps.diagnostic_output.outputs.ready == 'true'" in impact["if"]
    assert "content-impact-details-ready == 'true'" in impact["if"]
    assert "github.event.repository.private == true" in impact["if"]
    assert impact["with"]["retention-days"] == 7
    assert impact["with"]["if-no-files-found"] == "error"
    assert impact["with"]["include-hidden-files"] is True
    assert impact["with"]["path"].replace("${{ env.OMNIFLOW_REPORT_DIR }}", ".omniflow").splitlines() == [
        ".omniflow/repository/column-impact.json", ".omniflow/repository/column-impact.md",
    ]
    assert not by_id["omniflow"].get("continue-on-error", False)
    for step in all_steps:
        if step["name"] in {"Comment on PR", "Upload SARIF"}:
            assert ".omniflow/repository/" not in str(step)
            assert "steps.diagnostic_output.outputs.ready == 'true'" in step["if"]
    if name == "omniflow-environments.yml":
        assert workflow(name)["permissions"]["pull-requests"] == "read"
        assert not any(step["name"] == "Comment on PR" for step in all_steps)


def test_examples_share_identical_receipt_implementation():
    assert receipt_step(NAMES[0])["run"] == receipt_step(NAMES[1])["run"]
    assert receipt_step(NAMES[0])["env"] == receipt_step(NAMES[1])["env"]
    prepare = [next(step for step in steps(name) if step.get("id") == "diagnostic_output")["run"]
               for name in NAMES]
    assert prepare[0] == prepare[1]


@pytest.mark.parametrize("name", NAMES)
def test_confirmed_uploads_link_current_run_and_preserve_failed_validation(tmp_path, name):
    text, summary, *_ = execute_receipt(tmp_path, name=name)
    assert text.count("**uploaded**") == 2
    assert text.count(f"[Download artifact]({URL})") == 2
    assert "generation: **generated**" in text
    assert "OmniFlow: Action required" in summary
    assert "Blocking findings: 1" in summary
    assert summary.count("### Diagnostic publication") == 1
    assert "unconfirmed" not in summary
    assert "upload failure does not change the validation decision" in text


@pytest.mark.parametrize(("outcome", "expected"), [
    ("failure", "failed"), ("cancelled", "failed"), ("skipped", "skipped"),
    ("", "skipped"), ("unknown", "unconfirmed"),
])
def test_non_success_never_links_stale_success_outputs(tmp_path, outcome, expected):
    text, *_ = execute_receipt(tmp_path, overrides={
        "PUBLIC_UPLOAD_OUTCOME": outcome, "REPOSITORY_UPLOAD_OUTCOME": outcome,
    })
    assert text.count(f"publication: **{expected}**") == 2
    assert "[Download artifact]" not in text
    assert "generation: **generated**" in text


@pytest.mark.parametrize(("artifact_id", "artifact_url"), [
    ("", URL), ("17", ""), ("0", URL), ("not-an-id", URL),
    ("17", URL.replace("/runs/123/", "/runs/122/")),
    ("17", URL.replace("example/analytics", "another/repo")),
    ("17", "https://attacker.invalid/artifact"),
    ("17", URL + ")\nUNTRUSTED_MARKDOWN"),
])
def test_success_requires_valid_current_run_artifact_id_and_url(tmp_path, artifact_id, artifact_url):
    text, *_ = execute_receipt(tmp_path, overrides={
        "PUBLIC_ARTIFACT_ID": artifact_id, "PUBLIC_ARTIFACT_URL": artifact_url,
        "REPOSITORY_ARTIFACT_ID": artifact_id, "REPOSITORY_ARTIFACT_URL": artifact_url,
    })
    assert text.count("publication: **unconfirmed**") == 2
    assert "[Download artifact]" not in text
    assert "UNTRUSTED_MARKDOWN" not in text


@pytest.mark.parametrize(("ready", "private"), [("false", "true"), ("", "true"), ("true", "false")])
def test_ineligible_private_receipt_never_claims_private_upload(tmp_path, ready, private):
    text, *_ = execute_receipt(tmp_path, overrides={
        "REPOSITORY_READY": ready, "PRIVATE_REPOSITORY": private,
    })
    assert "Repository diagnostics publication: **skipped**" in text
    assert "Repository diagnostics generation: **generated**" not in text
    assert text.count("[Download artifact]") == 1  # The public upload only.


def test_missing_report_and_empty_success_outputs_do_not_imply_pass(tmp_path):
    text, summary, *_ = execute_receipt(tmp_path, report=False, overrides={
        "PUBLIC_ARTIFACT_ID": "", "PUBLIC_ARTIFACT_URL": "",
        "REPOSITORY_READY": "false", "REPOSITORY_UPLOAD_OUTCOME": "skipped",
    })
    assert "absence of a report is not a pass" in summary
    assert "Public summary publication: **unconfirmed**" in text
    assert "**uploaded**" not in text


def test_public_and_repository_uploads_have_independent_outcomes(tmp_path):
    text, summary, *_ = execute_receipt(tmp_path, overrides={"PUBLIC_UPLOAD_OUTCOME": "failure"})
    assert "Public summary publication: **failed**" in text
    assert "Repository diagnostics publication: **uploaded**" in text
    assert text.count("[Download artifact]") == 1
    assert "OmniFlow: Action required" in summary


def run_prepare(workspace, output, name="omniflow.yml"):
    script = next(step for step in steps(name) if step.get("id") == "diagnostic_output")["run"]
    return subprocess.run(["bash", "-c", script], cwd=workspace,
                          env={"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(output)},
                          capture_output=True, text=True)


@pytest.mark.parametrize("name", NAMES)
def test_preparation_removes_only_known_stale_files_before_bootstrap_failure(tmp_path, name):
    workspace = tmp_path / "workspace"
    public = workspace / ".omniflow" / "public"
    public.mkdir(parents=True)
    known = [public / file for file in ("report.json", "report.md", "report.sarif", "junit.xml", "evidence.json")]
    known.append(workspace / ".omniflow" / "artifact-manifest.json")
    for file in known:
        file.write_text("STALE_VALIDATION_PASS")
    unknown = public / "keep-user-file.txt"
    unknown.write_text("user-owned")
    result = run_prepare(workspace, tmp_path / "prepared", name)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "prepared").read_text() == "ready=true\n"
    assert all(not file.exists() for file in known)
    assert unknown.read_text() == "user-owned"
    # Simulate bootstrap stopping before producing any new report.
    _, summary, *_ = execute_receipt(tmp_path, name=name, report=False, overrides={
        "PUBLIC_ARTIFACT_ID": "", "PUBLIC_ARTIFACT_URL": "", "REPOSITORY_READY": "false",
    })
    assert "STALE_VALIDATION_PASS" not in summary
    assert "No fresh OmniFlow validation report" in summary
    assert "absence of a report is not a pass" in summary


@pytest.mark.parametrize("unsafe", ["root_symlink", "root_file", "public_symlink", "public_file",
                                     "report_symlink", "report_directory", "manifest_symlink"])
def test_preparation_rejects_unsafe_paths_before_any_deletion(tmp_path, unsafe):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = workspace / ".omniflow"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "report.md"
    sentinel.write_text("must survive")
    kept = []
    if unsafe == "root_symlink":
        root.symlink_to(outside, target_is_directory=True)
    elif unsafe == "root_file":
        root.write_text("must survive")
        kept.append(root)
    else:
        root.mkdir()
        manifest = root / "artifact-manifest.json"
        manifest.write_text("must survive")
        kept.append(manifest)
        public = root / "public"
        if unsafe == "public_symlink":
            public.symlink_to(outside, target_is_directory=True)
        elif unsafe == "public_file":
            public.write_text("must survive")
            kept.append(public)
        else:
            public.mkdir()
            first = public / "report.json"
            first.write_text("must survive")
            kept.append(first)
            if unsafe == "report_symlink":
                (public / "report.md").symlink_to(sentinel)
            elif unsafe == "report_directory":
                (public / "report.md").mkdir()
            else:
                manifest.unlink()
                kept.remove(manifest)
                manifest.symlink_to(sentinel)
    output = tmp_path / "prepared"
    result = run_prepare(workspace, output)
    assert result.returncode != 0
    assert "no fresh public report is authorized" in result.stdout
    assert not output.exists()
    assert sentinel.read_text() == "must survive"
    assert all(file.read_text() == "must survive" for file in kept)


def test_failed_preparation_never_reads_checked_out_report_or_links_stale_public_upload(tmp_path):
    text, summary, *_ = execute_receipt(tmp_path, overrides={
        "DIAGNOSTIC_OUTPUT_READY": "", "REPOSITORY_READY": "false",
    })
    assert "Validation decision:" not in summary
    assert "No fresh OmniFlow validation report" in summary
    assert "Public summary publication: **skipped**" in text
    assert "[Download artifact]" not in text


def test_pr_comment_uses_trusted_isolated_publisher_and_current_upload_outputs():
    comment_step = next(step for step in steps() if step["name"] == "Comment on PR")
    assert "python -I -m omniflow.github.report_publisher" in comment_step["run"]
    assert "cat " not in comment_step["run"]
    assert "gh pr" not in comment_step["run"]
    assert "PUBLICATION_RECEIPT" not in comment_step["env"]  # Never read stale checkout/runner receipt text.
    assert comment_step["env"]["PUBLIC_UPLOAD_OUTCOME"] == "${{ steps.public_upload.outcome }}"
    assert comment_step["env"]["REPOSITORY_ARTIFACT_URL"] == "${{ steps.repository_upload.outputs.artifact-url }}"
    assert "hashFiles(format('{0}/public/report.json', env.OMNIFLOW_REPORT_DIR))" in comment_step["if"]
    assert "github.event_name == 'pull_request_target'" in comment_step["if"]
