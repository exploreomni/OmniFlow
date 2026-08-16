import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omniflow.exceptions import OmniAPIError, SecurityPolicyError
from omniflow.github.repair_attempt import GitHubRepairAttemptGuard, load_repair_event

HEAD_SHA = "a" * 40
FAKE_GITHUB_TOKEN = "github-token"  # pragma: allowlist secret


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.headers = headers or {}

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def event_payload(*, fork=False, action="labeled", label="omniflow-ai-repair"):
    head_repo = "example/fork" if fork else "example/repo"
    return {
        "action": action,
        "label": {"name": label},
        "number": 7,
        "repository": {"full_name": "example/repo"},
        "sender": {"login": "maintainer"},
        "pull_request": {
            "number": 7,
            "head": {"sha": HEAD_SHA, "ref": "feature/repair", "repo": {"full_name": head_repo}},
            "base": {"ref": "main"},
        },
    }


class RepairAttemptTests(unittest.TestCase):
    def load_event(self, payload, *, event_name="pull_request_target"):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "event.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {
                    "GITHUB_ACTIONS": "true",
                    "GITHUB_EVENT_NAME": event_name,
                    "GITHUB_EVENT_PATH": str(path),
                    "GITHUB_REPOSITORY": "example/repo",
                    "GITHUB_ACTOR": "maintainer",
                },
                clear=True,
            ):
                return load_repair_event()

    def test_valid_same_repository_label_event_is_accepted(self):
        context = self.load_event(event_payload())
        self.assertEqual(context.repository, "example/repo")
        self.assertEqual(context.pull_request_number, 7)
        self.assertEqual(context.head_sha, HEAD_SHA)

    def test_untrusted_repair_events_fail_closed(self):
        cases = (
            (event_payload(fork=True), "pull_request_target"),
            (event_payload(action="opened"), "pull_request_target"),
            (event_payload(label="other"), "pull_request_target"),
            (event_payload(), "pull_request"),
        )
        for payload, event_name in cases:
            with self.subTest(payload=payload, event_name=event_name):
                with self.assertRaises(SecurityPolicyError):
                    self.load_event(payload, event_name=event_name)

    def test_attempt_claim_is_persisted_as_a_bot_comment(self):
        context = self.load_event(event_payload())
        session = FakeSession([FakeResponse([]), FakeResponse({"id": 42}, status_code=201)])
        guard = GitHubRepairAttemptGuard(token=FAKE_GITHUB_TOKEN, session=session)
        self.assertEqual(guard.claim(context), 42)
        self.assertEqual(session.calls[0][0], "GET")
        self.assertEqual(session.calls[1][0], "POST")
        self.assertIn(context.marker, session.calls[1][2]["json"]["body"])
        self.assertNotIn(FAKE_GITHUB_TOKEN, session.calls[1][2]["json"]["body"])

    def test_existing_bot_marker_blocks_duplicate_attempt(self):
        context = self.load_event(event_payload())
        session = FakeSession(
            [FakeResponse([{"user": {"login": "github-actions[bot]"}, "body": context.marker}])]
        )
        guard = GitHubRepairAttemptGuard(token=FAKE_GITHUB_TOKEN, session=session)
        with self.assertRaises(SecurityPolicyError):
            guard.claim(context)
        self.assertEqual(len(session.calls), 1)

    def test_untrusted_user_cannot_forge_attempt_marker(self):
        context = self.load_event(event_payload())
        session = FakeSession(
            [
                FakeResponse([{"user": {"login": "attacker"}, "body": context.marker}]),
                FakeResponse({"id": 42}, status_code=201),
            ]
        )
        guard = GitHubRepairAttemptGuard(token=FAKE_GITHUB_TOKEN, session=session)
        self.assertEqual(guard.claim(context), 42)

    def test_attempt_completion_uses_fixed_sanitized_status_text(self):
        context = self.load_event(event_payload())
        session = FakeSession([FakeResponse({"id": 42})])
        guard = GitHubRepairAttemptGuard(token=FAKE_GITHUB_TOKEN, session=session)
        guard.complete(context, comment_id=42, status="rolled_back")
        self.assertEqual(session.calls[0][0], "PATCH")
        self.assertIn("rolled back", session.calls[0][2]["json"]["body"])

    def test_github_error_does_not_echo_response_body(self):
        context = self.load_event(event_payload())
        session = FakeSession([FakeResponse({"private": "payload"}, status_code=500)])
        guard = GitHubRepairAttemptGuard(token=FAKE_GITHUB_TOKEN, session=session)
        with self.assertRaises(OmniAPIError) as raised:
            guard.claim(context)
        self.assertNotIn("private", str(raised.exception))

    def test_attempt_lookup_fails_closed_when_comment_history_exceeds_bound(self):
        context = self.load_event(event_payload())
        page = [{"user": {"login": "someone"}, "body": "ordinary"} for _ in range(100)]
        session = FakeSession([FakeResponse(page) for _ in range(10)])
        guard = GitHubRepairAttemptGuard(token=FAKE_GITHUB_TOKEN, session=session)
        with self.assertRaises(SecurityPolicyError):
            guard.claim(context)
        self.assertEqual(len(session.calls), 10)

    def test_attempt_lookup_rejects_oversized_responses(self):
        context = self.load_event(event_payload())
        session = FakeSession([FakeResponse([], headers={"Content-Length": str(9 * 1024 * 1024)})])
        guard = GitHubRepairAttemptGuard(token=FAKE_GITHUB_TOKEN, session=session)
        with self.assertRaises(OmniAPIError):
            guard.claim(context)


if __name__ == "__main__":
    unittest.main()
