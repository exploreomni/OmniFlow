import json
from types import SimpleNamespace

import pytest

from omniflow.cli import _run_context, cmd_diff
from omniflow.config import load_config
from omniflow.diff.diff_engine import diff_graphs
from omniflow.diff.semantic_graph import build_graph, load_yaml_graph
from omniflow.discovery import ModelContext
from omniflow.downstream import generate_downstream_dependencies
from omniflow.exceptions import ConfigError, SecurityPolicyError
from omniflow.view_identity import validate_view_names
from omniflow.yaml_pull import pull_yaml


class SnapshotClient:
    def __init__(self, referenced=True, orientation="name_to_path"):
        self.referenced = referenced
        self.orientation = orientation
        self.searches = []

    def get_model_yaml(self, model_id, *, branch_id=None, **kwargs):
        payload = {
            "files": {
                "model": "connection: synthetic-connection\n",
                "relationships": "[]\n",
                "Sales.topic": "base_view: schema__orders\n",
                "SCHEMA/orders.view": "dimensions:\n  id:\n    type: number\nfilters:\n  window:\n    type: "
                + ("number" if branch_id else "string") + "\n",
                "ARCHIVE/orders.view": "dimensions:\n  id:\n    type: number\n",
                "QUERY/orders.query.view": "dimensions:\n  id:\n    type: number\n",
            },
            "viewNames": {"schema__orders": "SCHEMA/orders.view", "archive__orders": "ARCHIVE/orders.view",
                          "schema__query": "QUERY/orders.query.view"},
            "checksums": {"SCHEMA/orders.view": "synthetic-checksum", "ARCHIVE/orders.view": "archive-checksum",
                          "QUERY/orders.query.view": "query-checksum"},
        }
        if self.orientation == "path_to_name":
            payload["viewNames"] = {path: name for name, path in payload["viewNames"].items()}
            payload["viewNames"].update({"model": "", "relationships": "", "Sales.topic": ""})
        return payload

    def search_content_references(self, model_id, *, find, find_type, **kwargs):
        self.searches.append((find_type, find))
        found = self.referenced and (find_type, find) == ("field", "schema__orders.window")
        return {"content": [{"document_id": "synthetic-dashboard"}] if found else []}


@pytest.mark.parametrize("referenced,reuse_base", [(True, False), (False, False), (True, True)])
@pytest.mark.parametrize("orientation", ["name_to_path", "path_to_name"])
def test_api_snapshots_preserve_canonical_names_through_the_context_gate(
    tmp_path, monkeypatch, referenced, reuse_base, orientation,
):
    client = SnapshotClient(referenced, orientation)
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
    assert set(load_yaml_graph(snapshot).views) == {"schema__orders", "archive__orders", "schema__query"}


def test_both_api_orientations_produce_identical_snapshots_graphs_diff_and_downstream(tmp_path, monkeypatch):
    monkeypatch.setattr("omniflow.downstream.utc_now_iso", lambda: "2026-09-23T00:00:00Z")
    outcomes = []
    for orientation in ("name_to_path", "path_to_name"):
        client = SnapshotClient(orientation=orientation)
        manifests, graphs = [], []
        for branch_id in (None, "branch"):
            snapshot = tmp_path / orientation / (branch_id or "base")
            manifests.append(pull_yaml(client=client, model_id="model", branch_id=branch_id, output_dir=snapshot))
            graphs.append(load_yaml_graph(snapshot, require_view_names=True))
            payload = client.get_model_yaml("model", branch_id=branch_id)
            assert {path: (snapshot / path).read_text() for path in payload["files"]} == payload["files"]
            assert {path: entry["checksum"] for path, entry in manifests[-1]["files"].items() if entry["checksum"]} == payload["checksums"]
        diff = diff_graphs(*graphs)
        downstream = generate_downstream_dependencies(
            client=client, model_id="model", branch_id="branch", diff_result=diff,
        )
        assert set(client.searches) == {("view", "schema__orders"), ("field", "schema__orders.window")}
        assert downstream["coverage_gaps"] == []
        assert any(change.get("field") == "schema__orders.window" for change in diff["changes"])
        outcomes.append((manifests, graphs, diff, downstream))
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize("include_empty_nonviews", [False, True])
def test_models_without_views_accept_empty_or_recognized_nonview_metadata(tmp_path, include_empty_nonviews):
    files = {"model": "connection: synthetic-connection\n", "relationships": "[]\n",
             "Sales.topic": "label: Sales\n"}

    class Client:
        def get_model_yaml(self, *args, **kwargs):
            return {"files": files, "viewNames": {path: "" for path in files} if include_empty_nonviews else {}}

    manifest = pull_yaml(client=Client(), model_id="model", branch_id=None, output_dir=tmp_path / "snapshot")
    assert manifest["view_names"] == {}
    graph = load_yaml_graph(tmp_path / "snapshot", require_view_names=True)
    assert graph.views == graph.fields == {}
    assert set(graph.files) == set(files)


@pytest.mark.parametrize("filename,text,can_be_empty", [
    ("settings.yaml", "type: model\n", True),
    ("joins.yaml", "- join_from_view: orders\n  join_to_view: customers\n", True),
    ("sales.yaml", "base_view: schema__orders\n", True),
    ("model.view", "type: model\n", False),
])
def test_api_empty_identity_uses_shared_payload_hints_but_explicit_view_type_wins(
    tmp_path, filename, text, can_be_empty,
):
    class Client:
        def get_model_yaml(self, *args, **kwargs):
            return {"files": {filename: text}, "viewNames": {filename: ""}}

    snapshot = tmp_path / "snapshot"
    if not can_be_empty:
        with pytest.raises(ConfigError, match="viewNames") as caught:
            pull_yaml(client=Client(), model_id="model", branch_id=None, output_dir=snapshot)
        assert caught.value.metadata_reason == "invalid_identity"
        assert not snapshot.exists()
        return
    manifest = pull_yaml(client=Client(), model_id="model", branch_id=None, output_dir=snapshot)
    assert manifest["view_names"] == {}
    assert load_yaml_graph(snapshot, require_view_names=True).views == {}


def test_invalid_new_metadata_preserves_all_files_in_a_reused_snapshot(tmp_path):
    snapshot = tmp_path / "snapshot"
    pull_yaml(client=SnapshotClient(orientation="path_to_name"), model_id="model", branch_id=None, output_dir=snapshot)
    before = {path.relative_to(snapshot): path.read_bytes() for path in snapshot.rglob("*") if path.is_file()}

    class InvalidClient(SnapshotClient):
        def get_model_yaml(self, *args, **kwargs):
            payload = super().get_model_yaml(*args, **kwargs)
            payload["viewNames"]["SCHEMA/orders.view"] = ""
            payload["files"]["NEW/created.view"] = "dimensions: {}\n"
            payload["viewNames"]["NEW/created.view"] = "new__created"
            return payload

    with pytest.raises(ConfigError, match="viewNames"):
        pull_yaml(client=InvalidClient(orientation="path_to_name"), model_id="model", branch_id="branch", output_dir=snapshot)
    assert {path.relative_to(snapshot): path.read_bytes() for path in snapshot.rglob("*") if path.is_file()} == before
    assert load_yaml_graph(snapshot, require_view_names=True).fields["schema__orders.window"]["type"] == "string"


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
    pytest.param(None, id="null-map"),
    pytest.param([], id="list-map"),
    pytest.param({}, id="empty-map-with-views"),
    pytest.param({"orders": {"file": "orders.view"}}, id="object-value"),
    pytest.param({1: "orders.view"}, id="nonstring-key"),
    pytest.param({"orders.view": None}, id="null-name"),
    pytest.param({"orders": "orders.view", "ARCHIVE/orders.view": "archive"}, id="mixed-orientations"),
    pytest.param({"orders.view": "ARCHIVE/orders.view", "ARCHIVE/orders.view": "orders.view"}, id="ambiguous-orientation"),
    pytest.param({"orders.view": "orders", "ARCHIVE/orders.view": "orders"}, id="duplicate-canonical-name"),
    pytest.param({"orders": "orders.view", "alias": "orders.view"}, id="duplicate-path"),
    pytest.param({"orders": "ORDERS.view", "archive": "ARCHIVE/orders.view"}, id="canonical-case-wrong-path"),
    pytest.param({"ORDERS.view": "orders", "ARCHIVE/orders.view": "archive"}, id="reverse-case-wrong-path"),
    pytest.param({"orders": "../orders.view"}, id="canonical-unsafe-path"),
    pytest.param({"orders.view": "orders", "ARCHIVE/orders.view": "archive", "../model.yaml": ""}, id="unsafe-blank-nonview"),
    pytest.param({"orders.view": "orders", "ARCHIVE/orders.view": "archive", "/model.yaml": ""}, id="absolute-blank-nonview"),
    pytest.param({"orders.view": "orders", "ARCHIVE/orders.view": "archive", "unknown.topic": ""}, id="unknown-blank-nonview"),
    pytest.param({"orders.view": "orders", "ARCHIVE/orders.view": "archive", "MODEL.yaml": ""}, id="case-wrong-blank-nonview"),
    pytest.param({"orders.view": "orders", "ARCHIVE/orders.view": "archive", "model.yaml": None}, id="null-nonview-name"),
    pytest.param({"orders.view": "orders", "ARCHIVE/orders.view": "archive", "model.yaml": " "}, id="whitespace-nonview-name"),
    pytest.param({"orders.view": "", "ARCHIVE/orders.view": "archive"}, id="empty-view-name"),
    pytest.param({"orders.view": " ", "ARCHIVE/orders.view": "archive"}, id="whitespace-view-name"),
    pytest.param({"orders.view": "bad\nname", "ARCHIVE/orders.view": "archive"}, id="control-character-name"),
    pytest.param({"orders": "orders.view"}, id="incomplete-map"),
    pytest.param({"orders": "orders.view", "archive": "ARCHIVE/orders.view", "model": "model.yaml"}, id="nonview-target"),
])
def test_invalid_or_incomplete_api_identity_is_rejected_before_snapshot_writes(tmp_path, mapping):
    class InvalidClient:
        def get_model_yaml(self, *args, **kwargs):
            return {"files": {"orders.view": "dimensions: {}\n", "ARCHIVE/orders.view": "dimensions: {}\n",
                              "model.yaml": "connection: synthetic-connection\n"}, "viewNames": mapping}

    with pytest.raises(ConfigError, match="viewNames") as caught:
        pull_yaml(client=InvalidClient(), model_id="model", branch_id=None, output_dir=tmp_path / "snapshot")
    assert caught.value.metadata_stage == "view_names"
    assert caught.value.metadata_reason in {
        "invalid_mapping", "invalid_path", "unsupported_orientation", "ambiguous_orientation", "duplicate_name",
        "duplicate_path", "invalid_identity", "non_view_identity", "incomplete_identity",
    }
    assert not (tmp_path / "snapshot").exists()


def test_absent_api_metadata_and_internal_canonical_snapshot_contract_remain_unchanged(tmp_path):
    class NoMetadataClient:
        def get_model_yaml(self, *args, **kwargs):
            return {"files": {"orders.view": "dimensions: {}\n"}}

    snapshot = tmp_path / "snapshot"
    manifest = pull_yaml(client=NoMetadataClient(), model_id="model", branch_id=None, output_dir=snapshot)
    assert manifest["view_names"] is None
    assert (snapshot / "orders.view").read_text() == "dimensions: {}\n"
    with pytest.raises(ConfigError, match="viewNames"):
        load_yaml_graph(snapshot, require_view_names=True)
    manifest["view_names"] = {"schema__orders": "orders.view"}
    (snapshot / "manifest.json").write_text(json.dumps(manifest))
    assert set(load_yaml_graph(snapshot, require_view_names=True).views) == {"schema__orders"}
    assert validate_view_names(manifest["view_names"], manifest["files"]) == manifest["view_names"]
    with pytest.raises(ConfigError, match="viewNames"):
        validate_view_names({"orders.view": "schema__orders"}, manifest["files"])
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
