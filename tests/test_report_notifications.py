from __future__ import annotations

from copy import deepcopy

import pytest

from omniflow.github.report_publisher import (
    MARKER,
    STATE_PREFIX,
    TRANSITION_PREFIX,
    PublicationUnavailable,
    Superseded,
    _read_json,
    publication_receipts,
    publish,
)
from omniflow.reporting.notification import (
    notification_decision,
    query_result_coverage,
    render_notification,
    render_pr_comment,
    render_transition_notice,
)

HEAD = "a" * 40
BASE = "b" * 40
RUN_URL = "https://github.com/example/analytics/actions/runs/123"


def environment():
    return {
        "GITHUB_EVENT_NAME": "pull_request_target", "GITHUB_REPOSITORY": "example/analytics",
        "GITHUB_SERVER_URL": "https://github.com", "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
        "DIAGNOSTIC_OUTPUT_READY": "true", "PUBLIC_UPLOAD_OUTCOME": "success", "PUBLIC_ARTIFACT_ID": "17",
        "PUBLIC_ARTIFACT_URL": RUN_URL + "/artifacts/17", "REPOSITORY_UPLOAD_OUTCOME": "success",
        "REPOSITORY_ARTIFACT_ID": "18", "REPOSITORY_ARTIFACT_URL": RUN_URL + "/artifacts/18",
        "IMPACT_UPLOAD_OUTCOME": "success", "IMPACT_ARTIFACT_ID": "19",
        "IMPACT_ARTIFACT_URL": RUN_URL + "/artifacts/19",
        "REPOSITORY_READY": "true", "PRIVATE_REPOSITORY": "true",
    }


def event():
    return {"number": 37, "pull_request": {
        "state": "open", "head": {"sha": HEAD, "ref": "test", "repo": {"full_name": "example/analytics"}},
        "base": {"sha": BASE, "ref": "main", "repo": {"full_name": "example/analytics"}},
    }}


def report(decision="fail"):
    return {
        "policy_decision": decision, "validation_complete": True, "exit_code": int(decision == "fail"),
        "summary": {"new_issues": 4 if decision == "fail" else 0, "existing_issues": 340},
        "issues": ([{"validator": "model", "type": "model_validation_error", "severity": "error",
                     "blocking": True, "message": "RAW_API_SENTINEL", "file": "PRIVATE_MODEL_SENTINEL"}] * 4
                   if decision == "fail" else []) + [
            {"validator": "semantic_lint", "severity": "warning", "blocking": False,
             "message": "RAW_SQL_SENTINEL", "field": "PRIVATE_FIELD_SENTINEL"} for _ in range(340)
        ],
    }


class API:
    def __init__(self):
        self.live = event()["pull_request"]
        self.run = {"id": 123, "run_number": 9, "run_attempt": 1, "workflow_id": 42,
                    "created_at": "2026-10-20T10:00:00Z",
                    "event": "pull_request_target", "head_branch": "test",
                    "head_repository": {"full_name": "example/analytics"}, "pull_requests": [{"number": 37}]}
        self.runs = [self.run]
        self.comments = []
        self.calls = []
        self.before = None
        self.fail_transition_after_save = False

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.before:
            self.before(method, path)
        if method == "GET":
            if path == "/pulls/37":
                return deepcopy(self.live)
            if path.startswith("/actions/runs/"):
                return deepcopy(self.run)
            page = int(path.split("page=")[-1])
            start = (page - 1) * 100
            if path.startswith("/actions/workflows/"):
                return {"workflow_runs": deepcopy(self.runs[start:start + 100]), "total_count": len(self.runs)}
            if path.startswith("/issues/37/comments?"):
                return deepcopy(self.comments[start:start + 100])
        if method == "POST":
            value = {"id": len(self.comments) + 1000, "body": body["body"],
                     "user": {"login": "github-actions[bot]", "type": "Bot"}}
            self.comments.append(value)
            if TRANSITION_PREFIX in value["body"] and self.fail_transition_after_save:
                self.fail_transition_after_save = False
                raise PublicationUnavailable("Synthetic ambiguous write.")
            return value
        if method == "PATCH":
            for value in self.comments:
                if value["id"] == int(path.rsplit("/", 1)[-1]):
                    value["body"] = body["body"]
                    return value
        raise AssertionError((method, path))

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


def test_short_notification_has_blockers_current_head_action_and_no_private_or_raw_text():
    text = render_notification(report(), head_sha=HEAD, run_url=RUN_URL, run_attempt=1,
                               publication=publication_receipts(environment()))
    assert "Blocking findings: 4" in text
    assert HEAD in text and RUN_URL in text
    assert "4 new or worsened" in text
    assert "**Next action:**" in text
    assert "340 (340 lint)" in text
    assert "Query-result correctness:** Not tested" in text
    assert "RAW_" not in text and "PRIVATE_" not in text
    assert len(text) < 2500
    assert text.count("### Diagnostic publication") == 1
    assert "unconfirmed" not in text


def comparison_report(validator, **changes):
    return {"validator": validator, "comparison_status": "complete", "validation_complete": True,
            "new_issues": 0, **changes}


def test_compact_pass_is_four_lines_even_with_large_existing_debt_and_does_not_mutate_evidence():
    data = report("pass") | {
        "check_reports": [comparison_report("model"), comparison_report("content")],
        "issues": [{"validator": "model", "severity": "error", "blocking": False,
                    "state": "existing", "message": "PRIVATE_MODEL_SENTINEL"}] * 12000,
    }
    before = deepcopy(data)
    text = render_pr_comment(data, head_sha=HEAD, run_url=RUN_URL, publication=publication_receipts(environment()))
    assert len(text.splitlines()) == 4 and len(text) < 600
    assert "Validation passed" in text and "Model: 0 new · Content: 0 new" in text
    assert HEAD[:7] in text and HEAD not in text
    assert f"[Full report]({RUN_URL}/artifacts/17)" in text
    for excluded in ("PRIVATE_", "12000", "Next action", "Diagnostic publication", "Query-result correctness",
                     "artifacts/18", "Nonblocking findings"):
        assert excluded not in text
    assert data == before and notification_decision(data) == "pass"
    # Detailed coverage/receipts remain available on the separate Actions surface.
    summary = render_notification(data, head_sha=HEAD, run_url=RUN_URL, run_attempt=1,
                                  publication=publication_receipts(environment()))
    assert "12000" in summary and "Query-result correctness" in summary and "artifacts/18" in summary


@pytest.mark.parametrize("context,expected", [
    ({}, "Model: see report"),
    ({"check_states": [{"validator": "model", "status": "disabled"}]}, "Model: disabled"),
    ({"check_reports": [comparison_report("model", new_issues=2)]}, "Model: 2 new"),
    ({"check_reports": [comparison_report("model", comparison_status="not_applicable")]}, "Model: see report"),
    ({"check_reports": [comparison_report("model", validation_complete=False)]}, "Model: see report"),
    ({"check_reports": [comparison_report("model", new_issues=-1)]}, "Model: see report"),
    ({"check_reports": [comparison_report("model")],
      "check_states": [{"validator": "model", "status": "not_run"}]}, "Model: see report"),
])
def test_compact_counts_require_completed_comparison_evidence(context, expected):
    text = render_pr_comment(report("pass") | context, head_sha=HEAD, run_url=RUN_URL)
    assert expected in text


def test_compact_multimodel_counts_preserve_disabled_and_missing_coverage():
    data = report("pass") | {"model_reports": [
        {"check_reports": [comparison_report("model", new_issues=2), comparison_report("content")]},
        {"check_states": [{"validator": "model", "status": "disabled"}]},
    ]}
    text = render_pr_comment(data, head_sha=HEAD, run_url=RUN_URL)
    assert "Model: 2 new (enabled scope)" in text and "Content: see report" in text
    data["model_reports"][1] = {"check_reports": [comparison_report("model", new_issues=3)]}
    assert "Model: 5 new" in render_pr_comment(data, head_sha=HEAD, run_url=RUN_URL)


@pytest.mark.parametrize("decision,complete,exit_code,title", [
    ("pass", False, 0, "Validation incomplete"),
    ("skipped", False, 0, "Skipped — not validated"),
    ("advisory", False, 0, "Advisory — impact unverified"),
    ("fail", False, 2, "Action required"),
])
def test_compact_nonpass_never_uses_zero_findings_as_success(decision, complete, exit_code, title):
    data = report("pass") | {"policy_decision": decision, "validation_complete": complete, "exit_code": exit_code}
    before = deepcopy(data)
    text = render_pr_comment(data, head_sha=HEAD, run_url=RUN_URL)
    assert title in text and "Validation is incomplete" in text
    assert "Validation passed" not in text and "No blocking findings" not in text
    assert "**Next action:**" in text and data == before


def test_compact_failure_limits_groups_but_counts_all_blockers_and_retains_action():
    issue = {"validator": "query_validation", "type": "query_validation_issue",
             "reason_code": "query_contract_regression", "severity": "error", "blocking": True,
             "query_contract_number": 1, "query_assertion_type": "group_count",
             "message": "RAW_SQL_SENTINEL", "next_action": "PRIVATE_ACTION_SENTINEL"}
    data = report() | {"issues": [issue | {"query_assertion_number": n} for n in range(1, 8)]}
    before = deepcopy(data)
    text = render_pr_comment(data, head_sha=HEAD, run_url=RUN_URL, publication=publication_receipts(environment()))
    assert "Blocking findings: 7" in text and text.count("- **OF-") == 3
    assert "Plus 4 additional blocking occurrence(s)" in text
    assert "entry 1, assertion 3" in text and "assertion 4" not in text
    assert "**Next action:**" in text and "RAW_" not in text and "PRIVATE_" not in text
    assert len(text) < 2000 and data == before


def test_compact_candidate_failure_is_not_described_as_a_new_model_defect():
    data = report() | {"validation_complete": False, "exit_code": 2, "issues": [{
        "validator": "candidate", "type": "candidate_verification_failed", "severity": "error",
        "blocking": True, "candidate_reason": "content_mismatch",
    }]}
    text = render_pr_comment(data, head_sha=HEAD, run_url=RUN_URL)
    assert "Validation is incomplete" in text and "**Next action:**" in text
    assert "No blocking findings" not in text and "Model: 0 new" not in text
    assert "OF-CANDIDATE" in text


@pytest.mark.parametrize("receipt", [
    {"kind": "public", "status": "failed", "url": RUN_URL + "/artifacts/17"},
    {"kind": "public", "status": "skipped", "url": RUN_URL + "/artifacts/17"},
    {"kind": "public", "status": "uploaded", "url": RUN_URL.replace("123", "122") + "/artifacts/17"},
    {"kind": "public", "status": "uploaded", "url": RUN_URL + "/artifacts/17)\nINJECT"},
    {"kind": "repository", "status": "uploaded", "url": RUN_URL + "/artifacts/18"},
])
def test_compact_unconfirmed_or_private_artifact_never_becomes_full_report_link(receipt):
    text = render_pr_comment(report("pass"), head_sha=HEAD, run_url=RUN_URL, publication=[receipt])
    assert "[Full report]" not in text and "INJECT" not in text
    assert "Report download unavailable" in text and "Validation passed" in text


def impact_report():
    return report("pass") | {"content_impact": {
        "status": "partial", "coverage_complete": False,
        "summary": {"dashboards": 2, "workbooks": 0, "queries": 3},
        "coverage_gaps": [{"reason_code": "content_impact_field_identity_unavailable"}],
    }}


def test_impact_comment_links_only_generated_uploaded_private_details_and_stays_compact():
    receipts = publication_receipts(environment() | {"CONTENT_IMPACT_READY": "true"})
    text = render_pr_comment(impact_report(), head_sha=HEAD, run_url=RUN_URL, publication=receipts)
    assert len(text.splitlines()) == 5
    assert f"[View affected content]({RUN_URL}/artifacts/19)" in text
    assert f"[Full report]({RUN_URL}/artifacts/17)" in text
    assert "repository-only" in text and "lower bounds" in text
    assert "PRIVATE_" not in text
    summary = render_notification(impact_report(), head_sha=HEAD, run_url=RUN_URL, run_attempt=1,
                                  publication=receipts)
    assert "View affected content" in summary and "matched uniquely" in summary


@pytest.mark.parametrize("changes,explanation", [
    ({}, "not generated or not authorized"),
    ({"CONTENT_IMPACT_READY": "true", "PRIVATE_REPOSITORY": "false"}, "not generated or not authorized"),
    ({"CONTENT_IMPACT_READY": "true", "REPOSITORY_READY": "false"}, "not generated or not authorized"),
    ({"CONTENT_IMPACT_READY": "true", "DIAGNOSTIC_OUTPUT_READY": "false"}, "not generated or not authorized"),
    ({"CONTENT_IMPACT_READY": "true", "IMPACT_UPLOAD_OUTCOME": "failure"}, "upload failed"),
    ({"CONTENT_IMPACT_READY": "true", "IMPACT_UPLOAD_OUTCOME": "skipped"}, "upload unconfirmed"),
    ({"CONTENT_IMPACT_READY": "true", "IMPACT_UPLOAD_OUTCOME": ""}, "upload unconfirmed"),
    ({"CONTENT_IMPACT_READY": "true", "IMPACT_ARTIFACT_URL": RUN_URL.replace("123", "122") + "/artifacts/19"},
     "upload unconfirmed"),
])
def test_impact_detail_link_requires_current_eligible_receipt(changes, explanation):
    data = impact_report()
    # Report content itself cannot authorize an artifact disclosure.
    data["content_impact"]["content_impact_details"] = "generated"
    text = render_pr_comment(data, head_sha=HEAD, run_url=RUN_URL,
                             publication=publication_receipts(environment() | changes))
    assert "View affected content" not in text and "artifacts/19" not in text
    assert explanation in text and "Validation passed" in text


@pytest.mark.parametrize("url", ["https://evil.invalid/data", RUN_URL + ")\nINJECT", RUN_URL + "/../123"])
def test_compact_links_and_transition_notices_reject_untrusted_urls(url):
    text = render_pr_comment(report("pass"), head_sha="HEAD_INJECT", run_url=url,
                             publication=[{"kind": "public", "status": "uploaded", "url": url + "/artifacts/17"}])
    text += render_transition_notice(transition="recovery", head_sha="HEAD_INJECT", run_url=url)
    assert "Run link unavailable" in text and "INJECT" not in text and "evil.invalid" not in text
    assert "[Full report]" not in text and "[View current result]" not in text


@pytest.mark.parametrize("value", ["https://evil.invalid/data", RUN_URL + "/../123", RUN_URL + ")\nINJECT"])
def test_notification_rejects_untrusted_link_shapes(value):
    text = render_notification(report("pass"), head_sha=HEAD, run_url=value, run_attempt=1,
                               publication=[{"kind": "public", "status": "uploaded", "url": value + "/artifacts/17"}])
    assert "[Download artifact]" not in text
    assert "INJECT" not in text and "evil.invalid" not in text


@pytest.mark.parametrize(("status", "expected"), [("failure", "failed"), ("cancelled", "failed"),
                                                   ("skipped", "skipped"), ("unknown", "unconfirmed")])
def test_publication_failure_never_changes_validation_or_uses_stale_links(status, expected):
    env = environment() | {"PUBLIC_UPLOAD_OUTCOME": status}
    receipts = publication_receipts(env)
    assert receipts[0] == {"kind": "public", "status": expected}
    text = render_notification(report("pass"), head_sha=HEAD, run_url=RUN_URL, run_attempt=1, publication=receipts)
    assert "Validation passed" in text
    assert text.count("Download artifact") == 1


def test_private_or_unfresh_upload_never_publishes_an_artifact_link():
    assert publication_receipts(environment() | {"PRIVATE_REPOSITORY": "false"})[1]["status"] == "skipped"
    assert all(item["status"] == "skipped" for item in publication_receipts(
        environment() | {"DIAGNOSTIC_OUTPUT_READY": "false"}))
    assert publication_receipts(environment() | {"PUBLIC_ARTIFACT_URL": RUN_URL.replace("123", "122")
                                                                                  + "/artifacts/17"})[0]["status"] == "unconfirmed"


@pytest.mark.parametrize(("check", "expected"), [
    ({"execution_status": "skipped", "query_validation_status": "disabled"}, "Not tested"),
    ({"execution_status": "completed"}, "Inconclusive"),
    ({"execution_status": "completed", "query_validation_status": "passed", "validation_complete": False}, "Inconclusive"),
    ({"execution_status": "completed", "query_validation_status": "passed", "validation_complete": True}, "Passed"),
    ({"execution_status": "completed", "query_validation_status": "regression"}, "Failed"),
])
def test_query_results_coverage_never_infers_correctness_from_model_pass(check, expected):
    data = {"model_reports": [{"check_reports": [{"validator": "query_validation", **check}]}]}
    assert query_result_coverage(data).startswith(expected)
    text = render_notification(report("pass") | data, head_sha=HEAD, run_url=RUN_URL, run_attempt=1)
    assert f"Query-result correctness:** {expected}" in text


@pytest.mark.parametrize(("other", "expected"), [
    ({"check_states": [{"validator": "query_validation", "status": "not_run", "blocked_by": "candidate"}]}, "Inconclusive"),
    ({}, "Inconclusive"),
    ({"check_states": [{"validator": "query_validation", "status": "disabled"}]}, "Passed"),
])
def test_query_coverage_does_not_ignore_another_models_unrun_check(other, expected):
    passed = {"check_reports": [{"validator": "query_validation", "query_validation_status": "passed",
                                  "execution_status": "completed", "validation_complete": True}]}
    data = report("pass") | {"model_reports": [passed, other]}
    assert query_result_coverage(data).startswith(expected)
    text = render_notification(data, head_sha=HEAD, run_url=RUN_URL, run_attempt=1)
    assert f"Query-result correctness:** {expected}" in text


def test_all_disabled_models_are_not_tested_but_enabled_unrun_query_is_inconclusive():
    disabled = {"check_states": [{"validator": "query_validation", "status": "disabled"}]}
    assert query_result_coverage({"model_reports": [disabled, disabled]}).startswith("Not tested")
    assert query_result_coverage({"check_states": [{"validator": "query_validation", "status": "not_run"}]}).startswith("Inconclusive")
    passed_with_incomplete_execution = {
        "check_reports": [{"validator": "query_validation", "query_validation_status": "passed", "validation_complete": True}],
        "check_states": [{"validator": "query_validation", "status": "partial"}],
    }
    assert query_result_coverage(passed_with_incomplete_execution).startswith("Inconclusive")


def test_query_notifications_preserve_distinct_assertion_positions_without_contract_names_or_values():
    from omniflow.diagnostics import public_diagnostic
    from omniflow.reporting.finding_groups import group_findings

    issue = {"validator": "query_validation", "type": "query_validation_issue",
             "reason_code": "query_contract_regression", "severity": "error", "blocking": True,
             "query_contract_number": 1, "query_assertion_number": 1, "query_assertion_type": "group_count",
             "contract_id": "PRIVATE_CONTRACT_SENTINEL", "query": "RAW_QUERY_SENTINEL", "value": 123.456}
    findings = [issue, issue | {"query_assertion_number": 2}, issue | {"query_contract_number": 2}, issue]
    data = report() | {"issues": findings}
    groups = group_findings(public_diagnostic(data)["issues"])
    assert len(groups) == 3
    assert sorted(count for _, count in groups) == [1, 1, 2]
    text = render_notification(data, head_sha=HEAD, run_url=RUN_URL, run_attempt=1)
    for reference in ("entry 1, assertion 1", "entry 1, assertion 2", "entry 2, assertion 1"):
        assert reference in text
    assert text.count("(`group_count`)") == 3
    assert "PRIVATE_CONTRACT_SENTINEL" not in text and "RAW_QUERY_SENTINEL" not in text and "123.456" not in text


def test_failure_rerun_is_idempotent_and_recovery_posts_one_distinct_notification():
    api = API()
    assert publish(api, report(), event(), environment())["transition"] == "failure"
    assert len(api.comments) == 1  # The initial failure summary is itself the notification.
    publish(api, report(), event(), environment())
    assert len(api.writes) == 1  # No redundant stable PATCH or transition POST.
    api.run.update(id=124, run_number=10)
    env = environment() | {"GITHUB_RUN_ID": "124", "PUBLIC_ARTIFACT_URL": RUN_URL.replace("123", "124") + "/artifacts/17",
                           "REPOSITORY_ARTIFACT_URL": RUN_URL.replace("123", "124") + "/artifacts/18"}
    result = publish(api, report("pass"), event(), env)
    assert result == {"status": "published", "transition": "recovery", "email_delivery": "unverified"}
    assert len(api.comments) == 2
    assert "Recovery notification" in api.comments[-1]["body"]
    notice = api.comments[-1]["body"].split("-->\n", 1)[1]
    assert len(notice.splitlines()) == 1 and len(notice) < 250
    assert "[View current result]" in notice and "Blocking findings" not in notice
    assert "### Diagnostic publication" not in api.comments[0]["body"]
    assert "runs/123" not in api.comments[0]["body"] + api.comments[-1]["body"]
    publish(api, report("pass"), event(), env)
    assert len(api.comments) == 2


def test_ambiguous_notification_post_is_reconciled_not_blindly_retried():
    api = API()
    api.fail_transition_after_save = True
    with pytest.raises(PublicationUnavailable):
        publish(api, report(), event(), environment())
    assert len(api.comments) == 1
    publish(api, report(), event(), environment())
    assert len(api.comments) == 1


@pytest.mark.parametrize("change", ["head", "base", "attempt", "closed", "newer_run", "unrelated_run"])
def test_out_of_order_and_non_authoritative_runs_never_write(change):
    api = API()
    if change in {"head", "base"}:
        api.live[change]["sha"] = "c" * 40
    elif change == "attempt":
        api.run["run_attempt"] = 2
    elif change == "closed":
        api.live["state"] = "closed"
    elif change == "newer_run":
        api.runs.append(api.run | {"id": 124, "run_number": 10})
    else:
        api.run["pull_requests"] = [{"number": 38}]
    with pytest.raises(Superseded):
        publish(api, report(), event(), environment())
    assert not api.writes


def test_freshness_is_rechecked_after_pagination_before_write():
    api = API()
    def change(method, path):
        if "/comments?" in path:
            api.live["head"]["sha"] = "c" * 40
    api.before = change
    with pytest.raises(Superseded):
        publish(api, report(), event(), environment())
    assert not api.writes


def test_comments_paginate_and_only_owned_bot_comment_can_be_replaced():
    api = API()
    api.comments = [{"id": i + 1, "body": MARKER, "user": {"login": "person", "type": "User"}}
                    for i in range(100)]
    api.comments.append({"id": 101, "body": MARKER + "\nlegacy detailed report",
                         "user": {"login": "github-actions[bot]", "type": "Bot"}})
    publish(api, report(), event(), environment())
    assert api.writes[0][0:2] == ("PATCH", "/issues/comments/101")
    assert all(item["body"] == MARKER for item in api.comments[:100])
    assert STATE_PREFIX in api.comments[100]["body"]


def test_truncated_or_excessive_pagination_fails_without_writes():
    api = API()
    api.comments = [{"id": i + 1, "body": "text", "user": {}} for i in range(1000)]
    with pytest.raises(PublicationUnavailable, match="safe bound"):
        publish(api, report(), event(), environment())
    assert not api.writes


def test_empty_pr_association_requires_exact_branch_and_source_repository():
    api = API()
    api.run["pull_requests"] = []
    publish(api, report("pass"), event(), environment())
    assert len(api.comments) == 1  # No recovery notification without a previous failure.
    api = API()
    api.run.update(pull_requests=[], head_repository={"full_name": "untrusted/fork"})
    with pytest.raises(Superseded):
        publish(api, report(), event(), environment())
    assert not api.writes


def test_incomplete_zero_exit_is_not_a_recovery():
    api = API()
    publish(api, report(), event(), environment())
    api.run.update(id=124, run_number=10)
    result = publish(api, report("pass") | {"validation_complete": False}, event(),
                     environment() | {"GITHUB_RUN_ID": "124"})
    assert result["transition"] == "none"
    assert len(api.comments) == 1
    assert "Validation incomplete" in api.comments[0]["body"]


def test_recovery_timeout_reconciles_exact_notification_without_duplicate():
    api = API()
    publish(api, report(), event(), environment())
    api.run.update(id=124, run_number=10)
    env = environment() | {"GITHUB_RUN_ID": "124"}
    api.fail_transition_after_save = True
    with pytest.raises(PublicationUnavailable):
        publish(api, report("pass"), event(), env)
    assert len(api.comments) == 2
    publish(api, report("pass"), event(), env)
    assert len(api.comments) == 2
    api.run.update(id=125, run_number=11)
    publish(api, report(), event(), environment() | {"GITHUB_RUN_ID": "125"})
    assert len(api.comments) == 3
    assert "Failure notification" in api.comments[-1]["body"]
    notice = api.comments[-1]["body"].split("-->\n", 1)[1]
    assert len(notice.splitlines()) == 1 and "[View current result]" in notice


def test_existing_newer_bot_state_prevents_old_run_overwrite_even_if_run_list_lags():
    api = API()
    api.run.update(id=124, run_number=10)
    publish(api, report("pass"), event(), environment() | {"GITHUB_RUN_ID": "124"})
    api.run.update(id=123, run_number=9)
    api.calls.clear()
    with pytest.raises(Superseded, match="newer result"):
        publish(api, report(), event(), environment())
    assert not api.writes


def test_unknown_or_truncated_run_inventory_is_not_publishable():
    api = API()
    api.runs = []
    with pytest.raises(PublicationUnavailable, match="absent"):
        publish(api, report(), event(), environment())
    assert not api.writes


@pytest.mark.parametrize("unsafe", ["symlink", "parent_symlink", "oversized", "duplicate_json"])
def test_publisher_rejects_unsafe_or_ambiguous_local_evidence(tmp_path, unsafe):
    real = tmp_path / "real"
    real.mkdir()
    path = real / "report.json"
    path.write_text('{"exit_code":0}')
    if unsafe == "symlink":
        link = tmp_path / "report.json"
        link.symlink_to(path)
        path = link
    elif unsafe == "parent_symlink":
        link = tmp_path / "alias"
        link.symlink_to(real, target_is_directory=True)
        path = link / "report.json"
    elif unsafe == "oversized":
        path.write_text("x" * 50)
    else:
        path.write_text('{"exit_code":0,"exit_code":1}')
    with pytest.raises(ValueError):
        _read_json(path, bound=40)
