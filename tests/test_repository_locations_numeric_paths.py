import pytest

from omniflow import repository_locations
from omniflow.repository_locations import SnapshotIndex

VIEW = "name: orders\ndimensions:\n  id:\n    sql: private_value\n"


@pytest.mark.parametrize("root", [
    "omni/release-20261020", "omni/20261020/v1.2.3", "omni/Analytics Démo 2026-10-20",
    "omni/release_20261020/build_20261021", "omni/Snowflake Analytics/20261020",
])
def test_numeric_metadata_in_verified_paths_keeps_exact_source_locations(root):
    source = SnapshotIndex(root, {"20261020/orders.view.yaml": VIEW})
    result = source.detail({"validator": "model", "yaml_path": "20261020/orders.view.yaml,dimensions,id,sql"})
    assert result["file"] == root + "/20261020/orders.view.yaml"
    assert result["line"] == 4 and result["field"] == "orders.id"
    assert "private_value" not in str(result)


@pytest.mark.parametrize("root", [
    "omni/ssn_000_12_3456", "omni/mrn_TEST123", "omni/cvv_123", "omni/000-12-3456",
    "omni/card_4242_4242_4242_4242", "omni/card_4242424242424242", "omni/555-123-4567",
    "omni/ghp_abcdefghijklmnopqrstuvwxyz", "omni/github_pat_abcdefghijklmnopqrstuvwxyz",
    "omni/sk_abcdefghijklmnopqrstuvwxyz", "omni/xoxb-abcdefghijklmnopqrstuvwxyz",
    "omni/a@example.com", "omni/../orders", "omni/%2e%2e/orders", "omni/a\n::error::",
])
def test_sensitive_or_injected_paths_remain_rejected_and_keep_reason(root):
    source = SnapshotIndex(root, {"orders.view.yaml": VIEW})
    for issue in ({"field": "orders.id"}, {"file": "orders.view.yaml"},
                  {"yaml_path": "orders.view.yaml,dimensions,id,sql"}):
        assert source.detail(issue) == {}
        assert source.unavailable_reason == "unsupported_source_path"


def test_active_credential_inside_numeric_path_still_blocks(monkeypatch):
    monkeypatch.setenv("OMNI_API_KEY", "ReleaseSecret20261020")
    source = SnapshotIndex("omni/ReleaseSecret20261020", {"orders.view.yaml": VIEW})
    assert source.detail({"file": "orders.view.yaml"}) == {}
    assert source.unavailable_reason == "unsupported_source_path"


@pytest.mark.parametrize("failure", ["inventory", "ambiguous", "yaml"])
def test_unavailable_reason_survives_repeated_file_and_identity_lookups(monkeypatch, failure):
    files = {"orders.view.yaml": VIEW}
    expected = {"inventory": "source_inventory_limit", "ambiguous": "ambiguous_source_location",
                "yaml": "unsupported_source_yaml"}[failure]
    if failure == "inventory":
        monkeypatch.setattr(repository_locations, "MAX_INVENTORY_FILES", 0)
    elif failure == "ambiguous":
        files["orders.view"] = VIEW
    else:
        files["orders.view.yaml"] = "dimensions: [unterminated"
    source = SnapshotIndex("omni/20261020", files)
    for issue in ({"file": "orders.view.yaml"}, {"file": "orders.view.yaml"}, {"field": "orders.id"}):
        assert source.detail(issue) == {}
        assert source.unavailable_reason == expected
