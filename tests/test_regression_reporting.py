import json
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from omniflow.artifacts import write_public_json, write_public_reports
from omniflow.diagnostics import public_diagnostic
from omniflow.github.annotations import annotation_lines
from omniflow.reporting.junit_report import to_junit
from omniflow.reporting.markdown_report import render_markdown_report
from omniflow.reporting.sarif_report import to_sarif
from omniflow.validators.model import run_model_validation

PRIVATE = "SYNTHETIC_PATIENT MRN-TEST-123 CARD-4242424242424242 synthetic@example.invalid"


@pytest.mark.parametrize("severity", ["error", "warning"])
@pytest.mark.parametrize("blocking,active", [(True, True), (False, True), (True, False), (False, False)])
def test_explicit_policy_is_authoritative_across_report_formats(severity, blocking, active):
    issue = {"validator": "model", "type": "model_validation_issue", "state": "existing",
             "severity": severity, "blocking": blocking, "active": active, "message": PRIVATE}
    safe = public_diagnostic({"issues": [issue], "summary": {"errors": int(severity == "error")}})
    expected = blocking and active
    assert safe["issues"][0]["severity"] == severity
    assert ElementTree.fromstring(to_junit(safe)).get("failures") == str(int(expected))
    result = to_sarif(safe)["runs"][0]["results"][0]
    assert result["level"] == severity
    assert result["properties"]["policy_blocking"] is expected
    assert result["kind"] == ("fail" if expected else "informational")
    assert f"Blocking issues (all checks): `{int(expected)}`" in render_markdown_report(safe)
    annotations = annotation_lines(safe["issues"])
    assert bool(annotations) is active
    if active:
        assert ("Blocks under configured policy" in annotations[0]) is blocking
        if severity == "error" and not blocking:
            assert annotations[0].startswith("::notice ")
    assert PRIVATE not in json.dumps(safe) + to_junit(safe) + json.dumps(result) + "".join(annotations)


def test_legacy_explicit_nonblocking_error_stays_out_of_blocker_section():
    report = {"policy_decision": "pass", "issues": [
        {"validator": "model", "severity": "error", "blocking": False, "message": "BASELINE-ERROR"},
    ]}
    prominent, advisory = render_markdown_report(report).split("<details>", 1)
    assert "Blocking issues (all checks): `0`" in prominent
    assert "BASELINE\\-ERROR" not in prominent and "BASELINE\\-ERROR" in advisory
    # Older reports without an explicit policy decision remain conservative.
    del report["issues"][0]["blocking"]
    assert ElementTree.fromstring(to_junit(report)).get("failures") == "1"


@pytest.mark.parametrize("redaction_level", ["standard", "strict"])
@pytest.mark.parametrize("baseline_unavailable", [False, True])
def test_model_comparison_artifacts_keep_health_policy_and_private_identity_separate(
    tmp_path, redaction_level, baseline_unavailable,
):
    current = [{"message": PRIVATE, "yaml_path": PRIVATE, "is_warning": False}]

    def validate(model_id, branch_id=None):
        assert model_id == PRIVATE
        return {"private": PRIVATE} if baseline_unavailable and branch_id is None else current

    model, code = run_model_validation(
        client=SimpleNamespace(validate_model=validate), model_id=PRIVATE,
        branch_id=PRIVATE, fail_on_new_only=True,
    )
    content = {"validator": "content", "comparison_mode": "new_only", "comparison_status": "complete",
               "comparison_source": "base_model", "content_health": "issues_in_scope",
               "regression_status": "no_new_issues_detected", "current_issues": 2, "baseline_issues": 2,
               "new_issues": 0, "existing_issues": 2, "resolved_issues": 0, "blocking_issues": 0}
    report = {"model_id": PRIVATE, "branch_id": PRIVATE, "policy_decision": "fail" if code else "pass",
              "exit_code": code, "validation_complete": not baseline_unavailable,
              "summary": model["summary"], "issues": model["issues"], "check_reports": [content, model]}
    safe = write_public_reports(report, output_dir=tmp_path, formats=["json", "markdown", "sarif", "junit"],
                                redaction_level=redaction_level)
    write_public_json(tmp_path / "public/evidence.json", report, redaction_level=redaction_level)
    model_check = safe["check_reports"][1]
    assert model_check["model_health"] == "issues_in_scope"
    assert model_check["blocking_issues"] == int(baseline_unavailable)
    assert model_check["comparison_status"] == ("unavailable" if baseline_unavailable else "complete")
    assert safe["summary"]["errors"] == 1
    assert model_check["issues"][-1]["severity"] == "error"
    assert model_check["issues"][-1]["blocking"] is False
    assert code == (4 if baseline_unavailable else 0)
    markdown = (tmp_path / "public/report.md").read_text()
    assert "### Model Comparison" in markdown and "### Content Comparison" in markdown
    assert "Current health errors: `1`" in markdown
    assert f"Blocking issues (all checks): `{int(baseline_unavailable)}`" in markdown
    assert ElementTree.parse(tmp_path / "public/junit.xml").getroot().get("failures") == str(int(baseline_unavailable))
    if baseline_unavailable:
        assert "model_comparison_unavailable" in json.dumps(safe)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            text = path.read_text()
            for marker in (PRIVATE, "MRN-TEST", "4242424242424242", "synthetic@example.invalid", "yaml_path"):
                assert marker not in text
    assert PRIVATE not in "\n".join(annotation_lines(safe["issues"]))


def test_new_model_warning_does_not_claim_a_policy_failure():
    report = public_diagnostic({"validator": "model", "comparison_mode": "new_only",
                               "comparison_status": "complete", "model_health": "issues_in_scope",
                               "current_issues": 1, "new_issues": 1, "blocking_issues": 0,
                               "issues": [{"validator": "model", "type": "model_validation_issue",
                                           "state": "new", "severity": "warning", "blocking": False}]})
    text = render_markdown_report(report)
    assert "New model issues were detected, but they do not block" in text
    assert "this check blocks the PR" not in text
    assert ElementTree.fromstring(to_junit(report)).get("failures") == "0"


@pytest.mark.parametrize("validator", ["model", "content"])
@pytest.mark.parametrize("status", ["ambiguous", "unavailable", "baseline_unavailable"])
def test_missing_comparison_never_claims_regression_success(validator, status):
    safe = public_diagnostic({"validator": validator, "comparison_mode": "new_only",
                              "comparison_status": status, "current_issues": 0,
                              "new_issues": 0, "blocking_issues": 1})
    text = render_markdown_report(safe)
    assert "A complete baseline comparison is unavailable" in text
    assert "regression check passes" not in text and "No current" not in text


def test_markdown_sample_prioritizes_comparison_blocker_over_large_baseline():
    existing = {"validator": "model", "type": "model_validation_issue", "severity": "error",
                "state": "existing", "blocking": False, "active": True}
    blocker = {"validator": "model", "type": "model_evidence_unavailable", "severity": "error",
               "reason_code": "ambiguous_model_identity", "blocking": True, "active": True}
    text = render_markdown_report(public_diagnostic({"issues": [existing] * 25 + [blocker]}))
    findings = text.split("## Findings", 1)[1]
    assert "1. `model` / `model_evidence_unavailable`" in findings
    assert "ambiguous_model_identity" in findings
    assert "Additional safe findings" in findings


def test_model_schema_fields_accept_only_fixed_vocabulary_and_numeric_counts():
    safe = public_diagnostic({"model_health": PRIVATE, "blocking_issues": PRIVATE, "improved_issues": PRIVATE,
                              "comparison_status": PRIVATE, "comparison_source": PRIVATE,
                              "issues": [{"validator": "model", "reason_code": PRIVATE, "state": PRIVATE}]})
    assert PRIVATE not in json.dumps(safe)
    safe = public_diagnostic({"model_health": "issues_in_scope", "comparison_status": "not_applicable",
                              "comparison_source": "none", "blocking_issues": 0, "improved_issues": 1,
                              "issues": [{"validator": "model", "type": "model_validation_issue",
                                          "severity": "warning", "state": "improved", "blocking": False}]})
    assert safe["comparison_status"] == "not_applicable" and safe["comparison_source"] == "none"
    assert safe["issues"][0]["state"] == "improved"
    assert "content" not in safe["issues"][0]["message"]


@pytest.mark.parametrize("reason", ["content_mismatch", "changed_during_validation", "verification_unavailable", PRIVATE])
def test_candidate_failure_keeps_only_fixed_reason_and_scope(reason):
    safe = public_diagnostic({"issues": [{
        "validator": "candidate", "type": "candidate_verification_failed", "severity": "error",
        "candidate_reason": reason, "validation_scope": "base", "message": PRIVATE,
        "model_id": PRIVATE, "file": PRIVATE, "snapshot_digest": PRIVATE,
    }]})
    finding = safe["issues"][0]
    assert ("candidate_reason" in finding) is (reason != PRIVATE)
    assert finding["validation_scope"] == "base"
    text = render_markdown_report(safe)
    assert "Check branch/base synchronization" in text
    if reason != PRIVATE:
        assert finding["candidate_reason"] == reason
        assert f"candidate reason: `{reason}`" in text
    assert PRIVATE not in json.dumps(safe) + text + "".join(annotation_lines(safe["issues"]))


@pytest.mark.parametrize("mode,complete,blocking,healthy", [
    ("all_issues", True, 0, True),
    ("all_issues", False, 1, False),
    ("new_only", True, 0, False),
    ("new_only", False, 1, False),
])
def test_empty_health_check_is_distinct_from_missing_regression_evidence(mode, complete, blocking, healthy):
    safe = public_diagnostic({"validator": "content", "comparison_mode": mode,
                              "comparison_status": "baseline_unavailable", "content_health": "no_issues_in_scope",
                              "current_issues": 0, "new_issues": 0, "blocking_issues": blocking,
                              "validation_complete": complete})
    text = render_markdown_report(safe)
    assert ("health check found no current issues" in text) is healthy
    assert ("A complete baseline comparison is unavailable" in text) is not healthy
    assert "regression check passes" not in text
