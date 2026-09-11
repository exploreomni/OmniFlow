import json
from collections import UserDict
from types import SimpleNamespace
from unittest import mock

import pytest

from omniflow.artifacts import write_public_reports
from omniflow.exceptions import OmniAPIError
from omniflow.github.annotations import annotation_lines
from omniflow.omni_client import OmniClient
from omniflow.security import public_safe
from omniflow.validators.content import run_content_validation
from omniflow.validators.model import parse_model_issues, run_model_validation


def client_for(payload):
    client = OmniClient(base_url="https://omni.example", api_key="synthetic-api-evidence-test")
    client._request = mock.Mock(return_value=payload)
    return client


@pytest.mark.parametrize("payload", [
    None, {}, [None], ["private-invalid-row"], [{"message": "known error"}, None],
    [{}], [{"message": None}], [{"message": {"private": "invalid-message"}}],
    *[[{"message": "known error", "is_warning": flag}] for flag in ("false", "true", 0, 1, None, [], {})],
])
def test_malformed_model_evidence_fails_in_client_and_parser(payload):
    for operation in (lambda: client_for(payload).validate_model("model"), lambda: parse_model_issues(payload)):
        with pytest.raises(OmniAPIError) as caught:
            operation()
        assert caught.value.exit_code == 4
        assert "private" not in str(caught.value)


@pytest.mark.parametrize("field, value", [
    ("yaml_path", {"private": "invalid-path"}), ("yaml_path", ["invalid-path"]), ("yaml_path", 42),
    ("auto_fix", "invalid-fix"), ("auto_fix", []),
    ("auto_fix", {"description_short": {"private": "invalid-description"}}),
    ("auto_fix", {"description_unique": 42}), ("auto_fix", {"description_unique": None}),
])
def test_model_metadata_is_typed_before_it_reaches_reports(field, value):
    payload = [{"message": "Known issue", "is_warning": False, field: value}]
    for operation in (lambda: client_for(payload).validate_model("model"), lambda: parse_model_issues(payload)):
        with pytest.raises(OmniAPIError) as caught:
            operation()
        assert "private" not in str(caught.value)


@pytest.mark.parametrize("payload, fail_on_warnings, expected_code, severity", [
    ([], False, 0, None),
    ([{"message": "Known error", "is_warning": False}], False, 1, "error"),
    ([{"message": "Known warning", "is_warning": True}], False, 0, "warning"),
    ([{"message": "Known warning", "is_warning": True}], True, 1, "warning"),
    ([{"message": "Unspecified severity"}], False, 1, "error"),
    ([{"message": "Optional metadata", "yaml_path": None, "auto_fix": None}], False, 1, "error"),
])
def test_valid_model_controls_keep_error_and_warning_policy(payload, fail_on_warnings, expected_code, severity):
    report, code = run_model_validation(
        client=client_for(payload), model_id="model", branch_id=None, fail_on_warnings=fail_on_warnings,
    )
    assert code == expected_code
    if severity:
        assert report["issues"][0]["severity"] == severity
        assert report["issues"][0]["blocking"] is bool(expected_code)
    else:
        assert report["issues"] == []
        assert report["summary"] == {"total_issues": 0, "errors": 0, "warnings": 0}


def test_model_parser_does_not_trust_alternate_client_severity_coercion():
    client = SimpleNamespace(validate_model=lambda *args, **kwargs: [{"message": "Error", "is_warning": "false"}])
    with pytest.raises(OmniAPIError):
        run_model_validation(client=client, model_id="model", branch_id=None)


def test_model_response_allowlists_metadata_without_retaining_raw_payload():
    payload = [{
        "message": "Known issue", "is_warning": False, "yaml_path": "orders.view",
        "auto_fix": {
            "description_short": "Repair the field", "description_unique": "Repair orders field",
            "unknown_private": {"nested": "discard-me"},
        },
        "unknown_private": "discard-me", "raw": "discard-me",
    }]
    normalized = client_for(payload).validate_model("model")
    assert set(normalized[0]) == {"message", "is_warning", "yaml_path", "auto_fix"}
    assert normalized[0]["auto_fix"] == {
        "description_short": "Repair the field", "description_unique": "Repair orders field",
    }
    assert "discard-me" not in json.dumps(parse_model_issues(normalized))
    assert payload[0]["auto_fix"]["unknown_private"] == {"nested": "discard-me"}


def test_strict_public_outputs_redact_all_auto_fix_metadata(tmp_path):
    marker = "synthetic-private-model-field"
    report, code = run_model_validation(
        client=client_for([{
            "message": "Known error", "is_warning": False,
            "auto_fix": {"description_short": marker, "description_unique": marker},
        }]),
        model_id="model", branch_id=None,
    )
    assert code == 1
    safe = write_public_reports(
        report, output_dir=tmp_path, formats=["json", "markdown", "sarif", "junit"], redaction_level="strict",
    )
    assert safe["issues"][0]["auto_fix"] == "[REDACTED]"
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert marker not in path.read_text()
    assert marker not in "\n".join(annotation_lines(safe["issues"]))
    # Standard policy retains documented suggestions, while strict removes the
    # whole object even if a caller bypasses the model-response normalizer.
    assert public_safe(report)["issues"][0]["auto_fix"]["description_short"] == marker
    assert public_safe({"auto_fix": {"unexpected": marker}}, redaction_level="strict") == {
        "auto_fix": "[REDACTED]",
    }


@pytest.mark.parametrize("payload", [
    None, [], {}, {"pageInfo": {}}, {"records": None}, {"records": {}},
    {"records": [None]}, {"records": [{"identifier": "doc"}, "private-invalid-record"]},
    {"records": [], "pageInfo": None}, {"records": [], "pageInfo": []},
    {"records": [], "pageInfo": {"hasNextPage": "false"}},
    {"records": [], "pageInfo": {"hasNextPage": 0}},
    {"records": [], "pageInfo": {"hasNextPage": True}},
    {"records": [], "pageInfo": {"hasNextPage": True, "nextCursor": None}},
    {"records": [], "pageInfo": {"hasNextPage": False, "nextCursor": "next"}},
    {"records": [], "pageInfo": {"nextCursor": " "}},
    {"records": [], "pageInfo": {"nextCursor": 42}},
])
def test_malformed_pagination_never_becomes_an_empty_catalog(payload):
    with pytest.raises(OmniAPIError) as caught:
        client_for(payload).list_content(labels=["Verified"])
    assert caught.value.exit_code == 4
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("payload", [
    {"records": []}, {"records": [], "pageInfo": {}},
    {"records": [], "pageInfo": {"hasNextPage": False}},
    {"records": [], "pageInfo": {"hasNextPage": False, "nextCursor": None}},
    UserDict({"records": [], "pageInfo": UserDict({"nextCursor": None})}),
])
def test_explicit_empty_catalog_and_optional_page_metadata_remain_valid(payload):
    assert client_for(payload).list_content(labels=["Verified"]) == []


def test_valid_pagination_follows_documented_continuation_before_returning_records():
    client = client_for(None)
    client._request.side_effect = [
        {"records": [{"identifier": "first"}], "pageInfo": {"hasNextPage": True, "nextCursor": "next"}},
        {"records": [{"identifier": "second"}], "pageInfo": {"hasNextPage": False, "nextCursor": None}},
    ]
    assert client.list_content() == [{"identifier": "first"}, {"identifier": "second"}]
    assert client._request.call_args_list[1].kwargs["params"]["cursor"] == "next"


@pytest.mark.parametrize("catalog", [None, {}, {"records": [None]}])
def test_broken_content_is_not_hidden_by_malformed_catalog(tmp_path, catalog):
    client = client_for(catalog)
    client.validate_content = mock.Mock(return_value={"content": [{
        "identifier": "doc", "dashboard_filter_issues": ["Known broken field"],
    }]})
    with pytest.raises(OmniAPIError):
        run_content_validation(
            client=client, model_id="model", branch_id=None, user_id=None,
            include_personal_folders=False, labels=["Verified"], fail_on_new_only=False,
            history_in=tmp_path / "absent.json", history_out=tmp_path / "history.json",
            report_out=tmp_path / "report.json",
        )
    assert not (tmp_path / "report.json").exists()
