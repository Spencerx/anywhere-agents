"""Startup orphan reconciliation for pack lifecycle transactions (Phase 2).

On every bootstrap, the composer scans for orphan transaction directories
left behind by a previous run that crashed mid-lifecycle. Each orphan is
classified by comparing on-disk content against the journal's pre-state
and new-content hashes, then one of:

- ``LIVE`` — another process still holds the transaction's lock file.
  Skipped without touching anything; the peer composer's commit or
  rollback will clean this up on its own exit. Windows busy-lock where
  the holder PID cannot be confirmed is treated the same as a proven
  live holder (``locks.is_held`` returns ``True`` for both).

  v0.5.0 Phase 8 adds a self-vs-foreign distinction for the ``locks_held``
  case: when the orchestrator already owns the lock pair AND the journal's
  recorded ``pid`` matches ``os.getpid()``, the orphan is from THIS run's
  transaction (e.g., a partial commit + crash inside the active outer
  block). Such self-orphans bypass the foreign-process safety check in
  the reapply path and can be reapplied immediately. ``OrphanClassification``
  carries an ``ownership`` field (``"self"`` / ``"foreign"`` / ``"unknown"``)
  so callers can tell them apart.
- ``ROLLBACK_OK`` — all op targets still match their pre-state hashes.
  The previous run staged content but never committed. Safe to delete
  the staging directory; no on-disk targets need reverting.
- ``ROLLFORWARD_OK`` — all op targets match the new-content hashes.
  The previous run committed successfully but crashed before cleaning
  the staging directory. Safe to delete the staging directory; on-disk
  targets are already in the intended final state.
- ``PARTIAL`` — some ops at pre-state, others at new-content, every op
  matches one or the other (no drift). The previous run committed
  partway. Caller may roll forward by reapplying the un-applied ops.
- ``DRIFT`` — at least one op's target matches neither pre-state nor
  new-content. On-disk state is unknown; leave in place and surface as
  a drift report per pack-architecture.md § "Atomicity contract".
- ``MALFORMED`` — the journal cannot be read as JSON. Leave the staging
  directory alone; surface to the user.

The module exposes two layers:

1. ``classify_orphan`` / ``scan_orphans`` / ``cleanup_staging`` — the
   v0.4.0 inspection primitives that read journals, label orphans, and
   delete staging dirs without touching on-disk targets.
2. ``reconcile_orphans`` (v0.5.0 Phase 7 / Deferral 2) — orchestrator
   wrapper consumed by ``compose_packs.main``. Walks the orphan list
   produced by ``scan_orphans`` and dispatches per-label: LIVE skip,
   ROLLBACK_OK / ROLLFORWARD_OK cleanup-only, PARTIAL reapply-or-drift,
   DRIFT / MALFORMED surface via ``drift_callback``. Optionally takes
   the user/repo lock pair itself when called outside compose
   (``locks_held=False``).
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import locks
from . import transaction as txn_mod

# ----- classification labels -----

LIVE = "live"
ROLLBACK_OK = "rollback_ok"
ROLLFORWARD_OK = "rollforward_ok"
PARTIAL = "partial"
DRIFT = "drift"
MALFORMED = "malformed"


@dataclass
class OpClassification:
    """Per-op reconciliation result within a transaction."""

    op_index: int
    op_kind: str
    target_path: str
    on_disk_state: str  # one of: "pre_state", "new_state", "drift", "absent"


@dataclass
class OrphanClassification:
    """Overall reconciliation result for one orphan staging directory.

    ``ownership`` is added in v0.5.0 Phase 8 to distinguish a partial
    transaction from THIS process (``"self"``) from one left behind by a
    foreign / earlier process (``"foreign"``). Set to ``"unknown"`` when
    the journal's PID could not be read or when the caller did not pass
    ``locks_held=True`` (the foreign-process safety check still applies).
    """

    staging_dir: Path
    label: str
    journal: dict[str, Any] | None = None
    ops: list[OpClassification] = field(default_factory=list)
    detail: str = ""
    ownership: str = "unknown"


def _sha256_of_path(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None
    except OSError as exc:
        # Permission errors etc.: return None so the caller reports drift.
        raise exc


def _classify_write_op(op: dict[str, Any]) -> OpClassification:
    target = Path(op["target_path"])
    pre = op.get("pre_state_sha256")
    new = op.get("new_content_sha256")
    actual = _sha256_of_path(target)
    if actual is None:
        state = "pre_state" if pre is None else "drift"
    elif actual == new:
        state = "new_state"
    elif actual == pre:
        state = "pre_state"
    else:
        state = "drift"
    return OpClassification(
        op_index=-1, op_kind=txn_mod.OP_WRITE, target_path=str(target),
        on_disk_state=state,
    )


def _classify_delete_op(op: dict[str, Any]) -> OpClassification:
    target = Path(op["target_path"])
    pre = op.get("pre_state_sha256")
    actual = _sha256_of_path(target)
    if actual is None:
        # File absent now. If pre-state was ALSO absent, this is a
        # delete-of-absent (a no-op) — indistinguishable between pre and
        # new state. Classify as pre_state so a mix of pre_state writes
        # + delete-of-absent stays ROLLBACK_OK rather than falsely
        # reporting PARTIAL (per pack-architecture.md reconciliation
        # semantics: PARTIAL implies at least one op actually committed).
        state = "pre_state" if pre is None else "new_state"
    elif pre is not None and actual == pre:
        # File still present at pre-state = delete hasn't happened = pre_state
        state = "pre_state"
    else:
        state = "drift"
    return OpClassification(
        op_index=-1, op_kind=txn_mod.OP_DELETE, target_path=str(target),
        on_disk_state=state,
    )


def _classify_restamp_op(op: dict[str, Any]) -> OpClassification:
    old_path = Path(op["old_path"])
    new_path = Path(op["new_path"])
    pre_old = op.get("pre_state_old_sha256")
    pre_new = op.get("pre_state_new_sha256")
    new_content = op.get("new_content_sha256")
    actual_old = _sha256_of_path(old_path)
    actual_new = _sha256_of_path(new_path)

    # New state: new_path has new_content, old_path is gone.
    if actual_new == new_content and actual_old is None:
        state = "new_state"
    # Pre-state: old_path matches its pre-hash; new_path at its pre-hash
    # (usually absent).
    elif (
        actual_old == pre_old
        and (actual_new == pre_new or (pre_new is None and actual_new is None))
    ):
        state = "pre_state"
    else:
        state = "drift"
    return OpClassification(
        op_index=-1, op_kind=txn_mod.OP_RESTAMP,
        target_path=f"{old_path} -> {new_path}",
        on_disk_state=state,
    )


def classify_orphan(
    staging_dir: Path,
    *,
    locks_held: bool = False,
    owner_pid: int | None = None,
) -> OrphanClassification:
    """Inspect one orphan staging directory and classify it.

    Reads the transaction journal, checks lock contention first (a live
    transaction always short-circuits), then compares on-disk content
    against each op's pre-state / new-content hashes.

    Parameters
    ----------
    staging_dir
        Orphan staging directory containing a ``transaction.json`` journal.
    locks_held
        v0.5.0 Phase 8: ``True`` when the caller already owns the
        compose lock pair (user-lock + repo-lock). In that case the
        helper compares the journal's recorded ``pid`` against
        ``owner_pid`` (or ``os.getpid()`` if ``owner_pid`` is ``None``)
        to set ``OrphanClassification.ownership``: ``"self"`` when the
        orphan is from this very process, ``"foreign"`` otherwise. The
        reapply path uses this distinction to skip the foreign-process
        safety check on a self-partial. With ``locks_held=False`` the
        ownership stays ``"unknown"`` and existing semantics apply.
    owner_pid
        Optional override for the "this process's PID" comparison —
        useful in tests where a fixture writes a journal under a
        synthesised PID. Ignored when ``locks_held=False``.
    """
    journal_path = staging_dir / txn_mod.JOURNAL_NAME
    if not journal_path.exists():
        return OrphanClassification(
            staging_dir=staging_dir,
            label=MALFORMED,
            detail=f"journal {journal_path} not found",
        )
    try:
        journal = txn_mod.load_journal(journal_path)
    except txn_mod.TransactionError as exc:
        return OrphanClassification(
            staging_dir=staging_dir,
            label=MALFORMED,
            detail=str(exc),
        )

    # v0.5.0 Phase 8: derive ownership. Default ``unknown`` covers the
    # ``locks_held=False`` path (foreign by safety) and any case where
    # the journal lacks a ``pid`` field. With ``locks_held=True``, an
    # exact PID match means the orphan is from THIS run — safe to
    # reapply immediately, no foreign-process gate needed.
    ownership = "unknown"
    if locks_held:
        comparison_pid = owner_pid if owner_pid is not None else os.getpid()
        journal_pid = journal.get("pid")
        if isinstance(journal_pid, int) and journal_pid == comparison_pid:
            ownership = "self"
        else:
            ownership = "foreign"

    lock_path_str = journal.get("lock_path")
    if isinstance(lock_path_str, str) and lock_path_str:
        lock_path = Path(lock_path_str)
        # Contention on the recorded lock path is authoritative: even
        # on Windows where holder PID cannot always be confirmed, a
        # busy lock means "live transaction, skip and retry".
        if locks.is_held(lock_path):
            return OrphanClassification(
                staging_dir=staging_dir,
                label=LIVE,
                journal=journal,
                detail=f"lock {lock_path} is held; skip this pass",
                ownership=ownership,
            )

    ops_in_journal = journal.get("ops")
    if not isinstance(ops_in_journal, list):
        return OrphanClassification(
            staging_dir=staging_dir,
            label=MALFORMED,
            journal=journal,
            detail="journal 'ops' is not a list",
            ownership=ownership,
        )

    classifications: list[OpClassification] = []
    for idx, op in enumerate(ops_in_journal):
        if not isinstance(op, dict) or "op" not in op:
            return OrphanClassification(
                staging_dir=staging_dir,
                label=MALFORMED,
                journal=journal,
                detail=f"op[{idx}] is malformed",
                ownership=ownership,
            )
        kind = op["op"]
        try:
            if kind == txn_mod.OP_WRITE:
                cls = _classify_write_op(op)
            elif kind == txn_mod.OP_DELETE:
                cls = _classify_delete_op(op)
            elif kind == txn_mod.OP_RESTAMP:
                cls = _classify_restamp_op(op)
            else:
                return OrphanClassification(
                    staging_dir=staging_dir,
                    label=MALFORMED,
                    journal=journal,
                    detail=f"op[{idx}] unknown kind {kind!r}",
                    ownership=ownership,
                )
        except (KeyError, TypeError, ValueError) as exc:
            # Structural field errors (missing target_path, wrong types,
            # non-parseable values) must surface as MALFORMED so a bad
            # orphan journal can never crash startup reconciliation and
            # block bootstrap (pack-architecture.md § "Atomicity contract"
            # — orphans with unreadable state are surfaced, not raised).
            return OrphanClassification(
                staging_dir=staging_dir,
                label=MALFORMED,
                journal=journal,
                detail=f"op[{idx}] missing or invalid field: {exc}",
                ownership=ownership,
            )
        except OSError as exc:
            return OrphanClassification(
                staging_dir=staging_dir,
                label=DRIFT,
                journal=journal,
                detail=f"cannot read op[{idx}] target: {exc}",
                ownership=ownership,
            )
        cls.op_index = idx
        classifications.append(cls)

    states = {c.on_disk_state for c in classifications}
    if "drift" in states:
        label = DRIFT
    elif states == {"pre_state"}:
        label = ROLLBACK_OK
    elif states == {"new_state"}:
        label = ROLLFORWARD_OK
    elif states <= {"pre_state", "new_state"}:
        label = PARTIAL
    else:
        # Defensive: unknown state set. Treat as drift.
        label = DRIFT

    return OrphanClassification(
        staging_dir=staging_dir,
        label=label,
        journal=journal,
        ops=classifications,
        ownership=ownership,
    )


def scan_orphans(
    search_dirs: list[Path],
    *,
    locks_held: bool = False,
    owner_pid: int | None = None,
) -> list[OrphanClassification]:
    """Find and classify every ``*.staging-*`` dir containing a journal.

    ``search_dirs`` typically includes ``~/.claude/hooks/`` (user-level
    staging parent) and ``<project>/.agent-config/`` (project-local
    staging parent). Non-existent search dirs are silently skipped.

    ``locks_held`` and ``owner_pid`` are forwarded to
    :func:`classify_orphan` so the per-orphan ownership label reflects
    whether the caller holds the compose lock pair (v0.5.0 Phase 8).
    """
    results: list[OrphanClassification] = []
    for base in search_dirs:
        if not base.exists():
            continue
        for entry in base.iterdir():
            if not entry.is_dir():
                continue
            if ".staging-" not in entry.name:
                continue
            journal_path = entry / txn_mod.JOURNAL_NAME
            if not journal_path.exists():
                continue
            results.append(
                classify_orphan(
                    entry, locks_held=locks_held, owner_pid=owner_pid,
                )
            )
    return results


def cleanup_staging(staging_dir: Path) -> None:
    """Remove an orphan staging directory after reconciliation.

    Safe to call on a directory that has already been partially cleaned;
    walks the tree and removes what it finds. Does NOT touch any target
    paths listed in the journal — those are caller-managed.
    """
    if not staging_dir.exists():
        return
    for entry in staging_dir.iterdir():
        try:
            if entry.is_dir():
                _rmtree(entry)
            else:
                entry.unlink()
        except OSError:
            pass
    try:
        staging_dir.rmdir()
    except OSError:
        pass


def _rmtree(root: Path) -> None:
    for child in root.iterdir():
        if child.is_dir():
            _rmtree(child)
        else:
            try:
                child.unlink()
            except OSError:
                pass
    try:
        root.rmdir()
    except OSError:
        pass


# ----- v0.5.0 Phase 7 / Deferral 2: orchestrator wrapper -----


@dataclass
class ReconciliationReport:
    """Outcome of one ``reconcile_orphans`` pass.

    Buckets the staging directories visited by reconciliation per terminal
    label so the caller (compose_packs) can render a summary, populate
    pending-updates state, and decide whether to abort the run.

    - ``live`` — staging dirs skipped because a peer process is still
      holding the recorded lock; will be retried on the next bootstrap.
    - ``rolled_back`` — ROLLBACK_OK orphans whose staging dirs were
      cleaned up (no on-disk targets needed reverting).
    - ``rolled_forward`` — ROLLFORWARD_OK orphans whose staging dirs were
      cleaned up (targets already at new state).
    - ``partial_reapplied`` — PARTIAL orphans whose un-applied ops were
      successfully reapplied and whose staging dirs were then cleaned.
    - ``blocking`` — DRIFT, MALFORMED, and unreapplyable PARTIAL orphans
      whose staging dirs were left in place; these surface to the user
      via ``drift_callback`` (when supplied) and gate the compose run.
    """

    live: list[Path] = field(default_factory=list)
    rolled_back: list[Path] = field(default_factory=list)
    rolled_forward: list[Path] = field(default_factory=list)
    partial_reapplied: list[Path] = field(default_factory=list)
    blocking: list[Path] = field(default_factory=list)


def _collect_staging_dirs(project_root: Path, user_root: Path) -> list[Path]:
    """Return the staging-dir search roots ``scan_orphans`` should walk.

    Per pack-architecture.md § "Pack lifecycle operations", staging dirs
    for v0.5.0 transactions live in two locations:

    - ``<project>/.agent-config/`` — project-local lifecycle (per-repo
      lock; matches ``compose_packs.py:199`` `pack-compose.staging-<pid>`).
    - ``<user>/.claude/hooks/`` — user-level lifecycle (per-user lock;
      hook restamping during install / update / uninstall).

    Non-existent paths are still passed through; ``scan_orphans``
    silently skips a missing search root.
    """
    return [
        project_root / ".agent-config",
        user_root / ".claude" / "hooks",
    ]


def _can_reapply_partial(orphan: OrphanClassification) -> bool:
    """Return True iff every still-pre-state op of ``orphan`` is reapplyable.

    PARTIAL means every op is at pre_state OR new_state on disk. The
    ones at new_state are already finished (no work needed). The ones at
    pre_state need re-application: copy/rename the staged file into
    target_path (write/restamp) or unlink the target (delete). For
    write/restamp we must be able to find ``staged_path`` on disk; if
    the staged file is gone, we cannot finish the commit and must
    surface the orphan as drift.
    """
    journal = orphan.journal or {}
    journal_ops = journal.get("ops") or []
    if len(journal_ops) != len(orphan.ops):
        # Journal/classification mismatch: treat as unsafe to reapply.
        return False
    for op_cls, op in zip(orphan.ops, journal_ops):
        if op_cls.on_disk_state != "pre_state":
            continue
        kind = op.get("op")
        if kind in (txn_mod.OP_WRITE, txn_mod.OP_RESTAMP):
            staged = op.get("staged_path")
            if not staged or not Path(staged).exists():
                return False
        # OP_DELETE needs no staged file; on-disk target is the only thing
        # it touches and ``on_disk_state == "pre_state"`` confirms the
        # target is still present (or absent in the same way it was
        # absent before staging — see _classify_delete_op).
    return True


class ForeignPartialError(RuntimeError):
    """Raised by :func:`_reapply_partial` when ``orphan.ownership == "foreign"``
    and the caller did not pass ``force=True``.

    Carries the staging dir for caller surfacing — the orchestrator
    catches this and routes the orphan to ``blocking`` so the operator
    can decide whether the foreign process intends to recover its own
    transaction or whether the staging dir is truly abandoned.
    """

    def __init__(self, staging_dir: Path) -> None:
        self.staging_dir = staging_dir
        super().__init__(
            f"refusing to reapply foreign-owned partial transaction at "
            f"{staging_dir}; pass force=True to override"
        )


def _reapply_partial(
    orphan: OrphanClassification, *, force: bool = False,
) -> None:
    """Re-run only the ops of ``orphan`` whose target is still at pre-state.

    Uses ``transaction._atomic_replace`` so the rename gets the same
    Windows AV-retry behaviour as a normal commit. Caller must have
    confirmed reapplyability via ``_can_reapply_partial`` first; missing
    staged files here would surface as ``FileNotFoundError`` from the
    rename and bubble up to the caller (which already routes that case
    to drift via the ``can_reapply`` gate).

    v0.5.0 Phase 8: when ``orphan.ownership == "foreign"`` and ``force``
    is False, raise :class:`ForeignPartialError` rather than reapplying.
    The foreign-process safety gate stops the orchestrator from
    finishing a partial commit that another process may still intend to
    recover. The orchestrator passes ``force=True`` for self-classified
    orphans (its own crashed transaction). ``"self"`` and ``"unknown"``
    ownership labels reapply unconditionally so existing v0.4.0 semantics
    (``locks_held=False`` reconciliation) remain unchanged.
    """
    if orphan.ownership == "foreign" and not force:
        raise ForeignPartialError(orphan.staging_dir)
    journal_ops = (orphan.journal or {}).get("ops") or []
    for op_cls, op in zip(orphan.ops, journal_ops):
        if op_cls.on_disk_state != "pre_state":
            continue
        kind = op.get("op")
        if kind == txn_mod.OP_WRITE:
            target = Path(op["target_path"])
            staged = Path(op["staged_path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            txn_mod._atomic_replace(str(staged), str(target))
        elif kind == txn_mod.OP_DELETE:
            target = Path(op["target_path"])
            try:
                target.unlink()
            except FileNotFoundError:
                # Already gone is success for delete-of-pre-state-absent.
                pass
        elif kind == txn_mod.OP_RESTAMP:
            old_path = Path(op["old_path"])
            new_path = Path(op["new_path"])
            staged = Path(op["staged_path"])
            new_path.parent.mkdir(parents=True, exist_ok=True)
            txn_mod._atomic_replace(str(staged), str(new_path))
            try:
                old_path.unlink()
            except FileNotFoundError:
                pass


def _reconcile_inner(
    project_root: Path,
    user_root: Path,
    drift_callback: Callable[[OrphanClassification], None] | None,
    *,
    locks_held: bool = False,
    owner_pid: int | None = None,
) -> ReconciliationReport:
    """Single-pass dispatch over the orphan list. Locks are caller-owned.

    ``locks_held`` / ``owner_pid`` are forwarded to ``scan_orphans`` so
    the ``OrphanClassification.ownership`` field reflects whether the
    orphan came from THIS process (self) or a foreign one.
    """
    staging_dirs = _collect_staging_dirs(project_root, user_root)
    report = ReconciliationReport()
    for orphan in scan_orphans(
        staging_dirs, locks_held=locks_held, owner_pid=owner_pid,
    ):
        if orphan.label == LIVE:
            report.live.append(orphan.staging_dir)
        elif orphan.label == ROLLBACK_OK:
            cleanup_staging(orphan.staging_dir)
            report.rolled_back.append(orphan.staging_dir)
        elif orphan.label == ROLLFORWARD_OK:
            cleanup_staging(orphan.staging_dir)
            report.rolled_forward.append(orphan.staging_dir)
        elif orphan.label == PARTIAL:
            if _can_reapply_partial(orphan):
                # v0.5.0 Phase 8: self-orphans (this run's own crashed
                # transaction) bypass the foreign-process gate inside
                # ``_reapply_partial`` via ``force=True``. Foreign and
                # unknown orphans take the default; ``_reapply_partial``
                # raises ``ForeignPartialError`` for foreign-owned ones,
                # which the orchestrator routes to ``blocking`` so an
                # operator can investigate.
                try:
                    _reapply_partial(
                        orphan, force=(orphan.ownership == "self"),
                    )
                except ForeignPartialError:
                    if drift_callback is not None:
                        drift_callback(orphan)
                    report.blocking.append(orphan.staging_dir)
                else:
                    cleanup_staging(orphan.staging_dir)
                    report.partial_reapplied.append(orphan.staging_dir)
            else:
                if drift_callback is not None:
                    drift_callback(orphan)
                report.blocking.append(orphan.staging_dir)
        elif orphan.label in (DRIFT, MALFORMED):
            if drift_callback is not None:
                drift_callback(orphan)
            report.blocking.append(orphan.staging_dir)
        # Any other label is treated like DRIFT defensively, but the
        # classifier only emits the six labels above so this branch is
        # currently unreachable.
    return report


def reconcile_orphans(
    project_root: Path,
    user_root: Path,
    *,
    locks_held: bool = False,
    drift_callback: Callable[[OrphanClassification], None] | None = None,
) -> ReconciliationReport:
    """Scan + dispatch every orphan staging dir under the two roots.

    Phase 7 / Deferral 2 (Codex R1 M7 + R2 H2 + R3 M3). Wraps
    ``scan_orphans`` + ``cleanup_staging`` so ``compose_packs.main`` can
    reconcile a single time per bootstrap with one call.

    Parameters
    ----------
    project_root
        Consumer-repo root (the directory containing ``.agent-config``).
    user_root
        User home root used to derive ``~/.claude/hooks/`` and the
        per-user lock path. In production, ``Path.home()``; in tests, a
        tmpdir.
    locks_held
        ``True`` when the caller (``compose_packs.main``) already owns
        the user-lock + repo-lock pair. The wrapper then runs the inner
        dispatch directly. ``False`` when reconciliation is invoked
        outside compose (e.g., a CLI ``pack list --drift`` audit) — the
        wrapper takes both locks itself for the duration of the pass.
    drift_callback
        Optional sink invoked once per blocking orphan (DRIFT, MALFORMED,
        or PARTIAL whose staged files cannot be reapplied). Compose uses
        this to populate ``pending-updates.json``; CLI audit modes can
        pass a printer.

    Returns
    -------
    ReconciliationReport
        Buckets of staging dirs by terminal disposition. Empty on a clean
        startup with no orphans.

    Notes
    -----
    The ``locks_held=True`` path relies on the classifier itself
    distinguishing self-held locks (recorded sidecar PID equal to
    ``os.getpid()``) from foreign-held locks. v0.5.0 ships the wrapper;
    a sibling task extends ``classify_orphan`` so a self-held lock no
    longer reads as LIVE. Until that lands, callers passing
    ``locks_held=True`` should arrange for their own staging dirs to be
    cleaned up by their own commit/rollback paths (compose's transaction
    block handles this) so reconciliation only ever sees foreign orphans.
    """
    if not locks_held:
        with (
            locks.acquire(locks.user_lock_path(user_root), timeout=30),
            locks.acquire(locks.repo_lock_path(project_root), timeout=30),
        ):
            return _reconcile_inner(
                project_root, user_root, drift_callback,
                locks_held=False,
            )
    return _reconcile_inner(
        project_root, user_root, drift_callback, locks_held=True,
    )
