"""Requested depth is explicit, consistent, and never evidence of exhaustive coverage."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from omniflow.cli import build_parser, cmd_content_validate, cmd_run
from omniflow.config import ContentValidationSettings, _to_config, load_config
from omniflow.diagnostics import public_diagnostic
from omniflow.discovery import ModelContext
from omniflow.exceptions import ConfigError, OmniAPIError
from omniflow.reporting.markdown_report import render_markdown_report
from omniflow.validators.content import HISTORY_SCHEMA_VERSION, ContentEvidenceError, run_content_validation


def validate(tmp_path, client, *, full=False, branch="branch"):
    return run_content_validation(
        client=client, model_id="model", branch_id=branch, user_id="user",
        include_personal_folders=False, labels=[], fail_on_new_only=True,
        force_full_validation=full, history_in=tmp_path / "history.json",
        history_out=tmp_path / "history.json", report_out=tmp_path / "report.json",
    )


def client_for(payload=None):
    return SimpleNamespace(
        validate_content=Mock(return_value={"content": []} if payload is None else payload),
        list_content=lambda **kw: [],
        validation_evidence_binding=lambda: ("https://omni.example", "a" * 64),
        content_identity_context=lambda **kw: {"identity_contract": "membership_v1", "identity_fingerprint": "b" * 64},
    )


@pytest.mark.parametrize("value", [False, True])
def test_depth_config_is_opt_in_and_loadable(tmp_path, monkeypatch, value):
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    path = tmp_path / "policy.yml"
    path.write_text(f"checks:\n  content_validation:\n    force_full_validation: {str(value).lower()}\n")
    assert load_config(path).content_validation.force_full_validation is value
    assert ContentValidationSettings().force_full_validation is False
    assert _to_config({}, None).content_validation.force_full_validation is False


@pytest.mark.parametrize("value", ["unexpected", "[]", "{}"])
def test_invalid_depth_config_is_rejected(value):
    import yaml
    with pytest.raises(ConfigError, match="content_validation.force_full_validation"):
        _to_config({"checks": {"content_validation": {"force_full_validation": yaml.safe_load(value)}}}, None)


@pytest.mark.parametrize("full", [False, True])
def test_branch_and_base_request_same_depth_and_report_only_requested_depth(tmp_path, full):
    client = client_for()
    report, code = validate(tmp_path, client, full=full)
    assert code == 0
    assert [call.kwargs["branch_id"] for call in client.validate_content.call_args_list] == [None, "branch"]
    for call in client.validate_content.call_args_list:
        assert call.kwargs.get("force_full_validation", False) is full
        assert ("force_full_validation" in call.kwargs) is full
    depth = "full" if full else "api_default"
    assert report["requested_validation_depth"] == depth
    public = public_diagnostic(report)
    assert public["requested_validation_depth"] == depth
    markdown = render_markdown_report(public)
    assert f"Requested validation depth: `{depth}`" in markdown
    assert "not verified exhaustive coverage" in markdown
    assert "requested_validation_depth" not in public_diagnostic({"requested_validation_depth": "private free text"})


@pytest.mark.parametrize("full", [False, True])
def test_verified_draft_evidence_rereads_preserve_depth(tmp_path, full):
    def document(identifier):
        return {"identifier": identifier, "document_id": f"physical-{identifier}",
                "dashboard_filter_issues": [{"filter_name": "filter", "issues": ["Existing issue"]}],
                "queries_and_issues": []}
    client = SimpleNamespace(
        validate_content=Mock(side_effect=lambda *a, branch_id=None, **kw: {
            "content": [document("draft" if branch_id else "published")]
        }),
        list_content=lambda **kw: [{"identifier": "published"}],
        get_document_identity=lambda identifier, **kw: {"identifier": identifier, "publishedIdentifier": "published"},
        list_document_drafts=lambda parent: [{
            "identifier": "draft", "publishedIdentifier": "published", "branch": {"id": "branch"},
            "status": "active", "draftOutOfDate": False, "updatedAt": "2026-10-15T12:00:00.000Z",
        }],
    )
    report, code = validate(tmp_path, client, full=full)
    assert code == 0 and report["existing_issues"] == 1
    assert [call.kwargs["branch_id"] for call in client.validate_content.call_args_list] == [None, "branch", None, "branch"]
    assert all(call.kwargs.get("force_full_validation", False) is full for call in client.validate_content.call_args_list)


@pytest.mark.parametrize("before,after,accepted", [(False, False, True), (True, True, True), (False, True, False), (True, False, False)])
def test_history_is_invalidated_when_requested_depth_changes(tmp_path, before, after, accepted):
    validate(tmp_path, client_for(), full=before, branch=None)
    saved = json.loads((tmp_path / "history.json").read_text())
    assert saved["schema_version"] == HISTORY_SCHEMA_VERSION == 4
    report, code = validate(tmp_path, client_for(), full=after, branch=None)
    assert report["history_status"] == ("accepted" if accepted else "invalidated_context")
    assert code == (0 if accepted else 4)


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("version", [2, 3])
def test_legacy_history_without_verified_identity_binding_is_never_reused(tmp_path, full, version):
    validate(tmp_path, client_for(), branch=None)
    history = json.loads((tmp_path / "history.json").read_text())
    history["schema_version"] = version
    if version == 2:
        history.pop("requested_validation_depth")
    (tmp_path / "history.json").write_text(json.dumps(history))
    report, code = validate(tmp_path, client_for(), full=full, branch=None)
    assert report["history_status"] == "invalidated_version"
    assert code == 4 and report["validation_complete"] is False


@pytest.mark.parametrize("invalid", [None, {}, "unknown"])
def test_invalid_history_depth_does_not_become_default(tmp_path, invalid):
    validate(tmp_path, client_for(), branch=None)
    history = json.loads((tmp_path / "history.json").read_text())
    history["requested_validation_depth"] = invalid
    (tmp_path / "history.json").write_text(json.dumps(history))
    with pytest.raises(OmniAPIError, match="invalid comparison context"):
        validate(tmp_path, client_for(), branch=None)


def test_full_depth_error_is_not_retried_at_shallower_depth(tmp_path):
    client = client_for()
    client.validate_content.side_effect = OmniAPIError("Synthetic timeout")
    with pytest.raises(OmniAPIError):
        validate(tmp_path, client, full=True)
    assert client.validate_content.call_count == 1
    assert client.validate_content.call_args.kwargs["force_full_validation"] is True
    assert not (tmp_path / "history.json").exists()


@pytest.mark.parametrize("command", ["run", "content"])
@pytest.mark.parametrize("full", [False, True])
def test_both_cli_entrypoints_propagate_depth_and_report_failure(tmp_path, monkeypatch, command, full):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    config = _to_config({}, None)
    config.content_validation.force_full_validation = full
    config.omni.model_id = "model"
    context = ModelContext(base_url="https://synthetic.example", model_id="model", model_path="model", branch_id="branch")
    client = client_for({"content": [], "errors": ["synthetic private detail"]})
    with patch("omniflow.cli.load_config", return_value=config), \
            patch("omniflow.cli.discover_contexts", return_value=[context]), \
            patch("omniflow.cli._client_and_branch_for_context", return_value=(client, "branch")), \
            patch("omniflow.cli._client_and_branch", return_value=(client, "branch")):
        if command == "run":
            args = build_parser().parse_args(["run", "--auto"])
            assert cmd_run(args, changed_files=[]) == 4
        else:
            args = build_parser().parse_args(["content", "validate"])
            with pytest.raises(ContentEvidenceError):
                cmd_content_validate(args)
    assert client.validate_content.call_args.kwargs.get("force_full_validation", False) is full
    report = json.loads((tmp_path / ".omniflow/public/report.json").read_text())
    assert report["issues"][0]["requested_validation_depth"] == ("full" if full else "api_default")
    assert report["validation_complete"] is False
    assert "synthetic private detail" not in json.dumps(report)
