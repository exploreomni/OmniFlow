"""Credential/actor-bound private history and unchanged run identity."""
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omniflow.diagnostics import public_diagnostic
from omniflow.exceptions import OmniAPIError
from omniflow.omni_client import OmniClient
from omniflow.validators import content

USER = "11111111-1111-4111-8111-111111111111"
MEMBER = "22222222-2222-4222-8222-222222222222"


def document(messages=("Existing issue",), identifier="published"):
    return {"identifier": identifier, "document_id": "physical-" + identifier,
            "dashboard_filter_issues": [],
            "queries_and_issues": [{"query_id_map_key": "query", "issues": list(messages)}]}


def bound_client():
    client = SimpleNamespace(origin="https://omni.example", authorization="synthetic-token", member=MEMBER)
    client.validation_evidence_binding = lambda: (
        client.origin, hashlib.sha256(client.authorization.encode()).hexdigest(),
    )
    client.content_identity_context = lambda **kw: {
        "identity_contract": "membership_v1",
        "identity_fingerprint": hashlib.sha256(json.dumps([
            client.validation_evidence_binding(), client.member, kw,
        ], sort_keys=True).encode()).hexdigest(),
    }
    client.validate_content = Mock(return_value={"content": [document()]})
    client.list_content = Mock(return_value=[{"identifier": "published"}])
    return client


def validate(tmp_path, client, **overrides):
    options = dict(
        client=client, model_id="model", branch_id="branch", user_id=USER,
        include_personal_folders=False, labels=[], fail_on_new_only=True,
        history_in=tmp_path / "history.json", history_out=tmp_path / "history.json",
        report_out=tmp_path / "report.json",
    )
    options.update(overrides)
    return content.run_content_validation(**options)


@pytest.mark.parametrize("changed", ["origin", "authorization", "member"])
def test_history_v4_accepts_same_scope_but_never_changed_credential_origin_or_member(tmp_path, changed):
    client = bound_client()
    validate(tmp_path, client, branch_id=None, fail_on_new_only=False)
    saved = json.loads((tmp_path / "history.json").read_text())
    assert saved["schema_version"] == 4 and saved["identity_contract"] == "membership_v1"
    assert len(saved["identity_fingerprint"]) == 64
    report, code = validate(tmp_path, client, branch_id=None)
    assert code == 0 and report["existing_issues"] == 1 and report["history_status"] == "accepted"
    setattr(client, changed, {"origin": "https://other.example", "authorization": "replacement-token",
                              "member": "33333333-3333-4333-8333-333333333333"}[changed])
    report, code = validate(tmp_path, client, branch_id=None)
    assert code == 4 and report["history_status"] == "invalidated_context"
    assert report["existing_issues"] == 0 and report["new_issues"] == 1


@pytest.mark.parametrize("context_without_binding", [False, True])
def test_unverifiable_adapter_history_cannot_become_a_clean_baseline(tmp_path, context_without_binding):
    client = SimpleNamespace(validate_content=lambda *a, **kw: {"content": []}, list_content=lambda **kw: [])
    if context_without_binding:
        client.content_identity_context = lambda **kw: {
            "identity_contract": "membership_v1", "identity_fingerprint": "a" * 64,
        }
    validate(tmp_path, client, branch_id=None, fail_on_new_only=False)
    history = json.loads((tmp_path / "history.json").read_text())
    assert history["identity_fingerprint"] is None
    report, code = validate(tmp_path, client, branch_id=None)
    assert code == 4 and report["history_status"] == "invalidated_identity"
    assert report["current_issues"] == 0 and report["validation_complete"] is False
    # Missing adapter identity does not prohibit a same-run branch/base comparison.
    assert validate(tmp_path, client)[1] == 0


def test_no_actor_history_still_requires_live_base_even_when_credential_is_bound(tmp_path):
    client = bound_client()
    validate(tmp_path, client, branch_id=None, user_id=None, fail_on_new_only=False)
    report, code = validate(tmp_path, client, branch_id=None, user_id=None)
    assert code == 4 and report["history_status"] == "invalidated_unscoped_user"


@pytest.mark.parametrize("phase", ["validator", "catalog", "document_mapping", "document_recheck", "before_output"])
@pytest.mark.parametrize("change", ["authorization", "member"])
def test_drift_during_evidence_reads_cannot_write_reports_or_history(tmp_path, monkeypatch, phase, change):
    client = bound_client()
    def mutate():
        setattr(client, change, "replacement-private-value")
    if phase == "validator":
        def fetch(*args, **kwargs):
            mutate()
            return {"content": [document()]}
        client.validate_content.side_effect = fetch
    elif phase == "catalog":
        def catalog(**kwargs):
            mutate()
            return [{"identifier": "published"}]
        client.list_content.side_effect = catalog
    elif phase in {"document_mapping", "document_recheck"}:
        client.validate_content.side_effect = lambda *a, branch_id=None, **kw: {
            "content": [document(identifier="draft" if branch_id else "published")],
        }
        calls = 0
        def drafts(published):
            nonlocal calls
            calls += 1
            if calls == (1 if phase == "document_mapping" else 2):
                mutate()
            return [{"identifier": "draft", "publishedIdentifier": "published", "branch": {"id": "branch"},
                     "status": "active", "draftOutOfDate": False, "updatedAt": "2026-01-01T00:00:00Z"}]
        client.list_document_drafts = drafts
    else:
        partition = content.partition_issues
        def changed_partition(*args):
            result = partition(*args)
            mutate()
            return result
        monkeypatch.setattr(content, "partition_issues", changed_partition)
    with pytest.raises(OmniAPIError) as caught:
        validate(tmp_path, client)
    assert caught.value.reason_code == "validation_identity_changed"
    assert caught.value.diagnostic_context["comparison_mode"] == "new_only"
    assert not (tmp_path / "report.json").exists() and not (tmp_path / "history.json").exists()
    assert "replacement-private-value" not in str(caught.value)


def test_catalog_cannot_mutate_requested_label_scope(tmp_path):
    client = bound_client()
    def catalog(**kwargs):
        kwargs["labels"].append("different-scope")
        return []
    client.list_content.side_effect = catalog
    with pytest.raises(OmniAPIError) as caught:
        validate(tmp_path, client, labels=["Verified"])
    assert caught.value.reason_code == "validation_identity_changed"
    assert not (tmp_path / "report.json").exists() and not (tmp_path / "history.json").exists()


def test_identity_fingerprint_stays_in_private_history_not_report_or_public_projection(tmp_path):
    client = bound_client()
    report, code = validate(tmp_path, client)
    assert code == 0
    saved = json.loads((tmp_path / "history.json").read_text())
    fingerprint = saved["identity_fingerprint"]
    assert fingerprint not in json.dumps(report) and MEMBER not in json.dumps(report)
    assert fingerprint not in (tmp_path / "report.json").read_text()
    public = public_diagnostic({**report, "identity_fingerprint": fingerprint, "identity_contract": "membership_v1"})
    assert fingerprint not in json.dumps(public) and USER not in json.dumps(public) and MEMBER not in json.dumps(public)


class Response:
    status_code = 200
    ok = True
    headers = {}

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class Session:
    def __init__(self, new):
        self.headers = {}
        self.calls = []
        self.new = new

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/whoami"):
            return Response({"keyScope": "organization", "user": {"id": USER, "membershipId": MEMBER}})
        if url.endswith("/content-validator"):
            messages = ["Existing issue"]
            if kwargs["params"].get("branch_id") and self.new:
                messages.append("Actual new issue")
            return Response({"content": [document(messages)]})
        assert url.endswith("/api/v1/content")
        return Response({"records": [{"identifier": "published"}], "pageInfo": {"hasNextPage": False}})


@pytest.mark.parametrize("new", [False, True])
def test_real_client_and_content_runner_bind_both_sides_to_same_membership(tmp_path, new):
    session = Session(new)
    client = OmniClient(base_url="https://omni.example", api_key="synthetic-token", session=session)
    report, code = validate(tmp_path, client, include_personal_folders=True)
    assert code == int(new) and report["existing_issues"] == 1 and report["new_issues"] == int(new)
    assert report["blocking_issues"] == int(new) and report["validation_complete"] is True
    validators = [kwargs["params"] for _, url, kwargs in session.calls if url.endswith("/content-validator")]
    assert [params.get("branch_id") for params in validators] == [None, "branch"]
    assert all(params["userId"] == MEMBER != USER and params["include_personal_folders"] == "true"
               for params in validators)
    catalog = [kwargs["params"] for _, url, kwargs in session.calls if url.endswith("/api/v1/content")]
    assert [params["scope"] for params in catalog] == ["organization", "restricted"]
    assert catalog[-1]["creatorId"] == MEMBER
    assert sum(url.endswith("/whoami") for _, url, _ in session.calls) == 1
