import copy
import json
from xml.etree import ElementTree

import pytest

from omniflow.diagnostics import public_diagnostic
from omniflow.github.annotations import public_annotation_lines
from omniflow.reporting.junit_report import to_junit
from omniflow.reporting.markdown_report import render_markdown_report
from omniflow.reporting.sarif_report import to_sarif


@pytest.mark.parametrize("finding,evidence", [
    ({"validator": "setup", "reason_code": "config_invalid_type", "config_key": "checks.content_validation.force_full_validation",
      "expected_type": "boolean", "source_kind": "policy", "source_line": 17},
     {"config_key": "checks.content_validation.force_full_validation", "expected_type": "boolean", "source_line": 17}),
    ({"validator": "content", "reason_code": "omni_forbidden", "http_status": 403,
      "api_operation": "content_validation", "attempt_count": 1, "validation_scope": "base"},
     {"http_status": 403, "attempt_count": 1, "validation_scope": "base"}),
    ({"validator": "content", "type": "content_evidence_unavailable", "reason_code": "document_identity_transport_unavailable",
      "api_operation": "document_state", "transport_reason": "omni_timeout", "attempt_count": 3, "max_attempts": 3,
      "document_reference": "document-1"},
     {"api_operation": "document_state", "transport_reason": "omni_timeout", "attempt_count": 3, "max_attempts": 3,
      "document_reference": "document-1"}),
    ({"validator": "candidate", "type": "candidate_verification_failed", "candidate_reason": "verification_unavailable",
      "metadata_stage": "view_names", "metadata_reason": "unsupported_orientation", "validation_scope": "base"},
     {"metadata_stage": "view_names", "metadata_reason": "unsupported_orientation", "validation_scope": "base"}),
    ({"validator": "model", "type": "model_validation_issue", "state": "existing", "validation_scope": "branch"},
     {"state": "existing", "validation_scope": "branch"}),
])
@pytest.mark.parametrize("blocking", [True, False])
def test_safe_evidence_survives_formats_without_changing_policy(finding, evidence, blocking):
    raw = {"policy_decision": "fail" if blocking else "pass", "validation_complete": True,
           "exit_code": int(blocking), "summary": {"errors": 1}, "issues": [{
               **finding, "severity": "error", "blocking": blocking,
               "message": "SYNTHETIC_PRIVATE_VALUE", "sql": "SYNTHETIC_PRIVATE_VALUE",
               "file": "SYNTHETIC_PRIVATE_PATH", "source_line": finding.get("source_line", 17),
           }]}
    before = copy.deepcopy(raw)
    safe = public_diagnostic(raw)
    assert raw == before
    issue = safe["issues"][0]
    sarif = to_sarif(safe)["runs"][0]["results"][0]
    junit = ElementTree.fromstring(to_junit(safe))
    detail = junit.find("testcase/failure" if blocking else "testcase/system-out").text
    annotation = "\n".join(public_annotation_lines(safe["issues"], limit=10))
    markdown = render_markdown_report(safe)
    for key, value in evidence.items():
        assert issue[key] == value
        assert sarif["properties"][key] == value
        assert f"{key}: {value}" in detail
        assert str(value) in annotation
        assert f"{key.replace('_', ' ')}: `{value}`" in markdown
    assert junit.get("failures") == str(int(blocking))
    assert sarif["properties"]["policy_blocking"] is blocking
    assert safe["exit_code"] == int(blocking)
    for output in (json.dumps(safe), json.dumps(sarif), detail, annotation, markdown):
        assert "SYNTHETIC_PRIVATE" not in output
    assert "locations" not in sarif


@pytest.mark.parametrize("reason", ["unsupported_orientation", "duplicate_name", "duplicate_path",
                                   "invalid_identity", "non_view_identity", "incomplete_identity"])
def test_bad_view_metadata_identifies_response_problem_not_folder_remediation(reason):
    issue = public_diagnostic({"issues": [{
        "validator": "candidate", "type": "candidate_verification_failed", "severity": "error",
        "candidate_reason": "verification_unavailable", "metadata_stage": "view_names", "metadata_reason": reason,
    }]})["issues"][0]
    assert issue["diagnostic_code"] == "OF-CANDIDATE-VIEW-METADATA"
    assert issue["category"] == "incomplete_evidence"
    assert "do not rename files or move folders" in issue["next_action"]
    assert "does not establish a model or content defect" in issue["message"]


def test_transport_cause_keeps_precedence_over_metadata_hint():
    issue = public_diagnostic({"issues": [{
        "validator": "candidate", "severity": "error", "cause_code": "omni_forbidden",
        "metadata_stage": "view_names", "metadata_reason": "incomplete_identity",
    }]})["issues"][0]
    assert issue["diagnostic_code"] == "OF-OMNI-FORBIDDEN"
