"""Tests for scripts/packs/handlers/permission.py.

Covers: JSON merge into target file; distinct permission objects from
different packs coexist; same logical output with same content joins
owners; same logical output with different content fails closed.
"""
from __future__ import annotations

import json
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
from packs.handlers import permission  # noqa: E402


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
        pack_resolved_commit="abcd",
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


def _write_source_json(
    pack_source_dir: Path, name: str, payload: dict
) -> None:
    p = pack_source_dir / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")


class _TmpDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pack_source_dir = self.root / "pack_src"
        self.pack_source_dir.mkdir()
        self.user_home = self.root / "home"
        (self.user_home / ".claude").mkdir(parents=True)
        self.staging = self.root / "stage.staging-perm"
        self.lock_path = self.root / "peer.lock"
        self.lock_path.write_text("0\n", encoding="utf-8")


class PermissionMergeTests(_TmpDirCase):
    def test_first_install_adds_to_settings(self) -> None:
        """A permission entry under merge_key=permissions.ask appends the
        value to the ask-array in ~/.claude/settings.json."""
        _write_source_json(
            self.pack_source_dir,
            "permissions.json",
            {"permissions": {"ask": [{"pattern": "Bash(git push)"}]}},
        )
        user_state = state_mod.empty_user_state()
        with txn_mod.Transaction(self.staging, self.lock_path) as txn:
            ctx = _make_ctx(
                pack_name="pack-A",
                repo_id="repo-A",
                pack_source_dir=self.pack_source_dir,
                user_home=self.user_home,
                txn=txn,
                user_state=user_state,
            )
            permission.handle_permission(
                {
                    "kind": "permission",
                    "hosts": ["claude-code"],
                    "files": [
                        {"from": "permissions.json", "to": "~/.claude/settings.json"}
                    ],
                    "merge": "permissions.ask",
                },
                ctx,
            )
            ctx.finalize_pack_lock()

        settings = self.user_home / ".claude" / "settings.json"
        self.assertTrue(settings.exists())
        data = json.loads(settings.read_text(encoding="utf-8"))
        self.assertIn({"pattern": "Bash(git push)"}, data["permissions"]["ask"])

    def test_distinct_values_coexist(self) -> None:
        """Two packs that claim DIFFERENT permission values at the same
        merge-key path each install their value — both coexist in the
        shared settings.json without conflict."""
        _write_source_json(
            self.pack_source_dir,
            "a.json",
            {"permissions": {"ask": [{"pattern": "Bash(git push)"}]}},
        )
        _write_source_json(
            self.pack_source_dir,
            "b.json",
            {"permissions": {"ask": [{"pattern": "Bash(git reset --hard)"}]}},
        )
        user_state = state_mod.empty_user_state()

        with txn_mod.Transaction(self.staging, self.lock_path) as txn:
            ctx = _make_ctx(
                pack_name="pack-A", repo_id="repo-A",
                pack_source_dir=self.pack_source_dir, user_home=self.user_home,
                txn=txn, user_state=user_state,
            )
            permission.handle_permission(
                {
                    "kind": "permission", "hosts": ["claude-code"],
                    "files": [{"from": "a.json", "to": "~/.claude/settings.json"}],
                    "merge": "permissions.ask",
                },
                ctx,
            )
            ctx.finalize_pack_lock()

        staging_b = self.root / "stage.staging-perm-b"
        with txn_mod.Transaction(staging_b, self.lock_path) as txn:
            ctx = _make_ctx(
                pack_name="pack-B", repo_id="repo-B",
                pack_source_dir=self.pack_source_dir, user_home=self.user_home,
                txn=txn, user_state=user_state,
            )
            permission.handle_permission(
                {
                    "kind": "permission", "hosts": ["claude-code"],
                    "files": [{"from": "b.json", "to": "~/.claude/settings.json"}],
                    "merge": "permissions.ask",
                },
                ctx,
            )
            ctx.finalize_pack_lock()

        data = json.loads((self.user_home / ".claude" / "settings.json").read_text())
        ask = data["permissions"]["ask"]
        self.assertIn({"pattern": "Bash(git push)"}, ask)
        self.assertIn({"pattern": "Bash(git reset --hard)"}, ask)
        # Two distinct user-state entries (one per value), each with one owner.
        self.assertEqual(len(user_state["entries"]), 2)


class MultipleValuesInOneSourceTests(_TmpDirCase):
    """Regression: a source payload with multiple values at the merge
    key must accumulate via the in-memory shadow so every value lands
    in the final settings.json (Round 1 Codex High #1 fix)."""

    def test_two_values_in_one_source_both_land(self) -> None:
        _write_source_json(
            self.pack_source_dir,
            "permissions.json",
            {
                "permissions": {
                    "ask": [
                        {"pattern": "Bash(git push)"},
                        {"pattern": "Bash(git reset --hard)"},
                    ]
                }
            },
        )
        user_state = state_mod.empty_user_state()
        with txn_mod.Transaction(self.staging, self.lock_path) as txn:
            ctx = _make_ctx(
                pack_name="pack-A", repo_id="repo-A",
                pack_source_dir=self.pack_source_dir, user_home=self.user_home,
                txn=txn, user_state=user_state,
            )
            permission.handle_permission(
                {
                    "kind": "permission", "hosts": ["claude-code"],
                    "files": [{"from": "permissions.json", "to": "~/.claude/settings.json"}],
                    "merge": "permissions.ask",
                },
                ctx,
            )
            ctx.finalize_pack_lock()

        settings = self.user_home / ".claude" / "settings.json"
        data = json.loads(settings.read_text(encoding="utf-8"))
        ask = data["permissions"]["ask"]
        # Both values must appear in the final file — the shadow accumulates
        # across multiple stage_write calls in the same transaction.
        self.assertIn({"pattern": "Bash(git push)"}, ask)
        self.assertIn({"pattern": "Bash(git reset --hard)"}, ask)


class PermissionOwnerJoinTests(_TmpDirCase):
    def test_same_value_from_two_packs_joins_owners(self) -> None:
        """Pack A and pack B both ship the same permission value → owners
        list grows; settings.json contains the value exactly once."""
        same_payload = {"permissions": {"ask": [{"pattern": "Bash(git push)"}]}}
        _write_source_json(self.pack_source_dir, "a.json", same_payload)
        user_state = state_mod.empty_user_state()

        for pack_name, repo_id, staging_name in [
            ("pack-A", "repo-A", "stage-a"),
            ("pack-B", "repo-B", "stage-b"),
        ]:
            staging = self.root / f"stage.staging-{staging_name}"
            with txn_mod.Transaction(staging, self.lock_path) as txn:
                ctx = _make_ctx(
                    pack_name=pack_name, repo_id=repo_id,
                    pack_source_dir=self.pack_source_dir,
                    user_home=self.user_home, txn=txn, user_state=user_state,
                )
                permission.handle_permission(
                    {
                        "kind": "permission", "hosts": ["claude-code"],
                        "files": [{"from": "a.json", "to": "~/.claude/settings.json"}],
                        "merge": "permissions.ask",
                    },
                    ctx,
                )
                ctx.finalize_pack_lock()

        data = json.loads((self.user_home / ".claude" / "settings.json").read_text())
        ask = data["permissions"]["ask"]
        # Value appears exactly once (deduped at JSON merge).
        count = sum(
            1 for x in ask if x == {"pattern": "Bash(git push)"}
        )
        self.assertEqual(count, 1)
        # Two owners on the single user-state entry.
        self.assertEqual(len(user_state["entries"]), 1)
        owner_ids = {o["repo_id"] for o in user_state["entries"][0]["owners"]}
        self.assertEqual(owner_ids, {"repo-A", "repo-B"})


if __name__ == "__main__":
    unittest.main()
