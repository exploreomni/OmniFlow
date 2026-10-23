import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from omniflow.artifacts import write_public_reports
from omniflow.cli import _content_impact_authorized, _published_impact_client, main
from omniflow.config import load_config
from omniflow.content_impact_reporting import (
    clear_content_impact,
    coverage_explanations,
    current_validation_report,
    public_content_impact,
    publish_content_impact,
    seal_validation_report,
)
from omniflow.diagnostics import public_diagnostic
from omniflow.discovery import ModelContext
from omniflow.exceptions import ConfigError, SecurityPolicyError
from omniflow.reporting.notification import render_pr_comment
from omniflow.repository_policy import RepositoryPolicy


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith(("GITHUB_", "OMNI_", "OMNIFLOW_")):
            monkeypatch.delenv(key)
    Path(".omniflow.yml").write_text(yaml.safe_dump({"checks": {"dbt_impact": {
        "enabled": True, "content_impact": {"enabled": True, "sql_dialect": "snowflake"},
    }}}))
    config = load_config()
    report = {"exit_code": 1, "policy_decision": "fail", "validation_complete": True,
              "summary": {"errors": 1}, "issues": [
                  {"validator": "dbt_impact", "severity": "error", "type": "field_deleted", "blocking": True}],
              "check_states": [{"validator": "dbt_impact", "status": "completed"}]}
    write_public_reports(report, output_dir=config.reporting.output_dir,
                         formats=config.reporting.formats, redaction_level="strict")
    seal_validation_report(config)
    return config


def _detail(status="partial"):
    return {"status": status, "coverage_complete": status == "complete", "scope": "published",
            "summary": {"dashboards": 2, "workbooks": 1, "queries": 4, "affected_fields": 30,
                        "dashboard_filters": 1},
            "coverage_gaps": [{"reason_code": "document_unavailable", "secret": "private-customer-value"}],
            "impacts": [{"dependency_path": ["private_table.amount", "private_view.net"],
                         "document_name": "Private dashboard", "owner": "owner@example.invalid"}]}


def test_projection_is_idempotent_and_drops_identity():
    safe = public_content_impact(_detail())
    assert public_content_impact(safe) == safe
    projected = public_diagnostic({"content_impact": _detail()})
    assert projected["content_impact"] == safe
    text = json.dumps(projected)
    assert "private_" not in text and "owner@" not in text and "Private dashboard" not in text


def test_saved_projection_does_not_upgrade_incomplete_coverage():
    detail = {"status": "complete", "coverage_complete": True,
              "summary": dict.fromkeys(("affected_fields", "dashboards", "workbooks", "queries",
                                       "dashboard_filters", "searches", "unverified_documents"), 0),
              "coverage_gap_count": 1, "coverage_reasons": None, "coverage_causes": None}
    safe = public_content_impact(detail)
    assert safe["status"] == "partial"
    assert safe["coverage_complete"] is False
    assert public_content_impact(safe) == safe


def test_enrichment_cannot_change_prior_gate(configured):
    original = current_validation_report(configured)
    with patch("omniflow.content_impact_reporting.resolve_repository_policy",
               return_value=RepositoryPolicy(False, "repository_not_private")):
        assert publish_content_impact(configured, _detail(), original) is False
    enriched = current_validation_report(configured)
    assert {k: v for k, v in enriched.items() if k != "content_impact"} == original
    assert enriched["exit_code"] == 1
    assert not Path(".omniflow/repository/column-impact.json").exists()
    assert "partial" in Path(".omniflow/public/report.md").read_text()


def test_private_detail_requires_live_policy(configured):
    with patch("omniflow.content_impact_reporting.resolve_repository_policy",
               return_value=RepositoryPolicy(True, "repository_verified")):
        assert publish_content_impact(configured, _detail(), current_validation_report(configured))
    assert "Private dashboard" in Path(".omniflow/repository/column-impact.json").read_text()
    assert "Private dashboard" not in Path(".omniflow/public/report.json").read_text()
    clear_content_impact(Path(".omniflow"))
    assert not Path(".omniflow/repository/column-impact.json").exists()


def test_private_detail_clears_when_live_policy_changes(configured):
    with patch("omniflow.content_impact_reporting.resolve_repository_policy",
               return_value=RepositoryPolicy(True, "repository_verified")):
        publish_content_impact(configured, _detail(), current_validation_report(configured))
    with patch("omniflow.content_impact_reporting.resolve_repository_policy",
               return_value=RepositoryPolicy(False, "repository_not_private")):
        assert not publish_content_impact(configured, _detail(), current_validation_report(configured))
    assert not Path(".omniflow/repository/column-impact.json").exists()
    assert not Path(".omniflow/repository/column-impact.md").exists()


def test_public_rerender_clears_old_sidecar_and_receipt(configured):
    original = current_validation_report(configured)
    publish_content_impact(configured, _detail(), original)
    assert Path(".omniflow/public/content-impact.json").exists()
    write_public_reports(original, output_dir=configured.reporting.output_dir,
                         formats=["markdown"], redaction_level="strict")
    assert not Path(".omniflow/public/content-impact.json").exists()
    assert not Path(".omniflow/.impact-receipt.json").exists()


@pytest.mark.parametrize("gate_result", [0, 1, 2])
def test_optional_receipt_failure_preserves_validation_result(configured, gate_result, capsys):
    with patch("omniflow.cli._execute_run", return_value=gate_result), \
         patch("omniflow.cli.seal_validation_report", side_effect=OSError("private-failure-text")):
        assert main(["run", "--auto"]) == gate_result
    assert "private-failure-text" not in capsys.readouterr().out


def test_private_links_are_scoped_and_public_links_withheld(configured):
    detail = _detail()
    detail["impacts"][0]["documents"] = [{"name": "Private dashboard",
        "url": "https://demo.omniapp.co/w/doc?secret=discard#fragment", "owner": {"name": "Test owner"},
        "queries": [{"query_key": "1", "usage": "dashboard", "url": "https://evil.example/steal"}]}]
    with patch("omniflow.content_impact_reporting.resolve_repository_policy",
               return_value=RepositoryPolicy(True, "repository_verified")), \
         patch("omniflow.discovery.discover_published_contexts", return_value=[
             ModelContext(base_url="https://demo.omniapp.co", model_id="model", model_path="omni")]):
        publish_content_impact(configured, detail, current_validation_report(configured))
    text = Path(".omniflow/repository/column-impact.md").read_text()
    assert "https://demo.omniapp.co/w/doc" in text and "Test owner" in text
    assert "secret=" not in text and "evil.example" not in text
    assert "demo.omniapp.co" not in Path(".omniflow/public/report.md").read_text()


def test_private_content_is_deduplicated_with_source_paths_and_recovery(configured):
    detail = _detail()
    document = {"document_id": "doc", "name": "Affected document", "owner": {"name": "Test owner"},
                "queries": [{"query_key": "1", "name": "Affected query", "usage": "dashboard"}]}
    source = {"model_path": "omni", "view_file": "sales/orders.view.yaml", "field_name": "amount",
              "relation": "SALES.ORDERS", "column": "AMOUNT", "source_revision": "base",
              "change_kind": "modified", "confidence": "exact", "dependency_path": ["orders.amount"],
              "documents": [document]}
    detail["impacts"] = [source, source | {"source_revision": "head", "confidence": "potential"},
                         source | {"view_file": "private/orders.view.yaml", "documents": []}]
    detail["coverage_gaps"] = [{"reason_code": "content_impact_field_identity_unavailable", "impact_index": 2},
                               {"reason_code": "content_impact_query_expression_unverified", "impact_index": 0}]
    with patch("omniflow.content_impact_reporting.resolve_repository_policy",
               return_value=RepositoryPolicy(True, "repository_verified")):
        publish_content_impact(configured, detail, current_validation_report(configured))
    text = Path(".omniflow/repository/column-impact.md").read_text()
    assert text.count("### Affected document") == text.count("Affected query") == 1
    assert "Test owner" in text and "SALES.ORDERS.AMOUNT" in text
    assert "Confirmed source dependency" in text and "Potential impact" in text
    assert "base / modified" in text and "head / modified" in text
    assert "location: omni / private/orders.view.yaml / amount / base" in text
    assert "matched uniquely" in text and "Saved calculations" in text
    public = Path(".omniflow/public/report.md").read_text()
    assert "matched uniquely" in public and "Saved calculations" in public
    assert "private/orders" not in public and "Affected document" not in public and "Test owner" not in public


@pytest.mark.parametrize("reason,expected", [
    ("content_impact_field_identity_unavailable", "matched uniquely"),
    ("dbt_explicit_view_mapping_unresolved", "checks.dbt_impact.table_mapping"),
    ("dbt_sql_dynamic_projection", "Dynamic macros"),
    ("content_impact_query_expression_unverified", "Saved calculations"),
    ("content_impact_budget_exhausted", "limit was reached"),
    ("omni_forbidden", "acting-user permissions"),
])
def test_coverage_recovery_prose_is_fixed_and_public_safe(reason, expected):
    detail = _detail("unavailable")
    detail["coverage_gaps"] = [{"reason_code": reason, "cause_code": reason,
                                "file": "PRIVATE_PATH", "message": "SECRET_VALUE"}]
    text = " ".join(coverage_explanations(detail))
    assert expected in text
    assert "PRIVATE_PATH" not in text and "SECRET_VALUE" not in text


@pytest.mark.parametrize("gate_exit", [0, 1])
def test_unresolved_mapping_details_remain_private_and_do_not_change_gate(configured, gate_exit):
    original = current_validation_report(configured)
    original.update(exit_code=gate_exit, policy_decision="fail" if gate_exit else "pass",
                    issues=original["issues"] if gate_exit else [], summary={"errors": gate_exit})
    detail = _detail("unavailable")
    detail.update(impacts=[], summary={"dashboards": 0, "queries": 0}, coverage_gaps=[{
        "reason_code": "dbt_explicit_view_mapping_unresolved",
        "configured_view": "missing_private_view", "source_revision": "head",
    }])
    with patch("omniflow.content_impact_reporting.resolve_repository_policy",
               return_value=RepositoryPolicy(True, "repository_verified")):
        assert publish_content_impact(configured, detail, original)
    enriched = current_validation_report(configured)
    assert {key: value for key, value in enriched.items() if key != "content_impact"} == original
    assert "dbt_explicit_view_mapping_unresolved" in enriched["content_impact"]["coverage_reasons"]
    public = Path(".omniflow/public/report.md").read_text()
    assert "checks.dbt_impact.table_mapping" in public and "affected content is unknown" in public
    assert "missing_private_view" not in public + json.dumps(enriched)
    assert r"missing\_private\_view" not in public
    private = Path(".omniflow/repository/column-impact.md").read_text()
    assert r"missing\_private\_view" in private and "location: head" in private
    private_data = json.loads(Path(".omniflow/repository/column-impact.json").read_text())
    assert private_data["coverage_gaps"][0]["configured_view"] == "missing_private_view"
    assert "checks.dbt_impact.table_mapping[].omni_view" in private
    comment = render_pr_comment(enriched, head_sha="a" * 40, run_url="https://github.com/o/r/actions/runs/1")
    assert "affected content is unknown" in comment and "0 dashboard" not in comment
    assert "missing_private_view" not in comment


def test_stale_or_tampered_report_cannot_enrich(configured, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    with pytest.raises(ConfigError):
        current_validation_report(configured)
    monkeypatch.delenv("GITHUB_RUN_ATTEMPT")
    Path(".omniflow/public/report.json").write_text('{"exit_code":0}')
    with pytest.raises(ConfigError):
        current_validation_report(configured)


def test_compact_comment_one_optional_line():
    report = {"policy_decision": "pass", "validation_complete": True, "issues": [], "content_impact": _detail()}
    text = render_pr_comment(report, head_sha="a" * 40, run_url="https://github.com/o/r/actions/runs/1")
    assert len(text.splitlines()) == 5
    assert "lower bounds" in text
    assert "Private dashboard" not in text
    report["content_impact"] = _detail("unavailable")
    text = render_pr_comment(report, head_sha="a" * 40, run_url="https://github.com/o/r/actions/runs/1")
    assert "affected content is unknown" in text
    assert "0 dashboard" not in text


def test_fork_cannot_receive_enrichment(configured, monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    with patch("omniflow.cli.pull_request_revision", side_effect=[("a" * 40, "o/r"), ("b" * 40, "fork/r")]):
        assert _content_impact_authorized() is False


def test_environment_mismatch_never_loads_credential_or_client(configured):
    context = ModelContext(base_url="https://demo.omniapp.co", model_id="model", model_path="omni",
                           environment="production")
    with patch("omniflow.cli.require_api_key") as key, patch("omniflow.cli.OmniClient") as client:
        with pytest.raises(SecurityPolicyError):
            _published_impact_client(context, 30)
    key.assert_not_called()
    client.assert_not_called()


def test_disabled_command_makes_no_calls(configured):
    Path(".omniflow.yml").write_text("checks:\n  dbt_impact:\n    enabled: true\n")
    with patch("omniflow.cli.require_api_key", side_effect=AssertionError("must not load credentials")):
        assert main(["dbt", "content-impact"]) == 0


def test_command_uses_separate_enrichment_and_preserves_failure(configured):
    original = current_validation_report(configured)
    with patch("omniflow.dbt_column_impact.collect_column_impacts", return_value={
        "schema_version": 1, "coverage_complete": True, "coverage_gaps": [], "impacts": []}), \
         patch("omniflow.content_impact.enrich_content_impact", return_value=_detail()), \
         patch("omniflow.cli.get_dbt_changed_files", return_value=["models/orders.sql"]), \
         patch("omniflow.cli.discover_published_contexts", return_value=[]), \
         patch("omniflow.cli.require_api_key", side_effect=AssertionError("lazy credential not needed by stub")):
        assert main(["dbt", "content-impact"]) == 0
    enriched = current_validation_report(configured)
    assert enriched["exit_code"] == original["exit_code"] == 1
    assert enriched["content_impact"]["summary"]["dashboards"] == 2


def test_api_failure_is_advisory_and_never_zero_impact(configured):
    with patch("omniflow.dbt_column_impact.collect_column_impacts", side_effect=ValueError("private-sql-value")), \
         patch("omniflow.cli.get_dbt_changed_files", return_value=["models/orders.sql"]):
        assert main(["dbt", "content-impact"]) == 0
    report = current_validation_report(configured)
    assert report["exit_code"] == 1
    assert report["content_impact"]["status"] == "unavailable"
    assert "private-sql-value" not in json.dumps(report)


@pytest.mark.parametrize("setting,value", [("sql_dialect", "not-a-dialect"), ("max_searches", 0),
                                           ("timeout_seconds", 999), ("unknown_setting", True)])
def test_config_limits(configured, setting, value):
    Path(".omniflow.yml").write_text(yaml.safe_dump({"checks": {"dbt_impact": {
        "content_impact": {setting: value}}}}))
    with pytest.raises(ConfigError):
        load_config()


def test_composite_separates_opt_in_credential():
    root = Path(__file__).resolve().parents[1]
    action = yaml.safe_load((root / "action.yml").read_text())
    steps = {step.get("id"): step for step in action["runs"]["steps"] if step.get("id")}
    assert "OMNI_API_KEY" not in steps["dbt_impact"]["env"]
    assert "OMNI_API_KEY" not in steps["route"]["env"]
    assert steps["content_impact"]["continue-on-error"] is True
    assert "steps.route.outputs.content_impact == 'true'" in steps["content_impact"]["if"]
    assert steps["content_impact"]["env"]["OMNI_API_KEY"] == "${{ inputs['omni-api-key'] }}"
    assert steps["content_impact"]["env"]["OMNIFLOW_ENVIRONMENT"] == "${{ inputs['validation-environment'] }}"
    assert action["outputs"]["content-impact-details-ready"]["value"] == (
        "${{ steps.content_impact.outputs.content_impact_details_ready }}")
    for name in ("omniflow.yml", "omniflow-environments.yml"):
        workflow = yaml.safe_load((root / ".github/workflow-examples" / name).read_text())
        for job in workflow["jobs"].values():
            impact_upload = next(step for step in job["steps"] if step.get("id") == "impact_upload")
            assert impact_upload["with"]["path"].splitlines() == [
                "${{ env.OMNIFLOW_REPORT_DIR }}/repository/column-impact.json",
                "${{ env.OMNIFLOW_REPORT_DIR }}/repository/column-impact.md",
            ]
            assert "content-impact-details-ready == 'true'" in impact_upload["if"]
            assert "github.event.repository.private == true" in impact_upload["if"]
            assert impact_upload["continue-on-error"] is True
            for step in job.get("steps", []):
                if step.get("env", {}).get("REPOSITORY_READY") == (
                        "${{ steps.omniflow.outputs.repository-diagnostics-ready }}"):
                    assert step["env"]["CONTENT_IMPACT_READY"] == (
                        "${{ steps.omniflow.outputs.content-impact-details-ready }}")
                    assert step["env"]["IMPACT_UPLOAD_OUTCOME"] == "${{ steps.impact_upload.outcome }}"
                    assert step["env"]["IMPACT_ARTIFACT_URL"] == "${{ steps.impact_upload.outputs.artifact-url }}"
