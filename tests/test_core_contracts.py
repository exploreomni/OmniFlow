import json
from types import SimpleNamespace
from unittest import mock

import pytest

from omniflow.artifacts import write_artifact_manifest, write_public_reports
from omniflow.cli import cmd_run
from omniflow.config import ContractSettings
from omniflow.contracts import evaluate_contracts
from omniflow.diff.diff_engine import diff_graphs
from omniflow.diff.semantic_graph import build_graph
from omniflow.discovery import ModelContext
from omniflow.downstream import generate_downstream_dependencies
from omniflow.exceptions import ConfigError, OmniAPIError, SecurityPolicyError
from omniflow.github.annotations import annotation_lines
from omniflow.security import public_safe, redact
from omniflow.validators.content import extract_issues, run_content_validation


def test_dotted_view_names_preserve_both_sources_in_the_diff():
    files = {
        "views/sales.orders.view.yaml": {"dimensions": {"id": {"type": "number"}}},
        "views/sales.customers.view.yaml": {"dimensions": {"id": {"type": "number"}}},
    }
    base = build_graph(files)
    head = build_graph({name: {"dimensions": {}} for name in files})
    deleted = [change for change in diff_graphs(base, head)["changes"] if change["type"] == "field_deleted"]
    assert {change["field"] for change in deleted} == {"sales.orders.id", "sales.customers.id"}
    assert {change["file"] for change in deleted} == set(files)


@pytest.mark.parametrize(
    "files",
    [
        {"one/orders.view": {}, "two/orders.view": {}},
        {"one.view": {"name": "orders"}, "two.view": {"name": "orders"}},
        {"model.yaml": {"topics": {"orders": {}}}, "orders.topic": {}},
        {"orders.view": {"dimensions": {"id": {}}, "measures": {"id": {}}}},
        {"orders.view": {"dimensions": [{"name": "id"}, {"name": "id"}]}},
    ],
)
def test_ambiguous_semantic_identities_fail_before_overwriting(files):
    with pytest.raises(ConfigError, match="[Aa]mbiguous|[Dd]uplicate"):
        build_graph(files)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"unexpected": []},
        {"content": None},
        {"content": [None]},
        {"content": [{"queries_and_issues": {}}]},
        {"content": [{"queries_and_issues": [None]}]},
        {"content": [{"queries_and_issues": [{"issues": None}]}]},
        {"content": [{"queries_and_issues": [{"issues": [42]}]}]},
        {"content": [{"dashboard_filter_issues": "broken"}]},
        {"issues": None},
        {"issues": [False]},
    ],
)
def test_unusable_content_evidence_is_an_api_error(payload):
    with pytest.raises(OmniAPIError, match="response"):
        extract_issues(payload)


@pytest.mark.parametrize("payload", [{"content": []}, {"issues": []}, []])
def test_known_empty_content_shapes_remain_valid(payload):
    assert extract_issues(payload) == []


def test_malformed_document_cannot_disappear_during_label_filtering(tmp_path):
    client = SimpleNamespace(
        validate_content=lambda *args, **kwargs: {"content": [None]},
        list_content=lambda **kwargs: [{"identifier": "allowed"}],
    )
    with pytest.raises(OmniAPIError):
        run_content_validation(
            client=client,
            model_id="model",
            branch_id=None,
            user_id=None,
            include_personal_folders=False,
            labels=["Verified"],
            fail_on_new_only=False,
            history_in=tmp_path / "history.json",
            history_out=tmp_path / "history.json",
            report_out=tmp_path / "report.json",
        )
    assert not (tmp_path / "report.json").exists()


def test_malformed_targeted_response_becomes_a_failing_coverage_gap():
    client = SimpleNamespace(search_content_references=lambda *args, **kwargs: {"content": [None]})
    diff = {"changes": [{"type": "field_deleted", "risk": "breaking", "field": "orders.id"}]}
    dependencies = generate_downstream_dependencies(
        client=client,
        model_id="model",
        branch_id="branch",
        diff_result=diff,
    )
    report, exit_code = evaluate_contracts(
        diff_result=diff,
        dependencies=dependencies,
        settings=ContractSettings(),
        model_id="model",
    )
    assert dependencies["generation_mode"] == "targeted_unavailable"
    assert report["summary"]["coverage_errors"] == 1
    assert exit_code == 1


def test_malformed_content_fails_cli_and_cleans_restricted_artifacts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = ModelContext(base_url="https://omni.example", model_id="model", model_path="omni/model")
    args = SimpleNamespace(
        config=None,
        auto=True,
        skip_reason=None,
        base_url=None,
        model_id=None,
        model_path=None,
        branch_id=None,
        branch_name=None,
        user_id=None,
        include_personal_folders=None,
    )

    def context_run(*, output_dir, **kwargs):
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "private.json").write_text("synthetic-private-data", encoding="utf-8")
        extract_issues({"unexpected": []})
        return {"issues": []}, 0

    with mock.patch("omniflow.cli.discover_contexts", return_value=[context]):
        with mock.patch("omniflow.cli._run_context", side_effect=context_run):
            assert cmd_run(args) == 4
    report = json.loads((tmp_path / ".omniflow/public/report.json").read_text())
    assert report["policy_decision"] == "fail"
    assert not (tmp_path / ".omniflow/restricted").exists()


@pytest.mark.parametrize("level", ["standard", "strict"])
def test_active_credentials_and_data_fields_do_not_reach_public_formats(tmp_path, monkeypatch, level):
    secrets = {
        "OMNI_API_KEY": "synthetic-shared",
        "OMNIFLOW_SYNC_API_KEY": "synthetic-shared-sync",
        "OMNIFLOW_REPAIR_API_KEY": "synthetic-repair",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    secrets["OMNIFLOW_SYNC_STATE_TOKEN"] = "synthetic-sensitive-sync-state-value"
    monkeypatch.setenv("OMNIFLOW_SYNC_STATE_TOKEN", secrets["OMNIFLOW_SYNC_STATE_TOKEN"])
    raw = " ".join(secrets.values())
    assert all(value not in redact(raw) for value in secrets.values())
    report = {
        "tool_version": "test",
        "summary": {},
        "issues": [
            {
                "validator": "ai_eval",
                "severity": "error",
                "message": raw,
                "prompt": "synthetic-patient-question",
                "branch_error": "synthetic-query-answer",
                "conversation_id": "synthetic-conversation",
                "query_results": [["synthetic-row"]],
            }
        ],
    }
    safe = write_public_reports(
        report, output_dir=tmp_path, formats=["json", "markdown", "sarif", "junit"], redaction_level=level
    )
    output = "\n".join(path.read_text() for path in (tmp_path / "public").iterdir())
    output += "\n".join(annotation_lines(safe["issues"]))
    for value in [
        *secrets.values(),
        "synthetic-patient-question",
        "synthetic-query-answer",
        "synthetic-conversation",
        "synthetic-row",
    ]:
        assert value not in output
    assert public_safe({"message": "x" * 5000})["message"].endswith("…")


def test_manifest_inventories_outputs_without_disclosing_restricted_paths(tmp_path):
    for relative in [
        "public/dbt-impact.json",
        "public/report.json",
        "restricted/customer-sensitive-id/ai-eval-detail.json",
        "restricted/customer-sensitive-id/history.json",
        "restricted/customer-sensitive-id/yaml-head/private-table.view",
    ]:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    write_artifact_manifest(output_dir=tmp_path, restricted_artifacts_enabled=False, redaction_level="strict")
    manifest_text = (tmp_path / "artifact-manifest.json").read_text()
    manifest = json.loads(manifest_text)
    assert "public/dbt-impact.json" in manifest["public_artifacts"]
    assert manifest["restricted_inventory"]["file_count"] == 3
    assert manifest["restricted_cleanup"]["status"] == "pending"
    assert "customer-sensitive-id" not in manifest_text
    assert "private-table" not in manifest_text


def test_manifest_distinguishes_absent_from_retained_and_rejects_symlinks(tmp_path):
    write_artifact_manifest(output_dir=tmp_path, restricted_artifacts_enabled=False, redaction_level="standard")
    manifest = json.loads((tmp_path / "artifact-manifest.json").read_text())
    assert manifest["restricted_cleanup"]["status"] == "absent"
    (tmp_path / "restricted").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(SecurityPolicyError):
        write_artifact_manifest(output_dir=tmp_path, restricted_artifacts_enabled=True, redaction_level="standard")
