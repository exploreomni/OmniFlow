import json

import pytest

from omniflow.cli import _pull_analysis_yaml, _run_context
from omniflow.config import load_config
from omniflow.diff.diff_engine import diff_graphs
from omniflow.diff.semantic_graph import build_graph, load_yaml_graph
from omniflow.diff.yaml_loader import load_yaml_files
from omniflow.discovery import ModelContext
from omniflow.exceptions import ConfigError, OmniAPIError, SecurityPolicyError
from omniflow.yaml_pull import pull_yaml


@pytest.mark.parametrize("operation", ["delete", "modified", "missing", "unsafe", "symlink"])
def test_snapshot_inventory_and_hashes_prevent_stale_or_corrupt_evidence(tmp_path, operation):
    class Client:
        def get_model_yaml(self, *args, branch_id=None, **kwargs):
            files = {"orders.view": "dimensions: {}\n"}
            names = {"orders": "orders.view"}
            if not branch_id:
                files.update({"sales.topic": "base_view: orders\n", "customers.view": "dimensions: {}\n"})
                names["customers"] = "customers.view"
            return {"files": files, "viewNames": names}

    root = tmp_path / "snapshot"
    client = Client()
    pull_yaml(client=client, model_id="model", branch_id=None, output_dir=root)
    before = load_yaml_graph(root, require_view_names=True)
    if operation == "delete":
        pull_yaml(client=client, model_id="model", branch_id="head", output_dir=root)
        after = load_yaml_graph(root, require_view_names=True)
        assert set(after.files) == {"orders.view"}
        changes = diff_graphs(before, after)["changes"]
        assert {change["type"] for change in changes} == {"topic_deleted", "view_deleted"}
        assert (root / "sales.topic").exists()  # Excluded, not destructively removed.
        return
    if operation == "modified":
        (root / "orders.view").write_text("dimensions: {id: {type: number}}\n")
    elif operation == "missing":
        (root / "orders.view").unlink()
    elif operation == "symlink":
        (root / "orders.view").unlink()
        (root / "orders.view").symlink_to(root / "customers.view")
    else:
        path = root / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["files"]["../outside.view"] = manifest["files"].pop("orders.view")
        path.write_text(json.dumps(manifest))
    with pytest.raises((ConfigError, SecurityPolicyError), match="snapshot|symbolic"):
        load_yaml_graph(root, require_view_names=True)


@pytest.mark.parametrize("filename", ["model.view", "relationships.view", "model.query.view", "relationships.view.yaml"])
def test_explicit_view_types_override_reserved_basenames(filename):
    path = f"SCHEMA/{filename}"
    graph = build_graph({path: {"dimensions": {"id": {"type": "number"}}}}, view_names={"canonical": path})
    assert set(graph.views) == {"canonical"}
    assert set(graph.fields) == {"canonical.id"}
    assert not graph.model and not graph.relationships
    assert set(build_graph({"model.topic": {"base_view": "canonical"}}).topics) == {"model"}


@pytest.mark.parametrize("referenced,reuse_base,resolve_error", [(True, False, False), (False, False, False),
                                                               (True, True, False), (True, False, True)])
def test_inheritance_uses_resolved_api_evidence_without_losing_authored_snapshot(
    tmp_path, monkeypatch, referenced, reuse_base, resolve_error,
):
    class Client:
        def __init__(self):
            self.pulls = []
            self.searches = []

        def get_model_yaml(self, model_id, *, branch_id=None, fully_resolved=False, **kwargs):
            self.pulls.append((branch_id, fully_resolved))
            if resolve_error and fully_resolved:
                raise OmniAPIError("Synthetic resolved snapshot unavailable")
            definition = "filters:\n  window:\n    type: " + ("number" if branch_id else "string") + "\n"
            return {
                "files": {"template.view": "template: true\n" + definition,
                          "orders.view": "extends: [template]\n" + (definition if fully_resolved else ""),
                          "resolved/orders.view": "dimensions: {marker: {type: string}}\n"},
                "viewNames": {"template": "template.view", "orders": "orders.view",
                              "archived_orders": "resolved/orders.view"},
            }

        def search_content_references(self, model_id, *, find, **kwargs):
            self.searches.append(find)
            return {"content": [{"document_id": "dashboard"}] if referenced and find == "orders.window" else []}

    client = Client()
    config = load_config(None)
    config.content_validation.enabled = config.model_validation.enabled = config.semantic_lint.enabled = False
    monkeypatch.setattr("omniflow.cli._client_and_branch_for_context", lambda *a, **k: (client, "branch"))
    saved = _pull_analysis_yaml(client=client, model_id="model", branch_id=None, output_dir=tmp_path / "saved") if reuse_base else None
    arguments = dict(config=config, context=ModelContext(base_url="https://omni.example", model_id="model", model_path="omni/model"),
                     output_dir=tmp_path / "restricted", comparison_base_yaml_dir=saved)
    if resolve_error:
        with pytest.raises(OmniAPIError, match="unavailable"):
            _run_context(**arguments)
        assert client.searches == []
        return
    _, code = _run_context(**arguments)
    assert code == int(referenced)
    assert "orders.window" in client.searches
    assert client.pulls == [(None, False), (None, True), ("branch", False), ("branch", True)]
    authored = tmp_path / "restricted/yaml-head"
    assert (authored / "orders.view").read_text() == "extends: [template]\n"
    assert (authored / "resolved/orders.view").read_text() == "dimensions: {marker: {type: string}}\n"
    assert "resolved/orders.view" in load_yaml_files(authored)  # Includes checksum verification.
    with pytest.raises(ConfigError, match="Unresolved inheritance"):
        load_yaml_graph(authored)
    assert "orders.window" in load_yaml_graph(authored.with_name("yaml-head-resolved")).fields
    assert json.loads((authored / "manifest.json").read_text())["fully_resolved"] is False
