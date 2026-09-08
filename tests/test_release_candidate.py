import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "verify_release_candidate", Path(__file__).resolve().parents[1] / "scripts/verify_release_candidate.py"
)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
SHA = "a" * 40


def evidence(*, main=SHA, conclusion="success", missing_job=False, other_sha=False):
    def fetch(path, *, pages=False):
        if path.endswith("git/ref/heads/main"):
            return {"object": {"sha": main}}
        if "/workflows/" in path:
            return [{"workflow_runs": [{"id": 1, "head_sha": "b" * 40 if other_sha else SHA,
                    "head_branch": "main", "event": "push", "status": "completed",
                    "conclusion": conclusion, "html_url": "https://github.com/example/repo/actions/runs/1"}]}]
        return [{"jobs": [{"name": name, "status": "completed", "conclusion": "success"}
                          for names in release.REQUIRED_JOBS.values() for name in names
                          if not (missing_job and name == "package")]}]
    return fetch


def test_release_accepts_only_exact_main_with_all_required_jobs():
    assert len(release.verify("example/repo", SHA, evidence())) == 5


@pytest.mark.parametrize("kwargs", [
    {"main": "b" * 40}, {"conclusion": "failure"}, {"conclusion": "skipped"},
    {"missing_job": True}, {"other_sha": True},
])
def test_release_rejects_stale_failed_skipped_or_incomplete_evidence(kwargs):
    with pytest.raises(ValueError):
        release.verify("example/repo", SHA, evidence(**kwargs))
