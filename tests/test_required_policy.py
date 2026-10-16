import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from omniflow import cli
from omniflow.config import load_config
from omniflow.exceptions import ConfigError, MissingPolicyError
from omniflow.validators.content import run_content_validation

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_environment():
    # No developer credentials, GitHub event, or local policy overrides in fixtures.
    with patch.dict(os.environ, {}, clear=True):
        yield


@pytest.mark.parametrize("path", [None, ".omniflow.yml", "missing-custom-policy.yml"])
def test_missing_policy_never_silently_uses_defaults_or_environment(tmp_path, monkeypatch, path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMNI_FAIL_ON_NEW_ONLY", "true")
    with pytest.raises(MissingPolicyError) as caught:
        load_config(path)
    assert caught.value.exit_code == 2
    assert caught.value.reason_code == "missing_policy_config"


@pytest.mark.parametrize("body", ["", "# Comments alone do not configure policy\n", "{}", "null", "[]", "false"])
def test_empty_policy_is_not_a_valid_starter(tmp_path, body):
    path = tmp_path / ".omniflow.yml"
    path.write_text(body)
    with pytest.raises(ConfigError, match="top-level mapping"):
        load_config(path)


def test_starter_enables_core_checks_without_optional_scope_or_features(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path(".omniflow.yml").write_text((ROOT / ".omniflow.yml").read_text())
    config = load_config()
    assert config.source == Path(".omniflow.yml")
    assert config.content_validation.enabled and config.content_validation.fail_on_new_only
    assert config.content_validation.labels == []
    assert not config.content_validation.classify_comment_only
    assert config.model_validation.enabled and config.semantic_lint.enabled and config.contracts.enabled
    assert config.model_validation.fail_on_new_only
    assert not config.model_validation.fail_on_warnings
    assert all((config.contracts.fail_on_deleted_referenced_fields,
                config.contracts.fail_on_renamed_referenced_fields,
                config.contracts.fail_on_referenced_field_type_changes,
                config.contracts.fail_on_referenced_join_cardinality_changes,
                config.contracts.fail_on_coverage_gaps))
    assert not any((config.dbt_exposures.enabled, config.dbt_impact.enabled, config.ai_eval.enabled,
                    config.ai_repair.enabled, config.dbt_sync.enabled, config.breaking_change_hold.enabled))
    assert not config.security.retain_restricted_artifacts
    assert not config.security.allow_raw_response_output
    assert config.reporting.output_dir == ".omniflow"
    assert config.reporting.formats == ["json", "markdown", "sarif", "junit"]


def test_custom_policy_and_explicit_strict_comparison_are_preserved(tmp_path, monkeypatch):
    path = tmp_path / "custom-policy.yml"
    path.write_text("checks:\n  content_validation:\n    fail_on_new_only: false\n")
    assert load_config(path).content_validation.fail_on_new_only is False
    monkeypatch.setenv("OMNI_FAIL_ON_NEW_ONLY", "true")
    assert load_config(path).content_validation.fail_on_new_only is True


@pytest.mark.parametrize("event", ["pull_request", "pull_request_target"])
@pytest.mark.parametrize("base_policy", [False, True])
def test_pr_requires_trusted_base_policy_and_ignores_head_policy(tmp_path, monkeypatch, event, base_policy):
    monkeypatch.chdir(tmp_path)

    def git(*args):
        subprocess.run(["git", *args], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    if base_policy:
        Path(".omniflow.yml").write_text("checks:\n  content_validation:\n    fail_on_new_only: true\n")
        git("add", ".omniflow.yml")
    git("-c", "user.name=OmniFlow Tests", "-c", "user.email=test@example.com",
        "commit", "--allow-empty", "-qm", "trusted base")
    git("switch", "-qc", "feature/test")
    Path(".omniflow.yml").write_text("checks:\n  content_validation:\n    fail_on_new_only: false\n")
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    if base_policy:
        assert load_config().content_validation.fail_on_new_only is True
    else:
        with pytest.raises(MissingPolicyError):
            load_config()
    # Even a valid local file cannot substitute for an unfetched trusted base ref.
    monkeypatch.setenv("GITHUB_BASE_REF", "unfetched-base")
    with pytest.raises(MissingPolicyError):
        load_config()


@pytest.mark.parametrize("command", [["route", "--auto"], ["run", "--auto"]])
def test_missing_policy_has_actionable_private_safe_cli_and_reports(tmp_path, monkeypatch, capsys, command):
    monkeypatch.chdir(tmp_path)
    with patch("omniflow.cli.OmniClient") as client, patch("omniflow.cli.discover_contexts") as discovery:
        assert cli.main([*command, "--config", "SYNTHETIC_PRIVATE_CUSTOMER.yml"]) == 2
    client.assert_not_called()
    discovery.assert_not_called()
    stderr = capsys.readouterr().err
    assert "Required OmniFlow policy" in stderr
    assert ".omniflow.yml" in stderr and "target base branch" in stderr
    assert "missing_policy_config" in stderr
    assert "SYNTHETIC_PRIVATE_CUSTOMER" not in stderr
    report = json.loads(Path(".omniflow/public/report.json").read_text())
    assert report["exit_code"] == 2 and report["policy_decision"] == "fail"
    assert report["issues"][0]["reason_code"] == "missing_policy_config"
    for name in ("report.json", "report.md", "report.sarif", "junit.xml"):
        text = (Path(".omniflow/public") / name).read_text()
        assert "Required OmniFlow policy" in text
        assert "SYNTHETIC_PRIVATE_CUSTOMER" not in text


@pytest.mark.parametrize("new_issue", [False, True])
def test_starter_content_policy_passes_existing_debt_but_blocks_new_findings(tmp_path, new_issue):
    config = load_config(ROOT / ".omniflow.yml")

    def response(*messages):
        return {"content": [{"document_id": "synthetic-document", "identifier": "synthetic-document",
                             "dashboard_filter_issues": [{"filter_name": "Synthetic filter",
                                                          "issues": list(messages)}]}]}

    baseline = response("Existing missing field")
    current = response("Existing missing field", "New missing field") if new_issue else baseline
    fetch = Mock(side_effect=lambda *args, branch_id=None, **kwargs: current if branch_id else baseline)
    report, code = run_content_validation(
        client=SimpleNamespace(validate_content=fetch, list_content=lambda **kwargs: []),
        model_id="synthetic-model", branch_id="synthetic-branch", user_id=None,
        include_personal_folders=False, labels=config.content_validation.labels,
        fail_on_new_only=config.content_validation.fail_on_new_only,
        history_in=tmp_path / "missing.json", history_out=tmp_path / "history.json",
        report_out=tmp_path / "report.json",
    )
    assert code == int(new_issue)
    assert report["comparison_mode"] == "new_only" and report["comparison_status"] == "complete"
    assert report["existing_issues"] == 1 and report["new_issues"] == int(new_issue)
    assert [call.kwargs["branch_id"] for call in fetch.call_args_list] == ["synthetic-branch", None]
