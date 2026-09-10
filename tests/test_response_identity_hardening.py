import json

import pytest
import requests

from omniflow.exceptions import OmniAPIError, OmniAuthError
from omniflow.omni_client import OmniClient


@pytest.mark.parametrize("targeted", [False, True], ids=["validation", "targeted-search"])
@pytest.mark.parametrize(
    "branch_id,metadata,status,error",
    [
        (None, {}, 200, None),
        ("head-branch", {}, 200, None),
        (None, {"model_id": "model-1", "branch": None}, 200, None),
        ("head-branch", {"model_id": "model-1"}, 200, None),
        ("head-branch", {"branch": {"id": "head-branch", "name": "private-name"}}, 200, None),
        ("head-branch", {"branch": {}}, 200, None),
        ("head-branch", {"branch": {"name": "main"}}, 200, None),
        (None, {"model_id": "private-other-model"}, 200, OmniAPIError),
        ("head-branch", {"model_id": None}, 200, OmniAPIError),
        (None, {"model_id": " "}, 200, OmniAPIError),
        (None, {"model_id": 1}, 200, OmniAPIError),
        ("head-branch", {"branch": None}, 200, OmniAPIError),
        (None, {"branch": {"id": "head-branch"}}, 200, OmniAPIError),
        (None, {"branch": {}}, 200, OmniAPIError),
        ("head-branch", {"branch": {"id": "private-other-branch"}}, 200, OmniAPIError),
        ("head-branch", {"branch": {"id": None}}, 200, OmniAPIError),
        ("head-branch", {"branch": {"id": ""}}, 200, OmniAPIError),
        ("head-branch", {"branch": {"id": []}}, 200, OmniAPIError),
        ("head-branch", {"branch": "head-branch"}, 200, OmniAPIError),
        (None, {"branch": []}, 200, OmniAPIError),
        ("head-branch", {"model_id": "private-other-model"}, 401, OmniAuthError),
        ("head-branch", {"branch": None}, 403, OmniAuthError),
    ],
)
def test_content_response_identity_uses_documented_optional_metadata(
    monkeypatch, targeted, branch_id, metadata, status, error
):
    payload = {"content": [], **metadata}
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(payload).encode("utf-8")
    response._content_consumed = True
    session = requests.Session()
    calls = []

    def transport(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return response

    monkeypatch.setattr(session, "request", transport)
    client = OmniClient(base_url="https://omni.example", api_key="synthetic-test-token", session=session)
    operation = client.search_content_references if targeted else client.validate_content
    kwargs = {"branch_id": branch_id}
    if targeted:
        kwargs.update(find="orders.status", find_type="field")
    if error is None:
        assert operation("model-1", **kwargs) == payload
    else:
        with pytest.raises(error) as raised:
            operation("model-1", **kwargs)
        assert "private-" not in str(raised.value)
    assert len(calls) == 1
    assert calls[0][:2] == ("GET", "https://omni.example/api/v1/models/model-1/content-validator")
    assert calls[0][2]["params"].get("branch_id") == branch_id
    if targeted:
        assert calls[0][2]["params"]["find_type"] == "FIELD"
