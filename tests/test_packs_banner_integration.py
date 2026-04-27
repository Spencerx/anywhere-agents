"""Round-trip test for ``pending-updates.json`` ↔ ``session_bootstrap.py``.

Phase 8 Task 8.4: ``session_bootstrap.py`` reads
``<project-root>/.agent-config/pending-updates.json`` after the existing
compact bootstrap line and prints a one-line notice telling the user how
to apply the deferred updates. The notice covers BOTH platforms in one
line so a user on either OS sees the right command:

  - ``bash .agent-config/bootstrap.sh`` (Linux / macOS)
  - ``pwsh -File .agent-config/bootstrap.ps1`` (Windows)

The notice must not regress the existing banner-gate logic
(``session-event.json`` vs ``banner-emitted.json``); this test only
asserts the new ``_maybe_print_pending_updates`` helper in isolation.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import session_bootstrap  # noqa: E402


def _capture_pending_notice_output(project_root: Path) -> str:
    """Run only the ``_maybe_print_pending_updates`` step and capture stdout.

    Sidesteps the full ``main()`` flow (which would try to spawn the
    bootstrap subprocess, write session-event.json, etc.) so the test
    isolates the new helper.
    """
    buf = io.StringIO()
    with redirect_stdout(buf):
        session_bootstrap._maybe_print_pending_updates(project_root)
    return buf.getvalue()


class TestSessionBootstrapPendingNotice(unittest.TestCase):
    """``_maybe_print_pending_updates`` reads ``pending-updates.json`` and
    prints a compact one-line notice. It must:

    - Print the count + plural agreement (``1 pack update`` vs
      ``2 pack updates``).
    - Mention every pack name from the JSON.
    - Carry both apply commands (Linux/macOS bash + Windows pwsh) so a
      user on either OS sees the command they need.
    - Stay silent when the file is absent, malformed, or has no packs —
      a missing file is the normal post-clean-run state.
    """

    def test_prints_compact_notice_when_pending_updates_present(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pending = root / ".agent-config" / "pending-updates.json"
            pending.parent.mkdir(parents=True)
            pending.write_text(
                json.dumps({
                    "ts": "2026-04-25T14:32:11Z",
                    "host": "claude-code",
                    "packs": [
                        {
                            "name": "profile",
                            "current": "ab",
                            "available": "ef",
                            "kind": "passive",
                        },
                    ],
                }),
                encoding="utf-8",
            )
            output = _capture_pending_notice_output(root)
        self.assertIn("1 pack update pending", output)
        self.assertIn("profile", output)
        # Cross-platform apply commands.
        self.assertIn("bash .agent-config/bootstrap.sh", output)
        self.assertIn("pwsh -File .agent-config/bootstrap.ps1", output)

    def test_pluralization_for_multiple_packs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pending = root / ".agent-config" / "pending-updates.json"
            pending.parent.mkdir(parents=True)
            pending.write_text(
                json.dumps({
                    "ts": "2026-04-25T14:32:11Z",
                    "host": "claude-code",
                    "packs": [
                        {"name": "profile", "current": "ab", "available": "ef", "kind": "passive"},
                        {"name": "paper-workflow", "current": "12", "available": "34", "kind": "passive"},
                    ],
                }),
                encoding="utf-8",
            )
            output = _capture_pending_notice_output(root)
        self.assertIn("2 pack updates pending", output)
        self.assertIn("profile", output)
        self.assertIn("paper-workflow", output)

    def test_silent_when_file_absent(self) -> None:
        """No file → no output. The default state for a clean run."""
        with tempfile.TemporaryDirectory() as d:
            output = _capture_pending_notice_output(Path(d))
        self.assertEqual(output, "")

    def test_silent_when_packs_list_empty(self) -> None:
        """An empty ``packs`` list is the same as no drift; stay silent
        rather than printing a confusing zero-count line."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pending = root / ".agent-config" / "pending-updates.json"
            pending.parent.mkdir(parents=True)
            pending.write_text(
                json.dumps({
                    "ts": "2026-04-25T14:32:11Z",
                    "host": "claude-code",
                    "packs": [],
                }),
                encoding="utf-8",
            )
            output = _capture_pending_notice_output(root)
        self.assertEqual(output, "")

    def test_silent_when_file_malformed(self) -> None:
        """A malformed JSON file should not raise — the hook stays silent
        rather than crashing the SessionStart pipeline."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pending = root / ".agent-config" / "pending-updates.json"
            pending.parent.mkdir(parents=True)
            pending.write_text("not json", encoding="utf-8")
            output = _capture_pending_notice_output(root)
        self.assertEqual(output, "")


if __name__ == "__main__":
    unittest.main()
