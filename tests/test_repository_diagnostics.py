import copy
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from omniflow import cli
from omniflow.artifacts import write_emergency_failure, write_public_reports
from omniflow.config import _to_config
from omniflow.diagnostics import public_diagnostic
from omniflow.discovery import ModelContext
from omniflow.exceptions import SecurityPolicyError
from omniflow.repository_diagnostics import (
    _load_snapshot,
    clear_repository_reports,
    finish_repository_context,
    publish_repository_output,
    record_candidate_diagnostics,
    repository_diagnostic_availability,
    repository_reporting,
    start_repository_context,
)
from omniflow.repository_policy import RepositoryPolicy

YAML = """dimensions:
  id:
    primary_key: true
    sql: secret_patient_literal
measures:
  total:
    sql: secret_card_literal
"""


@pytest.fixture
def repository(tmp_path, monkeypatch, request):
    monkeypatch.chdir(tmp_path)

    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Synthetic test")
    git("config", "user.email", "synthetic@example.invalid")
    (tmp_path / ".omni").mkdir()
    (tmp_path / "omni").mkdir()
    suffix = getattr(request, "param", "")
    source_path = tmp_path / f"omni/orders.view{suffix}"
    source_path.write_text(YAML)
    context = ModelContext(model_id="private-model", base_url="https://synthetic.omniapp.co",
                           model_path="omni", branch_id="candidate", branch_name="candidate")
    (tmp_path / ".omni/flow.json").write_text(json.dumps({"version": 1, "models": [vars(context)]}))
    git("add", ".")
    git("commit", "-m", "Synthetic base")
    base = git("rev-parse", "HEAD")
    head_yaml = ("# Head-only source line\n" if suffix else "") + YAML + "# unrelated change\n"
    source_path.write_text(head_yaml)
    git("add", ".")
    git("commit", "-m", "Synthetic head")
    head = git("rev-parse", "HEAD")
    policy = RepositoryPolicy(True, "repository_verified", "example/private", head, base)
    monkeypatch.setattr("omniflow.repository_diagnostics.resolve_repository_policy", lambda config: policy)

    class Client:
        def get_model_yaml(self, model_id, *, branch_id, **kwargs):
            return {"files": {"orders.view": head_yaml if branch_id else YAML}}

    return context, Client(), policy


def findings():
    return [
        {"validator": "semantic_lint", "rule_id": "require_measure_descriptions", "file": "orders.view",
         "field": "orders.total", "severity": "warning", "message": "secret_patient_literal"},
        {"validator": "model", "type": "model_validation_issue", "yaml_path": "orders.view,measures,total",
         "message": "secret_card_literal", "severity": "error", "state": "existing", "blocking": False},
        {"validator": "content", "type": "content_validation_issue", "issue_type": "dashboard_filter",
         "message": "secret_patient_literal", "filter_name": "patient-secret", "document_id": "private-doc",
         "severity": "error", "state": "new", "blocking": True},
    ]


def report(issues):
    return {"tool": "omniflow", "issues": issues, "summary": {"errors": 2, "warnings": 1, "total_issues": 3},
            "exit_code": 1, "policy_decision": "fail", "validation_complete": True}


def execute(context, client, issues):
    evidence = start_repository_context(context, client, branch_id="candidate")
    finish_repository_context(evidence, context, client, issues, branch_id="candidate")
    return evidence


def write(value, output_dir=".omniflow"):
    return write_public_reports(value, output_dir=output_dir,
                                formats=["json", "markdown", "sarif", "junit"], redaction_level="standard")


@pytest.mark.parametrize("side", ["base", "head"])
def test_mismatch_samples_are_private_observations_without_successful_candidate_proof(repository, side):
    from omniflow.candidate import CandidateVerificationError

    context, _, policy = repository
    issue = {"validator": "candidate", "type": "candidate_verification_failed", "severity": "error",
             "candidate_reason": "file_inventory_mismatch", "contents_comparison": "not_compared", "blocking": True}
    error = CandidateVerificationError("file_inventory_mismatch", candidate_samples=[{
        "git_path": "orders.view", "omni_path": "other/orders.view", "comparison": "not_compared", "pairing": "basename_hint",
    }])
    error.validation_scope = "base" if side == "base" else "branch"
    error.candidate_git_sha = policy.base_sha if side == "base" else policy.head_sha
    error.candidate_model_id = context.model_id
    error.candidate_branch_id = None if side == "base" else context.branch_id
    with repository_reporting(object(), output_dir=Path(".omniflow")):
        record_candidate_diagnostics(context, error, issue)
        safe = write({**report([issue]), "exit_code": 2, "validation_complete": False})
    private = json.loads(Path(".omniflow/repository/report.json").read_text())
    finding = private["issues"][0]
    assert finding["candidate_samples"][0]["git_path"] == "omni/orders.view"
    assert finding["candidate_samples"][0]["comparison"] == "not_compared"
    assert finding["candidate_evidence_kind"] == "observations_not_correspondence"
    assert finding["git_revision"] == error.candidate_git_sha
    assert private["exit_code"] == safe["exit_code"] == 2
    assert private["validation_complete"] is safe["validation_complete"] is False
    assert "candidate_samples" not in safe["issues"][0]
    assert "orders.view" not in json.dumps(safe)
    assert safe["repository_diagnostics"]["status"] == "generated"
    assert safe["repository_diagnostics"]["publication_status"] == "unconfirmed"


@pytest.mark.parametrize("binding", ["candidate_git_sha", "candidate_model_id", "candidate_branch_id"])
def test_mismatch_samples_require_registered_live_context_and_exact_revision(repository, binding):
    from omniflow.candidate import CandidateVerificationError

    context, _, policy = repository
    issue = {"validator": "candidate", "severity": "error"}
    error = CandidateVerificationError("file_inventory_mismatch", candidate_samples=[{"git_path": "orders.view"}])
    error.candidate_git_sha, error.candidate_model_id, error.candidate_branch_id = policy.head_sha, context.model_id, context.branch_id
    setattr(error, binding, "not-the-verified-target")
    with repository_reporting(object(), output_dir=Path(".omniflow")):
        record_candidate_diagnostics(context, error, issue)
        safe = write(report([issue]))
    assert not Path(".omniflow/repository/report.json").exists()
    assert safe["repository_diagnostics"]["status"] == "unavailable"


def test_private_default_enriches_separate_reports_not_public_or_annotations(repository, capsys, monkeypatch):
    context, client, _ = repository
    issues = findings()
    value = report(issues)
    before = copy.deepcopy(value)
    with repository_reporting(object(), output_dir=Path(".omniflow")):
        assert execute(context, client, issues) is not None
        safe = write(value)
    assert value == before
    detail = json.loads(Path(".omniflow/repository/report.json").read_text())
    assert detail["policy_decision"] == safe["policy_decision"] == "fail"
    assert detail["exit_code"] == safe["exit_code"] == 1
    assert detail["summary"] == safe["summary"]
    assert len(detail["issues"]) == len(safe["issues"]) == 3
    assert detail["issues"][0]["file"] == "omni/orders.view"
    assert detail["issues"][0]["line"] == 6
    assert detail["issues"][1]["file"] == "omni/orders.view"
    assert detail["issues"][1]["blocking"] is False
    assert "file" not in detail["issues"][2]
    for path in Path(".omniflow").rglob("*"):
        if path.is_file():
            text = path.read_text()
            for forbidden in ("secret_patient_literal", "secret_card_literal", "patient-secret", "private-doc"):
                assert forbidden not in text, path
            if path.parent.name != "repository":
                assert "orders.total" not in text and "omni/orders.view" not in text
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    cli._emit_github_annotations(safe["issues"], limit=20)
    assert "orders.total" not in capsys.readouterr().out
    markdown = Path(".omniflow/repository/report.md").read_text()
    assert "Repository-only diagnostics" in markdown and "line: `6`" in markdown
    sarif = json.loads(Path(".omniflow/repository/report.sarif").read_text())
    location = sarif["runs"][0]["results"][0]["locations"][0]["physicalLocation"]
    assert location["region"]["startLine"] == 6


def test_minimal_optout_removes_stale_files_and_never_reads_omni(repository, monkeypatch):
    context, client, _ = repository
    with repository_reporting(object(), output_dir=Path(".omniflow")):
        issues = findings()
        execute(context, client, issues)
        write(report(issues))
    monkeypatch.setattr("omniflow.repository_diagnostics.resolve_repository_policy",
                        lambda config: RepositoryPolicy(False, "minimal_requested"))
    monkeypatch.setattr(client, "get_model_yaml", lambda *args, **kwargs: pytest.fail("must not read Omni"))
    with repository_reporting(object(), output_dir=Path(".omniflow")):
        assert start_repository_context(context, client, branch_id="candidate") is None
        write(report(findings()))
    assert not Path(".omniflow/repository/report.json").exists()
    assert json.loads(Path(".omniflow/repository/status.json").read_text())["reason"] == "minimal_requested"


@pytest.mark.parametrize("change", ["snapshot", "scope", "model", "host"])
def test_unverified_mapping_stays_minimal_without_changing_decision(repository, change):
    context, client, _ = repository
    if change == "snapshot":
        client.get_model_yaml = lambda *args, **kwargs: {"files": {"orders.view": "wrong: snapshot"}}
    elif change == "scope":
        context.model_path = "."
    elif change == "model":
        context.model_id = "another-private-model"
    else:
        context.base_url = "https://another.omniapp.co"
    with repository_reporting(object(), output_dir=Path(".omniflow")):
        assert execute(context, client, findings()) is None
        safe = write(report(findings()))
    assert safe["exit_code"] == 1
    assert not Path(".omniflow/repository/report.json").exists()


def test_snapshot_drift_after_validation_withholds_locations(repository):
    context, client, _ = repository
    issues = findings()
    with repository_reporting(object(), output_dir=Path(".omniflow")):
        evidence = start_repository_context(context, client, branch_id="candidate")
        client.get_model_yaml = lambda *args, **kwargs: {"files": {"orders.view": "wrong: snapshot"}}
        finish_repository_context(evidence, context, client, issues, branch_id="candidate")
        write(report(issues))
    assert not Path(".omniflow/repository/report.json").exists()


def test_registry_bytes_must_match_immutable_tree_even_when_model_snapshot_matches(repository, monkeypatch):
    from omniflow import repository_diagnostics

    context, client, _ = repository
    original = repository_diagnostics._read_text

    def replaced(repository, sha, path, *, max_bytes):
        value = original(repository, sha, path, max_bytes=max_bytes)
        return value + " " if path == ".omni/flow.json" else value

    monkeypatch.setattr(repository_diagnostics, "_read_text", replaced)
    with repository_reporting(object(), output_dir=Path(".omniflow")):
        assert start_repository_context(context, client, branch_id="candidate") is None
        write(report(findings()))
    assert not Path(".omniflow/repository/report.json").exists()


def test_candidate_proofs_still_bind_reporting_aliases_to_fresh_omni_evidence(repository, monkeypatch):
    context, client, policy = repository
    proofs = {}
    for side in ("head", "base"):
        snapshot = _load_snapshot(policy, context.model_path, side)
        proofs[side] = {"status": "verified", "method": "authored_yaml_snapshot", f"{side}_sha": snapshot.sha,
                        "snapshot_digest": snapshot.digest, "file_count": len(snapshot.files)}
    original = client.get_model_yaml
    calls = []

    def observed(*args, **kwargs):
        calls.append(kwargs["branch_id"])
        return original(*args, **kwargs)

    monkeypatch.setattr(client, "get_model_yaml", observed)
    with repository_reporting(object(), output_dir=Path(".omniflow")):
        issues = findings()
        evidence = start_repository_context(context, client, branch_id="candidate", head_proof=proofs["head"],
                                            base_proof=proofs["base"])
        finish_repository_context(evidence, context, client, issues, branch_id="candidate",
                                  head_proof=proofs["head"], base_proof=proofs["base"])
        write(report(issues))
    assert Path(".omniflow/repository/report.json").exists()
    assert calls == ["candidate", None, "candidate", None]


@pytest.mark.parametrize("custom_source", [False, True])
@pytest.mark.parametrize("use_proofs", [False, True])
def test_default_filter_and_api_custom_sources_keep_diagnostics_consistent(
    repository, monkeypatch, custom_source, use_proofs,
):
    from omniflow.candidate import verify_candidate

    context, client, policy = repository
    Path("omni/tooling.yaml").write_text("not: model-source\n")
    Path("omni/README.md").write_text("Not an Omni source\n")
    if custom_source:
        Path("omni/custom.yaml").write_text("dimensions: {}\n")
    subprocess.run(["git", "add", "omni"], check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Synthetic scoped sources"], check=True, capture_output=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    policy = RepositoryPolicy(True, "repository_verified", policy.repository, head, policy.base_sha)
    monkeypatch.setattr("omniflow.repository_diagnostics.resolve_repository_policy", lambda config: policy)
    original_api = client.get_model_yaml

    def api(*args, **kwargs):
        payload = original_api(*args, **kwargs)
        if custom_source and kwargs.get("branch_id"):
            payload["files"]["custom.yaml"] = "dimensions: {}\n"
        return payload

    client.get_model_yaml = api
    proofs = {}
    if use_proofs:
        monkeypatch.setattr("omniflow.candidate.pull_request_revision",
                            lambda side: (policy.head_sha if side == "head" else policy.base_sha, policy.repository))
        proofs = {f"{side}_proof": verify_candidate(client, context, revision_side=side)
                  for side in ("head", "base")}
        assert proofs["head_proof"]["file_count"] == (2 if custom_source else 1)
    with repository_reporting(object(), output_dir=Path(".omniflow")):
        issues = findings()
        evidence = start_repository_context(context, client, branch_id="candidate", **proofs)
        assert evidence is not None and set(evidence.snapshots) == {"head", "base"}
        assert set(evidence.snapshots["head"].files) == (
            {"orders.view", "custom.yaml"} if custom_source else {"orders.view"}
        )
        finish_repository_context(evidence, context, client, issues, branch_id="candidate", **proofs)
        write(report(issues))
    detail = json.loads(Path(".omniflow/repository/report.json").read_text())
    assert detail["issues"][0]["file"] == "omni/orders.view"


@pytest.mark.parametrize("use_proofs", [False, True])
def test_custom_only_snapshot_can_be_verified_but_drift_withholds_details(repository, monkeypatch, use_proofs):
    from omniflow.candidate import verify_candidate

    context, client, policy = repository
    Path("omni/orders.view").rename("omni/custom.yaml")
    subprocess.run(["git", "add", "omni"], check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Synthetic custom source"], check=True, capture_output=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    policy = RepositoryPolicy(True, "repository_verified", policy.repository, head, policy.base_sha)
    monkeypatch.setattr("omniflow.repository_diagnostics.resolve_repository_policy", lambda config: policy)
    original_api = client.get_model_yaml

    def api(*args, **kwargs):
        payload = original_api(*args, **kwargs)
        if kwargs.get("branch_id"):
            payload["files"]["custom.yaml"] = payload["files"].pop("orders.view")
        return payload

    client.get_model_yaml = api
    proofs = {}
    if use_proofs:
        monkeypatch.setattr("omniflow.candidate.pull_request_revision",
                            lambda side: (policy.head_sha if side == "head" else policy.base_sha, policy.repository))
        proofs = {f"{side}_proof": verify_candidate(client, context, revision_side=side)
                  for side in ("head", "base")}
    with repository_reporting(object(), output_dir=Path(".omniflow")):
        evidence = start_repository_context(context, client, branch_id="candidate", **proofs)
        assert evidence is not None and set(evidence.snapshots["head"].files) == {"custom.yaml"}
        client.get_model_yaml = lambda *args, **kwargs: {"files": {"custom.yaml": "changed: source\n"}}
        # With no fresh final proof, independently changed API bytes cannot
        # authorize repository-only detail from the earlier snapshot.
        finish_repository_context(evidence, context, client, findings(), branch_id="candidate")
        write(report(findings()))
    assert not Path(".omniflow/repository/report.json").exists()


@pytest.mark.parametrize("repository", [".yaml", ".yml"], indirect=True)
@pytest.mark.parametrize("use_proofs", [False, True])
@pytest.mark.parametrize("api_directory", ["", "different/folder/"])
def test_serialized_git_paths_keep_head_base_locations_and_public_privacy(
    repository, monkeypatch, use_proofs, api_directory,
):
    from omniflow.candidate import verify_candidate

    context, client, policy = repository
    original_api = client.get_model_yaml

    def relocated_api(*args, **kwargs):
        payload = original_api(*args, **kwargs)
        return {**payload, "files": {api_directory + name: text for name, text in payload["files"].items()}}

    monkeypatch.setattr(client, "get_model_yaml", relocated_api)
    snapshots = {side: _load_snapshot(policy, context.model_path, side) for side in ("head", "base")}
    source_name = next(iter(snapshots["head"].files))
    assert source_name.endswith((".view.yaml", ".view.yml"))
    proofs = {}
    if use_proofs:
        monkeypatch.setattr("omniflow.candidate.pull_request_revision",
                            lambda side: (policy.head_sha if side == "head" else policy.base_sha, policy.repository))
        proofs = {f"{side}_proof": verify_candidate(client, context, revision_side=side)
                  for side in ("head", "base")}
        for side, snapshot in snapshots.items():
            assert proofs[f"{side}_proof"]["snapshot_digest"] == snapshot.digest
    issues = [
        {"validator": "model", "type": "model_validation_issue", "severity": "error",
         "yaml_path": "orders.view,measures,total,sql", "message": "secret_patient_literal"},
        {"validator": "model", "type": "model_validation_issue", "severity": "error",
         "yaml_path": "omni/orders.view,measures,total,sql", "state": "resolved", "active": False,
         "message": "secret_card_literal"},
    ]
    with repository_reporting(object(), output_dir=Path(".omniflow")):
        evidence = start_repository_context(context, client, branch_id="candidate", **proofs)
        assert evidence is not None and set(evidence.snapshots) == {"head", "base"}
        finish_repository_context(evidence, context, client, issues, branch_id="candidate", **proofs)
        safe = write(report(issues))
    detail = json.loads(Path(".omniflow/repository/report.json").read_text())
    assert [(item["file"], item["line"], item["source_revision"]) for item in detail["issues"]] == [
        (f"omni/{source_name}", 8, "head"), (f"omni/{source_name}", 7, "base"),
    ]
    assert "orders" not in json.dumps(safe)
    for path in Path(".omniflow").rglob("*"):
        if path.is_file():
            text = path.read_text()
            assert "secret_patient_literal" not in text and "secret_card_literal" not in text
            if path.parent.name != "repository":
                assert source_name not in text and "orders.total" not in text


@pytest.mark.parametrize("repository", [".yaml"], indirect=True)
@pytest.mark.parametrize("side", ["git", "api"])
def test_serialization_collisions_withhold_repository_details(repository, monkeypatch, side):
    context, client, policy = repository
    if side == "api":
        original = client.get_model_yaml

        def ambiguous(*args, **kwargs):
            payload = original(*args, **kwargs)
            payload["files"]["orders.view.yaml"] = payload["files"]["orders.view"]
            return payload

        monkeypatch.setattr(client, "get_model_yaml", ambiguous)
    else:
        Path("omni/orders.view").write_text(YAML)
        subprocess.run(["git", "add", "omni/orders.view"], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Synthetic ambiguous alias"], check=True, capture_output=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        changed = RepositoryPolicy(True, "repository_verified", policy.repository, head, policy.base_sha)
        monkeypatch.setattr("omniflow.repository_diagnostics.resolve_repository_policy", lambda config: changed)
    with repository_reporting(object(), output_dir=Path(".omniflow")):
        assert execute(context, client, findings()) is None
        safe = write(report(findings()))
    assert safe["exit_code"] == 1
    assert not Path(".omniflow/repository/report.json").exists()
    assert "orders" not in json.dumps(safe)


def test_deleted_and_resolved_findings_use_base_locations(repository):
    context, client, _ = repository
    issues = findings()[:1]
    issues[0].update(state="resolved", active=False)
    with repository_reporting(object(), output_dir=Path(".omniflow")):
        execute(context, client, issues)
        write(report(issues))
    detail = json.loads(Path(".omniflow/repository/report.json").read_text())
    assert detail["issues"][0]["source_revision"] == "base"
    assert detail["issues"][0]["active"] is False


def test_saved_enriched_report_cannot_reauthorize_details(repository):
    context, client, _ = repository
    with repository_reporting(object(), output_dir=Path(".omniflow")):
        issues = findings()
        execute(context, client, issues)
        write(report(issues))
    saved = json.loads(Path(".omniflow/repository/report.json").read_text())
    safe = write(saved)
    assert "orders.total" not in json.dumps(safe)
    assert not Path(".omniflow/repository/report.json").exists()


def test_unrelated_equal_dictionary_does_not_inherit_in_memory_authority(repository):
    context, client, _ = repository
    with repository_reporting(object(), output_dir=Path(".omniflow")):
        issues = findings()
        execute(context, client, issues)
        write(report(copy.deepcopy(issues)))
    detail = json.loads(Path(".omniflow/repository/report.json").read_text())
    assert detail["repository_diagnostics"]["mapped_findings"] == 0


def test_emergency_failure_clears_details(repository):
    context, client, _ = repository
    with repository_reporting(object(), output_dir=Path(".omniflow")):
        issues = findings()
        execute(context, client, issues)
        write(report(issues))
        write_emergency_failure(output_dir=".omniflow")
    assert not Path(".omniflow/repository/report.json").exists()
    assert "orders" not in Path(".omniflow/public/report.json").read_text()


def test_partial_detail_write_cleans_up_and_preserves_validation_result(repository, monkeypatch):
    context, client, _ = repository
    from omniflow.reporting import writer
    original = writer.write_reports

    def fail_detail(value, *, output_dir, formats):
        original(value, output_dir=output_dir, formats=formats)
        if Path(output_dir).name == "repository":
            raise ValueError("secret_patient_literal")

    monkeypatch.setattr(writer, "write_reports", fail_detail)
    with repository_reporting(object(), output_dir=Path(".omniflow")):
        issues = findings()
        execute(context, client, issues)
        safe = write(report(issues))
    assert safe["exit_code"] == 1
    assert not Path(".omniflow/repository/report.json").exists()
    assert "secret_patient_literal" not in Path(".omniflow/repository/status.json").read_text()


def test_symlink_cleanup_does_not_touch_target(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "target").write_text("preserve me")
    (tmp_path / ".omniflow/repository").mkdir(parents=True)
    (tmp_path / ".omniflow/repository/report.json").symlink_to(tmp_path / "target")
    with pytest.raises(SecurityPolicyError):
        clear_repository_reports(".omniflow")
    assert (tmp_path / "target").read_text() == "preserve me"


def test_public_projection_drops_forged_repository_markers():
    safe = public_diagnostic({"diagnostic_audience": "repository", "repository_diagnostic_schema_version": 1,
                              "issues": [{"file": "private.view", "line": 42, "field": "private.patient"}]})
    assert "private" not in json.dumps(safe)


@pytest.mark.parametrize("new_error", [False, True])
def test_full_cli_regression_result_identical_with_detail_and_optout(repository, monkeypatch, new_error):
    context, client, policy = repository
    config = _to_config({"checks": {"content_validation": {"enabled": False},
                                    "model_validation": {"enabled": True, "fail_on_new_only": True},
                                    "semantic_lint": {"enabled": False}},
                         "contracts": {"enabled": False}}, Path(".omniflow.yml"))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    monkeypatch.setenv("GITHUB_OUTPUT", str(Path("action-outputs").resolve()))
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "discover_contexts", lambda **kwargs: [context])
    monkeypatch.setattr(cli, "_client_and_branch_for_context", lambda *args, **kwargs: (client, "candidate"))
    monkeypatch.setattr("omniflow.candidate.pull_request_revision",
                        lambda side: (policy.head_sha if side == "head" else policy.base_sha, policy.repository))

    def validate(model_id, *, branch_id):
        current = [{"message": "existing synthetic error", "yaml_path": "orders.view,measures,total",
                    "is_warning": False}]
        if branch_id and new_error:
            current.append({"message": "new synthetic error", "yaml_path": "orders.view,dimensions,id",
                            "is_warning": False})
        return current

    client.validate_model = validate
    args = SimpleNamespace(config=".omniflow.yml", auto=True, skip_reason=None, model_path=None)
    codes = []
    summaries = []
    for mode in ("repository", "minimal"):
        if mode == "minimal":
            monkeypatch.setattr("omniflow.repository_diagnostics.resolve_repository_policy",
                                lambda config: RepositoryPolicy(False, "minimal_requested"))
        codes.append(cli.cmd_run(args))
        safe = json.loads(Path(".omniflow/public/report.json").read_text())
        summaries.append((safe["summary"], safe["policy_decision"], safe["issues"]))
        assert Path(".omniflow/repository/report.json").exists() is (mode == "repository")
        assert "orders.total" not in json.dumps(safe)
        assert not Path(".omniflow/restricted").exists()
    assert codes == [int(new_error), int(new_error)]
    # References are stable within each independently projected run; findings
    # and gate state must be identical regardless of output audience.
    assert summaries[0] == summaries[1]
    outputs = Path("action-outputs").read_text().splitlines()
    assert outputs == ["repository_diagnostics_ready=true", "repository_diagnostics_ready=false"]


def test_workflow_templates_never_upload_detail_based_only_on_disk_presence():
    root = Path(__file__).resolve().parents[1]
    import yaml

    for name in ("omniflow.yml", "omniflow-environments.yml"):
        workflow = yaml.safe_load((root / ".github/workflow-examples" / name).read_text())
        steps = workflow["jobs"]["omniflow"]["steps"]
        uploads = [step for step in steps if step["name"] == "Upload repository diagnostics"]
        assert len(uploads) == 1
        assert "repository-diagnostics-ready == 'true'" in uploads[0]["if"]
        assert "github.event.repository.private == true" in uploads[0]["if"]
        assert uploads[0]["with"]["retention-days"] == 7
        assert ".omniflow/repository/report.json" in uploads[0]["with"]["path"]
        for step in steps:
            if step["name"] in {"Comment on PR", "Upload SARIF"}:
                assert ".omniflow/repository/" not in json.dumps(step)


def test_generation_receipt_never_claims_successful_publication(repository, monkeypatch, capsys):
    context, client, _ = repository
    monkeypatch.setenv("GITHUB_OUTPUT", str(Path("action-outputs").resolve()))
    with repository_reporting(object(), output_dir=Path(".omniflow")):
        execute(context, client, findings())
        safe = write(report(findings()))
        assert repository_diagnostic_availability()["status"] == "generated"
        publish_repository_output()
    assert Path("action-outputs").read_text() == "repository_diagnostics_ready=true\n"
    assert safe["repository_diagnostics"]["publication_status"] == "unconfirmed"
    output = capsys.readouterr().out
    assert "generated on the runner" in output
    assert "upload is not yet confirmed" in output
    assert "are available in the separate repository artifact" not in output


def test_failed_readiness_handoff_does_not_advertise_an_upload(repository, monkeypatch, capsys):
    context, client, _ = repository
    # A directory cannot serve as the output file. No retry or alternate path.
    monkeypatch.setenv("GITHUB_OUTPUT", str(Path.cwd()))
    with repository_reporting(object(), output_dir=Path(".omniflow")):
        execute(context, client, findings())
        write(report(findings()))
        publish_repository_output()
    output = capsys.readouterr().out
    assert "readiness could not be handed to the workflow" in output
    assert "not a model defect" in output
    assert "are available" not in output
