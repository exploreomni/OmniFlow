from omniflow.downstream import generate_downstream_dependencies


class SearchClient:
    def __init__(self, documents):
        self.documents = documents

    def search_content_references(self, *args, **kwargs):
        return {"content": self.documents}


def dependencies(documents):
    return generate_downstream_dependencies(
        client=SearchClient(documents), model_id="model", branch_id=None,
        diff_result={"changes": [{"field": "orders.amount"}]},
    )["dependencies"]


def test_identifier_only_documents_do_not_collapse():
    result = dependencies([{"identifier": "first"}, {"identifier": "second"}])
    assert [item["content_identifier"] for item in result] == ["first", "second"]


def test_query_map_keys_are_document_local_identities():
    result = dependencies([
        {"identifier": "first", "queries_and_issues": [{"query_id_map_key": "1"}, {"query_id_map_key": "2"}]},
        {"identifier": "second", "queries_and_issues": [{"query_id_map_key": "1"}]},
    ])
    assert [(item["content_identifier"], item["query_id"]) for item in result] == [
        ("first", "1"), ("first", "2"), ("second", "1"),
    ]


def test_repeated_document_and_query_evidence_is_still_deduplicated():
    document = {"identifier": "first", "queries_and_issues": [{"query_id_map_key": "1"}]}
    assert len(dependencies([document, document])) == 1


def test_existing_query_uuid_priority_is_preserved():
    result = dependencies([{"document_id": "doc", "queries_and_issues": [
        {"query_presentation_id": "uuid", "query_id": "other", "query_id_map_key": "1"},
    ]}])
    assert result[0]["query_id"] == "uuid"
    assert result[0]["query_id_map_key"] == "1"
