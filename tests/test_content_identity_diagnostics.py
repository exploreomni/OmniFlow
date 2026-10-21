"""Identity diagnostics explain recovery without disclosing an actor or token."""

import json

import pytest

from omniflow.diagnostic_contract import REASON_DETAILS
from omniflow.diagnostics import public_diagnostic, safe_exception_message
from omniflow.exceptions import OmniFlowError
from omniflow.github.annotations import public_annotation_lines
from omniflow.reporting.junit_report import to_junit
from omniflow.reporting.markdown_report import render_markdown_report
from omniflow.reporting.sarif_report import to_sarif

PRIVATE_MARKERS = (
    "11111111-2222-4333-8444-555555555555",
    "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    "synthetic-owner@example.invalid",
    "SYNTHETIC_IDENTITY_RAW_RESPONSE",
    "SYNTHETIC_IDENTITY_CREDENTIAL",
)


@pytest.mark.parametrize("reason,category,action_fragment", [
    ("content_identity_invalid", "incomplete_evidence", "whoami"),
    ("content_personal_user_required", "configuration", "user UUID, not membership UUID"),
    ("content_actor_unresolved", "configuration", "will not substitute another actor"),
    ("content_personal_actor_unresolved", "configuration", "will not silently substitute"),
    ("content_pat_impersonation_unsupported", "configuration", "even when they identify the PAT owner"),
    ("validation_identity_changed", "incomplete_evidence", "Start a fresh validation run"),
])
def test_identity_guidance_survives_every_public_surface_without_private_context(reason, category, action_fragment):
    private = " | ".join(PRIVATE_MARKERS)
    report = {
        "policy_decision": "fail", "exit_code": 4, "validation_complete": False,
        "issues": [{
            "validator": "content", "type": "validation_execution_failed", "severity": "error",
            "blocking": True, "reason_code": reason, "service": "omni", "api_operation": "identity_lookup",
            "message": private, "raw": private, "user_id": PRIVATE_MARKERS[0],
            "membership_id": PRIVATE_MARKERS[1], "identity_fingerprint": private,
        }],
    }
    safe = public_diagnostic(report)
    issue = safe["issues"][0]
    expected_code = "OF-" + reason.upper().replace("_", "-")
    assert issue["reason_code"] == reason
    assert issue["diagnostic_code"] == expected_code
    assert issue["category"] == category
    assert issue["blocking"] is True and safe["validation_complete"] is False
    assert action_fragment in issue["next_action"]
    assert issue["title"] == REASON_DETAILS[reason][1]
    outputs = [
        json.dumps(safe), render_markdown_report(safe), json.dumps(to_sarif(safe)), to_junit(safe),
        "\n".join(public_annotation_lines(safe["issues"], limit=5)),
        safe_exception_message(OmniFlowError(private, exit_code=4, reason_code=reason)),
    ]
    for output in outputs:
        assert expected_code in output
        assert not any(marker in output for marker in PRIVATE_MARKERS)


def test_http_404_does_not_invent_an_actor_mapping_diagnosis():
    issue = public_diagnostic({"issues": [{
        "validator": "content", "type": "validation_execution_failed", "severity": "error",
        "reason_code": "omni_http_error", "http_status": 404, "api_operation": "content_validation",
        "message": "SYNTHETIC_IDENTITY_RAW_RESPONSE", "user_id": PRIVATE_MARKERS[0],
    }]})["issues"][0]
    assert issue["diagnostic_code"] == "OF-OMNI-HTTP-ERROR"
    assert issue["http_status"] == 404
    assert "Verify the target and supported request parameters" in issue["next_action"]
    assert "membership" not in issue["title"] + issue["next_action"]
    assert "SYNTHETIC_IDENTITY_RAW_RESPONSE" not in json.dumps(issue)


@pytest.mark.parametrize("status", ["invalidated_identity", "invalidated_context", "invalidated_version"])
def test_public_history_status_keeps_invalidation_but_not_the_identity(status):
    safe = public_diagnostic({
        "history_status": status, "validation_complete": False,
        "identity_contract": "membership_v1", "identity_fingerprint": PRIVATE_MARKERS[0],
        "user_id": PRIVATE_MARKERS[1], "issues": [],
    })
    assert safe["history_status"] == status
    assert safe["validation_complete"] is False
    assert "identity_contract" not in safe and "identity_fingerprint" not in safe and "user_id" not in safe
    assert not any(marker in json.dumps(safe) for marker in PRIVATE_MARKERS)
