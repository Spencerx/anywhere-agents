"""Tests for scripts/packs/passive.py (v2 passive adapter).

Covers URL derivation from {repo, ref, from} and happy-path fetch via
the legacy composer's mocked urlopen. Network is not touched; all fetch
tests substitute a mock opener.
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from packs import dispatch  # noqa: E402
from packs import passive as passive_mod  # noqa: E402
from packs import state as state_mod  # noqa: E402
from packs import transaction as txn_mod  # noqa: E402

import compose_rule_packs as _legacy  # noqa: E402


def _fake_urlopen(content_by_url):
    def _opener(url, *args, **kwargs):
        if url not in content_by_url:
            import urllib.error
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        resp = mock.MagicMock()
        resp.read.return_value = content_by_url[url]
        resp.__enter__ = lambda self_: resp
        resp.__exit__ = lambda self_, *a: None
        return resp
    return _opener


class DeriveRawUrlTests(unittest.TestCase):
    def test_github_url_derives_raw(self) -> None:
        url = passive_mod._derive_raw_url(
            "https://github.com/yzhao062/agent-style", "v0.3.2", "docs/rule-pack.md"
        )
        self.assertEqual(
            url,
            "https://raw.githubusercontent.com/yzhao062/agent-style/v0.3.2/docs/rule-pack.md",
        )

    def test_github_url_with_git_suffix(self) -> None:
        url = passive_mod._derive_raw_url(
            "https://github.com/yzhao062/agent-style.git", "main", "x.md"
        )
        self.assertEqual(
            url, "https://raw.githubusercontent.com/yzhao062/agent-style/main/x.md"
        )

    def test_raw_githubusercontent_rejects(self) -> None:
        """Pre-resolved raw URLs are not the canonical v2 source shape."""
        with self.assertRaisesRegex(ValueError, r"raw URLs are derived"):
            passive_mod._derive_raw_url(
                "https://raw.githubusercontent.com/foo/bar", "main", "x.md"
            )

    def test_unsupported_host_rejects(self) -> None:
        with self.assertRaisesRegex(
            ValueError, r"unsupported source host"
        ):
            passive_mod._derive_raw_url(
                "https://gitlab.com/foo/bar", "main", "x.md"
            )

    def test_invalid_github_path_rejects(self) -> None:
        with self.assertRaisesRegex(ValueError, r"expected"):
            passive_mod._derive_raw_url(
                "https://github.com/missing-repo-part", "main", "x.md"
            )


class HandlePassiveEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache_dir = self.root / "cache"
        self.staging = self.root / "stage.staging-passive"
        self.lock_path = self.root / "peer.lock"
        self.lock_path.write_text("0\n", encoding="utf-8")

    def _make_ctx(self, txn):
        return dispatch.DispatchContext(
            pack_name="agent-style",
            pack_source_url="https://github.com/yzhao062/agent-style",
            pack_requested_ref="v0.3.2",
            pack_resolved_commit="v0.3.2",
            pack_update_policy="locked",
            # v0.5.0 archive adapter keys on
            # ``ctx.pack_source_dir / mapping['from']`` existing. The
            # legacy bundled-pack contract leaves the rule-pack body
            # outside ``.agent-config/repo/`` so this directory does not
            # have to contain ``docs/rule-pack.md`` — the adapter falls
            # through to ``_legacy.fetch_rule_pack``, mocked below.
            pack_source_dir=self.root / "pack_src",
            project_root=self.root / "project",
            user_home=self.root / "home",
            repo_id="r",
            txn=txn,
            pack_lock=state_mod.empty_pack_lock(),
            project_state=state_mod.empty_project_state(),
            user_state=state_mod.empty_user_state(),
        )

    def test_happy_path_composes_with_begin_end_markers(self) -> None:
        pack = {
            "name": "agent-style",
            "source": {
                "repo": "https://github.com/yzhao062/agent-style",
                "ref": "v0.3.2",
            },
        }
        entry = {"files": [{"from": "docs/rule-pack.md", "to": "AGENTS.md"}]}
        rule_pack_text = "# Agent style rules\n"
        expected_url = "https://raw.githubusercontent.com/yzhao062/agent-style/v0.3.2/docs/rule-pack.md"

        with txn_mod.Transaction(self.staging, self.lock_path) as txn:
            ctx = self._make_ctx(txn)
            with mock.patch(
                "urllib.request.urlopen",
                _fake_urlopen({expected_url: rule_pack_text.encode("utf-8")}),
            ):
                composed = passive_mod.handle_passive_entry(
                    entry, pack, ctx,
                    upstream_agents_md="# Upstream\n",
                    cache_dir=self.cache_dir,
                    no_cache=False,
                )
            ctx.finalize_pack_lock()

        # Check composed text carries begin/end markers per the legacy
        # composer's format.
        self.assertIn("<!-- rule-pack:agent-style:begin", composed)
        self.assertIn("<!-- rule-pack:agent-style:end", composed)
        self.assertIn("# Agent style rules", composed)
        self.assertIn("# Upstream", composed)

        # Lock record captures passive role with file-level sha256.
        pack_entry = ctx.pack_lock["packs"]["agent-style"]
        file_entry = pack_entry["files"][0]
        self.assertEqual(file_entry["role"], "passive")
        self.assertIsNone(file_entry["host"])
        self.assertEqual(file_entry["source_path"], "docs/rule-pack.md")
        self.assertEqual(file_entry["output_paths"], ["AGENTS.md"])
        self.assertEqual(
            file_entry["input_sha256"],
            hashlib.sha256(rule_pack_text.encode("utf-8")).hexdigest(),
        )

    def test_non_agents_md_target_rejects(self) -> None:
        pack = {
            "name": "some-pack",
            "source": {"repo": "https://github.com/x/y", "ref": "main"},
        }
        entry = {"files": [{"from": "docs/foo.md", "to": "docs/foo.md"}]}
        with txn_mod.Transaction(self.staging, self.lock_path) as txn:
            ctx = self._make_ctx(txn)
            with self.assertRaisesRegex(ValueError, r"AGENTS.md"):
                passive_mod.handle_passive_entry(
                    entry, pack, ctx,
                    upstream_agents_md="",
                    cache_dir=self.cache_dir,
                    no_cache=False,
                )

    def test_missing_source_ref_rejects(self) -> None:
        pack = {"name": "x", "source": {"repo": "https://github.com/x/y"}}
        entry = {"files": [{"from": "a.md", "to": "AGENTS.md"}]}
        with txn_mod.Transaction(self.staging, self.lock_path) as txn:
            ctx = self._make_ctx(txn)
            with self.assertRaisesRegex(ValueError, r"source.ref"):
                passive_mod.handle_passive_entry(
                    entry, pack, ctx,
                    upstream_agents_md="",
                    cache_dir=self.cache_dir,
                    no_cache=False,
                )

    def test_missing_source_rejects(self) -> None:
        pack = {"name": "x"}  # no source at all
        entry = {"files": [{"from": "a.md", "to": "AGENTS.md"}]}
        with txn_mod.Transaction(self.staging, self.lock_path) as txn:
            ctx = self._make_ctx(txn)
            with self.assertRaisesRegex(ValueError, r"requires a pack-level 'source'"):
                passive_mod.handle_passive_entry(
                    entry, pack, ctx,
                    upstream_agents_md="",
                    cache_dir=self.cache_dir,
                    no_cache=False,
                )


if __name__ == "__main__":
    unittest.main()
