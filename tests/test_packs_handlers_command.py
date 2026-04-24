"""Tests for scripts/packs/handlers/command.py.

kind: command is the v0.4.0 forward-compat slot — parse + warn + no-op.
Tests verify: warning emitted verbatim, no file written, no state entries.
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from packs import dispatch  # noqa: E402
from packs import handlers  # noqa: E402
from packs import state as state_mod  # noqa: E402
from packs import transaction as txn_mod  # noqa: E402
from packs.handlers import command  # noqa: E402


class CommandNoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project_root = self.root / "project"
        self.project_root.mkdir()
        self.pack_source_dir = self.root / "pack_src"
        self.pack_source_dir.mkdir()
        self.staging = self.root / "stage.staging-cmd"
        self.lock_path = self.root / "peer.lock"
        self.lock_path.write_text("0\n", encoding="utf-8")

    def test_emits_warning_and_writes_nothing(self) -> None:
        err = io.StringIO()
        with txn_mod.Transaction(self.staging, self.lock_path) as txn:
            ctx = dispatch.DispatchContext(
                pack_name="some-pack",
                pack_source_url="bundled:aa",
                pack_requested_ref="bundled",
                pack_resolved_commit="bundled",
                pack_update_policy="locked",
                pack_source_dir=self.pack_source_dir,
                project_root=self.project_root,
                user_home=self.root / "home",
                repo_id="r",
                txn=txn,
                pack_lock=state_mod.empty_pack_lock(),
                project_state=state_mod.empty_project_state(),
                user_state=state_mod.empty_user_state(),
            )
            with redirect_stderr(err):
                command.handle_command(
                    {
                        "kind": "command",
                        "hosts": ["claude-code"],
                        "files": [{"from": "cmd.md", "to": ".claude/commands/cmd.md"}],
                    },
                    ctx,
                )
            ctx.finalize_pack_lock()

        # No file written.
        self.assertFalse(
            (self.project_root / ".claude" / "commands" / "cmd.md").exists()
        )

        # No lock-file entry for this pack.
        packs = ctx.pack_lock.get("packs", {})
        self.assertNotIn("some-pack", packs)

        # No project or user state entry.
        self.assertEqual(ctx.project_state["entries"], [])
        self.assertEqual(ctx.user_state["entries"], [])

        # Warning text is the exact message from the handler.
        self.assertIn(command.WARNING_MESSAGE, err.getvalue())

    def test_warning_message_stable(self) -> None:
        """Pack-architecture.md:486 promises this exact warning; regression
        test guards against accidental wording changes."""
        self.assertEqual(
            command.WARNING_MESSAGE,
            "no-op at v0.4.0; full support in a later release",
        )


if __name__ == "__main__":
    unittest.main()
