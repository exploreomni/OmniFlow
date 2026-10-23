from __future__ import annotations

import copy
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from omniflow.content_impact import collect_and_enrich_content_impact, enrich_content_impact
from omniflow.discovery import ModelContext
from omniflow.exceptions import ConfigError, OmniAPIError, OmniAuthError
from omniflow.omni_client import OmniClient, OmniReadBudget

BASE = "https://example.omniapp.co"
MODEL = "11111111-1111-4111-8111-111111111111"
USER = "22222222-2222-4222-8222-222222222222"
MEMBER = "33333333-3333-4333-8333-333333333333"
FIELD = "orders.amount"
VIEW = "schema: PUBLIC\ntable_name: ORDERS\ndimensions:\n  amount:\n    sql: ${TABLE}.AMOUNT\n"
CONTEXT = ModelContext(base_url=BASE, model_id=MODEL, model_path="omni", branch_id="do-not-use")


def impact(**updates):
    return {"model_path": "omni", "view_file": "orders.view.yaml", "field_name": "amount",
            "source_revision": "base", "change_kind": "modified", "confidence": "exact", **updates}


def query(key="1", *, fields=None, filters=None, sorts=None):
    return {"id": "uuid-" + key, "queryIdentifierMapKey": key, "name": "Private query",
            "url": f"{BASE}/w/doc?key={key}&secret=never-copy#fragment",
            "query": {"modelId": MODEL, "fields": [FIELD] if fields is None else fields,
                      "filters": filters or {}, "sorts": sorts or []}}


def state(keys=("1",), *, rendered=("1",), controls=None):
    result = {"modelId": MODEL, "name": "Private document", "description": "",
              "queryPresentations": {"data": {key: {"type": "query", "query": query(key)["query"]}
                                               for key in keys}, "order": list(keys)}}
    if rendered is not None:
        result["containers"] = [{"type": "page", "container": {"type": "grid", "children": [
            {"type": "stack", "children": [{"type": "query", "id": key} for key in rendered]},
        ]}}]
        result["controls"] = {"data": controls or {}, "order": list(controls or {})}
    return result


class FakeClient:
    def __init__(self):
        self.yaml = {"files": {"orders.view": VIEW}, "viewNames": {"orders": "orders.view"}}
        self.documents = [{"identifier": "doc", "name": "Private document", "type": "Published",
                           "url": BASE + "/w/doc?secret=never-copy", "owner": {"email": "private@example.test"}}]
        self.queries = {"queries": [query()]}
        self.state = state()
        self.calls = []
        self.search_error = None
        self.state_error = None

    @contextmanager
    def bounded_reads(self, budget):
        self.budget = budget
        yield self

    def get_model_yaml(self, model_id, **kwargs):
        self.budget.consume()
        self.calls.append(("yaml", model_id, kwargs))
        return self.yaml

    def search_content_references(self, model_id, **kwargs):
        self.budget.consume()
        self.calls.append(("search", model_id, kwargs))
        if self.search_error:
            raise self.search_error
        return {"content": copy.deepcopy(self.documents)}

    def get_document_queries(self, document_id, **kwargs):
        self.budget.consume()
        self.calls.append(("queries", document_id, kwargs))
        return self.queries

    def get_document_state(self, document_id, **kwargs):
        self.budget.consume()
        self.calls.append(("state", document_id, kwargs))
        if self.state_error:
            raise self.state_error
        return self.state


def run(client=None, impacts=None, *, contexts=None, settings=None, **kwargs):
    return enrich_content_impact(
        static_report={"schema_version": 1, "coverage_complete": True, "coverage_gaps": [],
                       "impacts": [impact()] if impacts is None else impacts},
        contexts=[CONTEXT] if contexts is None else contexts,
        client_factory=lambda context: client or FakeClient(),
        settings=settings or SimpleNamespace(enabled=True, max_searches=100, timeout_seconds=120), **kwargs,
    )


def reasons(result):
    return {gap["reason_code"] for gap in result["coverage_gaps"]}


def test_native_tiles_deduplicate_and_saved_tabs_are_workbook_only():
    client = FakeClient()
    client.queries = {"queries": [query("1"), query("2")]}
    client.state = state(("1", "2"), rendered=("1", "1"))
    result = run(client)
    assert result["status"] == "complete"
    assert result["summary"] == dict(affected_fields=1, dashboards=1, workbooks=1, queries=2,
                                      dashboard_filters=0, searches=1, unverified_documents=0)
    assert [item["usage"] for item in result["impacts"][0]["documents"][0]["queries"]] == ["dashboard", "workbook"]
    assert all(call[2]["branch_id"] is None for call in client.calls if call[0] in {"yaml", "search"})


def test_native_container_discriminators_and_sort_schema():
    client = FakeClient()
    client.state["containers"] = [{"containerType": "page", "container": {"containerType": "grid", "children": [
        {"containerType": "stack", "children": [{"type": "query", "id": "1"}]},
    ]}}]
    definition = query(fields=[], sorts=[{"column_name": FIELD, "sort_descending": True}])["query"]
    client.queries["queries"][0]["query"] = definition
    client.state["queryPresentations"]["data"]["1"]["query"] = {k: v for k, v in definition.items() if k != "modelId"}
    client.state["workbookModelId"] = "workbook-model"
    definition["modelId"] = "workbook-model"
    result = run(client)
    assert result["status"] == "complete"
    assert result["summary"]["dashboards"] == 1
    assert result["impacts"][0]["documents"][0]["queries"][0]["reference_kinds"] == ["sort"]


def test_source_impacts_share_search_and_document_reads():
    client = FakeClient()
    result = run(client, [impact(), impact(source_revision="head"), impact(field_name="tax")])
    assert result["summary"]["searches"] == 2
    assert [call[0] for call in client.calls].count("queries") == 1
    assert [call[0] for call in client.calls].count("state") == 1
    assert result["impacts"][0]["documents"] == result["impacts"][1]["documents"]


@pytest.mark.parametrize("kind", ["field", "filter", "sort"])
def test_explicit_query_reference_kinds(kind):
    client = FakeClient()
    value = query(fields=[FIELD] if kind == "field" else [],
                  filters={FIELD: {"values": ["private"]}} if kind == "filter" else {},
                  sorts=[{"field": FIELD, "sort": "asc"}] if kind == "sort" else [])
    client.queries = {"queries": [value]}
    client.state["queryPresentations"]["data"]["1"]["query"] = value["query"]
    result = run(client)
    assert result["status"] == "complete"
    assert result["impacts"][0]["documents"][0]["queries"][0]["reference_kinds"] == [kind]
    assert "private" not in str(result["summary"])


def test_filter_only_reference_counts_separately_from_queries():
    client = FakeClient()
    client.queries = {"queries": []}
    client.state = state((), rendered=(), controls={"filter1": {
        "config": {"type": "number", "fieldName": FIELD, "values": ["private literal"], "kind": "EQUALS"},
    }})
    result = run(client)
    assert result["status"] == "complete"
    assert result["summary"]["dashboard_filters"] == 1
    assert result["summary"]["queries"] == 0
    assert result["summary"]["dashboards"] == 1
    assert "private literal" not in str(result)


def test_workbook_without_layout_is_not_dashboard():
    client = FakeClient()
    client.state = state(rendered=None)
    result = run(client)
    assert result["status"] == "complete"
    assert result["summary"]["workbooks"] == 1
    assert result["summary"]["dashboards"] == 0


@pytest.mark.parametrize("state_change,reason", [
    ({"containers": [{"type": "unsupported"}]}, "content_impact_layout_unsupported"),
    ({"draftOf": {"identifier": "published"}}, "content_impact_document_scope_mismatch"),
    ({"modelId": "different-model"}, "content_impact_document_scope_mismatch"),
    ({"app": {"url": "/private/app"}}, "content_impact_app_unsupported"),
    ({"controls": {"data": {"x": {"config": {"type": "composite"}}}, "order": ["x"]}},
     "content_impact_controls_unsupported"),
])
def test_unknown_or_wrong_scope_is_not_zero_impact_proof(state_change, reason):
    client = FakeClient()
    client.state.update(state_change)
    result = run(client)
    assert result["status"] == "partial"
    assert not result["coverage_complete"]
    assert reason in reasons(result)
    assert result["summary"]["unverified_documents"] == 1


def test_draft_search_result_is_not_fetched_as_published():
    client = FakeClient()
    client.documents[0]["type"] = "Draft"
    result = run(client)
    assert "content_impact_draft_document" in reasons(result)
    assert not any(call[0] in {"queries", "state"} for call in client.calls)


@pytest.mark.parametrize("error", [OmniAuthError("private", reason_code="omni_forbidden"),
                                  OmniAPIError("private", reason_code="omni_timeout")])
def test_transport_failures_stay_explicit_without_private_messages(error):
    client = FakeClient()
    client.search_error = error
    result = run(client)
    assert result["status"] == "unavailable"
    assert result["coverage_gaps"][0]["cause_code"] == error.reason_code
    assert "private" not in str(result["coverage_gaps"])


def test_classic_layout_422_is_partial_and_does_not_trigger_upgrade():
    client = FakeClient()
    client.state_error = OmniAPIError("classic", reason_code="omni_http_error", diagnostic_context={"http_status": 422})
    result = run(client)
    assert result["status"] == "partial"
    assert "content_impact_layout_unavailable" in reasons(result)
    assert result["summary"]["unverified_documents"] == 1
    assert next(gap for gap in result["coverage_gaps"] if gap["reason_code"] == "content_impact_layout_unavailable")["http_status"] == 422


def test_empty_targeted_result_is_complete_only_for_requested_scope():
    client = FakeClient()
    client.documents = []
    result = run(client)
    assert result["status"] == "complete"
    assert result["scope"] == "published" and result["advisory"]
    assert result["summary"]["queries"] == 0
    assert [call[0] for call in client.calls] == ["yaml", "search"]


def test_missing_view_names_or_ambiguous_context_never_guesses():
    client = FakeClient()
    del client.yaml["viewNames"]
    assert run(client)["status"] == "unavailable"
    client = FakeClient()
    result = run(client, contexts=[CONTEXT, CONTEXT])
    assert "content_impact_model_scope_ambiguous" in reasons(result)
    assert not client.calls


def test_moved_duplicate_views_use_immutable_qualified_identity():
    client = FakeClient()
    other = VIEW.replace("PUBLIC", "OTHER")
    client.yaml = {"files": {"published/orders.view": VIEW, "elsewhere/orders.view": other},
                   "viewNames": {"orders": "published/orders.view", "other_orders": "elsewhere/orders.view"}}
    entry = impact(view_file="New folder/orders.view.yaml")
    calls = []

    def reader(context, path, revision):
        calls.append((context, path, revision))
        return VIEW

    assert run(client, [entry])["status"] == "unavailable"
    result = run(client, [entry], source_file_reader=reader)
    assert result["status"] == "complete"
    assert result["impacts"][0]["canonical_field"] == FIELD
    assert calls == [(CONTEXT, entry["view_file"], "base")]


def test_search_budget_and_disabled_mode_do_not_scan_all_content():
    client = FakeClient()
    result = run(client, [impact(), impact(field_name="tax")],
                 settings=SimpleNamespace(enabled=True, max_searches=1, timeout_seconds=120))
    assert result["summary"]["searches"] == 1
    assert "content_impact_budget_exhausted" in reasons(result)
    client = FakeClient()
    run(client, settings=SimpleNamespace(enabled=False))
    assert not client.calls


def test_metadata_links_are_returned_same_origin_and_token_free():
    result = run()
    document = result["impacts"][0]["documents"][0]
    assert document["url"] == BASE + "/w/doc"
    assert document["queries"][0]["url"] == BASE + "/w/doc?key=1"
    assert "never-copy" not in str(result)
    client = FakeClient()
    client.documents[0]["url"] = "https://evil.test/leak"
    client.queries["queries"][0]["url"] = "https://user:password@example.omniapp.co/w/doc"
    item = run(client)["impacts"][0]["documents"][0]
    assert "url" not in item and "url" not in item["queries"][0]


def test_changed_document_or_cross_model_query_remains_unverified():
    client = FakeClient()
    client.state["queryPresentations"]["data"]["1"]["query"]["fields"] = ["orders.tax"]
    assert "content_impact_document_changed" in reasons(run(client))
    client = FakeClient()
    client.queries["queries"][0]["query"]["modelId"] = "another-model"
    assert "content_impact_document_scope_mismatch" in reasons(run(client))


@pytest.mark.parametrize("mutation", [
    lambda client: client.state["queryPresentations"]["order"].append("missing"),
    lambda client: client.queries["queries"].append(copy.deepcopy(client.queries["queries"][0])),
    lambda client: client.state.update(containers=[{"type": "reference", "referenceKey": "missing"}]),
    lambda client: client.state.update(containers=[{"type": "query", "id": "missing"}]),
    lambda client: client.state.update(containers=[{"type": "filter", "id": "missing"}]),
    lambda client: client.state.update(containers=[{"type": "stack", "containerType": "grid", "children": []}]),
])
def test_inconsistent_document_identity_and_layout_never_claim_complete(mutation):
    client = FakeClient()
    mutation(client)
    assert run(client)["status"] == "partial"


def test_literal_values_do_not_create_field_references():
    client = FakeClient()
    definition = query(fields=["orders.tax"], filters={"orders.tax": {"values": [FIELD]}})["query"]
    client.queries["queries"][0]["query"] = definition
    client.state["queryPresentations"]["data"]["1"]["query"] = definition
    result = run(client)
    assert "content_impact_reference_unverified" in reasons(result)
    assert result["summary"]["queries"] == 0


@pytest.mark.parametrize("extra,reason", [
    ({"calculations": [{"name": "private", "sql": "private expression"}]}, "content_impact_query_expression_unverified"),
    ({"controls": [{"field": "orders.tax"}]}, "content_impact_query_expression_unverified"),
    ({"fields": [FIELD, "local_calculation"]}, "content_impact_query_expression_unverified"),
    ({"fields": [FIELD + "[date]"]}, "content_impact_timeframe_identity_unverified"),
])
def test_workbook_expressions_and_timeframes_are_not_claimed_complete(extra, reason):
    client = FakeClient()
    definition = client.queries["queries"][0]["query"]
    definition.update(extra)
    client.state["queryPresentations"]["data"]["1"]["query"] = definition
    result = run(client)
    assert result["status"] == "partial"
    assert reason in reasons(result)


def test_static_failure_reason_is_preserved_without_arbitrary_payload():
    result = enrich_content_impact(
        static_report={"schema_version": 1, "coverage_complete": False, "impacts": [], "coverage_gaps": [
            {"reason_code": "dbt_sql_wildcard", "file": "models/orders.sql", "raw_sql": "PRIVATE"},
        ]}, contexts=[CONTEXT], client_factory=lambda context: FakeClient(),
        settings=SimpleNamespace(enabled=True, max_searches=1, timeout_seconds=120),
    )
    assert "dbt_sql_wildcard" in reasons(result)
    assert "PRIVATE" not in str(result)


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.headers = payload, status, {}
        self.ok = status == 200

    def json(self):
        return self.payload

    def close(self):
        pass


class Session:
    def __init__(self, responses):
        self.responses, self.headers, self.calls = list(responses), {}, []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def identity(scope):
    return Response({"keyScope": scope, "user": {"id": USER, "membershipId": MEMBER}})


def test_query_api_org_uses_verified_membership_not_global_id():
    session = Session([identity("organization"), Response({"queries": []})])
    client = OmniClient(base_url=BASE, api_key="test", session=session)
    client.get_document_queries("doc", user_id=USER)
    assert session.calls[-1][2]["params"] == {"userId": MEMBER}
    with pytest.raises(ConfigError, match="acting-user scope"):
        client.get_document_state("doc", user_id=USER)
    assert len(session.calls) == 2


def test_pat_has_no_impersonation_and_reads_state_in_credential_scope():
    session = Session([identity("user"), Response({"queries": []}), Response(state())])
    client = OmniClient(base_url=BASE, api_key="test", session=session)
    client.get_document_queries("doc", include_personal_folders=True)
    client.get_document_state("doc", include_personal_folders=True)
    assert session.calls[1][2]["params"] == {}
    assert session.calls[2][2]["params"] is None


def test_read_budget_disables_retries_bounds_timeout_and_restores_client():
    session = Session([Response({}, 429), Response({"queries": []})])
    client = OmniClient(base_url=BASE, api_key="test", session=session)
    budget = OmniReadBudget(time.monotonic() + 2, 1)
    with client.bounded_reads(budget):
        with pytest.raises(OmniAPIError) as error:
            client.get_document_queries("doc")
        assert error.value.reason_code == "omni_rate_limited"
        assert len(session.calls) == 1
        assert 0 < session.calls[0][2]["timeout"] <= 2
        with pytest.raises(OmniAPIError) as exhausted:
            client.get_document_queries("doc")
        assert exhausted.value.reason_code == "content_impact_budget_exhausted"
        with pytest.raises(ConfigError, match="write requests"):
            client._request("POST", "/anything")
    client.get_document_queries("doc")
    assert client._read_budget is None


def test_expired_budget_makes_no_http_request():
    session = Session([])
    client = OmniClient(base_url=BASE, api_key="test", session=session)
    with client.bounded_reads(OmniReadBudget(time.monotonic() - 1, 1)), pytest.raises(OmniAPIError) as error:
        client.get_document_queries("doc")
    assert error.value.reason_code == "content_impact_timeout"
    assert not session.calls


def coordinated(collector, client, *, reader=None, verifier=None, contexts=None):
    return collect_and_enrich_content_impact(
        collect_static=collector, contexts=[CONTEXT] if contexts is None else contexts,
        client_factory=lambda context: client,
        settings=SimpleNamespace(enabled=True, max_searches=100, timeout_seconds=120),
        source_file_reader=reader, client_verifier=verifier,
    )


def test_coordinator_resolves_canonical_names_once_with_shared_budget():
    client = FakeClient()
    verified, reads, mappings = [], [], []

    def verifier(context, scoped_client):
        assert scoped_client.budget.requests == 0
        scoped_client.budget.consume()
        verified.append(context)

    def reader(context, path, revision):
        reads.append((path, revision))
        return VIEW

    def collector(resolver):
        for scope in ("base", "head"):
            mappings.append(resolver(scope, "omni", {"omni/orders.view.yaml": {"parsed": "not-proof"}}))
        return {"schema_version": 1, "coverage_complete": True, "coverage_gaps": [],
                "impacts": [impact(), impact(source_revision="head")]}

    result = coordinated(collector, client, reader=reader, verifier=verifier)
    assert result["status"] == "complete"
    assert mappings == [{"omni/orders.view.yaml": "orders"}] * 2
    assert len(verified) == 1
    assert reads == [("orders.view.yaml", "base"), ("orders.view.yaml", "head")]
    assert [call[0] for call in client.calls] == ["yaml", "search", "queries", "state"]
    assert client.budget.requests == 5  # verifier + YAML + search + two detail reads


def test_coordinator_comment_only_never_opens_client():
    result = collect_and_enrich_content_impact(
        collect_static=lambda resolver: {"schema_version": 1, "coverage_complete": True, "coverage_gaps": [], "impacts": []},
        contexts=[CONTEXT], client_factory=lambda context: pytest.fail("No metadata call for no semantic delta"),
        settings=SimpleNamespace(enabled=True, max_searches=100, timeout_seconds=120),
    )
    assert result["status"] == "complete"


def test_coordinator_qualifies_moved_duplicate_view_names_from_original_text():
    client = FakeClient()
    client.yaml = {"files": {"old/a/orders.view": VIEW, "old/b/orders.view": VIEW.replace("PUBLIC", "OTHER")},
                   "viewNames": {"orders": "old/a/orders.view", "other_orders": "old/b/orders.view"}}

    def collector(resolver):
        mapping = resolver("base", "omni", {"omni/new/a/orders.view.yaml": VIEW,
                                            "omni/new/b/orders.view.yaml": VIEW.replace("PUBLIC", "OTHER")})
        assert mapping == {"omni/new/a/orders.view.yaml": "orders", "omni/new/b/orders.view.yaml": "other_orders"}
        return {"schema_version": 1, "coverage_complete": True, "coverage_gaps": [],
                "impacts": [impact(view_file="new/a/orders.view.yaml")]}

    assert coordinated(collector, client)["status"] == "complete"


def test_coordinator_keeps_mapping_failure_even_when_collector_handles_exception():
    client = FakeClient()
    client.yaml.pop("viewNames")

    def collector(resolver):
        for scope in ("base", "head"):
            with pytest.raises(ConfigError):
                resolver(scope, "omni", {"omni/orders.view.yaml": VIEW})
        return {"schema_version": 1, "coverage_complete": False,
                "coverage_gaps": [{"reason_code": "omni_view_identity_unavailable"}], "impacts": []}

    result = coordinated(collector, client)
    assert result["status"] == "unavailable"
    assert "content_impact_field_identity_unavailable" in reasons(result)
    assert "omni_view_identity_unavailable" in reasons(result)
    assert [call[0] for call in client.calls] == ["yaml"]


def test_coordinator_verifier_failure_prevents_model_and_content_reads():
    client = FakeClient()

    def verifier(context, scoped_client):
        raise ConfigError("Wrong target environment")

    def collector(resolver):
        with pytest.raises(ConfigError):
            resolver("base", "omni", {"omni/orders.view.yaml": VIEW})
        return {"schema_version": 1, "coverage_complete": False, "coverage_gaps": [], "impacts": [impact()]}

    result = coordinated(collector, client, verifier=verifier)
    assert result["status"] == "unavailable"
    assert not client.calls


def test_coordinator_parsed_payload_without_original_text_cannot_establish_identity():
    client = FakeClient()

    def collector(resolver):
        with pytest.raises(ConfigError):
            resolver("base", "omni", {"omni/orders.view.yaml": {"schema": "PUBLIC", "table_name": "ORDERS"}})
        return {"schema_version": 1, "coverage_complete": False, "coverage_gaps": [], "impacts": []}

    assert coordinated(collector, client)["status"] == "unavailable"
    assert not client.calls
