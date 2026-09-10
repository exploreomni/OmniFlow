import json
from types import SimpleNamespace

import pytest

from omniflow.cli import _run_context, cmd_diff
from omniflow.config import load_config
from omniflow.diff.semantic_graph import build_graph, load_yaml_graph
from omniflow.discovery import ModelContext
from omniflow.exceptions import ConfigError, SecurityPolicyError
from omniflow.yaml_pull import pull_yaml


class SnapshotClient:
    def __init__(self, referenced=True):
        self.referenced = referenced
        self.searches = []

    def get_model_yaml(self, model_id, *, branch_id=None, **kwargs):
        return {
            "files": {
                "SCHEMA/orders.view": "dimensions:\n  id:\n    type: number\nfilters:\n  window:\n    type: "
                + ("number" if branch_id else "string") + "\n",
                "ARCHIVE/orders.view": "dimensions:\n  id:\n    type: number\n",
            },
            "viewNames": {"schema__orders": "SCHEMA/orders.view", "archive__orders": "ARCHIVE/orders.view"},
            "checksums": {"SCHEMA/orders.view": "synthetic-checksum"},
        }

    def search_content_references(self, model_id, *, find, find_type, **kwargs):
        self.searches.append((find_type, find))
        found = self.referenced and (find_type, find) == ("field", "schema__orders.window")
        return {"content": [{"document_id": "synthetic-dashboard"}] if found else []}


@pytest.mark.parametrize("referenced,reuse_base", [(True, False), (False, False), (True, True)])
def test_api_snapshots_preserve_canonical_names_through_the_context_gate(tmp_path, monkeypatch, referenced, reuse_base):
    client = SnapshotClient(referenced)
    config = load_config(None)
    config.content_validation.enabled = config.model_validation.enabled = config.semantic_lint.enabled = False
    monkeypatch.setattr("omniflow.cli._client_and_branch_for_context", lambda *a, **k: (client, "branch"))
    base_dir = tmp_path / "previous-base" if reuse_base else None
    if base_dir:
        pull_yaml(client=client, model_id="model", branch_id=None, output_dir=base_dir)
    report, code = _run_context(
        config=config, context=ModelContext(base_url="https://omni.example", model_id="model", model_path="omni/model"),
        output_dir=tmp_path / "restricted", comparison_base_yaml_dir=base_dir,
    )
    assert code == int(referenced)
    assert set(client.searches) == {("view", "schema__orders"), ("field", "schema__orders.window")}
    assert report["check_reports"][0]["summary"]["coverage_errors"] == 0
    snapshot = tmp_path / "restricted" / "yaml-head"
    manifest = json.loads((snapshot / "manifest.json").read_text())
    assert manifest["view_names"]["schema__orders"] == "SCHEMA/orders.view"
    assert manifest["files"]["SCHEMA/orders.view"]["checksum"] == "synthetic-checksum"
    assert (snapshot / "SCHEMA/orders.view").read_text() == client.get_model_yaml("model", branch_id="branch")["files"][
        "SCHEMA/orders.view"
    ]
    assert set(load_yaml_graph(snapshot).views) == {"schema__orders", "archive__orders"}


def test_standalone_diff_uses_query_view_mapping_and_preserves_qualified_names(tmp_path):
    class QueryClient:
        def get_model_yaml(self, model_id, *, branch_id=None, **kwargs):
            path = "Renamed/orders.query.view" if branch_id else "Original/orders.query.view"
            return {"files": {path: "dimensions:\n  id:\n    type: number\n"}, "viewNames": {"schema__query": path}}

    for branch in ("base", "head"):
        pull_yaml(client=QueryClient(), model_id="model", branch_id="branch" if branch == "head" else None,
                  output_dir=tmp_path / branch)
    report_path = tmp_path / "diff.json"
    assert cmd_diff(SimpleNamespace(base=tmp_path / "base", head=tmp_path / "head", report_out=report_path)) == 0
    changes = json.loads(report_path.read_text())["changes"]
    assert [change["type"] for change in changes] == ["file_renamed"]
    assert changes[0]["name"] == "schema__query"
    graph = build_graph({"views/sales.orders.view.yaml": {}, "other/schema__query.query.view": {}})
    assert set(graph.views) == {"sales.orders", "schema__query"}
    mapped = build_graph({"orders.view": {"name": "not_canonical"}}, view_names={"schema__orders": "orders.view"})
    assert mapped.views["schema__orders"]["name"] == "schema__orders"


@pytest.mark.parametrize("mapping", [
    None, [], {}, {"orders": {"file": "orders.view"}}, {"orders": "ORDERS.view"},
    {"orders": "orders.view", "alias": "orders.view"}, {"bad\nname": "orders.view"},
    {"orders": "../orders.view"},
])
def test_invalid_or_incomplete_api_identity_never_becomes_unreferenced(tmp_path, mapping):
    class InvalidClient:
        def get_model_yaml(self, *args, **kwargs):
            result = {"files": {"orders.view": "dimensions:\n  id:\n    type: number\n"}}
            if mapping is not None:
                result["viewNames"] = mapping
            return result

    with pytest.raises(ConfigError, match="[Ii]dentity|viewNames"):
        pull_yaml(client=InvalidClient(), model_id="model", branch_id=None, output_dir=tmp_path / "snapshot")
        load_yaml_graph(tmp_path / "snapshot", require_view_names=True)
    with pytest.raises(ConfigError, match="non-view"):
        build_graph({"orders.topic": {"base_view": "orders"}}, view_names={"orders": "orders.topic"})


def test_raw_and_legacy_snapshots_and_manifest_safety(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "orders.view").write_text("dimensions: {}\n")
    assert set(load_yaml_graph(raw).views) == {"orders"}
    with pytest.raises(ConfigError, match="viewNames"):
        load_yaml_graph(raw, require_view_names=True)
    nested = raw / "SCHEMA"
    nested.mkdir()
    (raw / "orders.view").rename(nested / "orders.view")
    with pytest.raises(ConfigError, match="unresolved"):
        load_yaml_graph(raw)
    manifest = raw / "manifest.json"
    for text in ('{"files": {}}', "[", "[]"):
        manifest.write_text(text)
        with pytest.raises(ConfigError, match="snapshot"):
            load_yaml_graph(raw)
    manifest.write_text('{"view_names": {}}')
    monkeypatch.setattr("omniflow.diff.yaml_loader.MAX_YAML_FILE_BYTES", 20)
    manifest.write_text(" " * 21)
    with pytest.raises(SecurityPolicyError, match="manifest"):
        load_yaml_graph(raw)
    manifest.unlink()
    manifest.symlink_to(tmp_path / "missing")
    with pytest.raises(SecurityPolicyError, match="symbolic"):
        load_yaml_graph(raw)
