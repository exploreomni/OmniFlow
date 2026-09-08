import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow():
    return yaml.load((ROOT / ".github/workflows/secret-scan.yml").read_text(), Loader=yaml.BaseLoader)


def test_scanner_preserves_read_only_full_history_and_redacted_output():
    scan = workflow()
    assert scan["permissions"] == {"contents": "read"}
    steps = scan["jobs"]["gitleaks"]["steps"]
    assert [step["uses"].split("@")[0] for step in steps if "uses" in step] == ["actions/checkout"]
    assert steps[0]["with"]["fetch-depth"] == "0"
    assert steps[0]["with"]["persist-credentials"] == "false"
    command = steps[-1]["run"]
    assert "--redact" in command and '--log-opts="--all"' in command
    assert "--report" not in command
    assert "secrets." not in str(scan)


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="checksum verifier requires sha256sum")
def test_corrupt_scanner_archive_stops_before_extraction(tmp_path):
    steps = workflow()["jobs"]["gitleaks"]["steps"]
    install = next(step["run"] for step in steps if step.get("name") == "Install verified Gitleaks CLI")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Simulate a successful download with invalid bytes. The real checksum tool
    # must stop this script before tar can process the untrusted archive.
    curl = bin_dir / "curl"
    curl.write_text('#!/bin/sh\nwhile [ "$1" != "--output" ]; do shift; done\nshift\nprintf corrupt > "$1"\n')
    curl.chmod(0o700)
    tar = bin_dir / "tar"
    tar.write_text('#!/bin/sh\ntouch "$RUNNER_TEMP/extracted"\n')
    tar.chmod(0o700)
    if sys.platform == "darwin":
        # macOS's sha256sum lacks GNU long flags; use its real SHA-256 verifier
        # with the same check/strict contract. Linux exercises sha256sum directly.
        checksum = bin_dir / "sha256sum"
        checksum.write_text('#!/bin/sh\nexec /usr/bin/shasum -a 256 "$@"\n')
        checksum.chmod(0o700)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_PATH": str(tmp_path / "github-path"),
    }
    result = subprocess.run(["bash", "-c", install], env=env, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "FAILED" in result.stdout
    assert not (tmp_path / "extracted").exists()
    assert not (tmp_path / "github-path").exists()
