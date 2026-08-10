"""Per-method probe + driver tests for auth.py v0.5.0 extensions."""
from __future__ import annotations

import os
import pathlib
import sys
import unittest
from unittest.mock import patch, MagicMock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.packs import auth  # noqa: E402


class TestSSHAgentAvailable(unittest.TestCase):
    @patch("subprocess.run")
    def test_returns_true_when_ssh_add_l_exits_0(self, run):
        run.return_value = MagicMock(returncode=0, stdout="2048 SHA256:... key (RSA)\n", stderr="")
        self.assertTrue(auth.ssh_agent_available())

    @patch("subprocess.run")
    def test_returns_false_when_ssh_add_l_exits_1_no_identities(self, run):
        run.return_value = MagicMock(returncode=1, stdout="", stderr="The agent has no identities.\n")
        self.assertFalse(auth.ssh_agent_available())

    @patch("subprocess.run")
    def test_returns_false_when_ssh_add_not_on_path(self, run):
        run.side_effect = FileNotFoundError("ssh-add not found")
        self.assertFalse(auth.ssh_agent_available())


class TestGhCliAuthenticated(unittest.TestCase):
    @patch("subprocess.run")
    def test_returns_true_when_gh_auth_status_exits_0(self, run):
        run.return_value = MagicMock(returncode=0, stdout="", stderr="Logged in to github.com as yzhao062\n")
        self.assertTrue(auth.gh_cli_authenticated())

    @patch("subprocess.run")
    def test_returns_false_when_gh_auth_status_exits_1(self, run):
        run.return_value = MagicMock(returncode=1, stdout="", stderr="You are not logged in.\n")
        self.assertFalse(auth.gh_cli_authenticated())

    @patch("subprocess.run")
    def test_returns_false_when_gh_not_on_path(self, run):
        run.side_effect = FileNotFoundError("gh not found")
        self.assertFalse(auth.gh_cli_authenticated())


class TestGithubTokenAvailable(unittest.TestCase):
    @patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_abc123"})
    def test_returns_true_when_env_set(self):
        self.assertTrue(auth.github_token_available())

    @patch.dict(os.environ, {"GITHUB_TOKEN": ""})
    def test_returns_false_when_env_empty(self):
        self.assertFalse(auth.github_token_available())

    @patch.dict(os.environ, {}, clear=True)
    def test_returns_false_when_env_unset(self):
        self.assertFalse(auth.github_token_available())


class TestProbeCaching(unittest.TestCase):
    """A probe answers once per run, not once per pack.

    Before this, a five-pack consumer re-ran `gh auth status` for every
    pack that walked the auth chain, which was about 1.8 s of a 7 s
    bootstrap spent re-asking a question whose answer cannot change while
    the run is in flight.
    """

    @patch("subprocess.run")
    def test_no_caching_without_an_open_scope(self, run):
        """The default must stay uncached.

        Module state that is live by default would outlive the run that
        wanted it and would leak one test's probe result into the next.
        """
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        auth.gh_cli_authenticated()
        auth.gh_cli_authenticated()
        self.assertEqual(run.call_count, 2)

    @patch("subprocess.run")
    def test_gh_probe_shells_out_once_inside_a_scope(self, run):
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with auth.probe_cache():
            for _ in range(4):
                self.assertTrue(auth.gh_cli_authenticated())
        self.assertEqual(run.call_count, 1)

    @patch("subprocess.run")
    def test_negative_result_is_cached_as_well(self, run):
        run.return_value = MagicMock(returncode=1, stdout="", stderr="")
        with auth.probe_cache():
            self.assertFalse(auth.ssh_agent_available())
            self.assertFalse(auth.ssh_agent_available())
        self.assertEqual(run.call_count, 1)

    @patch("subprocess.run")
    def test_missing_binary_is_cached_as_well(self, run):
        run.side_effect = FileNotFoundError("gh not found")
        with auth.probe_cache():
            self.assertFalse(auth.gh_cli_authenticated())
            self.assertFalse(auth.gh_cli_authenticated())
        self.assertEqual(run.call_count, 1)

    @patch("subprocess.run")
    def test_the_two_probes_do_not_share_a_cache_entry(self, run):
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with auth.probe_cache():
            auth.ssh_agent_available()
            auth.gh_cli_authenticated()
        self.assertEqual(run.call_count, 2)

    @patch("subprocess.run")
    def test_leaving_the_scope_drops_the_cache(self, run):
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with auth.probe_cache():
            auth.gh_cli_authenticated()
        with auth.probe_cache():
            auth.gh_cli_authenticated()
        self.assertEqual(run.call_count, 2)

    @patch("subprocess.run")
    def test_an_exception_still_restores_the_previous_scope(self, run):
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with auth.probe_cache():
            auth.gh_cli_authenticated()
            with self.assertRaises(RuntimeError):
                with auth.probe_cache():
                    raise RuntimeError("boom")
            # The outer scope's answer survived the inner scope's unwind.
            auth.gh_cli_authenticated()
        self.assertEqual(run.call_count, 1)

    def test_token_probe_stays_uncached(self):
        """An env read costs nothing, so caching would only add staleness."""
        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_abc123"}):
            self.assertTrue(auth.github_token_available())
        with patch.dict(os.environ, {"GITHUB_TOKEN": ""}):
            self.assertFalse(auth.github_token_available())


class TestResolveRefWithAuthChain(unittest.TestCase):
    @patch("subprocess.run")
    @patch("scripts.packs.auth.ssh_agent_available", return_value=True)
    def test_ssh_path_resolves_to_commit_sha(self, _probe, run):
        # `git ls-remote git@... main` -> "<sha>\trefs/heads/main"
        run.return_value = MagicMock(
            returncode=0,
            stdout="abc123def4567890" + "0" * 24 + "\trefs/heads/main\n",
            stderr="",
        )
        sha, method = auth.resolve_ref_with_auth_chain(
            "https://github.com/yzhao062/agent-pack", "main")
        self.assertEqual(sha, "abc123def4567890" + "0" * 24)
        self.assertEqual(method, "ssh")

    @patch("subprocess.run")
    @patch("scripts.packs.auth.ssh_agent_available", return_value=False)
    @patch("scripts.packs.auth.gh_cli_authenticated", return_value=True)
    def test_gh_path_when_ssh_unavailable(self, _gh, _ssh, run):
        run.return_value = MagicMock(
            returncode=0,
            stdout="def456" + "0" * 34 + "\trefs/tags/v0.1.0\n",
            stderr="",
        )
        sha, method = auth.resolve_ref_with_auth_chain(
            "https://github.com/yzhao062/agent-pack", "v0.1.0")
        self.assertEqual(method, "gh")

    @patch("subprocess.run")
    @patch("scripts.packs.auth.ssh_agent_available", return_value=False)
    @patch("scripts.packs.auth.gh_cli_authenticated", return_value=False)
    @patch("scripts.packs.auth.github_token_available", return_value=False)
    def test_anonymous_path_when_all_others_unavailable(self, _t, _gh, _ssh, run):
        run.return_value = MagicMock(
            returncode=0,
            stdout="789abc" + "0" * 34 + "\trefs/heads/main\n",
            stderr="",
        )
        sha, method = auth.resolve_ref_with_auth_chain(
            "https://github.com/yzhao062/agent-pack", "main")
        self.assertEqual(method, "anonymous")

    @patch("subprocess.run")
    @patch("scripts.packs.auth.ssh_agent_available", return_value=True)
    def test_explicit_method_skips_chain_and_fails_closed(self, _ssh, run):
        run.return_value = MagicMock(returncode=128, stdout="", stderr="Permission denied (publickey)")
        with self.assertRaises(auth.AuthChainExhaustedError) as ctx:
            auth.resolve_ref_with_auth_chain(
                "git@github.com:yzhao062/CV.git", "master",
                explicit_method="ssh")
        self.assertIn("ssh", str(ctx.exception))
        self.assertIn("explicit", str(ctx.exception).lower())


class TestFetchWithMethod(unittest.TestCase):
    @patch("subprocess.run")
    def test_ssh_clone_uses_git_at_form(self, run):
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        # patch resolved_commit lookup
        with patch("scripts.packs.auth._git_rev_parse_head", return_value="ab12" * 10):
            archive = auth.fetch_with_method(
                "https://github.com/yzhao062/agent-pack", "main", "ssh",
                dest=None,  # tempdir auto-created; test only checks invocation
            )
        argv = run.call_args.args[0]
        self.assertIn("git@github.com:yzhao062/agent-pack.git", argv)

    @patch("subprocess.run")
    def test_gh_clone_uses_credential_helper_flag(self, run):
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with patch("scripts.packs.auth._git_rev_parse_head", return_value="ab12" * 10):
            archive = auth.fetch_with_method(
                "https://github.com/yzhao062/agent-pack", "main", "gh", dest=None)
        argv = run.call_args.args[0]
        # Look for the credential.helper config flag with gh auth git-credential
        joined = " ".join(argv)
        self.assertIn("credential.helper", joined)
        self.assertIn("gh auth git-credential", joined)

    @patch("subprocess.run")
    @patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_sentinel_secret_xyz"})
    def test_github_token_clone_does_not_leak_token_in_argv(self, run):
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with patch("scripts.packs.auth._git_rev_parse_head", return_value="ab12" * 10):
            archive = auth.fetch_with_method(
                "https://github.com/yzhao062/agent-pack", "main", "github_token", dest=None)
        argv_str = " ".join(run.call_args.args[0])
        # Token must not appear anywhere in argv
        self.assertNotIn("ghp_sentinel_secret_xyz", argv_str)
        self.assertNotIn("Bearer", argv_str)
        # The env passed should set GIT_ASKPASS to a helper
        env = run.call_args.kwargs.get("env", {})
        self.assertIn("GIT_ASKPASS", env)

    @patch("subprocess.run")
    def test_anonymous_clone_no_credential_config(self, run):
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with patch("scripts.packs.auth._git_rev_parse_head", return_value="ab12" * 10):
            archive = auth.fetch_with_method(
                "https://github.com/yzhao062/agent-pack", "main", "anonymous", dest=None)
        argv_str = " ".join(run.call_args.args[0])
        self.assertNotIn("credential.helper", argv_str)
        self.assertNotIn("Authorization", argv_str)

    @patch("subprocess.run")
    def test_fetch_with_method_rejects_credential_url(self, run, tmp_path=None):
        """Defense-in-depth: fetch_with_method rejects credential-bearing URLs
        before any git subprocess runs (Codex Round 2 H finding).

        The CI token-smoke workflow calls fetch_with_method directly, bypassing
        the orchestrator's reject_credential_url guard. Without this guard, a
        caller passing ``https://ghp_secret@github.com/o/r`` would leak the
        token into ``git clone`` argv. The per-method helper guard closes the
        gap.
        """
        with self.assertRaises(auth.CredentialURLError) as ctx:
            auth.fetch_with_method(
                "https://ghp_secret_token_xyz@github.com/o/r",
                "main",
                "github_token",
                dest=None,
            )
        # subprocess.run must not be called: the guard short-circuits before
        # any clone argv is built.
        run.assert_not_called()
        # Error message must redact the token (no raw secret in exception).
        self.assertNotIn("ghp_secret_token_xyz", str(ctx.exception))


class TestGitLsRemoteRejectsCredentialURL(unittest.TestCase):
    @patch("subprocess.run")
    def test_git_ls_remote_rejects_credential_url(self, run):
        """Defense-in-depth: _git_ls_remote rejects credential-bearing URLs
        before any git subprocess runs (Codex Round 2 H finding).

        ``_git_ls_remote`` is called internally by ``resolve_ref_with_auth_chain``
        but is also reachable directly. A direct caller passing
        ``https://ghp_secret@github.com/o/r`` would leak the token into
        ``git ls-remote`` argv. The per-method helper guard closes the gap.
        """
        with self.assertRaises(auth.CredentialURLError) as ctx:
            auth._git_ls_remote(
                "https://ghp_secret_token_abc@github.com/o/r",
                "main",
                "anonymous",
            )
        # subprocess.run must not be called: the guard short-circuits before
        # any ls-remote argv is built.
        run.assert_not_called()
        # Error message must redact the token (no raw secret in exception).
        self.assertNotIn("ghp_secret_token_abc", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
