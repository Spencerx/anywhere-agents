"""Tests for scripts/packs/uninstall.py (internal engine)."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from packs import state as state_mod  # noqa: E402
from packs import uninstall as uninstall_mod  # noqa: E402


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Fixture(unittest.TestCase):
    """Base fixture: builds a fake consumer project with state files."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project_root = self.root / "project"
        self.project_root.mkdir()
        (self.project_root / ".agent-config").mkdir()
        self.user_home = self.root / "home"
        (self.user_home / ".claude").mkdir(parents=True)
        self.repo_id = "test-repo-id"


class NoOpTests(_Fixture):
    def test_no_state_files_is_no_op(self) -> None:
        result = uninstall_mod.run_uninstall_all(
            self.project_root, user_home=self.user_home, repo_id=self.repo_id,
        )
        self.assertEqual(result.status, uninstall_mod.STATUS_NO_OP)

    def test_empty_pack_lock_is_no_op(self) -> None:
        state_mod.save_pack_lock(
            self.project_root / ".agent-config" / "pack-lock.json",
            state_mod.empty_pack_lock(),
        )
        result = uninstall_mod.run_uninstall_all(
            self.project_root, user_home=self.user_home, repo_id=self.repo_id,
        )
        self.assertEqual(result.status, uninstall_mod.STATUS_NO_OP)


class ProjectLocalCleanupTests(_Fixture):
    def test_clean_removes_tracked_file(self) -> None:
        # Arrange: a pack-lock entry for a project-local file + the file.
        target_rel = ".claude/commands/demo.md"
        target_abs = self.project_root / target_rel
        target_abs.parent.mkdir(parents=True)
        content = b"pointer\n"
        target_abs.write_bytes(content)

        pack_lock = {
            "version": state_mod.SCHEMA_VERSION,
            "packs": {
                "demo-pack": {
                    "source_url": "bundled:aa",
                    "requested_ref": "bundled",
                    "resolved_commit": "bundled",
                    "pack_update_policy": "locked",
                    "files": [
                        {
                            "role": "active-skill",
                            "host": "claude-code",
                            "source_path": ".claude/commands/demo.md",
                            "input_sha256": _sha(content),
                            "output_paths": [target_rel],
                            "output_scope": "project-local",
                            "effective_update_policy": "locked",
                        }
                    ],
                }
            },
        }
        state_mod.save_pack_lock(
            self.project_root / ".agent-config" / "pack-lock.json", pack_lock,
        )

        result = uninstall_mod.run_uninstall_all(
            self.project_root, user_home=self.user_home, repo_id=self.repo_id,
        )
        self.assertEqual(result.status, uninstall_mod.STATUS_CLEAN)
        self.assertFalse(target_abs.exists())
        self.assertIn(target_rel, result.files_deleted)
        self.assertIn("demo-pack", result.packs_removed)

    def test_drift_preserves_state_files(self) -> None:
        """Regression for Round 1 Codex High #1: drift is fail-closed;
        state files must NOT be rewritten to empty once drift is seen,
        otherwise the drifted file has no ownership record left for a
        safe retry."""
        target_rel = ".claude/commands/demo.md"
        target_abs = self.project_root / target_rel
        target_abs.parent.mkdir(parents=True)
        target_abs.write_bytes(b"user-modified\n")

        lock_payload = {
            "version": state_mod.SCHEMA_VERSION,
            "packs": {
                "demo-pack": {
                    "source_url": "bundled:aa",
                    "requested_ref": "bundled",
                    "resolved_commit": "bundled",
                    "pack_update_policy": "locked",
                    "files": [
                        {
                            "role": "active-skill",
                            "host": "claude-code",
                            "source_path": ".claude/commands/demo.md",
                            "input_sha256": _sha(b"original\n"),
                            "output_paths": [target_rel],
                            "output_scope": "project-local",
                            "effective_update_policy": "locked",
                        }
                    ],
                }
            },
        }
        lock_path = self.project_root / ".agent-config" / "pack-lock.json"
        state_mod.save_pack_lock(lock_path, lock_payload)

        result = uninstall_mod.run_uninstall_all(
            self.project_root, user_home=self.user_home, repo_id=self.repo_id,
        )
        self.assertEqual(result.status, uninstall_mod.STATUS_DRIFT)
        # The on-disk pack-lock must STILL carry the original entry so a
        # retry (after the user resolves drift) can find it.
        reloaded = state_mod.load_pack_lock(lock_path)
        self.assertIn("demo-pack", reloaded["packs"])

    def test_drift_skips_delete(self) -> None:
        target_rel = ".claude/commands/demo.md"
        target_abs = self.project_root / target_rel
        target_abs.parent.mkdir(parents=True)
        # Write a different content than what pack-lock says we installed.
        target_abs.write_bytes(b"user-modified\n")

        pack_lock = {
            "version": state_mod.SCHEMA_VERSION,
            "packs": {
                "demo-pack": {
                    "source_url": "bundled:aa",
                    "requested_ref": "bundled",
                    "resolved_commit": "bundled",
                    "pack_update_policy": "locked",
                    "files": [
                        {
                            "role": "active-skill",
                            "host": "claude-code",
                            "source_path": ".claude/commands/demo.md",
                            "input_sha256": _sha(b"original\n"),
                            "output_paths": [target_rel],
                            "output_scope": "project-local",
                            "effective_update_policy": "locked",
                        }
                    ],
                }
            },
        }
        state_mod.save_pack_lock(
            self.project_root / ".agent-config" / "pack-lock.json", pack_lock,
        )

        result = uninstall_mod.run_uninstall_all(
            self.project_root, user_home=self.user_home, repo_id=self.repo_id,
        )
        self.assertEqual(result.status, uninstall_mod.STATUS_DRIFT)
        self.assertIn(target_rel, result.drift_paths)
        # File NOT deleted.
        self.assertTrue(target_abs.exists())


class MalformedStateTests(_Fixture):
    def test_malformed_pack_lock_returns_malformed(self) -> None:
        (self.project_root / ".agent-config" / "pack-lock.json").write_text(
            "{not valid json",
            encoding="utf-8",
        )
        result = uninstall_mod.run_uninstall_all(
            self.project_root, user_home=self.user_home, repo_id=self.repo_id,
        )
        self.assertEqual(result.status, uninstall_mod.STATUS_MALFORMED)


class UserLevelOwnersTests(_Fixture):
    def test_decrement_preserves_peer_owner(self) -> None:
        """Repo A uninstall: its owner record drops, but peer (repo B)
        keeps the user-level entry and the on-disk file."""
        hook_path = self.user_home / ".claude" / "hooks" / "demo" / "01-hook.py"
        hook_path.parent.mkdir(parents=True)
        content = b"# hook\n"
        hook_path.write_bytes(content)
        content_sha = _sha(content)

        # User-level state has TWO owners for this hook.
        user_state = {
            "version": state_mod.SCHEMA_VERSION,
            "entries": [
                {
                    "kind": "active-hook",
                    "target_path": str(hook_path),
                    "expected_sha256_or_json": content_sha,
                    "owners": [
                        {
                            "repo_id": self.repo_id,
                            "pack": "demo",
                            "requested_ref": "v1",
                            "resolved_commit": "abc",
                            "expected_sha256_or_json": content_sha,
                        },
                        {
                            "repo_id": "other-repo",
                            "pack": "demo",
                            "requested_ref": "v1",
                            "resolved_commit": "abc",
                            "expected_sha256_or_json": content_sha,
                        },
                    ],
                }
            ],
        }
        state_mod.save_user_state(
            self.user_home / ".claude" / "pack-state.json", user_state,
        )

        pack_lock = {
            "version": state_mod.SCHEMA_VERSION,
            "packs": {
                "demo": {
                    "source_url": "bundled:aa",
                    "requested_ref": "v1",
                    "resolved_commit": "abc",
                    "pack_update_policy": "locked",
                    "files": [
                        {
                            "role": "active-hook",
                            "host": "claude-code",
                            "source_path": "scripts/hook.py",
                            "input_sha256": content_sha,
                            "output_paths": [str(hook_path)],
                            "output_scope": "user-level",
                            "effective_update_policy": "locked",
                        }
                    ],
                }
            },
        }
        state_mod.save_pack_lock(
            self.project_root / ".agent-config" / "pack-lock.json", pack_lock,
        )

        result = uninstall_mod.run_uninstall_all(
            self.project_root, user_home=self.user_home, repo_id=self.repo_id,
        )
        self.assertEqual(result.status, uninstall_mod.STATUS_CLEAN)
        # Hook file still present (peer still owns it).
        self.assertTrue(hook_path.exists())
        # User-state entry retained; only repo A's owner record removed.
        reloaded = state_mod.load_user_state(
            self.user_home / ".claude" / "pack-state.json"
        )
        self.assertEqual(len(reloaded["entries"]), 1)
        owner_ids = {o["repo_id"] for o in reloaded["entries"][0]["owners"]}
        self.assertEqual(owner_ids, {"other-repo"})


class PermissionPartialCleanupTests(_Fixture):
    """Regression for Round 1 Codex Medium #4: active-permission entries
    use composite target_path keys in user-state but pack-lock records
    only the settings.json path. Engine must (a) match permission
    entries by prefix + (b) surface PARTIAL because JSON unmerge from
    settings.json isn't implemented."""

    def test_permission_owners_decrement_returns_partial(self) -> None:
        settings_path = self.user_home / ".claude" / "settings.json"
        # Pre-existing permission user-state entry with this repo listed.
        user_state = {
            "version": state_mod.SCHEMA_VERSION,
            "entries": [
                {
                    "kind": "active-permission",
                    "target_path": (
                        f"{settings_path}#permissions.ask#"
                        '{"pattern":"Bash(git push)"}'
                    ),
                    "expected_sha256_or_json": {"pattern": "Bash(git push)"},
                    "owners": [
                        {
                            "repo_id": self.repo_id,
                            "pack": "demo",
                            "requested_ref": "v1",
                            "resolved_commit": "abc",
                            "expected_sha256_or_json": {
                                "pattern": "Bash(git push)"
                            },
                        }
                    ],
                }
            ],
        }
        state_mod.save_user_state(
            self.user_home / ".claude" / "pack-state.json", user_state,
        )

        pack_lock = {
            "version": state_mod.SCHEMA_VERSION,
            "packs": {
                "demo": {
                    "source_url": "bundled:aa",
                    "requested_ref": "v1",
                    "resolved_commit": "abc",
                    "pack_update_policy": "locked",
                    "files": [
                        {
                            "role": "active-permission",
                            "host": "claude-code",
                            "source_path": "permissions.json",
                            "input_sha256": _sha(b"{}"),
                            "output_paths": [str(settings_path)],
                            "output_scope": "user-level",
                            "effective_update_policy": "locked",
                        }
                    ],
                }
            },
        }
        state_mod.save_pack_lock(
            self.project_root / ".agent-config" / "pack-lock.json", pack_lock,
        )

        result = uninstall_mod.run_uninstall_all(
            self.project_root, user_home=self.user_home, repo_id=self.repo_id,
        )
        # Must NOT be "clean" — JSON unmerge of the permission value
        # out of settings.json is not implemented.
        self.assertEqual(result.status, uninstall_mod.STATUS_PARTIAL)
        # Owner record was decremented (and since it was the last owner,
        # the whole entry was pruned).
        reloaded = state_mod.load_user_state(
            self.user_home / ".claude" / "pack-state.json"
        )
        self.assertEqual(reloaded["entries"], [])


if __name__ == "__main__":
    unittest.main()
