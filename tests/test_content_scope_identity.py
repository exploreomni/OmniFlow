import json

import pytest

from omniflow.diagnostics import public_diagnostic
from omniflow.exceptions import ConfigError, OmniAPIError, OmniAuthError, exception_diagnostic
from omniflow.omni_client import OmniClient

USER = "11111111-1111-4111-8111-111111111111"
MEMBER = "22222222-2222-4222-8222-222222222222"
OTHER_USER = "33333333-3333-4333-8333-333333333333"
OTHER_MEMBER = "44444444-4444-4444-8444-444444444444"
PRIVATE = "SYNTHETIC_PRIVATE_IDENTITY_TEXT"


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.headers = {}
        self.ok = status == 200
        self.text = PRIVATE

    def json(self):
        return self.payload


class Session:
    def __init__(self, responses):
        self.headers = {}
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def identity(scope="organization", user=USER, member=MEMBER):
    return {"keyScope": scope, "user": {"id": user, "membershipId": member, "email": PRIVATE}}


def page(records=(), cursor=None):
    return {"records": list(records), "pageInfo": {"hasNextPage": cursor is not None, "nextCursor": cursor}}


def client_for(*responses):
    session = Session([value if isinstance(value, Response) else Response(value) for value in responses])
    return OmniClient(base_url="https://omni.example", api_key="fixture-token", session=session), session


@pytest.mark.parametrize("scope,user_id", [("organization", USER), ("user", None)])
def test_known_labeled_personal_object_is_included_and_each_scope_paginates_independently(scope, user_id):
    known = {"identifier": "known-personal", "labels": [{"name": "Verified"}]}
    client, session = client_for(
        identity(scope), page([{"identifier": "shared-one"}], "shared-next"),
        page([{"identifier": "shared-two"}]), page([{"identifier": "personal-one"}], "private-next"),
        page([known]),
    )
    result = client.list_content(labels=["Verified"], include_personal_folders=True, user_id=user_id)
    assert result[-1] == known
    assert len(result) == 4
    assert session.calls[0][1].endswith("/api/v1/whoami")
    for index, (_, _, call) in enumerate(session.calls[1:]):
        assert call["params"]["labels"] == "Verified"
        assert call["params"]["include"] == "labels"
        assert call["params"]["pageSize"] == 100
        assert "userId" not in call["params"]
        assert "branchId" not in call["params"] and "branch_id" not in call["params"]
        assert call["params"]["scope"] == ("organization" if index < 2 else "restricted")
        if index >= 2:
            assert call["params"]["creatorId"] == MEMBER != USER
        else:
            assert "creatorId" not in call["params"]
    assert session.calls[2][2]["params"]["cursor"] == "shared-next"
    assert "cursor" not in session.calls[3][2]["params"]
    assert session.calls[4][2]["params"]["cursor"] == "private-next"


@pytest.mark.parametrize("scope,user_id", [("organization", USER), ("user", None)])
def test_validator_keeps_user_uuid_and_catalog_uses_membership_with_one_identity_read(scope, user_id):
    client, session = client_for(identity(scope), {"content": []}, {"content": []}, page(), page())
    for branch in (None, "feature"):
        client.validate_content("model", branch_id=branch, user_id=user_id, include_personal_folders=True)
    client.list_content(include_personal_folders=True, user_id=user_id)
    assert sum(url.endswith("/whoami") for _, url, _ in session.calls) == 1
    for _, _, call in session.calls[1:3]:
        assert call["params"]["include_personal_folders"] == "true"
        assert call["params"].get("userId") == (USER if scope == "organization" else None)
        assert MEMBER not in call["params"].values()
    assert session.calls[-1][2]["params"]["creatorId"] == MEMBER


@pytest.mark.parametrize("method", ["validator", "catalog"])
@pytest.mark.parametrize("scope,user_id,reason", [
    ("organization", None, "content_personal_user_required"),
    ("organization", OTHER_USER, "content_personal_actor_unresolved"),
    ("organization", MEMBER, "content_personal_actor_unresolved"),
    ("organization", "not-a-uuid", "content_personal_actor_unresolved"),
    ("user", USER, "content_pat_impersonation_unsupported"),
    ("user", OTHER_USER, "content_pat_impersonation_unsupported"),
])
def test_unproven_scope_fails_before_any_validator_or_catalog_request(method, scope, user_id, reason):
    client, session = client_for(identity(scope))
    with pytest.raises(ConfigError) as caught:
        if method == "validator":
            client.validate_content("model", user_id=user_id, include_personal_folders=True)
        else:
            client.list_content(user_id=user_id, include_personal_folders=True)
    assert caught.value.reason_code == reason
    assert caught.value.exit_code == 2
    assert len(session.calls) == 1
    assert session.calls[0][1].endswith("/whoami")
    assert USER not in str(caught.value) and MEMBER not in str(caught.value)
    safe = public_diagnostic({"issues": [{
        "validator": "content", "type": "validation_execution_failed", "severity": "error",
        **exception_diagnostic(caught.value),
    }]})
    assert safe["issues"][0]["reason_code"] == reason
    assert safe["issues"][0]["api_operation"] == "identity_lookup"
    assert USER not in json.dumps(safe) and MEMBER not in json.dumps(safe)


@pytest.mark.parametrize("payload", [
    {}, [], None, identity(scope="unknown"), identity(scope=[]), identity(user=None),
    identity(member=None), identity(member="not-a-uuid"), {"keyScope": "user", "user": {"id": USER}},
])
def test_invalid_identity_is_not_cached_or_treated_as_empty_content(payload):
    client, session = client_for(payload, identity("user"), page(), page())
    with pytest.raises(OmniAPIError) as caught:
        client.list_content(include_personal_folders=True)
    assert caught.value.reason_code == "content_identity_invalid"
    assert caught.value.exit_code == 4
    assert PRIVATE not in str(caught.value)
    assert client.list_content(include_personal_folders=True) == []
    assert len(session.calls) == 4
    assert all(session.calls[index][1].endswith("/whoami") for index in (0, 1))


def test_cache_cannot_substitute_owner_for_a_later_different_actor():
    client, session = client_for(identity(), page(), page())
    client.list_content(include_personal_folders=True, user_id=USER)
    with pytest.raises(ConfigError, match="will not substitute") as caught:
        client.list_content(include_personal_folders=True, user_id=OTHER_USER)
    assert caught.value.reason_code == "content_personal_actor_unresolved"
    assert len(session.calls) == 3


@pytest.mark.parametrize("change", ["credential", "instance", "new_client"])
def test_identity_cache_is_bound_to_instance_credential_and_client_lifetime(change):
    client, session = client_for(identity("user"), page(), page(), identity("user", OTHER_USER, OTHER_MEMBER), page(), page())
    client.list_content(include_personal_folders=True)
    if change == "credential":
        session.headers["Authorization"] = "Bearer replacement-fixture"
    elif change == "instance":
        client.base_url = "https://other-omni.example"
    else:
        client = OmniClient(base_url="https://omni.example", api_key="fixture-token", session=session)
    client.list_content(include_personal_folders=True)
    assert session.calls[3][1].endswith("/whoami")
    assert session.calls[2][2]["params"]["creatorId"] == MEMBER
    assert session.calls[5][2]["params"]["creatorId"] == OTHER_MEMBER


@pytest.mark.parametrize("location", ["identity", "restricted_first", "restricted_later"])
def test_permission_denial_preserves_typed_failure_and_never_returns_partial_catalog(location):
    denied = Response({"private": PRIVATE}, status=403)
    responses = {
        "identity": [denied],
        "restricted_first": [identity("user"), page([{"identifier": "shared"}]), denied],
        "restricted_later": [identity("user"), page(), page([{"identifier": "personal"}], "next"), denied],
    }[location]
    client, _ = client_for(*responses)
    with pytest.raises(OmniAuthError) as caught:
        client.list_content(include_personal_folders=True)
    error = caught.value
    assert error.exit_code == 3
    evidence = exception_diagnostic(error)
    assert evidence["reason_code"] == "omni_forbidden"
    assert evidence["http_status"] == 403
    assert evidence["api_operation"] == ("identity_lookup" if location == "identity" else "content_list")
    safe = public_diagnostic({"issues": [{
        "validator": "content", "type": "validation_execution_failed", "severity": "error", **evidence,
    }]})
    assert safe["issues"][0]["api_operation"] == evidence["api_operation"]
    assert PRIVATE not in json.dumps(safe)
    assert USER not in json.dumps(safe) and MEMBER not in json.dumps(safe)


def test_shared_only_catalog_does_not_request_identity_or_restricted_content():
    client, session = client_for(page([{"identifier": "shared"}]))
    assert client.list_content(user_id=OTHER_USER) == [{"identifier": "shared"}]
    assert len(session.calls) == 1
    assert session.calls[0][1].endswith("/content")
    assert session.calls[0][2]["params"] == {"include": "labels", "scope": "organization", "pageSize": 100}


def test_restricted_pagination_failure_cannot_return_shared_only_success():
    client, _ = client_for(identity("user"), page([{"identifier": "shared"}]), {
        "records": [], "pageInfo": {"hasNextPage": True, "nextCursor": None},
    })
    with pytest.raises(OmniAPIError) as caught:
        client.list_content(include_personal_folders=True)
    assert caught.value.reason_code == "omni_pagination_invalid"
