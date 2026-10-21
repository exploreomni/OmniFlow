from omniflow.reporting.markdown_report import render_markdown_report


def report(**overrides):
    return {
        "diagnostic_schema_version": 2, "policy_decision": "fail", "exit_code": 4,
        "validation_complete": False, "summary": {"errors": 1}, "tool_version": "1.2.3",
        "tool_revision": {"commit": "a" * 40, "source": "github_action_ref"},
        "issues": [{
            "validator": "model", "severity": "error", "blocking": True,
            "reason_code": "model_comparison_unavailable", "cause_code": "omni_forbidden",
            "diagnostic_code": "OF-OMNI-FORBIDDEN", "title": "The API denied this operation.",
            "next_action": "Check target and endpoint access; do not paste credentials.",
            "message": "Model baseline evidence is unavailable.", "validation_scope": "base",
            "context_reference": "context-1",
        }],
        **overrides,
    }


def test_primary_blocker_action_and_tool_pin_are_prominent():
    markdown = render_markdown_report(report())
    before_findings = markdown.split("## Findings")[0]
    assert "**Primary blocker:**" in before_findings
    assert "OF-OMNI-FORBIDDEN" in before_findings
    assert "**Next action:** Check target" in before_findings
    assert "OmniFlow version: `1.2.3`" in before_findings
    assert f"OmniFlow Action revision: `{'a' * 40}`" in before_findings
    next_steps = markdown.split("## Next Steps")[1]
    assert "Check target and endpoint access" in next_steps
    assert "Content Validator" not in next_steps
    assert "new-only policy" not in next_steps


def test_next_steps_deduplicate_only_relevant_active_actions():
    issue = report()["issues"][0]
    markdown = render_markdown_report(report(issues=[issue, issue, {
        "validator": "content", "severity": "info", "active": False,
        "next_action": "THIS RESOLVED FOLLOWUP MUST NOT APPEAR",
    }]))
    next_steps = markdown.split("## Next Steps")[1]
    assert next_steps.count("Check target and endpoint access") == 1
    assert "RESOLVED FOLLOWUP" not in next_steps


def test_check_execution_retains_elapsed_and_not_run_cause():
    markdown = render_markdown_report(report(check_states=[
        {"validator": "candidate", "status": "failed", "elapsed_ms": 23},
        {"validator": "model", "status": "not_run", "elapsed_ms": 0, "blocked_by": "candidate"},
        {"validator": "ai_eval", "status": "skipped", "skipped_reason": "no_branch"},
    ]))
    assert "elapsed `23` ms" in markdown
    assert "blocked by `candidate`" in markdown
    assert "skipped reason `no_branch`" in markdown


def test_public_report_explains_repository_detail_unavailability_without_names():
    markdown = render_markdown_report(report(repository_diagnostics={
        "status": "unavailable", "diagnostics_reason": "repository_not_private",
    }))
    assert "Repository diagnostic detail: `unavailable` (`repository_not_private`)" in markdown


def test_generated_or_legacy_available_detail_does_not_claim_downloadable_artifact():
    for status in ("generated", "available"):
        markdown = render_markdown_report(report(repository_diagnostics={
            "status": status, "diagnostics_reason": "repository_verified",
            "publication_status": "unconfirmed",
        }))
        assert "Pre-upload snapshot" in markdown
        assert "does not record later delivery status" in markdown
        assert "upload is **unconfirmed**" not in markdown
        assert "updating only the Action pin does not add them" in markdown
        assert "Diagnostic publication" in markdown


def test_public_projection_cannot_forge_upload_confirmation_or_download_link():
    from omniflow.diagnostics import public_diagnostic

    safe = public_diagnostic(report(repository_diagnostics={
        "status": "generated", "publication_status": "uploaded",
        "artifact_url": "https://private.invalid/secret", "artifact_id": 123,
    }))
    assert safe["repository_diagnostics"] == {"status": "generated"}


def test_private_candidate_examples_are_bounded_and_do_not_claim_correspondence():
    issue = report()["issues"][0] | {
        "candidate_evidence_kind": "observations_not_correspondence", "source_revision": "head",
        "git_revision": "b" * 40,
        "candidate_samples": [
            {"git_path": f"omni/Model With Spaces/f{i}.view.yaml", "omni_path": f"nested/f{i}.view",
             "comparison": "not_compared", "pairing": "basename_hint"} for i in range(6)
        ],
        "location_unavailable_reason": "candidate_samples_unavailable",
    }
    markdown = render_markdown_report(report(
        diagnostic_audience="repository", issues=[issue],
        repository_diagnostics={"unavailable_reasons": {"file_budget_exceeded": 2}},
    ))
    assert "omni/Model With Spaces/f0.view.yaml" in markdown
    assert "f5.view" not in markdown
    assert "contents not compared; basename hint only" in markdown
    assert "do not prove that files correspond" in markdown
    assert "Do not rename or overwrite files" in markdown
    assert f"git revision: `{'b' * 40}`" in markdown
    assert "location unavailable reason: `candidate_samples_unavailable`" in markdown
    assert "Location evidence unavailable: `file_budget_exceeded` — `2`" in markdown


def test_minimal_report_does_not_render_private_observations_or_revision():
    issue = report()["issues"][0] | {
        "candidate_evidence_kind": "observations_not_correspondence", "git_revision": "b" * 40,
        "candidate_samples": [{"git_path": "PRIVATE_PATH.view", "comparison": "different"}],
    }
    markdown = render_markdown_report(report(issues=[issue]))
    assert "PRIVATE_PATH" not in markdown
    assert "b" * 40 not in markdown
    assert "Candidate path observations" not in markdown


def test_different_content_observation_does_not_describe_uncompared_bytes_as_equal():
    issue = report()["issues"][0] | {
        "candidate_evidence_kind": "observations_not_correspondence",
        "candidate_samples": [{"git_path": "omni/orders.view", "omni_path": "orders.view",
                               "comparison": "different", "pairing": "exact_or_unpaired"}],
    }
    markdown = render_markdown_report(report(diagnostic_audience="repository", issues=[issue]))
    assert "contents differ" in markdown
    assert "contents not compared" not in markdown


def test_query_report_points_to_exact_trusted_policy_assertion_and_safe_check_counts():
    from omniflow.diagnostics import public_diagnostic

    source = report(issues=[{
        "validator": "query_validation", "type": "query_validation_issue", "severity": "error", "blocking": True,
        "reason_code": "query_contract_regression", "query_contract_number": 2,
        "query_assertion_number": 3, "query_assertion_type": "null_rate",
        "contract_id": "PRIVATE_CONTRACT", "query": "PRIVATE_QUERY", "value": 0.87654,
    }], model_reports=[{"check_reports": [{
        "validator": "query_validation", "query_validation_status": "regression", "query_validation_mode": "enforce",
        "execution_status": "completed", "validation_complete": True, "contracts_total": 2,
        "contracts_checked": 2, "new_issue_count": 1, "existing_issue_count": 0,
        "query_submission_count": 6, "query_poll_count": 3,
    }]}])
    text = render_markdown_report(public_diagnostic(source))
    assert "trusted policy `checks.query_validation.contracts` entry 2, assertion 3 (`null_rate`)" in text
    assert "positions are one-based" in text
    assert "query validation status: `regression`" in text
    assert "contracts checked: `2`" in text and "query submission count: `6`" in text
    assert "PRIVATE_CONTRACT" not in text and "PRIVATE_QUERY" not in text and "0.87654" not in text
