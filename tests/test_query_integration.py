"""Orchestration boundaries independent of the query engine's unit tests."""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from omniflow import cli
from omniflow.candidate import CandidateVerificationError
from omniflow.config import _to_config, _validate_config_schema
from omniflow.diagnostics import public_diagnostic
from omniflow.discovery import ModelContext
from omniflow.exceptions import ConfigError, SecurityPolicyError
from omniflow.query_validation import QueryValidationSettings


def setup_run(monkeypatch, *, enabled=True):
    config = _to_config({}, None)
    config.content_validation.enabled = config.model_validation.enabled = False
    config.semantic_lint.enabled = config.contracts.enabled = False
    config.query_validation = QueryValidationSettings(mode="report" if enabled else "off")
    context = ModelContext(base_url="https://omni.example", model_id="model", model_path="model", branch_id="branch")
    monkeypatch.setattr(cli, "is_pull_request_event", lambda: True)
    monkeypatch.setattr(cli, "pull_request_revision", lambda side: (("a" if side == "base" else "b") * 40, "org/repo"))
    monkeypatch.setattr(cli, "_client_and_branch_for_context", lambda *a, **k: (Mock(), "branch"))
    monkeypatch.setattr(cli, "start_repository_context", Mock())
    monkeypatch.setattr(cli, "finish_repository_context", Mock())
    verification = Mock(side_effect=lambda *a, revision_side, **k: {"status": "verified", "side": revision_side})
    monkeypatch.setattr(cli, "verify_with_retry", verification)
    engine = Mock(return_value=({"validator": "query_validation", "query_validation_status": "passed",
                                "execution_status": "completed", "validation_complete": True, "issues": []}, 0))
    monkeypatch.setattr(cli, "run_query_validation", engine)
    return config, context, verification, engine


def run(config, context, tmp_path):
    return cli._run_context(config=config, context=context, output_dir=tmp_path, api_key=None)


def test_queries_require_base_and_head_even_when_model_and_content_disabled(monkeypatch, tmp_path):
    config, context, verification, engine = setup_run(monkeypatch)
    report, code = run(config, context, tmp_path)
    assert code == 0 and report["validation_complete"]
    assert [call.kwargs["revision_side"] for call in verification.call_args_list] == ["base", "head", "head", "base"]
    proof = engine.call_args.kwargs["provenance"]
    assert proof.base_verified and proof.head_verified
    assert proof.base_sha == "a" * 40 and proof.head_sha == "b" * 40


def test_default_off_makes_no_query_or_extra_candidate_calls(monkeypatch, tmp_path):
    config, context, verification, engine = setup_run(monkeypatch, enabled=False)
    run(config, context, tmp_path)
    engine.assert_not_called()
    verification.assert_not_called()


def test_query_success_is_invalidated_by_final_source_drift(monkeypatch, tmp_path):
    config, context, verification, engine = setup_run(monkeypatch)
    verification.side_effect = [{"status": "verified", "side": "base"}, {"status": "verified", "side": "head"},
                                CandidateVerificationError("changed_during_validation"),
                                {"status": "verified", "side": "base"}]
    with pytest.raises(CandidateVerificationError) as error:
        run(config, context, tmp_path)
    check = error.value.omniflow_progress.reports[0]
    assert check["query_validation_status"] == "inconclusive"
    assert check["validation_complete"] is False
    assert check["comparison_status"] == "unavailable"
    assert check["new_issue_count"] == check["existing_issue_count"] == 0
    assert verification.call_args_list[-1].kwargs["retry"] is False


def test_report_mode_incomplete_is_advisory_not_pass(monkeypatch, tmp_path):
    config, context, _, engine = setup_run(monkeypatch)
    engine.return_value = ({"validator": "query_validation", "query_validation_status": "inconclusive",
                           "execution_status": "partial", "validation_complete": False, "issues": []}, 0)
    report, code = run(config, context, tmp_path)
    assert code == 0 and report["policy_decision"] == "advisory"
    assert not report["validation_complete"]


def test_query_schema_is_validated_before_environment_expansion():
    with pytest.raises(ConfigError):
        _validate_config_schema({"checks": {"query_validation": {"snapshot_attestation": "${DYNAMIC}"}}})
    assert _to_config({}, None).query_validation.mode == "off"


def test_query_public_projection_never_copies_rows_queries_or_private_contract_identity():
    report = {"validator": "query_validation", "query_validation_status": "regression", "query_validation_mode": "report",
              "contracts_checked": 1, "query": {"filters": "PRIVATE"}, "rows": [{"name": "PRIVATE"}],
              "snapshot_attestation": "PRIVATE", "issues": [{"validator": "query_validation",
              "type": "query_validation_issue", "reason_code": "query_contract_regression", "contract_id": "PRIVATE",
              "message": "PRIVATE", "query_contract_number": 1, "query_assertion_type": "null_rate", "severity": "error"}]}
    safe = public_diagnostic(report)
    assert "PRIVATE" not in json.dumps(safe)
    assert safe["query_validation_status"] == "regression"
    assert safe["issues"][0]["query_assertion_type"] == "null_rate"


def test_ai_proposals_cannot_run_in_github_actions(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    args = cli.build_parser().parse_args(["query", "propose", "--topic", "orders", "--prompt", "count", "--output", "new.json"])
    with pytest.raises(SecurityPolicyError):
        cli.cmd_query_propose(args)


def test_local_ai_proposal_is_private_and_never_overwritten(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(cli, "is_pull_request_event", lambda: False)
    config = _to_config({}, None)
    config.omni.model_id = "model"
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "_client_and_branch", lambda _: (Mock(), None))
    proposal = Mock(return_value={"approved": False, "query": {"fields": ["orders.count"]}})
    monkeypatch.setattr("omniflow.query_transport.propose_query", proposal)
    args = cli.build_parser().parse_args(["query", "propose", "--topic", "orders", "--prompt", "count", "--output", "private/proposal.json"])
    assert cli.cmd_query_propose(args) == 0
    target = Path("private/proposal.json")
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ConfigError):
        cli.cmd_query_propose(args)
    assert proposal.call_count == 1
