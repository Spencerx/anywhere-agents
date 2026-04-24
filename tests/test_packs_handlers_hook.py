"""Tests for scripts/packs/handlers/hook.py.

Covers: file deployment to user-level path; owners-join on matching
content across repos; owners-reject on mismatched content; no-rewrite
on already-owned content.
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from packs import dispatch  # noqa: E402
from packs import handlers  # noqa: E402
from packs import state as state_mod  # noqa: E402
from packs import transaction as txn_mod  # noqa: E402
from packs.handlers import hook  # noqa: E402


def _make_ctx(
    *,
    pack_name: str,
    repo_id: str,
    pack_source_dir: Path,
    user_home: Path,
    txn: txn_mod.Transaction,
    user_state: dict,
) -> dispatch.DispatchContext:
    return dispatch.DispatchContext(
        pack_name=pack_name,
        pack_source_url="bundled:aa",
        pack_requested_ref="v1",
        pack_resolved_commit="abcd1234",
        pack_update_policy="locked",
        pack_source_dir=pack_source_dir,
        project_root=pack_source_dir.parent / "project",
        user_home=user_home,
        repo_id=repo_id,
        txn=txn,
        pack_lock=state_mod.empty_pack_lock(),
        project_state=state_mod.empty_project_state(),
        user_state=user_state,
    )


class _TmpDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pack_source_dir = self.root / "pack_src"
        self.pack_source_dir.mkdir()
        self.user_home = self.root / "home"
        (self.user_home / ".claude").mkdir(parents=True)
        self.staging = self.root / "stage.staging-hook"
        self.lock_path = self.root / "peer.lock"
        self.lock_path.write_text("0\n", encoding="utf-8")

        # Pack ships a hook file.
        hook_src = self.pack_source_dir / "scripts" / "hook.py"
        hook_src.parent.mkdir()
        hook_src.write_bytes(b"# hook body\n")
        self.hook_content = b"# hook body\n"
        self.hook_sha = hashlib.sha256(self.hook_content).hexdigest()


class HookDeploymentTests(_TmpDirCase):
    def test_first_install_writes_file_and_creates_owner(self) -> None:
        user_state = state_mod.empty_user_state()
        with txn_mod.Transaction(self.staging, self.lock_path) as txn:
            ctx = _make_ctx(
                pack_name="agent-behave",
                repo_id="repo-A",
                pack_source_dir=self.pack_source_dir,
                user_home=self.user_home,
                txn=txn,
                user_state=user_state,
            )
            hook.handle_hook(
                {
                    "kind": "hook",
                    "hosts": ["claude-code"],
                    "files": [
                        {
                            "from": "scripts/hook.py",
                            "to": "~/.claude/hooks/agent-behave/01-hook.py",
                        }
                    ],
                },
                ctx,
            )
            ctx.finalize_pack_lock()

        deployed = self.user_home / ".claude" / "hooks" / "agent-behave" / "01-hook.py"
        self.assertTrue(deployed.exists())
        self.assertEqual(deployed.read_bytes(), self.hook_content)

        # user-state has one entry with one owner.
        self.assertEqual(len(user_state["entries"]), 1)
        entry = user_state["entries"][0]
        self.assertEqual(entry["kind"], "active-hook")
        self.assertEqual(entry["expected_sha256_or_json"], self.hook_sha)
        self.assertEqual(len(entry["owners"]), 1)
        self.assertEqual(entry["owners"][0]["repo_id"], "repo-A")


class HookOwnerJoinTests(_TmpDirCase):
    def test_second_repo_same_content_joins_owners(self) -> None:
        """Repo A installs pack X; repo B installs same pack X with same
        content → owners list grows; on-disk content unchanged."""
        user_state = state_mod.empty_user_state()

        # Repo A installs.
        with txn_mod.Transaction(self.staging, self.lock_path) as txn:
            ctx = _make_ctx(
                pack_name="agent-behave",
                repo_id="repo-A",
                pack_source_dir=self.pack_source_dir,
                user_home=self.user_home,
                txn=txn,
                user_state=user_state,
            )
            hook.handle_hook(
                {
                    "kind": "hook",
                    "hosts": ["claude-code"],
                    "files": [
                        {
                            "from": "scripts/hook.py",
                            "to": "~/.claude/hooks/agent-behave/01-hook.py",
                        }
                    ],
                },
                ctx,
            )
            ctx.finalize_pack_lock()

        # Capture on-disk content hash after repo A's install.
        deployed = self.user_home / ".claude" / "hooks" / "agent-behave" / "01-hook.py"
        content_after_a = deployed.read_bytes()

        # Repo B installs same pack + same content.
        staging_b = self.root / "stage.staging-hook-b"
        with txn_mod.Transaction(staging_b, self.lock_path) as txn:
            ctx = _make_ctx(
                pack_name="agent-behave",
                repo_id="repo-B",
                pack_source_dir=self.pack_source_dir,
                user_home=self.user_home,
                txn=txn,
                user_state=user_state,
            )
            hook.handle_hook(
                {
                    "kind": "hook",
                    "hosts": ["claude-code"],
                    "files": [
                        {
                            "from": "scripts/hook.py",
                            "to": "~/.claude/hooks/agent-behave/01-hook.py",
                        }
                    ],
                },
                ctx,
            )
            ctx.finalize_pack_lock()

        # On-disk content unchanged.
        self.assertEqual(deployed.read_bytes(), content_after_a)

        # Single user-state entry, two owners.
        self.assertEqual(len(user_state["entries"]), 1)
        entry = user_state["entries"][0]
        owner_ids = {o["repo_id"] for o in entry["owners"]}
        self.assertEqual(owner_ids, {"repo-A", "repo-B"})


class HookOwnerConflictTests(_TmpDirCase):
    def test_different_content_fails_closed(self) -> None:
        """Repo B attempts to install a different-content hook at the same
        user-level path → UserLevelOutputConflict; repo A's state is
        untouched."""
        user_state = state_mod.empty_user_state()

        # Repo A installs original.
        with txn_mod.Transaction(self.staging, self.lock_path) as txn:
            ctx_a = _make_ctx(
                pack_name="agent-behave",
                repo_id="repo-A",
                pack_source_dir=self.pack_source_dir,
                user_home=self.user_home,
                txn=txn,
                user_state=user_state,
            )
            hook.handle_hook(
                {
                    "kind": "hook",
                    "hosts": ["claude-code"],
                    "files": [
                        {
                            "from": "scripts/hook.py",
                            "to": "~/.claude/hooks/agent-behave/01-hook.py",
                        }
                    ],
                },
                ctx_a,
            )
            ctx_a.finalize_pack_lock()

        # Rewrite source file with different content; repo B attempts install.
        (self.pack_source_dir / "scripts" / "hook.py").write_bytes(
            b"# DIFFERENT hook body\n"
        )

        staging_b = self.root / "stage.staging-hook-b"
        with txn_mod.Transaction(staging_b, self.lock_path) as txn:
            ctx_b = _make_ctx(
                pack_name="agent-behave",
                repo_id="repo-B",
                pack_source_dir=self.pack_source_dir,
                user_home=self.user_home,
                txn=txn,
                user_state=user_state,
            )
            with self.assertRaises(state_mod.UserLevelOutputConflict):
                hook.handle_hook(
                    {
                        "kind": "hook",
                        "hosts": ["claude-code"],
                        "files": [
                            {
                                "from": "scripts/hook.py",
                                "to": "~/.claude/hooks/agent-behave/01-hook.py",
                            }
                        ],
                    },
                    ctx_b,
                )

        # Repo A's owner record is preserved; no repo-B owner joined.
        self.assertEqual(len(user_state["entries"]), 1)
        owner_ids = {o["repo_id"] for o in user_state["entries"][0]["owners"]}
        self.assertEqual(owner_ids, {"repo-A"})


if __name__ == "__main__":
    unittest.main()
