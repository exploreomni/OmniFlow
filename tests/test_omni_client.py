import unittest
from unittest import mock

from omniflow.exceptions import ConfigError, OmniAPIError, SecurityPolicyError
from omniflow.omni_client import MAX_RESPONSE_BYTES, OmniClient

FAKE_API_KEY = "secret"  # pragma: allowlist secret


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.ok = 200 <= status_code < 300
        self.text = "text"

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.headers = {}
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class FailingSession(FakeSession):
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        raise self.responses.pop(0)


class InvalidJsonResponse(FakeResponse):
    def json(self):
        raise ValueError("private response fragment")


class StreamingResponse(FakeResponse):
    def __init__(self, chunks):
        super().__init__(None)
        self.chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size):
        del chunk_size
        yield from self.chunks

    def close(self):
        self.closed = True


class OmniClientTests(unittest.TestCase):
    def test_branch_resolution(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "records": [
                            {"id": "branch-1", "modelKind": "BRANCH", "baseModelId": "model-1", "name": "feature/a"}
                        ],
                        "pageInfo": {},
                    }
                )
            ]
        )
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=session)
        self.assertEqual(client.resolve_branch_id("model-1", "feature/a"), "branch-1")
        self.assertTrue(session.calls[0][2]["stream"])

    def test_model_metadata_returns_only_the_documented_connection_identity(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "records": [
                            {
                                "id": "model-1",
                                "connectionId": "connection-1",
                                "name": "private model name",
                            }
                        ],
                        "pageInfo": {},
                    }
                )
            ]
        )
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=session)
        self.assertEqual(
            client.get_model_metadata("model-1"),
            {"model_id": "model-1", "connection_id": "connection-1"},
        )
        self.assertEqual(session.calls[0][2]["params"]["modelId"], "model-1")

    def test_connection_refresh_coverage_returns_only_shared_model_ids(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "records": [
                            {
                                "id": "model-1",
                                "connectionId": "connection-1",
                                "modelKind": "SHARED",
                            },
                            {
                                "id": "model-2",
                                "connectionId": "connection-1",
                                "modelKind": "SHARED_EXTENSION",
                            },
                            {
                                "id": "branch-1",
                                "connectionId": "connection-1",
                                "modelKind": "BRANCH",
                            },
                        ],
                        "pageInfo": {},
                    }
                )
            ]
        )
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=session)
        self.assertEqual(
            client.list_refresh_affected_model_ids("connection-1"),
            ["model-1", "model-2"],
        )
        self.assertEqual(session.calls[0][2]["params"]["connectionId"], "connection-1")

    def test_connection_refresh_coverage_rejects_mismatches_and_empty_results(self):
        payloads = (
            {
                "records": [
                    {"id": "model-1", "connectionId": "other", "modelKind": "SHARED"},
                ],
                "pageInfo": {},
            },
            {"records": [], "pageInfo": {}},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                client = OmniClient(
                    base_url="https://omni.example",
                    api_key=FAKE_API_KEY,
                    session=FakeSession([FakeResponse(payload)]),
                )
                with self.assertRaises(OmniAPIError):
                    client.list_refresh_affected_model_ids("connection-1")

    def test_retries_429(self):
        session = FakeSession([FakeResponse({}, status_code=429), FakeResponse([], status_code=200)])
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=session)
        self.assertEqual(client.validate_model("model-1"), [])
        self.assertEqual(len(session.calls), 2)

    def test_error_response_does_not_echo_server_payload(self):
        response = FakeResponse({}, status_code=400)
        response.text = "sensitive customer payload"
        client = OmniClient(
            base_url="https://omni.example",
            api_key=FAKE_API_KEY,
            session=FakeSession([response]),
        )
        with self.assertRaises(OmniAPIError) as raised:
            client.validate_model("model-1")
        self.assertNotIn("sensitive customer payload", str(raised.exception))

    def test_rejects_oversized_and_invalid_json_responses_without_echoing_body(self):
        oversized = FakeResponse([], headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)})
        client = OmniClient(
            base_url="https://omni.example",
            api_key=FAKE_API_KEY,
            session=FakeSession([oversized]),
        )
        with self.assertRaises(OmniAPIError):
            client.validate_model("model-1")

        client = OmniClient(
            base_url="https://omni.example",
            api_key=FAKE_API_KEY,
            session=FakeSession([InvalidJsonResponse(None)]),
        )
        with self.assertRaises(OmniAPIError) as raised:
            client.validate_model("model-1")
        self.assertNotIn("private response fragment", str(raised.exception))

    def test_streamed_response_limit_closes_connection(self):
        response = StreamingResponse([b"123", b"45"])
        client = OmniClient(
            base_url="https://omni.example",
            api_key=FAKE_API_KEY,
            session=FakeSession([response]),
        )
        with mock.patch("omniflow.omni_client.MAX_RESPONSE_BYTES", 4):
            with self.assertRaises(OmniAPIError):
                client.validate_model("model-1")
        self.assertTrue(response.closed)

    def test_pagination_rejects_repeated_cursor_and_record_overflow(self):
        repeated = FakeSession(
            [
                FakeResponse({"records": [], "pageInfo": {"nextCursor": "same"}}),
                FakeResponse({"records": [], "pageInfo": {"nextCursor": "same"}}),
            ]
        )
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=repeated)
        with self.assertRaises(OmniAPIError):
            client.list_models()

        overflow = FakeSession([FakeResponse({"records": [{"id": "a"}, {"id": "b"}], "pageInfo": {}})])
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=overflow)
        with mock.patch("omniflow.omni_client.MAX_PAGINATION_RECORDS", 1):
            with self.assertRaises(OmniAPIError):
                client.list_models()

        malformed = FakeSession([FakeResponse({"records": ["bad", "records"], "pageInfo": {}})])
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=malformed)
        with mock.patch("omniflow.omni_client.MAX_PAGINATION_RECORDS", 1):
            with self.assertRaises(OmniAPIError):
                client.list_models()

    def test_rejects_invalid_timeout_and_find_type(self):
        with self.assertRaises(ConfigError):
            OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, timeout=0)
        client = OmniClient(
            base_url="https://omni.example",
            api_key=FAKE_API_KEY,
            session=FakeSession([]),
        )
        with self.assertRaises(ConfigError):
            client.search_content_references("model-1", find="orders", find_type="relationship")

    def test_content_validator_find_type_uses_documented_enum_values(self):
        session = FakeSession([FakeResponse({"content": []})])
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=session)
        client.search_content_references("model-1", find="orders.revenue", find_type="field")
        _, _, kwargs = session.calls[0]
        self.assertEqual(kwargs["params"]["find"], "orders.revenue")
        self.assertEqual(kwargs["params"]["find_type"], "FIELD")

    def test_get_dbt_exposures_uses_documented_endpoint(self):
        session = FakeSession([FakeResponse({"records": [], "pageInfo": {"hasNextPage": False}})])
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=session)
        self.assertEqual(client.get_dbt_exposures("model-1", branch_id="branch-1"), {"records": []})
        _, url, kwargs = session.calls[0]
        self.assertEqual(url, "https://omni.example/api/v1/models/model-1/dbt-exposures")
        self.assertEqual(kwargs["params"]["branch_id"], "branch-1")

    def test_schema_refresh_uses_documented_async_job_contract(self):
        session = FakeSession(
            [
                FakeResponse({"jobId": "job-1", "modelId": "model-1", "status": "running"}),
                FakeResponse(
                    {
                        "job_type": "refresh_schema",
                        "job_id": "job-1",
                        "status": "COMPLETED",
                        "private_future_field": "discard me",
                    }
                ),
            ]
        )
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=session)
        self.assertEqual(
            client.start_schema_refresh("model-1", branch_id="branch-1", hard_refresh=False),
            {"job_id": "job-1", "model_id": "model-1", "status": "RUNNING"},
        )
        self.assertEqual(
            client.get_schema_refresh_job_status("job-1"),
            {"job_id": "job-1", "job_type": "refresh_schema", "status": "COMPLETED"},
        )
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://omni.example/api/v1/models/model-1/refresh")
        self.assertEqual(kwargs["params"], {"hard_refresh": "false", "branch_id": "branch-1"})
        self.assertEqual(session.calls[1][1], "https://omni.example/api/v1/jobs/job-1/status")

    def test_schema_refresh_start_is_not_retried_because_it_is_not_idempotent(self):
        session = FakeSession([FakeResponse({}, status_code=500)])
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=session)
        with self.assertRaises(OmniAPIError):
            client.start_schema_refresh("model-1")
        self.assertEqual(len(session.calls), 1)

    def test_schema_refresh_rejects_mismatched_ids_and_unknown_statuses(self):
        payloads = (
            ("start", {"jobId": "job-1", "modelId": "other", "status": "running"}),
            ("status", {"job_type": "refresh_schema", "job_id": "other", "status": "COMPLETED"}),
            ("status", {"job_type": "other", "job_id": "job-1", "status": "COMPLETED"}),
            ("status", {"job_type": "refresh_schema", "job_id": "job-1", "status": "UNKNOWN"}),
        )
        for operation, payload in payloads:
            with self.subTest(operation=operation, payload=payload):
                client = OmniClient(
                    base_url="https://omni.example",
                    api_key=FAKE_API_KEY,
                    session=FakeSession([FakeResponse(payload)]),
                )
                with self.assertRaises(OmniAPIError):
                    if operation == "start":
                        client.start_schema_refresh("model-1")
                    else:
                        client.get_schema_refresh_job_status("job-1")

    def test_content_metadata_uses_documented_organization_scope(self):
        session = FakeSession([FakeResponse({"records": [], "pageInfo": {}})])
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=session)
        client.list_content(labels=["Verified"], branch_id="not-supported-by-content-api")
        _, _, kwargs = session.calls[0]
        self.assertEqual(
            kwargs["params"],
            {"include": "labels", "scope": "organization", "labels": "Verified", "pageSize": 100},
        )

    def test_personal_content_metadata_uses_restricted_scope(self):
        session = FakeSession(
            [
                FakeResponse({"records": [{"id": "org"}], "pageInfo": {}}),
                FakeResponse({"records": [{"id": "personal"}], "pageInfo": {}}),
            ]
        )
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=session)
        records = client.list_content(include_personal_folders=True, user_id="user-1")
        self.assertEqual([record["id"] for record in records], ["org", "personal"])
        self.assertEqual(session.calls[1][2]["params"]["scope"], "restricted")
        self.assertEqual(session.calls[1][2]["params"]["creatorId"], "user-1")

    def test_label_filtering_personal_content_requires_user_id(self):
        client = OmniClient(
            base_url="https://omni.example",
            api_key=FAKE_API_KEY,
            session=FakeSession([]),
        )
        with self.assertRaises(ConfigError):
            client.list_content(labels=["Verified"], include_personal_folders=True)

    def test_ai_job_methods_use_branch_and_discard_data_bearing_fields(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "jobId": "job-1",
                        "conversationId": "conversation-1",
                        "omniChatUrl": "https://omni.example/chat/conversation-1",
                    },
                    status_code=201,
                ),
                FakeResponse(
                    {
                        "id": "job-1",
                        "state": "COMPLETE",
                        "prompt": "private prompt",
                        "resultSummary": "private query result",
                    }
                ),
            ]
        )
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=session)
        self.assertEqual(
            client.create_ai_job("model-1", branch_id="branch-1", prompt="Fix validation errors"),
            {"job_id": "job-1"},
        )
        self.assertEqual(client.get_ai_job_status("job-1"), {"job_id": "job-1", "state": "COMPLETE"})
        self.assertEqual(
            session.calls[0][2]["json"],
            {"modelId": "model-1", "branchId": "branch-1", "prompt": "Fix validation errors"},
        )

    def test_ai_status_rejects_mismatched_job_and_unknown_state(self):
        for payload in (
            {"id": "other-job", "state": "COMPLETE"},
            {"id": "job-1", "state": "SURPRISE"},
        ):
            with self.subTest(payload=payload):
                client = OmniClient(
                    base_url="https://omni.example",
                    api_key=FAKE_API_KEY,
                    session=FakeSession([FakeResponse(payload)]),
                )
                with self.assertRaises(OmniAPIError):
                    client.get_ai_job_status("job-1")

    def test_ai_job_cancellation_uses_idempotent_documented_endpoint(self):
        session = FakeSession([FakeResponse({"jobId": "job-1", "state": "CANCELLED"})])
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=session)
        self.assertEqual(client.cancel_ai_job("job-1"), {"job_id": "job-1", "state": "CANCELLED"})
        self.assertEqual(session.calls[0][0], "POST")
        self.assertEqual(session.calls[0][1], "https://omni.example/api/v1/ai/jobs/job-1/cancel")

    def test_yaml_write_delete_and_git_commit_use_documented_contracts(self):
        session = FakeSession(
            [
                FakeResponse({"fileName": "orders.view", "success": True}),
                FakeResponse({"fileName": "temporary.view", "success": True}),
                FakeResponse(
                    {
                        "pr_url": "https://github.com/example/repo/pull/1",
                        "git_sha": "abc123",
                        "in_sync": False,
                        "did_sync": True,
                    }
                ),
            ]
        )
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=session)
        client.update_model_yaml(
            "model-1",
            branch_id="branch-1",
            file_name="orders.view",
            yaml_text="name: orders\n",
            previous_checksum="checksum-1",
            commit_message="OmniFlow rollback",
        )
        client.delete_model_yaml(
            "model-1",
            branch_id="branch-1",
            file_name="temporary.view",
            commit_message="OmniFlow rollback",
        )
        commit = client.commit_model_branch(
            "model-1",
            branch_id="branch-1",
            commit_message="OmniFlow AI repair",
        )
        self.assertEqual(commit["git_sha"], "abc123")
        self.assertEqual(
            session.calls[0][2]["json"],
            {
                "fileName": "orders.view",
                "yaml": "name: orders\n",
                "mode": "combined",
                "branchId": "branch-1",
                "commitMessage": "OmniFlow rollback",
                "previousChecksum": "checksum-1",
            },
        )
        self.assertEqual(session.calls[1][0], "DELETE")
        self.assertEqual(session.calls[1][2]["params"]["branchId"], "branch-1")
        self.assertEqual(
            session.calls[2][2]["json"],
            {
                "branch_id": "branch-1",
                "commit_message": "OmniFlow AI repair",
                "allow_branch_exists": True,
                "require_branch_exists": True,
            },
        )

    def test_yaml_writes_accept_safe_nested_authored_file_paths(self):
        session = FakeSession(
            [
                FakeResponse({"fileName": "Omni Training/orders.view", "success": True}),
                FakeResponse({"fileName": "Omni Training/orders.view", "success": True}),
            ]
        )
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=session)
        client.update_model_yaml(
            "model-1",
            branch_id="branch-1",
            file_name="Omni Training/orders.view",
            yaml_text="name: orders\n",
            previous_checksum="checksum-1",
            commit_message="OmniFlow rollback",
        )
        client.delete_model_yaml(
            "model-1",
            branch_id="branch-1",
            file_name="Omni Training/orders.view",
            commit_message="OmniFlow rollback",
        )
        self.assertEqual(session.calls[0][2]["json"]["fileName"], "Omni Training/orders.view")
        self.assertEqual(session.calls[1][2]["params"]["fileName"], "Omni Training/orders.view")

    def test_non_idempotent_writes_are_never_retried(self):
        for call in ("ai", "yaml", "delete", "commit"):
            with self.subTest(call=call):
                session = FakeSession([FakeResponse({}, status_code=500)])
                client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=session)
                with self.assertRaises(OmniAPIError):
                    if call == "ai":
                        client.create_ai_job("model-1", branch_id="branch-1", prompt="Fix errors")
                    elif call == "yaml":
                        client.update_model_yaml(
                            "model-1",
                            branch_id="branch-1",
                            file_name="orders.view",
                            yaml_text="name: orders\n",
                            previous_checksum="checksum-1",
                            commit_message="Rollback",
                        )
                    elif call == "delete":
                        client.delete_model_yaml(
                            "model-1",
                            branch_id="branch-1",
                            file_name="orders.view",
                            commit_message="Rollback",
                        )
                    else:
                        client.commit_model_branch(
                            "model-1",
                            branch_id="branch-1",
                            commit_message="AI repair",
                        )
                self.assertEqual(len(session.calls), 1)

    def test_non_idempotent_transport_failure_is_not_retried_or_leaked(self):
        from requests import ConnectionError

        session = FailingSession([ConnectionError("Bearer private-token")])
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=session)
        with self.assertRaises(OmniAPIError) as raised:
            client.create_ai_job("model-1", branch_id="branch-1", prompt="Fix errors")
        self.assertEqual(len(session.calls), 1)
        self.assertNotIn("private-token", str(raised.exception))

    def test_write_methods_reject_unsupported_file_names_and_multiline_messages(self):
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=FakeSession([]))
        with self.assertRaises(ConfigError):
            client.delete_model_yaml(
                "model-1",
                branch_id="branch-1",
                file_name="model",
                commit_message="Rollback",
            )
        with self.assertRaises(ConfigError):
            client.commit_model_branch(
                "model-1",
                branch_id="branch-1",
                commit_message="line one\nline two",
            )

    def test_yaml_writes_reject_unsafe_or_unsupported_authored_file_paths(self):
        client = OmniClient(base_url="https://omni.example", api_key=FAKE_API_KEY, session=FakeSession([]))
        unsafe_names = (
            "../orders.view",
            "/orders.view",
            "nested//orders.view",
            "nested/./orders.view",
            "nested\\orders.view",
            "nested/orders.view\nother.view",
        )
        for file_name in unsafe_names:
            with self.subTest(file_name=file_name), self.assertRaises(SecurityPolicyError):
                client.update_model_yaml(
                    "model-1",
                    branch_id="branch-1",
                    file_name=file_name,
                    yaml_text="name: orders\n",
                    previous_checksum="checksum-1",
                    commit_message="Rollback",
                )
        with self.assertRaises(ConfigError):
            client.update_model_yaml(
                "model-1",
                branch_id="branch-1",
                file_name="nested/orders.yaml",
                yaml_text="name: orders\n",
                previous_checksum="checksum-1",
                commit_message="Rollback",
            )
        with self.assertRaises(ConfigError):
            client.update_model_yaml(
                "model-1",
                branch_id="branch-1",
                file_name="nested/model",
                yaml_text="name: model\n",
                previous_checksum="checksum-1",
                commit_message="Rollback",
            )


if __name__ == "__main__":
    unittest.main()
