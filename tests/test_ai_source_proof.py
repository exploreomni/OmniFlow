"""AI-only PRs must prove the compared sources, including their final sample."""
import json
from unittest.mock import Mock

import pytest
from test_ai_eval import FakeEvalClient, result
from test_query_integration import run, setup_run

from omniflow import cli
from omniflow.candidate import CandidateVerificationError
from omniflow.diagnostics import public_diagnostic
from omniflow.validators.ai_eval import EvalFailure


def setup_ai(monkeypatch, *, regressed=False):
    config, context, proof, query = setup_run(monkeypatch, enabled=False)
    context.model_id = "model-1"
    config.ai_eval.enabled = True
    config.ai_eval.prompt_sets = [{"id": "set-1", "model_id": "model-1"}]
    client = FakeEvalClient({
        "main": {"status": "COMPLETE", "results": [result("PRIVATE-PROMPT", 1)]},
        "branch": {"status": "COMPLETE", "results": [result("PRIVATE-PROMPT", 0 if regressed else 1)]},
    })
    monkeypatch.setattr(cli, "_client_and_branch_for_context", lambda *a, **k: (client, context.branch_id))
    return config, context, proof, client


@pytest.mark.parametrize("all_issues", [False, True])
def test_ai_alone_proves_both_revisions_before_and_after_jobs(monkeypatch, tmp_path, all_issues):
    config, context, proof, client = setup_ai(monkeypatch)
    if all_issues:
        config.model_validation.enabled = True
        config.model_validation.fail_on_new_only = False
        monkeypatch.setattr(cli, "run_model_validation", Mock(return_value=({"validator": "model", "issues": []}, 0)))
    report, code = run(config, context, tmp_path)
    assert code == 0 and report["validation_complete"]
    assert [call.kwargs["revision_side"] for call in proof.call_args_list] == ["base", "head", "head", "base"]
    assert len(client.started) == 2


def test_unverified_ai_source_never_creates_jobs_even_when_advisory(monkeypatch, tmp_path):
    config, context, proof, client = setup_ai(monkeypatch)
    config.ai_eval.fail_on_regression = False
    proof.side_effect = CandidateVerificationError("file_inventory_mismatch")
    with pytest.raises(CandidateVerificationError):
        run(config, context, tmp_path)
    assert not client.started


@pytest.mark.parametrize("side", ["base", "head"])
@pytest.mark.parametrize("regressed", [False, True])
def test_final_ai_source_drift_removes_attribution(monkeypatch, tmp_path, side, regressed):
    config, context, proof, client = setup_ai(monkeypatch, regressed=regressed)
    calls = 0

    def verify(*args, revision_side, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 2 and revision_side == side:
            raise CandidateVerificationError("changed_during_validation")
        return {"status": "verified", "side": revision_side}

    proof.side_effect = verify
    with pytest.raises(CandidateVerificationError) as error:
        run(config, context, tmp_path)
    progress = error.value.omniflow_progress
    check = progress.reports[0]
    assert not check["validation_complete"]
    assert check["comparison_status"] == "unavailable"
    assert check["regressed_count"] == 0 and check["prompt_set_summaries"] == []
    assert progress.states["ai_eval"]["status"] == "partial"
    safe = public_diagnostic(check)
    assert "PRIVATE-PROMPT" not in json.dumps(safe)
    if regressed:
        assert safe["issues"][0]["reason_code"] == "ai_eval_source_unverified"
        assert safe["issues"][0]["blocking"] is False
    assert len(client.started) == 2


@pytest.mark.parametrize("scope", ["no_branch", "configured_other_model", "remote_other_model"])
def test_inapplicable_ai_has_no_extra_proof_or_jobs(monkeypatch, tmp_path, scope):
    config, context, proof, client = setup_ai(monkeypatch)
    if scope == "no_branch":
        context.branch_id = None
    elif scope == "configured_other_model":
        config.ai_eval.prompt_sets[0]["model_id"] = "other-model"
    else:
        config.ai_eval.prompt_sets[0].pop("model_id")
        original = client.get_ai_eval_prompt_set
        client.get_ai_eval_prompt_set = lambda id: {**original(id), "model_id": "other-model"}
    report, code = run(config, context, tmp_path)
    assert code == 0 and report["check_reports"][0]["execution_status"] == "skipped"
    proof.assert_not_called()
    assert not client.started


def test_invalid_prompt_set_fails_preflight_without_jobs(monkeypatch, tmp_path):
    config, context, proof, client = setup_ai(monkeypatch)
    client.get_ai_eval_prompt_set = lambda id: {"model_id": "model-1", "is_archived": True, "prompts": []}
    with pytest.raises(EvalFailure) as error:
        run(config, context, tmp_path)
    assert error.value.omniflow_progress.states["ai_eval"]["status"] == "failed"
    assert not client.started
    proof.assert_not_called()


def test_ai_operational_failure_remains_blocking_after_source_drift(monkeypatch, tmp_path):
    config, context, proof, client = setup_ai(monkeypatch)
    client.runs_by_id["branch"] = {"status": "FAILED", "results": []}
    proof.side_effect = [{"status": "verified", "side": "base"}, {"status": "verified", "side": "head"},
                         CandidateVerificationError("changed_during_validation"), {"status": "verified", "side": "base"}]
    with pytest.raises(CandidateVerificationError) as error:
        run(config, context, tmp_path)
    issue = error.value.omniflow_progress.reports[0]["issues"][0]
    assert issue["operational_failure"] and issue["blocking"]
