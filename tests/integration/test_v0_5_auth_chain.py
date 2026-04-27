"""v0.5.0 auth chain integration tests — real GitHub creds required.

Skipped by default in CI. Run locally with: pytest -m integration

Pre-flight: agent-pack@v0.1.0 tag must exist; cv-pin.json must record
a stable yzhao062/CV ref + file + sha256.
"""
from __future__ import annotations
import json, pathlib, subprocess, sys
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.packs import auth, source_fetch  # noqa: E402,F401


pytestmark = pytest.mark.integration

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.mark.integration
class TestPublicAuthMethods:
    def test_ssh_path_clones_agent_pack(self, tmp_path):
        archive = auth.fetch_with_method(
            "https://github.com/yzhao062/agent-pack", "v0.1.0", "ssh", dest=tmp_path / "ssh")
        assert (archive.archive_dir / "pack.yaml").exists()

    def test_gh_path_clones_agent_pack(self, tmp_path):
        archive = auth.fetch_with_method(
            "https://github.com/yzhao062/agent-pack", "v0.1.0", "gh", dest=tmp_path / "gh")
        assert (archive.archive_dir / "pack.yaml").exists()

    def test_token_path_clones_agent_pack(self, tmp_path):
        # Use the user's REAL GITHUB_TOKEN env var (do not override it).
        # Skip when absent so the suite can still run without creds.
        import os
        if not os.environ.get("GITHUB_TOKEN"):
            pytest.skip("GITHUB_TOKEN not set; skipping token-method integration test")
        archive = auth.fetch_with_method(
            "https://github.com/yzhao062/agent-pack", "v0.1.0", "github_token", dest=tmp_path / "tok")
        assert (archive.archive_dir / "pack.yaml").exists()

    def test_anonymous_path_clones_agent_pack(self, tmp_path):
        archive = auth.fetch_with_method(
            "https://github.com/yzhao062/agent-pack", "v0.1.0", "anonymous", dest=tmp_path / "anon")
        assert (archive.archive_dir / "pack.yaml").exists()


@pytest.mark.integration
class TestPrivateAuth:
    def test_cv_private_fetch_byte_equality(self, tmp_path):
        pin = json.loads((FIXTURES / "cv-pin.json").read_text())
        archive = auth.fetch_with_method(
            "git@github.com:yzhao062/CV.git", pin["ref"], "ssh", dest=tmp_path / "cv")
        body = (archive.archive_dir / pin["file"]).read_bytes()
        import hashlib
        assert hashlib.sha256(body).hexdigest() == pin["sha256"]


@pytest.mark.integration
class TestFailClosed:
    def test_explicit_ssh_against_nonexistent_repo_fails_closed(self, tmp_path):
        with pytest.raises(auth.AuthChainExhaustedError):
            auth.fetch_with_auth_chain(
                "git@github.com:yzhao062/this-does-not-exist.git",
                "main", explicit_method="ssh")


@pytest.mark.integration
class TestSentinelTokenLeak:
    def test_env_sentinel_never_leaks(self, tmp_path, monkeypatch):
        SENTINEL = "anywhere-agents-sentinel-12345"
        monkeypatch.setenv("GITHUB_TOKEN", SENTINEL)
        try:
            archive = auth.fetch_with_method(
                "https://github.com/yzhao062/agent-pack", "v0.1.0",
                "github_token", dest=tmp_path / "tok")
        except subprocess.CalledProcessError as e:
            stderr = e.stderr or ""
            assert SENTINEL not in stderr

    def test_url_sentinel_in_rejected_url_does_not_leak(self):
        SENTINEL = "anywhere-agents-sentinel-67890"
        try:
            auth.reject_credential_url(
                f"https://{SENTINEL}@github.com/owner/repo",
                source_layer="test")
        except auth.CredentialURLError as e:
            assert SENTINEL not in str(e)
