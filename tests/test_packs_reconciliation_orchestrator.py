"""Tests for ``reconciliation.reconcile_orphans`` orchestrator wrapper.

Phase 7 / Deferral 2 (Codex R1 M7 + R2 H2 + R3 M3): orchestrator wrapping
the v0.4.0 ``scan_orphans`` + ``cleanup_staging`` API. Six labels
(``LIVE``, ``ROLLBACK_OK``, ``ROLLFORWARD_OK``, ``PARTIAL``, ``DRIFT``,
``MALFORMED``) under both ``locks_held=False`` (wrapper takes its own
outer locks) and ``locks_held=True`` (compose already holds them).

These tests run in-process and mock ``scan_orphans`` to return synthetic
``OrphanClassification`` objects so the dispatch table can be asserted in
isolation from the (separately-tested) classifier. Real lock acquire is
exercised via ``packs.locks.acquire`` — same module the orchestrator
calls — so the ``locks_held=False`` branch genuinely takes a lock.

Phase 8 carry-forward C extends ``classify_orphan`` with the
``locks_held`` / ``owner_pid`` keyword arguments. Self-vs-foreign
ownership is asserted at the bottom under ``TestClassifyOrphanOwnership``.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from packs import locks  # noqa: E402
from packs import reconciliation  # noqa: E402
from packs import transaction as txn_mod  # noqa: E402


def _orphan(
    staging_dir: Path, label: str, *, journal: dict | None = None,
    ops: list | None = None, detail: str = "",
) -> reconciliation.OrphanClassification:
    """Build a synthetic OrphanClassification with the requested label."""
    return reconciliation.OrphanClassification(
        staging_dir=staging_dir,
        label=label,
        journal=journal,
        ops=ops or [],
        detail=detail,
    )


class _OrchestratorFixture(unittest.TestCase):
    """Common temp-dir scaffolding shared by every orchestrator test."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_root = Path(self.tmp.name)
        self.project_root = self.tmp_root / "project"
        self.user_root = self.tmp_root / "user"
        self.project_root.mkdir()
        self.user_root.mkdir()
        # Create the directories scan_orphans() would walk so the
        # _collect_staging_dirs helper (or whatever the implementation
        # names it) does not have to handle a missing search root.
        (self.project_root / ".agent-config").mkdir()
        (self.user_root / ".claude" / "hooks").mkdir(parents=True)
        # Capture drift_callback invocations.
        self.drift_calls: list[reconciliation.OrphanClassification] = []

    def _drift_callback(
        self, orphan: reconciliation.OrphanClassification
    ) -> None:
        self.drift_calls.append(orphan)


# =====================================================================
# locks_held=False — wrapper must take its own outer locks
# =====================================================================


class TestReconcileOrphansLockHeldFalse(_OrchestratorFixture):
    """All six classifications under ``locks_held=False``.

    The wrapper must:
      - acquire user_lock + repo_lock (via packs.locks.acquire).
      - dispatch each orphan based on its ``label``.
      - release locks before returning, even when an inner action raises.
    """

    def test_acquires_outer_locks_and_releases(self) -> None:
        """Wrapper must call locks.acquire on both user_lock and repo_lock."""
        acquired_paths: list[Path] = []

        @contextmanager
        def fake_acquire(path, timeout=30):
            acquired_paths.append(path)
            yield None

        with (
            patch.object(reconciliation.locks, "acquire", fake_acquire),
            patch.object(reconciliation, "scan_orphans", return_value=[]),
        ):
            report = reconciliation.reconcile_orphans(
                self.project_root, self.user_root, locks_held=False,
            )
        self.assertEqual(len(acquired_paths), 2)
        self.assertIn(locks.user_lock_path(self.user_root), acquired_paths)
        self.assertIn(locks.repo_lock_path(self.project_root), acquired_paths)
        self.assertIsNotNone(report)

    def test_LIVE_classification_skips(self) -> None:
        """LIVE: do nothing — no cleanup, no drift_callback. Record skip."""
        orphan = _orphan(self.tmp_root / "live.staging-x", reconciliation.LIVE)
        with (
            patch.object(reconciliation.locks, "acquire", _noop_acquire),
            patch.object(reconciliation, "scan_orphans", return_value=[orphan]),
            patch.object(reconciliation, "cleanup_staging") as cleanup,
        ):
            report = reconciliation.reconcile_orphans(
                self.project_root, self.user_root,
                locks_held=False, drift_callback=self._drift_callback,
            )
        cleanup.assert_not_called()
        self.assertEqual(self.drift_calls, [])
        self.assertIn(orphan.staging_dir, report.live)

    def test_ROLLBACK_OK_calls_cleanup(self) -> None:
        """ROLLBACK_OK: cleanup_staging called, no drift_callback."""
        orphan = _orphan(
            self.tmp_root / "rb.staging-x", reconciliation.ROLLBACK_OK,
        )
        with (
            patch.object(reconciliation.locks, "acquire", _noop_acquire),
            patch.object(reconciliation, "scan_orphans", return_value=[orphan]),
            patch.object(reconciliation, "cleanup_staging") as cleanup,
        ):
            report = reconciliation.reconcile_orphans(
                self.project_root, self.user_root,
                locks_held=False, drift_callback=self._drift_callback,
            )
        cleanup.assert_called_once_with(orphan.staging_dir)
        self.assertEqual(self.drift_calls, [])
        self.assertIn(orphan.staging_dir, report.rolled_back)

    def test_ROLLFORWARD_OK_calls_cleanup(self) -> None:
        """ROLLFORWARD_OK: cleanup_staging called, no drift_callback."""
        orphan = _orphan(
            self.tmp_root / "rf.staging-x", reconciliation.ROLLFORWARD_OK,
        )
        with (
            patch.object(reconciliation.locks, "acquire", _noop_acquire),
            patch.object(reconciliation, "scan_orphans", return_value=[orphan]),
            patch.object(reconciliation, "cleanup_staging") as cleanup,
        ):
            report = reconciliation.reconcile_orphans(
                self.project_root, self.user_root,
                locks_held=False, drift_callback=self._drift_callback,
            )
        cleanup.assert_called_once_with(orphan.staging_dir)
        self.assertEqual(self.drift_calls, [])
        self.assertIn(orphan.staging_dir, report.rolled_forward)

    def test_PARTIAL_reapply_succeeds_then_cleans(self) -> None:
        """PARTIAL with reapplyable ops: reapply, cleanup, no drift_callback.

        Builds a real partial-commit scenario on disk so the reapply path
        does actual work (rather than being mocked out).
        """
        a = self.tmp_root / "a.txt"
        b = self.tmp_root / "b.txt"
        a.write_bytes(b"a-pre")
        b.write_bytes(b"b-pre")
        lock_path = self.tmp_root / "peer.lock"
        lock_path.write_text("0\n", encoding="utf-8")
        staging = self.tmp_root / "stage.staging-partial"
        txn = txn_mod.Transaction(staging, lock_path)
        txn.__enter__()
        txn.stage_write(a, b"a-new")
        txn.stage_write(b, b"b-new")
        # Simulate partial commit: a applied, b not applied.
        a.write_bytes(b"a-new")

        # Classify so we get a real OrphanClassification with op-level
        # state == {pre_state, new_state} and PARTIAL label.
        orphan = reconciliation.classify_orphan(staging)
        self.assertEqual(orphan.label, reconciliation.PARTIAL)

        with (
            patch.object(reconciliation.locks, "acquire", _noop_acquire),
            patch.object(reconciliation, "scan_orphans", return_value=[orphan]),
        ):
            report = reconciliation.reconcile_orphans(
                self.project_root, self.user_root,
                locks_held=False, drift_callback=self._drift_callback,
            )

        # b should now be at new content (op was reapplied), a unchanged.
        self.assertEqual(a.read_bytes(), b"a-new")
        self.assertEqual(b.read_bytes(), b"b-new")
        # Staging dir cleaned up.
        self.assertFalse(staging.exists())
        # No drift surfaced.
        self.assertEqual(self.drift_calls, [])
        self.assertIn(staging, report.partial_reapplied)

    def test_PARTIAL_unreapplyable_calls_drift_callback(self) -> None:
        """PARTIAL where the staged file is missing → fall back to drift."""
        a = self.tmp_root / "a.txt"
        b = self.tmp_root / "b.txt"
        a.write_bytes(b"a-pre")
        b.write_bytes(b"b-pre")
        lock_path = self.tmp_root / "peer.lock"
        lock_path.write_text("0\n", encoding="utf-8")
        staging = self.tmp_root / "stage.staging-partial-broken"
        txn = txn_mod.Transaction(staging, lock_path)
        txn.__enter__()
        txn.stage_write(a, b"a-new")
        txn.stage_write(b, b"b-new")
        # Simulate partial commit: a applied, b not applied, AND the staged
        # file for b is gone (e.g., manual cleanup, AV-quarantine, etc.).
        a.write_bytes(b"a-new")
        # Locate and remove all staged files in the staging dir.
        for entry in staging.iterdir():
            if entry.suffix in {".new", ".restamp"}:
                entry.unlink()

        orphan = reconciliation.classify_orphan(staging)
        self.assertEqual(orphan.label, reconciliation.PARTIAL)

        with (
            patch.object(reconciliation.locks, "acquire", _noop_acquire),
            patch.object(reconciliation, "scan_orphans", return_value=[orphan]),
            patch.object(reconciliation, "cleanup_staging") as cleanup,
        ):
            report = reconciliation.reconcile_orphans(
                self.project_root, self.user_root,
                locks_held=False, drift_callback=self._drift_callback,
            )

        # Cannot reapply: drift_callback fired, staging left alone.
        self.assertEqual(len(self.drift_calls), 1)
        self.assertIs(self.drift_calls[0], orphan)
        cleanup.assert_not_called()
        self.assertIn(staging, report.blocking)

    def test_DRIFT_calls_drift_callback(self) -> None:
        """DRIFT: drift_callback invoked, no cleanup, recorded as blocking."""
        orphan = _orphan(
            self.tmp_root / "dr.staging-x", reconciliation.DRIFT,
            detail="unexpected content",
        )
        with (
            patch.object(reconciliation.locks, "acquire", _noop_acquire),
            patch.object(reconciliation, "scan_orphans", return_value=[orphan]),
            patch.object(reconciliation, "cleanup_staging") as cleanup,
        ):
            report = reconciliation.reconcile_orphans(
                self.project_root, self.user_root,
                locks_held=False, drift_callback=self._drift_callback,
            )
        self.assertEqual(len(self.drift_calls), 1)
        self.assertIs(self.drift_calls[0], orphan)
        cleanup.assert_not_called()
        self.assertIn(orphan.staging_dir, report.blocking)

    def test_MALFORMED_calls_drift_callback(self) -> None:
        """MALFORMED: drift_callback invoked, no cleanup, recorded as blocking."""
        orphan = _orphan(
            self.tmp_root / "mf.staging-x", reconciliation.MALFORMED,
            detail="journal not json",
        )
        with (
            patch.object(reconciliation.locks, "acquire", _noop_acquire),
            patch.object(reconciliation, "scan_orphans", return_value=[orphan]),
            patch.object(reconciliation, "cleanup_staging") as cleanup,
        ):
            report = reconciliation.reconcile_orphans(
                self.project_root, self.user_root,
                locks_held=False, drift_callback=self._drift_callback,
            )
        self.assertEqual(len(self.drift_calls), 1)
        self.assertIs(self.drift_calls[0], orphan)
        cleanup.assert_not_called()
        self.assertIn(orphan.staging_dir, report.blocking)

    def test_drift_without_callback_does_not_crash(self) -> None:
        """When drift_callback is None, DRIFT/MALFORMED still record blocking."""
        orphans = [
            _orphan(self.tmp_root / "d.staging", reconciliation.DRIFT),
            _orphan(self.tmp_root / "m.staging", reconciliation.MALFORMED),
        ]
        with (
            patch.object(reconciliation.locks, "acquire", _noop_acquire),
            patch.object(reconciliation, "scan_orphans", return_value=orphans),
        ):
            report = reconciliation.reconcile_orphans(
                self.project_root, self.user_root,
                locks_held=False, drift_callback=None,
            )
        self.assertEqual(len(report.blocking), 2)


# =====================================================================
# locks_held=True — caller (compose_packs) holds locks; do NOT re-acquire
# =====================================================================


class TestReconcileOrphansLockHeldTrue(_OrchestratorFixture):
    """Same six labels under ``locks_held=True``.

    The wrapper must NOT call ``locks.acquire`` again (caller already
    owns user_lock + repo_lock). The dispatch table behaviour is
    identical to ``locks_held=False``.
    """

    def test_does_not_acquire_locks_when_locks_held(self) -> None:
        """No locks.acquire calls when locks_held=True."""
        with (
            patch.object(reconciliation.locks, "acquire") as acquire,
            patch.object(reconciliation, "scan_orphans", return_value=[]),
        ):
            reconciliation.reconcile_orphans(
                self.project_root, self.user_root, locks_held=True,
            )
        acquire.assert_not_called()

    def test_LIVE_classification_skips(self) -> None:
        orphan = _orphan(self.tmp_root / "live.staging-x", reconciliation.LIVE)
        with (
            patch.object(reconciliation.locks, "acquire") as acquire,
            patch.object(reconciliation, "scan_orphans", return_value=[orphan]),
            patch.object(reconciliation, "cleanup_staging") as cleanup,
        ):
            report = reconciliation.reconcile_orphans(
                self.project_root, self.user_root,
                locks_held=True, drift_callback=self._drift_callback,
            )
        acquire.assert_not_called()
        cleanup.assert_not_called()
        self.assertEqual(self.drift_calls, [])
        self.assertIn(orphan.staging_dir, report.live)

    def test_ROLLBACK_OK_calls_cleanup(self) -> None:
        orphan = _orphan(
            self.tmp_root / "rb.staging-x", reconciliation.ROLLBACK_OK,
        )
        with (
            patch.object(reconciliation.locks, "acquire") as acquire,
            patch.object(reconciliation, "scan_orphans", return_value=[orphan]),
            patch.object(reconciliation, "cleanup_staging") as cleanup,
        ):
            report = reconciliation.reconcile_orphans(
                self.project_root, self.user_root,
                locks_held=True, drift_callback=self._drift_callback,
            )
        acquire.assert_not_called()
        cleanup.assert_called_once_with(orphan.staging_dir)
        self.assertIn(orphan.staging_dir, report.rolled_back)

    def test_ROLLFORWARD_OK_calls_cleanup(self) -> None:
        orphan = _orphan(
            self.tmp_root / "rf.staging-x", reconciliation.ROLLFORWARD_OK,
        )
        with (
            patch.object(reconciliation.locks, "acquire") as acquire,
            patch.object(reconciliation, "scan_orphans", return_value=[orphan]),
            patch.object(reconciliation, "cleanup_staging") as cleanup,
        ):
            report = reconciliation.reconcile_orphans(
                self.project_root, self.user_root,
                locks_held=True, drift_callback=self._drift_callback,
            )
        acquire.assert_not_called()
        cleanup.assert_called_once_with(orphan.staging_dir)
        self.assertIn(orphan.staging_dir, report.rolled_forward)

    def test_PARTIAL_reapply_succeeds_then_cleans(self) -> None:
        a = self.tmp_root / "a.txt"
        b = self.tmp_root / "b.txt"
        a.write_bytes(b"a-pre")
        b.write_bytes(b"b-pre")
        lock_path = self.tmp_root / "peer.lock"
        lock_path.write_text("0\n", encoding="utf-8")
        staging = self.tmp_root / "stage.staging-partial-2"
        txn = txn_mod.Transaction(staging, lock_path)
        txn.__enter__()
        txn.stage_write(a, b"a-new")
        txn.stage_write(b, b"b-new")
        a.write_bytes(b"a-new")

        orphan = reconciliation.classify_orphan(staging)
        self.assertEqual(orphan.label, reconciliation.PARTIAL)

        with (
            patch.object(reconciliation.locks, "acquire") as acquire,
            patch.object(reconciliation, "scan_orphans", return_value=[orphan]),
        ):
            report = reconciliation.reconcile_orphans(
                self.project_root, self.user_root,
                locks_held=True, drift_callback=self._drift_callback,
            )
        acquire.assert_not_called()
        self.assertEqual(b.read_bytes(), b"b-new")
        self.assertFalse(staging.exists())
        self.assertEqual(self.drift_calls, [])
        self.assertIn(staging, report.partial_reapplied)

    def test_DRIFT_calls_drift_callback(self) -> None:
        orphan = _orphan(self.tmp_root / "dr.staging", reconciliation.DRIFT)
        with (
            patch.object(reconciliation.locks, "acquire") as acquire,
            patch.object(reconciliation, "scan_orphans", return_value=[orphan]),
            patch.object(reconciliation, "cleanup_staging") as cleanup,
        ):
            report = reconciliation.reconcile_orphans(
                self.project_root, self.user_root,
                locks_held=True, drift_callback=self._drift_callback,
            )
        acquire.assert_not_called()
        cleanup.assert_not_called()
        self.assertEqual(len(self.drift_calls), 1)
        self.assertIs(self.drift_calls[0], orphan)
        self.assertIn(orphan.staging_dir, report.blocking)

    def test_MALFORMED_calls_drift_callback(self) -> None:
        orphan = _orphan(self.tmp_root / "mf.staging", reconciliation.MALFORMED)
        with (
            patch.object(reconciliation.locks, "acquire") as acquire,
            patch.object(reconciliation, "scan_orphans", return_value=[orphan]),
            patch.object(reconciliation, "cleanup_staging") as cleanup,
        ):
            report = reconciliation.reconcile_orphans(
                self.project_root, self.user_root,
                locks_held=True, drift_callback=self._drift_callback,
            )
        acquire.assert_not_called()
        cleanup.assert_not_called()
        self.assertEqual(len(self.drift_calls), 1)
        self.assertIs(self.drift_calls[0], orphan)
        self.assertIn(orphan.staging_dir, report.blocking)


# =====================================================================
# Mixed-classification, real-end-to-end, and search-dir wiring
# =====================================================================


class TestReconcileOrphansMixed(_OrchestratorFixture):
    """Verify the orchestrator handles a list of mixed labels in one pass."""

    def test_mixed_orphans_dispatched_independently(self) -> None:
        """A run with one of each label routes each orphan correctly."""
        orphans = [
            _orphan(self.tmp_root / "live.staging", reconciliation.LIVE),
            _orphan(self.tmp_root / "rb.staging", reconciliation.ROLLBACK_OK),
            _orphan(self.tmp_root / "rf.staging", reconciliation.ROLLFORWARD_OK),
            _orphan(self.tmp_root / "dr.staging", reconciliation.DRIFT),
            _orphan(self.tmp_root / "mf.staging", reconciliation.MALFORMED),
        ]
        cleanup_calls: list[Path] = []

        def fake_cleanup(p: Path) -> None:
            cleanup_calls.append(p)

        with (
            patch.object(reconciliation.locks, "acquire") as acquire,
            patch.object(reconciliation, "scan_orphans", return_value=orphans),
            patch.object(reconciliation, "cleanup_staging", side_effect=fake_cleanup),
        ):
            report = reconciliation.reconcile_orphans(
                self.project_root, self.user_root,
                locks_held=True, drift_callback=self._drift_callback,
            )
        # Cleanup called exactly for ROLLBACK_OK and ROLLFORWARD_OK.
        self.assertEqual(
            sorted(cleanup_calls),
            sorted([
                self.tmp_root / "rb.staging",
                self.tmp_root / "rf.staging",
            ]),
        )
        # drift_callback fired exactly for DRIFT and MALFORMED.
        self.assertEqual(len(self.drift_calls), 2)
        labels_called = {o.label for o in self.drift_calls}
        self.assertEqual(labels_called, {reconciliation.DRIFT, reconciliation.MALFORMED})
        # Report buckets accurate.
        self.assertIn(self.tmp_root / "live.staging", report.live)
        self.assertIn(self.tmp_root / "rb.staging", report.rolled_back)
        self.assertIn(self.tmp_root / "rf.staging", report.rolled_forward)
        self.assertIn(self.tmp_root / "dr.staging", report.blocking)
        self.assertIn(self.tmp_root / "mf.staging", report.blocking)

    def test_search_dirs_include_user_hooks_and_project_agent_config(self) -> None:
        """Both ``~/.claude/hooks/`` (user) and ``.agent-config/`` (project)
        must be passed to ``scan_orphans`` per pack-architecture.md."""
        captured_dirs: list[list[Path]] = []

        def fake_scan(dirs, **_kwargs):
            captured_dirs.append(list(dirs))
            return []

        with (
            patch.object(reconciliation.locks, "acquire") as acquire,
            patch.object(reconciliation, "scan_orphans", side_effect=fake_scan),
        ):
            reconciliation.reconcile_orphans(
                self.project_root, self.user_root, locks_held=True,
            )
        self.assertEqual(len(captured_dirs), 1)
        dirs = captured_dirs[0]
        # Must include both staging-parent locations.
        expected_user = self.user_root / ".claude" / "hooks"
        expected_repo = self.project_root / ".agent-config"
        self.assertIn(expected_user, dirs)
        self.assertIn(expected_repo, dirs)


@contextmanager
def _noop_acquire(path, timeout=30):
    """Stand-in for ``locks.acquire`` that does no real locking.

    Used by ``locks_held=False`` tests so the wrapper still exercises the
    "with locks.acquire(...)" path without contending real lock files
    (which would interfere with the running developer's own ~/.claude).
    """
    yield None


# =====================================================================
# Phase 8 carry-forward C: classify_orphan(locks_held=, owner_pid=) ownership
# =====================================================================


class TestClassifyOrphanOwnership(unittest.TestCase):
    """``classify_orphan`` accepts ``locks_held`` and ``owner_pid`` kwargs.

    With ``locks_held=True`` and a journal whose ``pid`` matches
    ``owner_pid`` (or ``os.getpid()`` if ``owner_pid`` is None), the
    returned ``OrphanClassification.ownership`` is ``"self"`` — a
    partial from THIS run of the orchestrator. PID mismatch yields
    ``"foreign"``. ``locks_held=False`` always yields ``"unknown"``;
    the foreign-process safety check still applies in that path.

    This is the v0.5.0 Phase 8 sub-label that lets the reapply path
    distinguish self-partials (safe to reapply immediately, the
    orchestrator owns the lock pair) from foreign-partials (must
    follow the existing safety semantics).
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_root = Path(self.tmp.name)

    def _build_partial_staging(
        self, *, journal_pid: int, parent_dir: Path | None = None,
    ) -> Path:
        """Build a real PARTIAL orphan staging dir with the given journal PID.

        Uses ``transaction.Transaction`` to stage two writes, then
        partial-commits one (mutating the journal's ``pid`` after the
        fact so the test fixture controls ownership).

        ``parent_dir`` defaults to the test root; tests that exercise
        the orchestrator end-to-end pass the project's ``.agent-config``
        so ``_collect_staging_dirs`` finds the orphan.
        """
        parent = parent_dir if parent_dir is not None else self.tmp_root
        parent.mkdir(parents=True, exist_ok=True)
        a = parent / f"a-{journal_pid}.txt"
        b = parent / f"b-{journal_pid}.txt"
        a.write_bytes(b"a-pre")
        b.write_bytes(b"b-pre")
        lock_path = parent / f"peer-{journal_pid}.lock"
        lock_path.write_text("0\n", encoding="utf-8")
        staging = parent / f"stage-{journal_pid}.staging-x"
        txn = txn_mod.Transaction(staging, lock_path)
        txn.__enter__()
        txn.stage_write(a, b"a-new")
        txn.stage_write(b, b"b-new")
        a.write_bytes(b"a-new")  # partial commit: a applied, b not.

        # Patch the journal's recorded pid to the requested value so we
        # can drive the self-vs-foreign comparison from the test.
        journal_path = staging / txn_mod.JOURNAL_NAME
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["pid"] = journal_pid
        journal_path.write_text(
            json.dumps(journal, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return staging

    def test_self_partial_when_pid_matches_and_locks_held(self) -> None:
        """``locks_held=True`` + journal PID matches ``owner_pid`` → ``self``."""
        my_pid = 99887766
        staging = self._build_partial_staging(journal_pid=my_pid)
        result = reconciliation.classify_orphan(
            staging, locks_held=True, owner_pid=my_pid,
        )
        self.assertEqual(result.label, reconciliation.PARTIAL)
        self.assertEqual(result.ownership, "self")

    def test_foreign_partial_when_pid_mismatch_and_locks_held(self) -> None:
        """``locks_held=True`` + journal PID different from ``owner_pid``
        → ``foreign`` — orphan is from another process or earlier run."""
        staging = self._build_partial_staging(journal_pid=11112222)
        result = reconciliation.classify_orphan(
            staging, locks_held=True, owner_pid=33334444,
        )
        self.assertEqual(result.label, reconciliation.PARTIAL)
        self.assertEqual(result.ownership, "foreign")

    def test_default_owner_pid_uses_os_getpid(self) -> None:
        """When ``owner_pid`` is omitted, ``os.getpid()`` is used. A
        journal whose PID equals the test process's own PID classifies
        as ``self``."""
        staging = self._build_partial_staging(journal_pid=os.getpid())
        result = reconciliation.classify_orphan(staging, locks_held=True)
        self.assertEqual(result.ownership, "self")

    def test_locks_held_false_always_unknown(self) -> None:
        """``locks_held=False`` (the default) keeps ownership at
        ``unknown`` regardless of the journal's PID. The foreign-process
        safety check stays in effect for that path — there is no caller
        guarantee that the lock pair is held."""
        staging = self._build_partial_staging(journal_pid=os.getpid())
        result = reconciliation.classify_orphan(staging)
        self.assertEqual(result.ownership, "unknown")

    def test_unknown_ownership_when_journal_lacks_pid(self) -> None:
        """A journal that pre-dates v0.5.0 (or that was hand-edited)
        may not carry ``pid``. With ``locks_held=True`` the comparison
        falls back to ``foreign`` (mismatch with ``os.getpid()``)
        rather than crashing."""
        staging = self._build_partial_staging(journal_pid=12345)
        # Drop the pid field so journal.get("pid") returns None.
        journal_path = staging / txn_mod.JOURNAL_NAME
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal.pop("pid", None)
        journal_path.write_text(
            json.dumps(journal, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result = reconciliation.classify_orphan(staging, locks_held=True)
        # Missing PID is treated as foreign (mismatch path) — never
        # silently classifies as self when the data does not back it.
        self.assertEqual(result.ownership, "foreign")

    def test_scan_orphans_threads_locks_held_and_owner_pid(self) -> None:
        """``scan_orphans`` must forward the new kwargs to
        ``classify_orphan`` so the orchestrator's pass-through wiring
        produces ownership-tagged classifications."""
        # Build under a directory ``scan_orphans`` walks (any dir
        # containing a ``*.staging-*`` subdir works).
        staging = self._build_partial_staging(journal_pid=os.getpid())
        results = reconciliation.scan_orphans(
            [staging.parent], locks_held=True,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].ownership, "self")

    def test_reconcile_orphans_locks_held_true_classifies_self(self) -> None:
        """The orchestrator wrapper, when called with ``locks_held=True``,
        must produce ``OrphanClassification.ownership == "self"`` for a
        staging dir whose journal PID matches the current process."""
        project_root = self.tmp_root / "project"
        user_root = self.tmp_root / "user"
        project_root.mkdir()
        user_root.mkdir()
        agent_config = project_root / ".agent-config"
        agent_config.mkdir()
        (user_root / ".claude" / "hooks").mkdir(parents=True)
        # Build the staging dir directly under .agent-config/ so the
        # journal's recorded staged_path values are valid post-discovery.
        staging = self._build_partial_staging(
            journal_pid=os.getpid(), parent_dir=agent_config,
        )

        observed: list[reconciliation.OrphanClassification] = []

        def callback(orphan):
            observed.append(orphan)

        report = reconciliation.reconcile_orphans(
            project_root, user_root,
            locks_held=True, drift_callback=callback,
        )
        # PARTIAL with self-ownership is reapplyable (the staged file
        # for the second op exists in the staging dir), so it routes to
        # partial_reapplied — drift_callback is not invoked.
        self.assertEqual(observed, [])
        self.assertEqual(len(report.partial_reapplied), 1)
        self.assertIn(staging, report.partial_reapplied)


# =====================================================================
# Phase 8 Round 4 fix: foreign-partial reapply gate.
# =====================================================================


class TestForeignPartialReapplyGate(unittest.TestCase):
    """Round 4 Issue 6: ``_reapply_partial`` must refuse to reapply a
    PARTIAL whose ownership is ``"foreign"`` unless ``force=True``. The
    orchestrator passes ``force=True`` for self-classified orphans; for
    foreign / unknown orphans, the default ``force=False`` is used. A
    foreign orphan therefore raises :class:`ForeignPartialError` from
    inside ``_reapply_partial``, and the orchestrator routes the staging
    dir to ``blocking`` so an operator can investigate."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_root = Path(self.tmp.name)

    def _build_partial_staging(
        self, *, journal_pid: int, parent_dir: Path | None = None,
    ) -> Path:
        """Same fixture builder as TestClassifyOrphanOwnership above."""
        parent = parent_dir if parent_dir is not None else self.tmp_root
        parent.mkdir(parents=True, exist_ok=True)
        a = parent / f"a-{journal_pid}.txt"
        b = parent / f"b-{journal_pid}.txt"
        a.write_bytes(b"a-pre")
        b.write_bytes(b"b-pre")
        lock_path = parent / f"peer-{journal_pid}.lock"
        lock_path.write_text("0\n", encoding="utf-8")
        staging = parent / f"stage-{journal_pid}.staging-x"
        txn = txn_mod.Transaction(staging, lock_path)
        txn.__enter__()
        txn.stage_write(a, b"a-new")
        txn.stage_write(b, b"b-new")
        a.write_bytes(b"a-new")  # partial commit: a applied, b not.

        journal_path = staging / txn_mod.JOURNAL_NAME
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["pid"] = journal_pid
        journal_path.write_text(
            json.dumps(journal, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return staging

    def test_reapply_partial_raises_foreign_partial_error_without_force(self) -> None:
        """A partial classified as ``"foreign"`` raises
        ``ForeignPartialError`` from ``_reapply_partial`` when
        ``force=False`` (the default)."""
        staging = self._build_partial_staging(journal_pid=11112222)
        orphan = reconciliation.classify_orphan(
            staging, locks_held=True, owner_pid=33334444,
        )
        self.assertEqual(orphan.ownership, "foreign")
        with self.assertRaises(reconciliation.ForeignPartialError):
            reconciliation._reapply_partial(orphan)

    def test_reapply_partial_with_force_true_applies(self) -> None:
        """``force=True`` overrides the foreign gate. Used by tooling
        that has already confirmed no peer process intends to recover the
        staging dir (e.g., explicit operator command)."""
        staging = self._build_partial_staging(journal_pid=11112222)
        # Locate the second pre-state target.
        b = self.tmp_root / "b-11112222.txt"
        self.assertEqual(b.read_bytes(), b"b-pre")
        orphan = reconciliation.classify_orphan(
            staging, locks_held=True, owner_pid=33334444,
        )
        self.assertEqual(orphan.ownership, "foreign")
        reconciliation._reapply_partial(orphan, force=True)
        self.assertEqual(b.read_bytes(), b"b-new")

    def test_self_partial_reapplies_without_force(self) -> None:
        """A ``"self"``-ownership orphan reapplies regardless of the
        ``force`` flag (the gate only fires for ``"foreign"``)."""
        staging = self._build_partial_staging(journal_pid=os.getpid())
        b = self.tmp_root / f"b-{os.getpid()}.txt"
        orphan = reconciliation.classify_orphan(
            staging, locks_held=True,
        )
        self.assertEqual(orphan.ownership, "self")
        # Default force=False is fine for a self-orphan.
        reconciliation._reapply_partial(orphan)
        self.assertEqual(b.read_bytes(), b"b-new")

    def test_unknown_ownership_reapplies_without_force(self) -> None:
        """``"unknown"`` (locks_held=False path) keeps existing v0.4.0
        semantics: reapply without the foreign-gate check. The
        foreign-process safety still applies via the LIVE classifier
        path (locks.is_held), separate from this gate."""
        staging = self._build_partial_staging(journal_pid=12345)
        b = self.tmp_root / "b-12345.txt"
        orphan = reconciliation.classify_orphan(staging)  # locks_held=False
        self.assertEqual(orphan.ownership, "unknown")
        reconciliation._reapply_partial(orphan)
        self.assertEqual(b.read_bytes(), b"b-new")

    def test_foreign_partial_not_reapplied_without_force(self) -> None:
        """End-to-end: stage a foreign partial, run reconcile_orphans
        with locks_held=True, assert it's left in place (not removed)
        and routed to ``blocking``. No on-disk target was reapplied."""
        project_root = self.tmp_root / "project"
        user_root = self.tmp_root / "user"
        project_root.mkdir()
        user_root.mkdir()
        agent_config = project_root / ".agent-config"
        agent_config.mkdir()
        (user_root / ".claude" / "hooks").mkdir(parents=True)
        # Build with a deliberately-foreign PID (not os.getpid()).
        foreign_pid = os.getpid() + 999_999
        staging = self._build_partial_staging(
            journal_pid=foreign_pid, parent_dir=agent_config,
        )
        b = agent_config / f"b-{foreign_pid}.txt"
        # Pre-condition: b is at pre-state (partial commit didn't touch it).
        self.assertEqual(b.read_bytes(), b"b-pre")

        observed: list[reconciliation.OrphanClassification] = []

        def callback(orphan):
            observed.append(orphan)

        report = reconciliation.reconcile_orphans(
            project_root, user_root,
            locks_held=True, drift_callback=callback,
        )
        # Foreign-owned partial: refused. The staging dir stays in place
        # so the foreign process (or operator) can recover it.
        self.assertTrue(staging.exists())
        # Drift callback fired so the operator sees the blocking orphan.
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0].staging_dir, staging)
        self.assertEqual(observed[0].ownership, "foreign")
        self.assertIn(staging, report.blocking)
        # On-disk target b was NOT reapplied (still at pre-state).
        self.assertEqual(b.read_bytes(), b"b-pre")


if __name__ == "__main__":
    unittest.main()
