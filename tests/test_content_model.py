import tempfile
import unittest
from pathlib import Path

from omniflow.validators.content import (
    collect_content_issues,
    filter_validator_payload,
    issue_identity,
    run_content_validation,
)
from omniflow.validators.model import parse_model_issues, run_model_validation


class FakeClient:
    def __init__(self):
        self.content_payload = {}
        self.base_content_payload = None
        self.content_records = []
        self.model_payload = []

    def validate_content(self, *args, **kwargs):
        if kwargs.get("branch_id") is None and self.base_content_payload is not None:
            return self.base_content_payload
        return self.content_payload

    def list_content(self, *args, **kwargs):
        return self.content_records

    def validate_model(self, *args, **kwargs):
        return self.model_payload


class ContentModelTests(unittest.TestCase):
    def test_content_issue_extraction_enriches_context(self):
        issues = collect_content_issues(
            {
                "content": [
                    {
                        "identifier": "dash-1",
                        "name": "Revenue",
                        "labels": [{"name": "Verified"}],
                        "owner": {"id": "u1", "name": "Alice"},
                        "queries_and_issues": [{"query_name": "Q1", "issues": [{"message": "Broken field"}]}],
                    }
                ]
            }
        )
        self.assertEqual(issues[0]["document_identifier"], "dash-1")
        self.assertEqual(issues[0]["document_labels"], ["Verified"])
        self.assertEqual(issues[0]["document_owner"], {"id": "u1", "name": "Alice"})

    def test_label_filtering_keeps_allowed_content(self):
        payload = {"content": [{"identifier": "a"}, {"identifier": "b"}]}
        self.assertEqual(filter_validator_payload(payload, {"b"})["content"], [{"identifier": "b"}])

    def test_issue_identity_ignores_metadata_enrichment(self):
        base = {"message": "Broken", "document_identifier": "dash-1"}
        enriched = {
            **base,
            "document_name": "Renamed dashboard",
            "query_name": "Renamed query",
            "document_labels": ["Verified"],
            "document_owner": {"name": "Alice"},
        }
        self.assertEqual(issue_identity(base), issue_identity(enriched))

    def test_content_validation_fail_on_new_only(self):
        client = FakeClient()
        client.content_payload = {
            "content": [
                {
                    "identifier": "dash-1",
                    "name": "Revenue",
                    "queries_and_issues": [{"query_name": "Q1", "issues": [{"message": "Broken field"}]}],
                }
            ]
        }
        client.content_records = [{"identifier": "dash-1", "labels": [{"name": "Verified"}]}]
        with tempfile.TemporaryDirectory() as tmp:
            report, exit_code = run_content_validation(
                client=client,
                model_id="model-1",
                branch_id=None,
                user_id=None,
                include_personal_folders=False,
                labels=["Verified"],
                history_in=Path(tmp) / "history.json",
                history_out=Path(tmp) / "history.json",
                report_out=Path(tmp) / "report.json",
                fail_on_new_only=True,
            )
        self.assertEqual(report["new_issues"], 1)
        self.assertEqual(exit_code, 1)

    def test_branch_content_compares_to_base_model_without_persisted_history(self):
        client = FakeClient()
        client.content_payload = {
            "content": [
                {
                    "identifier": "dash-1",
                    "name": "Revenue",
                    "queries_and_issues": [
                        {"query_name": "Existing", "issues": [{"message": "Existing issue"}]},
                        {"query_name": "New", "issues": [{"message": "New issue"}]},
                    ],
                }
            ]
        }
        client.base_content_payload = {
            "content": [
                {
                    "identifier": "dash-1",
                    "name": "Revenue",
                    "queries_and_issues": [{"query_name": "Existing", "issues": [{"message": "Existing issue"}]}],
                }
            ]
        }
        client.content_records = [{"identifier": "dash-1"}]
        with tempfile.TemporaryDirectory() as tmp:
            report, exit_code = run_content_validation(
                client=client,
                model_id="model-1",
                branch_id="branch-1",
                user_id=None,
                include_personal_folders=False,
                labels=[],
                history_in=Path(tmp) / "missing-history.json",
                history_out=Path(tmp) / "history.json",
                report_out=Path(tmp) / "report.json",
                fail_on_new_only=True,
            )
        self.assertEqual(report["comparison_source"], "base_model")
        self.assertEqual(report["new_issues"], 1)
        self.assertEqual(report["existing_issues"], 1)
        self.assertEqual(exit_code, 1)

    def test_content_validation_strips_raw_issue_by_default(self):
        client = FakeClient()
        client.content_payload = {
            "content": [
                {
                    "identifier": "dash-1",
                    "name": "Revenue",
                    "queries_and_issues": [
                        {
                            "query_name": "Q1",
                            "issues": [
                                {
                                    "message": "Broken field",
                                    "secretish_detail": "raw",  # pragma: allowlist secret
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        client.content_records = [{"identifier": "dash-1"}]
        with tempfile.TemporaryDirectory() as tmp:
            report, _ = run_content_validation(
                client=client,
                model_id="model-1",
                branch_id=None,
                user_id=None,
                include_personal_folders=False,
                labels=[],
                history_in=Path(tmp) / "history.json",
                history_out=Path(tmp) / "history.json",
                report_out=Path(tmp) / "report.json",
                fail_on_new_only=False,
            )
        self.assertNotIn("raw_issue", report["issues"][0]["raw"])

    def test_content_validation_can_include_raw_issue_when_explicitly_enabled(self):
        client = FakeClient()
        client.content_payload = {
            "content": [
                {
                    "identifier": "dash-1",
                    "name": "Revenue",
                    "queries_and_issues": [
                        {"query_name": "Q1", "issues": [{"message": "Broken field", "debug_detail": "raw"}]}
                    ],
                }
            ]
        }
        client.content_records = [{"identifier": "dash-1"}]
        with tempfile.TemporaryDirectory() as tmp:
            report, _ = run_content_validation(
                client=client,
                model_id="model-1",
                branch_id=None,
                user_id=None,
                include_personal_folders=False,
                labels=[],
                history_in=Path(tmp) / "history.json",
                history_out=Path(tmp) / "history.json",
                report_out=Path(tmp) / "report.json",
                fail_on_new_only=False,
                allow_raw_response_output=True,
            )
        self.assertIn("raw_issue", report["issues"][0]["raw"])

    def test_model_validation_parsing_and_exit_policy(self):
        client = FakeClient()
        client.model_payload = [{"message": "Warn", "is_warning": True}, {"message": "Error", "is_warning": False}]
        report, exit_code = run_model_validation(client=client, model_id="model-1", branch_id="branch-1")
        self.assertEqual(report["summary"], {"total_issues": 2, "errors": 1, "warnings": 1})
        self.assertEqual(exit_code, 1)
        self.assertEqual(parse_model_issues([{"message": "Warn", "is_warning": True}])[0]["severity"], "warning")


if __name__ == "__main__":
    unittest.main()
