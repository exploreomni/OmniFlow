"""Current-run, public-safe GitHub summaries and failure/recovery notifications.

Invoke the installed package with ``python -I -m`` from the trusted workflow.
Writes are not retried: an ambiguous response must be reconciled by listing the
bot's markers on a subsequent invocation. Workflow concurrency plus freshness
checks narrow races; GitHub does not provide atomic compare-and-swap comments.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..diagnostics import safe_exception_message
from ..exceptions import OmniFlowError
from ..json_evidence import reject_duplicate_keys
from ..reporting.notification import notification_decision, render_notification
from ..reporting.output_paths import report_directory
from .revalidation import GitHubRepository

MARKER = "<!-- omniflow-report -->"
STATE_PREFIX = "<!-- omniflow-state:v1 "
TRANSITION_PREFIX = "<!-- omniflow-transition:v1 "
MAX_PAGES = 10


class PublicationUnavailable(ValueError):
    """Fixed-text errors only: never echo an API body or local report."""


class Superseded(PublicationUnavailable):
    pass


def _positive(value: Any) -> int:
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
        value = int(value)
    if type(value) is not int or value <= 0:
        raise PublicationUnavailable("Publication requires exact positive run identifiers.")
    return value


def _read_json(path: Path, *, bound: int = 16 * 1024 * 1024) -> dict:
    # No symlinked checkout output, and no unbounded raw evidence reads.
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise PublicationUnavailable("Publication input is not a fresh regular file.")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > bound:
            raise PublicationUnavailable("Publication input exceeds its safe file bound.")
        data = stream.read(bound + 1)
    if len(data) > bound:
        raise PublicationUnavailable("Publication input exceeds its safe file bound.")
    try:
        value = json.loads(data, object_pairs_hook=reject_duplicate_keys)
    except (ValueError, UnicodeError) as exc:
        raise PublicationUnavailable(
            "OF-REPORT-MALFORMED: Required JSON evidence is malformed or ambiguous. "
            "Inspect report generation; no older report will be substituted."
        ) from exc
    if not isinstance(value, dict):
        raise PublicationUnavailable("Publication input is not an object.")
    return value


def publication_receipts(env: dict[str, str]) -> list[dict[str, str]]:
    """Only post-upload step outcomes and exact same-run artifact URLs count."""
    run_url = _run_url(env)
    receipts = []
    for kind, prefix in (("public", "PUBLIC"), ("repository", "REPOSITORY")):
        eligible = env.get("DIAGNOSTIC_OUTPUT_READY") == "true"
        if kind == "repository":
            eligible = eligible and env.get("REPOSITORY_READY") == "true" and env.get("PRIVATE_REPOSITORY") == "true"
        outcome = env.get(f"{prefix}_UPLOAD_OUTCOME", "") if eligible else "skipped"
        artifact = env.get(f"{prefix}_ARTIFACT_ID", "")
        url = env.get(f"{prefix}_ARTIFACT_URL", "")
        status = ("failed" if outcome in {"failure", "cancelled"} else
                  "skipped" if outcome in {"skipped", ""} else "unconfirmed")
        item = {"kind": kind, "status": status}
        if outcome == "success" and re.fullmatch(r"[1-9][0-9]*", artifact) and url == f"{run_url}/artifacts/{artifact}":
            item.update(status="uploaded", url=url)
        receipts.append(item)
    return receipts


def _run_url(env: dict[str, str]) -> str:
    server = env.get("GITHUB_SERVER_URL", "")
    repository = env.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"https://[A-Za-z0-9][A-Za-z0-9.-]*", server) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository,
    ):
        raise PublicationUnavailable("Publication requires a trusted GitHub run origin.")
    return f"{server}/{repository}/actions/runs/{_positive(env.get('GITHUB_RUN_ID'))}"


def _pages(api, path: str, key: str | None = None) -> list[dict]:
    items = []
    for page in range(1, MAX_PAGES + 1):
        response = api.request("GET", f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        batch = response.get(key) if key and isinstance(response, dict) else response
        if not isinstance(batch, list) or len(batch) > 100 or any(not isinstance(item, dict) for item in batch):
            raise PublicationUnavailable("GitHub publication enumeration is malformed.")
        if key and (type(response.get("total_count")) is not int or response["total_count"] < 0):
            raise PublicationUnavailable("GitHub publication enumeration lacks a total count.")
        items.extend(batch)
        if len(batch) < 100:
            if key and type(response.get("total_count")) is int and response["total_count"] > len(items):
                raise PublicationUnavailable("GitHub publication enumeration is incomplete.")
            return items
    raise PublicationUnavailable("GitHub publication enumeration exceeded its safe bound.")


def _stamp(body: str, prefix: str) -> dict | None:
    match = re.search(re.escape(prefix) + r"(\{[^\n]*\}) -->", body)
    if not match:
        return None
    try:
        data = json.loads(match[1], object_pairs_hook=reject_duplicate_keys)
        for key in ("run_id", "run_number", "attempt", "workflow_id"):
            data[key] = _positive(data[key])
        if not re.fullmatch(r"[0-9a-f]{40}", data["head"]) or data["decision"] not in {
            "fail", "pass", "skipped", "incomplete", "advisory",
        }:
            return None
        return data
    except (KeyError, TypeError, ValueError):
        return None


def _order(stamp: dict) -> tuple[int, int]:
    return stamp["run_number"], stamp["attempt"]


def _bot_comments(api, number: int) -> list[dict]:
    comments = _pages(api, f"/issues/{number}/comments")
    return [item for item in comments if isinstance(item.get("user"), dict)
            and item["user"].get("login") == "github-actions[bot]"
            and item.get("user", {}).get("type") == "Bot" and type(item.get("id")) is int
            and isinstance(item.get("body"), str)]


def _belongs_to_pr(run: dict, pr: dict, number: int) -> bool:
    associations = run.get("pull_requests")
    if not isinstance(associations, list) or any(
        not isinstance(item, dict) or type(item.get("number")) is not int for item in associations
    ):
        raise PublicationUnavailable("Workflow PR associations are unavailable.")
    if associations:
        return any(item["number"] == number for item in associations)
    # GitHub may omit pull_requests for pull_request_target. Match both branch
    # and source repository, not branch alone (fork names are not unique).
    repository = run.get("head_repository")
    if not isinstance(repository, dict) or not isinstance(repository.get("full_name"), str) or not isinstance(
        run.get("head_branch"), str,
    ):
        raise PublicationUnavailable("Workflow source identity is unavailable.")
    return (run["head_branch"] == pr["head"].get("ref")
            and repository["full_name"] == pr["head"].get("repo", {}).get("full_name"))


def assert_current(api, event: dict, env: dict[str, str], expected: dict | None = None) -> dict:
    number = _positive(event.get("number"))
    original = event.get("pull_request", {})
    live = api.request("GET", f"/pulls/{number}")
    if not isinstance(live, dict) or live.get("state") != "open":
        raise Superseded("Pull request is no longer open; no comment was published.")
    for side in ("head", "base"):
        prior = original.get(side, {})
        current = live.get(side, {})
        if (not re.fullmatch(r"[0-9a-f]{40}", str(prior.get("sha", "")))
                or current.get("sha") != prior["sha"]
                or current.get("repo", {}).get("full_name") != prior.get("repo", {}).get("full_name")):
            raise Superseded("Pull request revision changed; no stale comment was published.")
    if live["base"].get("repo", {}).get("full_name") != env.get("GITHUB_REPOSITORY"):
        raise PublicationUnavailable("Pull request base does not match this repository.")
    run_id = _positive(env.get("GITHUB_RUN_ID"))
    attempt = _positive(env.get("GITHUB_RUN_ATTEMPT"))
    run = api.request("GET", f"/actions/runs/{run_id}")
    if (not isinstance(run, dict) or run.get("id") != run_id or run.get("run_attempt") != attempt
            or run.get("event") != "pull_request_target" or run.get("conclusion") in {"cancelled", "stale"}
            or not _belongs_to_pr(run, live, number)):
        raise Superseded("This workflow attempt is not authoritative; no stale comment was published.")
    stamp = {"run_id": run_id, "attempt": attempt, "run_number": _positive(run.get("run_number")),
             "workflow_id": _positive(run.get("workflow_id")), "head": original["head"]["sha"]}
    if expected is not None and any(stamp[key] != expected[key] for key in stamp):
        raise Superseded("Workflow identity changed during publication.")
    created = run.get("created_at")
    if not isinstance(created, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", created):
        raise PublicationUnavailable("Workflow creation time is unavailable.")
    # Only this run and newer runs can supersede it. Do not enumerate all years
    # of repository history (GitHub caps filtered run searches at 1,000 rows).
    runs = _pages(api, f"/actions/workflows/{stamp['workflow_id']}/runs?event=pull_request_target"
                  f"&created={quote('>=' + created, safe='')}", "workflow_runs")
    if not any(other.get("id") == run_id and other.get("run_attempt") == attempt for other in runs):
        raise PublicationUnavailable("Current attempt is absent from the workflow run inventory.")
    for other in runs:
        if not _belongs_to_pr(other, live, number):
            continue
        other_order = (_positive(other.get("run_number")), _positive(other.get("run_attempt")))
        if other_order > _order(stamp):
            raise Superseded("A newer workflow run exists; no stale comment was published.")
    return stamp


def publish(api, report: dict, event: dict, env: dict[str, str]) -> dict:
    if env.get("GITHUB_EVENT_NAME") != "pull_request_target" or env.get("DIAGNOSTIC_OUTPUT_READY") != "true":
        raise PublicationUnavailable("Only fresh trusted pull-request reports may be published.")
    stamp = assert_current(api, event, env)
    stamp["decision"] = notification_decision(report)
    number = _positive(event.get("number"))
    body = render_notification(report, head_sha=stamp["head"], run_url=_run_url(env),
                               run_attempt=stamp["attempt"], publication=publication_receipts(env))
    comments = _bot_comments(api, number)
    stable = []
    transitions = []
    for comment in comments:
        data = _stamp(comment["body"], STATE_PREFIX) or _stamp(comment["body"], TRANSITION_PREFIX)
        if data and data["workflow_id"] != stamp["workflow_id"]:
            continue
        if data and _order(data) > _order(stamp):
            raise Superseded("A newer result is already published; no stale comment was published.")
        if MARKER in comment["body"]:
            stable.append(comment)
        if _stamp(comment["body"], TRANSITION_PREFIX):
            transitions.append(comment)
    stable.sort(key=lambda item: item["id"], reverse=True)
    transitions.sort(key=lambda item: (_order(_stamp(item["body"], TRANSITION_PREFIX)), item["id"]), reverse=True)
    current = stable[0] if stable else None
    previous = _stamp(transitions[0]["body"], TRANSITION_PREFIX) if transitions else None
    old_stable = _stamp(current["body"], STATE_PREFIX) if current else None
    # A failed transition POST may have followed a successful stable PATCH.
    # The last actual transition remains the recovery/deduplication authority.
    prior_decision = previous["decision"] if previous else (old_stable or {}).get("decision")
    transition = ("failure" if stamp["decision"] == "fail" and (not previous or prior_decision != "fail") else
                  "recovery" if stamp["decision"] == "pass" and prior_decision == "fail" else None)
    serialized = json.dumps(stamp, sort_keys=True, separators=(",", ":"))
    stable_body = f"{MARKER}\n{STATE_PREFIX}{serialized} -->\n{body}"
    # Creating the first stable failure comment already creates a notification:
    # do not create a redundant second comment. Preserve that historical marker
    # through future edits so a failed recovery POST can still be reconciled.
    initial_notice = _stamp(current["body"], TRANSITION_PREFIX) if current else None
    if initial_notice is None and current is None and transition == "failure":
        initial_notice = stamp
    if initial_notice is not None:
        stable_body += TRANSITION_PREFIX + json.dumps(initial_notice, sort_keys=True, separators=(",", ":")) + " -->\n"
    if not current or current["body"] != stable_body:
        assert_current(api, event, env, stamp)
        if current:
            api.request("PATCH", f"/issues/comments/{current['id']}", {"body": stable_body})
        else:
            api.request("POST", f"/issues/{number}/comments", {"body": stable_body})
    if transition:
        # Re-list immediately before POST. This reconciles retry-after-timeout
        # and repeated invocations; never blindly retry a notification write.
        latest = _bot_comments(api, number)
        transition_stamps = [_stamp(item["body"], TRANSITION_PREFIX) for item in latest]
        if any(data and data["workflow_id"] == stamp["workflow_id"] and _order(data) > _order(stamp)
               for data in transition_stamps):
            raise Superseded("A newer notification exists; no stale notification was published.")
        duplicate = any(data == stamp for data in transition_stamps)
        if not duplicate:
            assert_current(api, event, env, stamp)
            label = "Failure notification" if transition == "failure" else "Recovery notification"
            api.request("POST", f"/issues/{number}/comments", {
                "body": f"{TRANSITION_PREFIX}{serialized} -->\n**{label}**\n\n{body}",
            })
    return {"status": "published", "transition": transition or "none", "email_delivery": "unverified"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", action="store_true", help="Render post-upload summary without remote writes")
    parser.add_argument("--report-dir", default=os.getenv("OMNIFLOW_REPORT_DIR", ".omniflow"),
                        help="Explicit trusted output directory; never searches or falls back to another directory")
    args = parser.parse_args()
    env = dict(os.environ)
    try:
        if env.get("DIAGNOSTIC_OUTPUT_READY") != "true":
            raise PublicationUnavailable("No fresh report is authorized for publication.")
        report = _read_json(report_directory(args.report_dir) / "public" / "report.json")
        event = _read_json(Path(env["GITHUB_EVENT_PATH"]))
        if args.summary:
            head = event.get("pull_request", {}).get("head", {}).get("sha", env.get("GITHUB_SHA", ""))
            print(render_notification(report, head_sha=head, run_url=_run_url(env),
                                      run_attempt=_positive(env.get("GITHUB_RUN_ATTEMPT")),
                                      publication=publication_receipts(env)), end="")
        else:
            api = GitHubRepository(env.get("GITHUB_REPOSITORY", ""), env.get("GH_TOKEN", ""))
            publish(api, report, event, env)
            print("OmniFlow current-run summary reconciled. GitHub comment publication does not verify email delivery.")
        return 0
    except Superseded:
        print("OmniFlow notification skipped: this PR revision or workflow attempt has been superseded.")
        return 0
    except PublicationUnavailable as exc:
        print(f"::error::OmniFlow notification not published: {exc} Validation results are unchanged.")
        return 1
    except OmniFlowError as exc:
        print("::error::OmniFlow notification publication failed. " + safe_exception_message(exc))
        return 1
    except FileNotFoundError:
        print("::error::OF-REPORT-MISSING: Required current decision JSON was not found in the selected report directory. "
              "Match OMNIFLOW_REPORT_DIR to trusted reporting.output_dir and inspect the operation step. No fallback was used.")
        return 1
    except PermissionError:
        print("::error::OF-REPORT-ACCESS: Current decision evidence could not be read. Check runner output permissions; "
              "do not substitute an older report.")
        return 1
    except (json.JSONDecodeError, UnicodeDecodeError):
        print("::error::OF-REPORT-MALFORMED: Current decision evidence is not valid JSON. "
              "Inspect report generation and rerun; do not edit a result into a pass.")
        return 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print("::error::OmniFlow notification publication could not be verified. Check this run's publication step; "
              "validation results are unchanged. No blind notification retry was attempted.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
