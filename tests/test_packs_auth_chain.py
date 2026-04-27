"""Auth chain orchestrator tests for v0.5.0."""
from __future__ import annotations

import pathlib
import unittest
from unittest.mock import patch, MagicMock

from scripts.packs import auth
from scripts.packs import source_fetch


class TestFetchWithAuthChain(unittest.TestCase):
    @patch("scripts.packs.auth.fetch_with_method")
    @patch("scripts.packs.auth.ssh_agent_available", return_value=True)
    def test_first_method_succeeds_short_circuits(self, _ssh, fetch):
        fetch.return_value = source_fetch.PackArchive(
            url="https://github.com/x/y", ref="main",
            resolved_commit="ab" * 20, method="ssh",
            archive_dir=pathlib.Path("/tmp/x"),
            canonical_id="x/y", cache_key="abcd1234/ab12",
        )
        archive = auth.fetch_with_auth_chain("https://github.com/x/y", "main")
        self.assertEqual(archive.method, "ssh")
        # Only ssh method tried
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[2], "ssh")

    @patch("scripts.packs.auth.fetch_with_method")
    @patch("scripts.packs.auth.ssh_agent_available", return_value=True)
    @patch("scripts.packs.auth.gh_cli_authenticated", return_value=True)
    def test_falls_through_to_gh_when_ssh_fetch_fails(self, _gh, _ssh, fetch):
        import subprocess
        fetch.side_effect = [
            subprocess.CalledProcessError(128, ["git"], stderr="Permission denied"),
            source_fetch.PackArchive(
                url="https://github.com/x/y", ref="main",
                resolved_commit="cd" * 20, method="gh",
                archive_dir=pathlib.Path("/tmp/x"),
                canonical_id="x/y", cache_key="abcd1234/cd12",
            ),
        ]
        archive = auth.fetch_with_auth_chain("https://github.com/x/y", "main")
        self.assertEqual(archive.method, "gh")
        self.assertEqual(fetch.call_count, 2)

    @patch("scripts.packs.auth.fetch_with_method")
    @patch("scripts.packs.auth.ssh_agent_available", return_value=True)
    def test_explicit_method_skips_chain_and_fails_closed(self, _ssh, fetch):
        import subprocess
        fetch.side_effect = subprocess.CalledProcessError(255, ["git"], stderr="bad key")
        with self.assertRaises(auth.AuthChainExhaustedError):
            auth.fetch_with_auth_chain(
                "git@github.com:x/y", "main", explicit_method="ssh")

    @patch("scripts.packs.auth.fetch_with_method")
    @patch("scripts.packs.auth.ssh_agent_available", return_value=False)
    @patch("scripts.packs.auth.gh_cli_authenticated", return_value=False)
    @patch("scripts.packs.auth.github_token_available", return_value=False)
    def test_anonymous_only_when_others_unavailable(self, _t, _gh, _ssh, fetch):
        fetch.return_value = source_fetch.PackArchive(
            url="https://github.com/x/y", ref="main",
            resolved_commit="ef" * 20, method="anonymous",
            archive_dir=pathlib.Path("/tmp/x"),
            canonical_id="x/y", cache_key="abcd1234/ef12",
        )
        archive = auth.fetch_with_auth_chain("https://github.com/x/y", "main")
        self.assertEqual(archive.method, "anonymous")
        self.assertEqual(fetch.call_count, 1)


class TestAuthChainExhaustedComposite(unittest.TestCase):
    @patch("scripts.packs.auth.fetch_with_method")
    @patch("scripts.packs.auth.ssh_agent_available", return_value=True)
    @patch("scripts.packs.auth.gh_cli_authenticated", return_value=True)
    @patch("scripts.packs.auth.github_token_available", return_value=False)
    def test_aggregates_failures_and_redacts_secrets(self, _t, _gh, _ssh, fetch):
        """When every method fails, the composite error names all
        attempts and redacts any token-like material in stderr."""
        import subprocess
        fetch.side_effect = [
            subprocess.CalledProcessError(128, ["git"], stderr="Permission denied (publickey)"),
            subprocess.CalledProcessError(
                128, ["git"],
                stderr="remote: token ghp_sentinel_xyz invalid",
            ),
            subprocess.CalledProcessError(128, ["git"], stderr="anonymous fetch failed"),
        ]
        with self.assertRaises(auth.AuthChainExhaustedError) as ctx:
            auth.fetch_with_auth_chain("https://github.com/x/y", "main")
        msg = str(ctx.exception)
        # All three attempted methods are mentioned in the composite
        self.assertIn("ssh", msg)
        self.assertIn("gh", msg)
        self.assertIn("anonymous", msg)
        # github_token branch was skipped (probe returned False)
        self.assertIn("github_token", msg)
        self.assertIn("skipped", msg)
        # Token material from stderr is redacted
        self.assertNotIn("ghp_sentinel_xyz", msg)
        self.assertIn("<redacted>", msg)


class TestRedactUrlUserinfo(unittest.TestCase):
    def test_https_token_userinfo_redacted(self):
        result = auth.redact_url_userinfo(
            "https://ghp_secret123@github.com/owner/repo")
        self.assertEqual(result, "https://<redacted>@github.com/owner/repo")

    def test_https_user_password_redacted(self):
        result = auth.redact_url_userinfo(
            "https://user:password@github.com/owner/repo")
        self.assertEqual(result, "https://<redacted>@github.com/owner/repo")

    def test_ssh_transport_username_unchanged(self):
        result = auth.redact_url_userinfo("git@github.com:owner/repo")
        self.assertEqual(result, "git@github.com:owner/repo")

    def test_ssh_with_password_userinfo_redacted(self):
        result = auth.redact_url_userinfo("ssh://user:pass@host/path")
        self.assertEqual(result, "ssh://user:<redacted>@host/path")


class TestRedactSecretText(unittest.TestCase):
    def test_known_secret_replaced(self):
        result = auth.redact_secret_text(
            "the token is ghp_sentinel_xyz everywhere",
            known_secrets=["ghp_sentinel_xyz"],
        )
        self.assertNotIn("ghp_sentinel_xyz", result)
        self.assertIn("<redacted>", result)

    def test_token_prefix_pattern_replaced(self):
        result = auth.redact_secret_text("Bearer ghp_abc123def456")
        self.assertNotIn("ghp_abc", result)


class TestRejectCredentialURLRedaction(unittest.TestCase):
    def test_rejection_message_does_not_echo_raw_secret(self):
        with self.assertRaises(auth.CredentialURLError) as ctx:
            auth.reject_credential_url(
                "https://ghp_sentinel_secret@github.com/x/y",
                source_layer="agent-config.yaml")
        msg = str(ctx.exception)
        self.assertNotIn("ghp_sentinel_secret", msg)
        self.assertIn("<redacted>", msg)


class TestPublicEntryPointsRejectCredentialURL(unittest.TestCase):
    """Codex Round 2 H3-A defense-in-depth: ``resolve_ref_with_auth_chain``
    and ``fetch_with_auth_chain`` MUST reject credential URLs at parse
    time, before any ``git ls-remote`` / ``git clone`` argv assembly.

    Without these checks, a caller that forgets to pre-validate (CLI,
    future plugin, anyone) leaks credentials in URL userinfo into git
    argv. The CLI also pre-validates (Fix #3-B) so this is the second
    line of defense.
    """

    def test_resolve_ref_with_auth_chain_rejects_credential_url(self):
        """Token in URL must raise ``CredentialURLError`` BEFORE any
        ``_git_ls_remote`` call. Patching ``_git_ls_remote`` confirms it
        was never reached."""
        with patch("scripts.packs.auth._git_ls_remote") as ls_remote:
            with self.assertRaises(auth.CredentialURLError):
                auth.resolve_ref_with_auth_chain(
                    "https://ghp_secret_xyz@github.com/x/y", "main",
                )
        ls_remote.assert_not_called()

    def test_fetch_with_auth_chain_rejects_credential_url(self):
        """Token in URL must raise ``CredentialURLError`` BEFORE any
        ``fetch_with_method`` call. Patching ``fetch_with_method``
        confirms it was never reached."""
        with patch("scripts.packs.auth.fetch_with_method") as fetch:
            with self.assertRaises(auth.CredentialURLError):
                auth.fetch_with_auth_chain(
                    "https://ghp_secret_xyz@github.com/x/y", "main",
                )
        fetch.assert_not_called()

    def test_resolve_ref_with_auth_chain_does_not_leak_token_in_error(self):
        """The ``CredentialURLError`` message itself must not echo the
        raw token bytes. Defense against a future change to the error
        text that loses redaction."""
        with self.assertRaises(auth.CredentialURLError) as ctx:
            auth.resolve_ref_with_auth_chain(
                "https://ghp_sentinel_abc@github.com/x/y", "main",
            )
        msg = str(ctx.exception)
        self.assertNotIn("ghp_sentinel_abc", msg)
        self.assertIn("<redacted>", msg)


if __name__ == "__main__":
    unittest.main()
