import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from xml.etree import ElementTree

import pytest

from omniflow.artifacts import write_public_reports
from omniflow.cli import build_parser, cmd_run
from omniflow.config import load_config
from omniflow.discovery import ModelContext
from omniflow.exceptions import OmniAPIError, OmniFlowError
from omniflow.security import secure_write_text


def configured_run(monkeypatch, tmp_path, client, *, two_models=False):
    monkeypatch.chdir(tmp_path)
    config = load_config(None)
    config.reporting.formats = ["json", "markdown", "sarif", "junit"]
    config.content_validation.fail_on_new_only = False
    config.semantic_lint.enabled = True
    config.contracts.enabled = True
    config.ai_eval.enabled = False
    config.dbt_exposures.enabled = False
    config.breaking_change_hold.enabled = False
    contexts = [ModelContext(base_url="https://omni.example", model_id="model-a", model_path="a", branch_id="branch-a")]
    if two_models:
        contexts.append(ModelContext(base_url="https://omni.example", model_id="model-b", model_path="b", branch_id="branch-b"))
    monkeypatch.setattr("omniflow.cli.load_config", lambda _: config)
    monkeypatch.setattr("omniflow.cli.discover_contexts", lambda **_: contexts)
    monkeypatch.setattr("omniflow.cli._client_and_branch_for_context", lambda context, *a, **k: (client, context.branch_id))
    return config, build_parser().parse_args(["run", "--auto"])


@pytest.mark.parametrize("exception", [OmniAPIError("Model API unavailable"), RuntimeError("SYNTHETIC-PRIVATE-EXCEPTION")])
def test_later_failure_keeps_completed_content_and_classifies_later_checks(tmp_path, monkeypatch, exception):
    client = SimpleNamespace(
        validate_content=lambda *a, **k: {"content": [{"identifier": "document", "dashboard_filter_issues": ["Broken filter"]}]},
        list_content=lambda **k: [],
        validate_model=mock.Mock(side_effect=exception),
    )
    _, args = configured_run(monkeypatch, tmp_path, client)
    code = cmd_run(args, changed_files=[])
    assert code == (4 if isinstance(exception, OmniAPIError) else 6)
    report = json.loads(Path(".omniflow/public/report.json").read_text())
    assert report["validation_complete"] is False
    model = report["model_reports"][0]
    states = {item["validator"]: item["status"] for item in model["check_states"]}
    assert states["content"] == "completed"
    assert states["model"] == "failed"
    assert states["semantic_diff"] == states["contracts"] == "not_run"
    assert states["ai_eval"] == "disabled"
    assert len(report["issues"]) == 2
    assert report["issues"][0]["state"] == "new"
    assert all(issue["model_id"] == "model-a" and issue["branch_id"] == "branch-a" for issue in report["issues"])
    assert model["check_reports"][0]["validator"] == "content"
    assert json.loads(Path(".omniflow/public/evidence.json").read_text())["validation_complete"] is False
    assert not Path(".omniflow/restricted").exists()
    assert "SYNTHETIC-PRIVATE-EXCEPTION" not in "\n".join(path.read_text() for path in Path(".omniflow/public").iterdir())


def test_failed_model_does_not_discard_another_models_complete_validation(tmp_path, monkeypatch):
    def validate_model(model_id, **kwargs):
        if model_id == "model-a":
            raise OmniAPIError("Unavailable")
        return []
    client = SimpleNamespace(validate_content=lambda *a, **k: {"content": []}, list_content=lambda **k: [], validate_model=validate_model)
    config, args = configured_run(monkeypatch, tmp_path, client, two_models=True)
    config.semantic_lint.enabled = config.contracts.enabled = False
    assert cmd_run(args, changed_files=[]) == 4
    report = json.loads(Path(".omniflow/public/report.json").read_text())
    assert [model["validation_complete"] for model in report["model_reports"]] == [False, True]
    assert report["validation_complete"] is False
    assert report["issues"][0]["model_id"] == "model-a"
    assert len(report["model_reports"][1]["check_reports"]) == 2


@pytest.mark.parametrize("failure_point", ["serialization", "discovery"])
def test_unexpected_failure_replaces_stale_public_artifacts_without_private_exception(tmp_path, monkeypatch, failure_point):
    client = SimpleNamespace(validate_content=lambda *a, **k: {"content": []}, list_content=lambda **k: [], validate_model=lambda *a, **k: [])
    config, args = configured_run(monkeypatch, tmp_path, client)
    config.semantic_lint.enabled = config.contracts.enabled = False
    for name in ("report.json", "report.md", "report.sarif", "junit.xml", "evidence.json"):
        secure_write_text(Path(".omniflow/public") / name, "STALE-GREEN-RESULT")
    if failure_point == "serialization":
        monkeypatch.setattr("omniflow.artifacts.write_reports", mock.Mock(side_effect=RuntimeError("PRIVATE-SERIALIZER-DETAIL")))
        with pytest.raises(OmniFlowError, match="Report generation failed") as caught:
            cmd_run(args, changed_files=[])
        assert caught.value.exit_code == 6
    else:
        monkeypatch.setattr("omniflow.cli.discover_contexts", mock.Mock(side_effect=RuntimeError("PRIVATE-DISCOVERY-DETAIL")))
        assert cmd_run(args, changed_files=[]) == 6
    for path in Path(".omniflow/public").iterdir():
        value = path.read_text()
        assert "STALE-GREEN-RESULT" not in value
        assert "PRIVATE-" not in value
    report = json.loads(Path(".omniflow/public/report.json").read_text())
    assert report["exit_code"] == 6
    assert report["policy_decision"] == "fail"
    assert report["validation_complete"] is False
    assert ElementTree.fromstring(Path(".omniflow/public/junit.xml").read_text()).get("failures") == "1"


def test_emergency_report_does_not_reenter_broken_privacy_or_formatting_pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr("omniflow.artifacts.public_safe", mock.Mock(side_effect=RecursionError("PRIVATE-PAYLOAD")))
    with pytest.raises(OmniFlowError, match="Report generation failed"):
        write_public_reports({"issues": []}, output_dir=tmp_path, formats=["json", "markdown"], redaction_level="strict")
    report = json.loads((tmp_path / "public/report.json").read_text())
    assert report["exit_code"] == 6
    assert "PRIVATE" not in (tmp_path / "public/report.md").read_text()
