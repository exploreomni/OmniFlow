import pytest
from test_candidate_change_scope import compare

from omniflow.candidate import CandidateVerificationError
from omniflow.diagnostics import public_diagnostic


def test_transitive_canonical_dependencies_with_unrelated_drift(monkeypatch):
    git = {"orders.view.yaml": "sql: ${shared__users.id}",
           "users.view.yaml": "dimensions: {id: {sql: '${tiers.id}'}}",
           "tiers.view.yaml": "dimensions: {id: {type: number}}", "other.view.yaml": "label: Git"}
    api = {p.removesuffix(".yaml"): t for p, t in git.items()}
    api["other.view"] = "label: unrelated drift"
    names = {"orders": "orders.view", "shared__users": "users.view", "tiers": "tiers.view", "other": "other.view"}
    for side in ("head", "base"):
        proof, reads = compare(monkeypatch, git, api, ["omni/orders.view.yaml"], side=side, names=names)
        assert proof["dependency_file_count"] == 2
        assert reads == {"omni/" + p for p in git if p != "other.view.yaml"}
    for deps in ("users.view", "tiers.view"):
        with pytest.raises(CandidateVerificationError, match="pr_change_not_synchronized"):
            compare(monkeypatch, git, {**api, deps: "label: stale"}, ["omni/orders.view.yaml"], names=names)


def test_topic_checks_its_base_view_and_cycles_terminate(monkeypatch):
    files = {"sales.topic": "base_view: orders", "orders.view": "sql: ${users.id}",
             "users.view": "sql: ${orders.id}", "other.view": "label: Git"}
    proof, reads = compare(monkeypatch, files, {**files, "other.view": "label: Omni"}, ["omni/sales.topic"])
    assert proof["dependency_file_count"] == 2
    assert reads == {"omni/sales.topic", "omni/orders.view", "omni/users.view"}


@pytest.mark.parametrize("path,definition", [
    ("orders.view", "sql: SELECT * FROM a_dynamic_relation"),
    ("orders.view", "sql: '{{ model_var }}'"),
    ("orders.view", "extends: parent"),
    ("sales.topic", "base_view: orders\njoins: {users: {}}"),
    ("relationships", "- join_from_view: orders\n  join_to_view: users"),
    ("model", "access_grants: {protected: {user_attribute: role}}"),
    ("orders.view", "sql: ${unrecognized.field}"),
])
def test_unresolved_dependencies_use_exact_full_model(monkeypatch, path, definition):
    files = {path: definition, "users.view": "label: users"}
    proof, _ = compare(monkeypatch, files, files, ["omni/" + path])
    assert proof["method"] == "authored_yaml_snapshot" and proof["file_count"] == 2
    projected = public_diagnostic({"candidate_verification": proof})["candidate_verification"]
    assert projected["source_fallback_reason"] in {"dependency_scope_unresolved", "dependency_identity_unresolved"}
    with pytest.raises(CandidateVerificationError) as stale:
        compare(monkeypatch, files, {**files, "users.view": "label: stale"}, ["omni/" + path])
    assert stale.value.diagnostic_context["source_fallback_reason"] == proof["source_fallback_reason"]


def test_explicit_global_join_edges_only_reach_connected_views(monkeypatch):
    files = {
        "orders.view": "sql: ${users.id}", "users.view": "label: Users",
        "tiers.view": "label: Tiers", "unconnected.view": "label: Other",
        "model.yaml": "label: Test model\nconnection: synthetic",
        "relationships.yaml": "- join_from_view: users\n  join_to_view: tiers\n  on_sql: ${users.id} = ${tiers.id}\n",
    }
    proof, reads = compare(monkeypatch, files, {**files, "unconnected.view": "label: Unrelated drift"},
                           ["omni/orders.view"])
    assert proof["method"] == "pr_changed_files" and proof["dependency_file_count"] == 4
    assert reads == {"omni/" + p for p in files if p != "unconnected.view"}
    with pytest.raises(CandidateVerificationError):
        compare(monkeypatch, files, {**files, "tiers.view": "label: Missing field"}, ["omni/orders.view"])


def test_global_inheritance_requires_the_full_model_for_a_view_dependency(monkeypatch):
    files = {"orders.view": "sql: ${users.id}", "users.view": "type: view",
             "model": "extends: another_model", "spare.view": "label: Other"}
    proof, _ = compare(monkeypatch, files, files, ["omni/orders.view"])
    assert proof["method"] == "authored_yaml_snapshot" and proof["file_count"] == 4


def test_deleted_view_cannot_hide_an_unsynchronized_consumer(monkeypatch):
    files = {"remaining.view": "sql: ${deleted.value}", "other.view": "label: Other"}
    with pytest.raises(CandidateVerificationError) as stale:
        compare(monkeypatch, files, {**files, "remaining.view": "sql: 1"}, ["omni/deleted.view"],
                names={"remaining": "remaining.view", "other": "other.view"})
    assert stale.value.diagnostic_context["source_fallback_reason"] == "dependency_consumers_unverified"
