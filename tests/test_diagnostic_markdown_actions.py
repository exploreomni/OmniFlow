import pytest

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


@pytest.mark.parametrize("reason,explanation", [
    ("repository_public", "GitHub identifies this repository as public"),
    ("repository_internal", "GitHub reports internal visibility"),
    ("repository_privacy_unverified", "does not establish that the repository is public"),
])
def test_privacy_reason_explains_observation_without_changing_gate(reason, explanation):
    from omniflow.diagnostics import public_diagnostic

    source = report(repository_diagnostics={"status": "unavailable", "diagnostics_reason": reason})
    safe = public_diagnostic(source)
    assert safe["repository_diagnostics"]["diagnostics_reason"] == reason
    assert safe["exit_code"] == source["exit_code"] and safe["validation_complete"] is False
    markdown = render_markdown_report(safe)
    assert explanation in markdown and "Validation decisions are unchanged" in markdown


def test_final_candidate_recheck_interruption_survives_public_projection():
    from omniflow.diagnostics import public_diagnostic

    markdown = render_markdown_report(public_diagnostic(report(check_states=[{
        "validator": "candidate", "status": "partial", "blocked_by": "content",
        "incomplete_reason": "final_recheck_not_run", "elapsed_ms": 42,
    }])))
    assert "`candidate`: `partial`" in markdown
    assert "incomplete reason `final_recheck_not_run`" in markdown
    assert "blocked by `content`" in markdown


@pytest.mark.parametrize("proof,expected", [
    ({"method": "pr_changed_files", "status": "verified", "changed_file_count": 2,
      "file_count": 1, "absent_file_count": 1}, ["changed PR files only", "Changed files: `2`", "expected absent: `1`"]),
    ({"method": "pr_changed_files", "status": "scope_empty", "changed_file_count": 0, "file_count": 0,
      "absent_file_count": 0}, ["zero Omni files in this model", "live validator results"]),
    ({"method": "authored_yaml_snapshot", "status": "verified", "file_count": 1253},
     ["full authored model", "evaluated side: `1253`"]),
])
def test_report_explains_only_the_evaluated_source_scope(proof, expected):
    from omniflow.diagnostics import public_diagnostic

    safe = public_diagnostic(report(model_reports=[{"candidate_verification": {
        **proof, "snapshot_digest": "PRIVATE", "private_path": "PRIVATE",
    }}]))
    assert safe["model_reports"][0]["candidate_verification"] == proof
    markdown = render_markdown_report(safe)
    assert all(text in markdown for text in expected) and "PRIVATE" not in markdown
    assert "`running`" not in markdown


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


@pytest.mark.parametrize("visibility", ["repository_internal", "repository_public", "repository_verified"])
def test_customer_mismatch_guidance_across_channels(visibility, monkeypatch):
    from omniflow.diagnostics import public_diagnostic
    from omniflow.github.annotations import public_annotation_lines
    from omniflow.reporting.notification import render_notification, render_pr_comment, render_transition_notice
    from omniflow.reporting.recovery import diagnostic_guide

    pin = "a" * 40
    monkeypatch.setenv("OMNIFLOW_ACTION_REF", pin)
    raw = report(issues=[{
        "validator": "candidate", "type": "candidate_verification_failed", "candidate_reason": "content_mismatch",
        "severity": "error", "blocking": True, "validation_scope": "branch", "git_file_count": 1253,
        "omni_file_count": 1253, "git_only_count": 0, "omni_only_count": 0, "compared_file_count": 379,
        "message": "SENSITIVE_SQL", "candidate_samples": [{"git_path": "SENSITIVE_PATH"}],
        "guide_anchor": "SENSITIVE_ANCHOR", "recovery_steps": ["SENSITIVE_STEP"],
    }, {"validator": "model", "type": "model_validation_issue", "severity": "error", "blocking": False,
        "regression_status": "unknown"}], repository_diagnostics={
            "status": "generated" if visibility == "repository_verified" else "unavailable",
            "diagnostics_reason": visibility,
        })
    safe = public_diagnostic(raw)
    issue = safe["issues"][0]
    url = diagnostic_guide(issue, safe)
    markdown = render_markdown_report(safe)
    assert "874" in markdown and "1 nonblocking model and content finding" in markdown
    assert "How to resolve it" in markdown.split("## Findings")[0]
    assert markdown.count("How to resolve it") == 1
    assert "Confirm the fix" in markdown and "If it persists" in markdown
    assert ("No detailed artifact was generated" in markdown) == (visibility != "repository_verified")
    args = {"head_sha": "b" * 40, "run_url": "https://github.com/demo/repo/actions/runs/123"}
    pr = render_pr_comment(raw, **args)
    summary = render_notification(raw, **args, run_attempt=1)
    notice = render_transition_notice(report=raw, transition="failure", **args)
    logs = "\n".join(public_annotation_lines(safe["issues"], limit=10))
    assert pr.index(issue["title"]) < pr.index("OF-CANDIDATE-CONTENT")
    assert issue["recovery_steps"][0] in notice and "How to resolve it" not in pr
    for channel in (markdown, pr, summary, notice, logs):
        assert f"/{pin}/" in url and url in channel
        assert "SENSITIVE_" not in channel


def test_mismatch_names_and_lines_are_private_but_the_actual_fix_is_shared(monkeypatch):
    from omniflow.diagnostics import public_diagnostic
    from omniflow.github import annotations
    from omniflow.reporting.markdown_report import candidate_location_lines
    from omniflow.reporting.notification import render_pr_comment

    metadata = {"status": "generated", "diagnostics_reason": "repository_verified"}
    raw = report(repository_diagnostics=metadata, issues=[{
        "validator": "candidate", "type": "candidate_verification_failed", "candidate_reason": "content_mismatch",
        "severity": "error", "blocking": True, "validation_scope": "branch", "difference_class": "terminal_newline_only",
        "newline_missing_from": "git",
    }])
    image = public_diagnostic(raw)
    issue = image["issues"][0]
    sample = {"git_path": "omni/private_model/agents.view.yaml", "omni_path": "agents.view",
              "comparison": "different", "pairing": "authored_identity", "first_differing_line": 9}
    private_issue = dict(issue, candidate_evidence_kind="observations_not_correspondence", candidate_samples=[sample],
                         git_revision="b" * 40, source_revision="head")
    private = dict(image, diagnostic_audience="repository", issues=[private_issue])
    top = render_markdown_report(private).split("**How to resolve it**")[0]
    assert "agents.view.yaml` differs from Omni file `agents.view` at line 9" in top
    assert "Git file is missing the final newline" in top
    assert not candidate_location_lines(private_issue, image)
    assert not candidate_location_lines(dict(private_issue, source_revision="base"), private)
    for changes in ({"git_path": "../private"}, {"first_differing_line": True}, {"pairing": "basename_hint"}):
        assert not candidate_location_lines(dict(private_issue, candidate_samples=[sample | changes]), private)

    run = "https://github.com/demo/repo/actions/runs/123"
    receipt = {"kind": "repository", "status": "uploaded", "url": f"{run}/artifacts/12"}
    log = "\n".join(annotations.public_annotation_lines(image["issues"], limit=5))
    pr = render_pr_comment(private, head_sha="b" * 40, run_url=run, publication=[receipt])
    assert log.startswith("::error::Content mismatch. The Git file is missing")
    assert "candidate_reason=" not in log and "Find the filename and line" in log
    assert f"[Open private comparison report]({receipt['url']})" in pr and "locally]" not in pr
    assert pr.startswith("## OmniFlow: Content mismatch")
    for text in (pr, log):
        assert "Add the final newline to the Git file" in text
        assert "agents.view" not in text and "line 9" not in text
    for wrong_receipt in (dict(receipt, url="https://elsewhere.invalid"), dict(receipt, status="unconfirmed")):
        fallback = render_pr_comment(private, head_sha="b" * 40, run_url=run, publication=[wrong_receipt])
        assert "Find the filename and line locally" in fallback
        assert "Open private comparison report" not in fallback
    monkeypatch.setattr(annotations, "repository_diagnostic_availability",
                        lambda: dict(metadata, candidate_summary_written=True))
    assert "Open this run’s Summary" in "\n".join(annotations.public_annotation_lines(image["issues"], limit=5))


@pytest.mark.parametrize("evidence,detail,fix,paired", [
    ({"contents_comparison": "different", "difference_class": "authored_text_changed",
      "git_only_count": 0, "omni_only_count": 0}, "beyond comments", "Confirm the intended definition", True),
    ({"contents_comparison": "not_compared", "git_only_count": 1, "omni_only_count": 0},
     "no matching file in the Omni branch", "added or renamed files", False),
    ({"contents_comparison": "not_compared", "git_only_count": 0, "omni_only_count": 2},
     "Omni branch has 2 files", "deletions and renames", False),
])
def test_changed_source_reports_an_incomplete_comparison_with_evidence_specific_fix(evidence, detail, fix, paired):
    from omniflow.diagnostics import public_diagnostic
    from omniflow.github.annotations import public_annotation_lines
    from omniflow.reporting.markdown_report import candidate_location_lines
    from omniflow.reporting.notification import render_pr_comment

    raw = report(issues=[dict(evidence, validator="candidate", type="candidate_verification_failed",
                              candidate_reason="pr_change_not_synchronized", severity="error", validation_scope="branch",
                              blocking=True)], repository_diagnostics={"status": "generated", "diagnostics_reason": "repository_verified"})
    safe = public_diagnostic(raw)
    issue = dict(safe["issues"][0], candidate_evidence_kind="observations_not_correspondence", source_revision="head",
                 git_revision="b" * 40, candidate_samples=[{"git_path": "omni/sensitive.view.yaml", "omni_path": "sensitive.view",
                     "comparison": "different", "pairing": "authored_identity", "first_differing_line": 4}])
    private = dict(safe, diagnostic_audience="repository", issues=[issue])
    assert bool(candidate_location_lines(issue, private)) is paired
    private_header = render_markdown_report(private).split("**How to resolve it**")[0]
    assert ("line 4" in private_header) is paired
    args = {"head_sha": "b" * 40, "run_url": "https://github.com/test/demo/actions/runs/123"}
    for surface in (render_pr_comment(private, **args), "\n".join(public_annotation_lines(safe["issues"], limit=3))):
        assert detail in surface and fix in surface and "OF-PR-CHANGE-NOT-SYNCHRONIZED" in surface
        assert "sensitive.view" not in surface and "line 4" not in surface and "OF-CANDIDATE-CONTENT" not in surface
