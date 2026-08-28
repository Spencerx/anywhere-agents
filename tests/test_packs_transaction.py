"""Tests for scripts/packs/transaction.py.

Covers staged write / delete / restamp ops; successful commit; rollback
on exception; journal content; mid-commit crash leaves journal on disk.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from packs import transaction  # noqa: E402


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _TmpDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lock_path = self.root / "peer.lock"
        self.lock_path.write_text("0\n", encoding="utf-8")


class WriteOpTests(_TmpDirCase):
    def test_stage_write_then_commit(self) -> None:
        target = self.root / "out" / "file.txt"
        staging = self.root / "stage.staging-aaaa"
        with transaction.Transaction(staging, self.lock_path) as txn:
            txn.stage_write(target, b"hello")
        self.assertEqual(target.read_bytes(), b"hello")
        # Staging dir cleaned on commit.
        self.assertFalse(staging.exists())

    def test_write_overwrite_preserves_atomicity(self) -> None:
        target = self.root / "out.txt"
        target.write_bytes(b"old")
        staging = self.root / "stage.staging-aaaa"
        with transaction.Transaction(staging, self.lock_path) as txn:
            txn.stage_write(target, b"new")
        self.assertEqual(target.read_bytes(), b"new")

    def test_rollback_on_exception_preserves_target(self) -> None:
        target = self.root / "out.txt"
        target.write_bytes(b"old")
        staging = self.root / "stage.staging-aaaa"
        try:
            with transaction.Transaction(staging, self.lock_path) as txn:
                txn.stage_write(target, b"new")
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        # Target unchanged; staging gone.
        self.assertEqual(target.read_bytes(), b"old")
        self.assertFalse(staging.exists())

    def test_rollback_on_exception_without_commit_leaves_no_state(self) -> None:
        target = self.root / "out.txt"
        # Target did not exist before.
        staging = self.root / "stage.staging-aaaa"
        try:
            with transaction.Transaction(staging, self.lock_path) as txn:
                txn.stage_write(target, b"new")
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertFalse(target.exists())
        self.assertFalse(staging.exists())


class DeleteOpTests(_TmpDirCase):
    def test_stage_delete_then_commit(self) -> None:
        target = self.root / "goes-away.txt"
        target.write_bytes(b"x")
        staging = self.root / "stage.staging-aaaa"
        with transaction.Transaction(staging, self.lock_path) as txn:
            txn.stage_delete(target)
        self.assertFalse(target.exists())

    def test_delete_of_missing_file_is_idempotent(self) -> None:
        target = self.root / "already-gone.txt"
        staging = self.root / "stage.staging-aaaa"
        with transaction.Transaction(staging, self.lock_path) as txn:
            txn.stage_delete(target)
        self.assertFalse(target.exists())


class RestampOpTests(_TmpDirCase):
    def test_stage_restamp_moves_content_and_removes_old(self) -> None:
        old_path = self.root / "01-foo.py"
        new_path = self.root / "02-foo.py"
        content = b"# hook body"
        old_path.write_bytes(content)
        staging = self.root / "stage.staging-aaaa"
        with transaction.Transaction(staging, self.lock_path) as txn:
            txn.stage_restamp(old_path, new_path, content)
        self.assertFalse(old_path.exists())
        self.assertTrue(new_path.exists())
        self.assertEqual(new_path.read_bytes(), content)


class MultiOpTests(_TmpDirCase):
    def test_write_plus_delete_both_apply(self) -> None:
        a = self.root / "a.txt"
        b = self.root / "b.txt"
        a.write_bytes(b"old-a")
        b.write_bytes(b"old-b")
        staging = self.root / "stage.staging-aaaa"
        with transaction.Transaction(staging, self.lock_path) as txn:
            txn.stage_write(a, b"new-a")
            txn.stage_delete(b)
        self.assertEqual(a.read_bytes(), b"new-a")
        self.assertFalse(b.exists())


class JournalTests(_TmpDirCase):
    def test_journal_records_liveness_metadata(self) -> None:
        import os

        target = self.root / "out.txt"
        staging = self.root / "stage.staging-aaaa"
        # Use the context but don't commit; inspect the journal mid-transaction.
        txn = transaction.Transaction(staging, self.lock_path)
        txn.__enter__()
        try:
            txn.stage_write(target, b"hi")
            journal = transaction.load_journal(staging / "transaction.json")
            self.assertEqual(journal["version"], transaction.JOURNAL_VERSION)
            self.assertEqual(journal["pid"], os.getpid())
            self.assertEqual(journal["lock_path"], str(self.lock_path))
            self.assertIn("created_at", journal)
            self.assertEqual(len(journal["ops"]), 1)
            op = journal["ops"][0]
            self.assertEqual(op["op"], "write")
            self.assertEqual(op["new_content_sha256"], _sha(b"hi"))
        finally:
            # Roll back to clean up (don't commit; that would write to target).
            txn.__exit__(RuntimeError, RuntimeError("test"), None)

    def test_journal_records_pre_state_hash(self) -> None:
        target = self.root / "out.txt"
        target.write_bytes(b"before")
        staging = self.root / "stage.staging-aaaa"
        txn = transaction.Transaction(staging, self.lock_path)
        txn.__enter__()
        try:
            txn.stage_write(target, b"after")
            journal = transaction.load_journal(staging / "transaction.json")
            op = journal["ops"][0]
            self.assertEqual(op["pre_state_sha256"], _sha(b"before"))
        finally:
            txn.__exit__(RuntimeError, RuntimeError("test"), None)

    def test_restamp_journal_carries_both_paths(self) -> None:
        old_path = self.root / "01-foo.py"
        new_path = self.root / "02-foo.py"
        old_path.write_bytes(b"content")
        staging = self.root / "stage.staging-aaaa"
        txn = transaction.Transaction(staging, self.lock_path)
        txn.__enter__()
        try:
            txn.stage_restamp(old_path, new_path, b"content")
            journal = transaction.load_journal(staging / "transaction.json")
            op = journal["ops"][0]
            self.assertEqual(op["op"], "restamp")
            self.assertEqual(op["old_path"], str(old_path))
            self.assertEqual(op["new_path"], str(new_path))
            self.assertEqual(op["pre_state_old_sha256"], _sha(b"content"))
            self.assertIsNone(op["pre_state_new_sha256"])
        finally:
            txn.__exit__(RuntimeError, RuntimeError("test"), None)


class MalformedJournalTests(_TmpDirCase):
    def test_load_missing_journal_raises(self) -> None:
        with self.assertRaises(transaction.TransactionError):
            transaction.load_journal(self.root / "nope.json")

    def test_load_malformed_json_raises(self) -> None:
        path = self.root / "bad.json"
        path.write_text("not json", encoding="utf-8")
        with self.assertRaises(transaction.TransactionError):
            transaction.load_journal(path)


class AtomicReplaceRetryTests(_TmpDirCase):
    """The _atomic_replace helper must cover both staged target writes
    (covered by _apply_op commit tests) AND the journal rewrite path so
    AV-induced sharing violations on transaction.json don't abort an
    otherwise valid transaction."""

    def test_journal_write_uses_atomic_replace_retry(self) -> None:
        """Monkeypatch os.replace inside the transaction module to fail
        twice with PermissionError then succeed; journal rewrite must
        retry through _atomic_replace (Windows-only behavior; on POSIX
        the retry loop is a no-op but the test still passes because
        there's no PermissionError to begin with)."""
        target = self.root / "out.txt"
        staging = self.root / "stage.staging-retry"

        call_count = {"n": 0}
        real_replace = transaction.os.replace

        def flaky_replace(src, dst):  # type: ignore[no-untyped-def]
            # Fail the journal rewrite the first two times (only on
            # Windows does _atomic_replace retry; on POSIX this function
            # is still called but the retry logic is skipped, so flaky
            # behavior doesn't exercise retry there).
            if "transaction.json" in str(dst) and call_count["n"] < 2 and transaction._IS_WINDOWS:
                call_count["n"] += 1
                raise PermissionError("simulated AV sharing violation")
            return real_replace(src, dst)

        transaction.os.replace = flaky_replace
        try:
            with transaction.Transaction(staging, self.lock_path) as txn:
                txn.stage_write(target, b"content")
            # Transaction committed despite flaky replaces (on Windows).
            self.assertEqual(target.read_bytes(), b"content")
        finally:
            transaction.os.replace = real_replace


class IdenticalWriteSkipTests(_TmpDirCase):
    """A staged write whose bytes already match the target is skipped at
    apply time, so a target that another process holds open cannot abort a
    transaction that was not going to change it (anywhere-agents#44).

    These tests patch ``transaction._atomic_replace`` rather than
    ``os.replace``. It is the narrower seam: ``_apply_op`` looks the helper
    up as a module global, and patching ``os`` would swap a builtin for
    every other thread in the process. The retry loop inside the helper has
    its own coverage in :class:`AtomicReplaceRetryTests`.
    """

    def _record_replacements(self) -> list[str]:
        """Patch the replace helper to record each destination it is asked
        to write, and return the list it appends to."""
        seen: list[str] = []
        real = transaction._atomic_replace

        def recording(src, dst):  # type: ignore[no-untyped-def]
            seen.append(str(dst))
            return real(src, dst)

        transaction._atomic_replace = recording
        self.addCleanup(setattr, transaction, "_atomic_replace", real)
        return seen

    def _deny_replacements_to(self, denied: Path) -> None:
        """Patch the replace helper so any rename onto ``denied`` fails the
        way Windows fails one whose destination is held open."""
        real = transaction._atomic_replace

        def denying(src, dst):  # type: ignore[no-untyped-def]
            if str(dst) == str(denied):
                raise PermissionError(5, "Access is denied")
            return real(src, dst)

        transaction._atomic_replace = denying
        self.addCleanup(setattr, transaction, "_atomic_replace", real)

    def test_identical_write_is_not_renamed(self) -> None:
        target = self.root / "out.txt"
        target.write_bytes(b"same")
        staging = self.root / "stage.staging-skip"
        seen = self._record_replacements()
        with transaction.Transaction(staging, self.lock_path) as txn:
            txn.stage_write(target, b"same")
        self.assertEqual(target.read_bytes(), b"same")
        self.assertNotIn(str(target), seen)

    def test_changed_write_is_still_renamed(self) -> None:
        target = self.root / "out.txt"
        target.write_bytes(b"old")
        staging = self.root / "stage.staging-write"
        seen = self._record_replacements()
        with transaction.Transaction(staging, self.lock_path) as txn:
            txn.stage_write(target, b"new")
        self.assertEqual(target.read_bytes(), b"new")
        self.assertIn(str(target), seen)

    def test_write_to_a_missing_target_is_still_renamed(self) -> None:
        """No target means nothing to compare against, so the op applies."""
        target = self.root / "nested" / "fresh.txt"
        staging = self.root / "stage.staging-fresh"
        seen = self._record_replacements()
        with transaction.Transaction(staging, self.lock_path) as txn:
            txn.stage_write(target, b"body")
        self.assertEqual(target.read_bytes(), b"body")
        self.assertIn(str(target), seen)

    def test_identical_write_survives_a_held_target(self) -> None:
        """The regression this change exists for. A worker executing a
        deployed script holds it open for minutes, and the rename over it
        used to abort every op queued behind it."""
        held = self.root / "dispatch-task.sh"
        held.write_bytes(b"script body")
        later = self.root / "queued-behind.txt"
        staging = self.root / "stage.staging-held"
        self._deny_replacements_to(held)
        with transaction.Transaction(staging, self.lock_path) as txn:
            txn.stage_write(held, b"script body")
            txn.stage_write(later, b"reached")
        self.assertEqual(held.read_bytes(), b"script body")
        self.assertEqual(later.read_bytes(), b"reached")

    def test_changed_write_to_a_held_target_still_aborts(self) -> None:
        """The skip is not a general "ignore a locked file" rule. Content
        that genuinely changed and cannot land has to fail loudly: the lock
        file records the hash the compose intended, so a quiet skip would
        leave it describing bytes that are not on disk."""
        held = self.root / "dispatch-task.sh"
        held.write_bytes(b"old script")
        staging = self.root / "stage.staging-held-changed"
        self._deny_replacements_to(held)
        with self.assertRaises(transaction.TransactionError):
            with transaction.Transaction(staging, self.lock_path) as txn:
                txn.stage_write(held, b"new script")
        self.assertEqual(held.read_bytes(), b"old script")

    def test_restamp_with_matching_new_path_still_removes_old(self) -> None:
        """Only the rename half of a restamp is skippable. Skipping the
        unlink too would leave the hook deployed under both prefixes."""
        old_path = self.root / "01-hook.py"
        new_path = self.root / "02-hook.py"
        old_path.write_bytes(b"body")
        new_path.write_bytes(b"body")
        staging = self.root / "stage.staging-restamp"
        seen = self._record_replacements()
        with transaction.Transaction(staging, self.lock_path) as txn:
            txn.stage_restamp(old_path, new_path, b"body")
        self.assertFalse(old_path.exists())
        self.assertEqual(new_path.read_bytes(), b"body")
        self.assertNotIn(str(new_path), seen)


if __name__ == "__main__":
    unittest.main()
