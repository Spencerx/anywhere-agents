"""Tests for scripts/packs/auth.py (credential rejection + fetch env + GitHub URL norm)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from packs import auth  # noqa: E402


class CredentialRejectTests(unittest.TestCase):
    def test_plain_https_passes(self) -> None:
        auth.reject_credential_url("https://example.com/foo/bar")

    def test_http_plain_passes(self) -> None:
        auth.reject_credential_url("http://example.com/foo")

    def test_user_at_host_rejects(self) -> None:
        with self.assertRaises(auth.CredentialURLError):
            auth.reject_credential_url("https://user@example.com/foo")

    def test_user_pass_at_host_rejects(self) -> None:
        with self.assertRaises(auth.CredentialURLError):
            auth.reject_credential_url("https://user:pass@example.com/foo")

    def test_token_at_host_rejects(self) -> None:
        with self.assertRaises(auth.CredentialURLError):
            auth.reject_credential_url("https://ghp_xyz@github.com/foo/bar")

    def test_ssh_scp_form_passes(self) -> None:
        """SSH transport username is not a credential."""
        auth.reject_credential_url("git@github.com:owner/repo.git")

    def test_ssh_url_form_passes(self) -> None:
        auth.reject_credential_url("ssh://git@github.com/owner/repo")

    def test_git_ssh_form_passes(self) -> None:
        auth.reject_credential_url("git+ssh://git@example.com/foo/bar")

    def test_ssh_url_with_password_rejects(self) -> None:
        """Regression for Round 1 Codex High #2: SSH URLs with a password
        in userinfo are NOT transport-only; they must be rejected per
        pack-architecture.md:392."""
        with self.assertRaises(auth.CredentialURLError):
            auth.reject_credential_url(
                "ssh://user:secret@github.com/owner/repo"
            )

    def test_git_ssh_url_with_password_rejects(self) -> None:
        with self.assertRaises(auth.CredentialURLError):
            auth.reject_credential_url(
                "git+ssh://user:secret@example.com/foo/bar"
            )

    def test_ssh_url_without_password_passes(self) -> None:
        """Username-only SSH (no password component) is transport-only
        and passes; this is the happy path for git@host: style invocations."""
        auth.reject_credential_url("ssh://someuser@github.com/owner/repo")

    def test_empty_url_passes(self) -> None:
        auth.reject_credential_url("")

    def test_source_layer_in_error(self) -> None:
        with self.assertRaises(auth.CredentialURLError) as cm:
            auth.reject_credential_url(
                "https://u@example.com/foo", source_layer="user-level"
            )
        self.assertIn("user-level", str(cm.exception))

    def test_uppercase_https_userinfo_rejects(self) -> None:
        """Regression for Round 2 Codex H3-reopened: URL schemes are
        case-insensitive per RFC 3986, so an uppercase ``HTTPS://`` with
        userinfo must reject the same as lowercase ``https://``.
        """
        with self.assertRaises(auth.CredentialURLError):
            auth.reject_credential_url("HTTPS://ghp_secret@github.com/foo/bar")
        with self.assertRaises(auth.CredentialURLError):
            auth.reject_credential_url("Https://user:pass@example.com/foo")

    def test_uppercase_ssh_password_rejects(self) -> None:
        with self.assertRaises(auth.CredentialURLError):
            auth.reject_credential_url("SSH://user:secret@github.com/owner/repo")
        with self.assertRaises(auth.CredentialURLError):
            auth.reject_credential_url("Git+SSH://user:secret@example.com/foo")


class RedactUrlUserinfoTests(unittest.TestCase):
    def test_https_userinfo_redacted(self) -> None:
        self.assertEqual(
            auth.redact_url_userinfo("https://ghp_secret@github.com/foo/bar"),
            "https://<redacted>@github.com/foo/bar",
        )

    def test_uppercase_https_userinfo_redacted(self) -> None:
        """Regression for Round 2 Codex H3-reopened: redactor must match
        ``HTTPS://`` the same as ``https://``.
        """
        result = auth.redact_url_userinfo(
            "HTTPS://ghp_secret@github.com/foo/bar"
        )
        self.assertNotIn("ghp_secret", result)
        self.assertIn("<redacted>", result)

    def test_ssh_password_redacted(self) -> None:
        self.assertEqual(
            auth.redact_url_userinfo("ssh://user:secret@github.com/owner/repo"),
            "ssh://user:<redacted>@github.com/owner/repo",
        )

    def test_uppercase_ssh_password_redacted(self) -> None:
        result = auth.redact_url_userinfo(
            "SSH://user:secret@github.com/owner/repo"
        )
        self.assertNotIn("secret", result)
        self.assertIn("<redacted>", result)

    def test_scp_form_unchanged(self) -> None:
        self.assertEqual(
            auth.redact_url_userinfo("git@github.com:owner/repo.git"),
            "git@github.com:owner/repo.git",
        )

    def test_plain_url_unchanged(self) -> None:
        self.assertEqual(
            auth.redact_url_userinfo("https://github.com/foo/bar"),
            "https://github.com/foo/bar",
        )


class NoninteractiveFetchEnvTests(unittest.TestCase):
    def test_sets_both_flags(self) -> None:
        env = auth.noninteractive_fetch_env({"EXISTING": "x"})
        self.assertEqual(env["EXISTING"], "x")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertIn("BatchMode=yes", env["GIT_SSH_COMMAND"])
        self.assertIn("ConnectTimeout=10", env["GIT_SSH_COMMAND"])

    def test_returns_fresh_copy(self) -> None:
        base = {"A": "1"}
        result = auth.noninteractive_fetch_env(base)
        result["NEW"] = "z"
        self.assertNotIn("NEW", base)

    def test_default_uses_os_environ(self) -> None:
        env = auth.noninteractive_fetch_env()
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")


class GithubUrlNormalizeTests(unittest.TestCase):
    def test_https_plain(self) -> None:
        self.assertEqual(
            auth.normalize_github_url("https://github.com/foo/bar"),
            ("foo", "bar"),
        )

    def test_https_with_git_suffix(self) -> None:
        self.assertEqual(
            auth.normalize_github_url("https://github.com/foo/bar.git"),
            ("foo", "bar"),
        )

    def test_ssh_scp_form(self) -> None:
        self.assertEqual(
            auth.normalize_github_url("git@github.com:foo/bar.git"),
            ("foo", "bar"),
        )

    def test_ssh_url_form(self) -> None:
        self.assertEqual(
            auth.normalize_github_url("ssh://git@github.com/foo/bar"),
            ("foo", "bar"),
        )

    def test_non_github_returns_none(self) -> None:
        self.assertIsNone(
            auth.normalize_github_url("https://gitlab.com/foo/bar")
        )
        self.assertIsNone(
            auth.normalize_github_url("https://github.mycompany.edu/foo/bar")
        )

    def test_malformed_github_raises(self) -> None:
        with self.assertRaises(auth.GithubURLParseError):
            auth.normalize_github_url("https://github.com/just-owner-no-repo")

    def test_canonical_identity_helper(self) -> None:
        self.assertEqual(
            auth.canonical_github_identity("https://github.com/a/b.git"),
            "a/b",
        )
        self.assertIsNone(
            auth.canonical_github_identity("https://gitlab.com/a/b")
        )


if __name__ == "__main__":
    unittest.main()
