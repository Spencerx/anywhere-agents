"""``kind: hook`` handler for the unified pack composer (v0.4.0 Phase 3).

Deploys a Python hook file to ``~/.claude/hooks/<pack>/NN-<name>.py`` and
tracks ownership in user-level ``pack-state.json`` so cross-repo installs
of the same hook coexist safely:

- Two repos installing the *same* hook pack (byte-identical content) JOIN
  the existing ``owners:`` list; on-disk content is unchanged.
- Two repos installing a hook pack at the same target path with
  *different* content: fail closed with ``UserLevelOutputConflict`` per
  pack-architecture.md § "Same-path / different-content conflict".

The manifest-order prefix (``01-``, ``02-``, …) on the target filename
is specified in the manifest's ``to:`` path in Phase 3. Consumer-side
``hook_order:`` override re-stamping is a future release concern.

Settings-file wiring (adding a ``hooks.<trigger>`` entry to
``~/.claude/settings.json``) is NOT done by this handler; manifests that
need the settings-level wiring ship a separate ``kind: permission`` entry
pointing at the same settings.json path. Keeping the file deployment and
the settings wiring separate lets each operation succeed or fail
independently, matching the schema split between ``kind: hook`` (file
deployment) and ``kind: permission`` (JSON merge).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .. import state as state_mod
from ..dispatch import DispatchContext, resolve_output_path


def handle_hook(entry: dict[str, Any], ctx: DispatchContext) -> None:
    """Dispatch a ``kind: hook`` active entry.

    For each ``files[i]`` mapping:
      - Read the source file content (from ``ctx.pack_source_dir``).
      - Resolve the target ``to:`` path (user-level per ``~/`` prefix).
      - Upsert the user-state entry with owners-merge or conflict-fail.
      - If created or joined with a fresh write needed, stage the write.
      - Record the output as ``role: active-hook`` in pack-lock.
    """
    files = entry["files"]
    owner_record = _build_owner_record(ctx)
    for mapping in files:
        src_rel = mapping["from"]
        dst_rel = mapping["to"]
        src = (ctx.pack_source_dir / src_rel).resolve()
        if not src.exists() or not src.is_file():
            raise FileNotFoundError(
                f"pack {ctx.pack_name!r} hook entry: source {src} does not "
                f"exist or is not a regular file (manifest 'from': {src_rel!r})"
            )
        dst, scope = resolve_output_path(dst_rel, ctx)
        content = src.read_bytes()
        content_sha = hashlib.sha256(content).hexdigest()

        result = state_mod.upsert_user_state_entry(
            ctx.user_state,
            kind="active-hook",
            target_path=str(dst),
            expected_sha256_or_json=content_sha,
            owner={**owner_record, "expected_sha256_or_json": content_sha},
        )

        # Stage the write only when we created a new entry; a join means
        # the on-disk content already matches the expected hash, so we
        # don't need to overwrite it (and doing so could race with the
        # other owner's in-flight transaction).
        if result == "created":
            ctx.txn.stage_write(dst, content)

        ctx.record_lock_file(
            {
                "role": "active-hook",
                "host": ctx.current_host,
                "source_path": src_rel,
                "input_sha256": content_sha,
                "output_paths": [str(dst)],
                "output_scope": scope,
                "effective_update_policy": ctx.pack_update_policy,
            }
        )


def _build_owner_record(ctx: DispatchContext) -> dict[str, Any]:
    """Return the owner record template for this pack's current install.

    Caller merges in ``expected_sha256_or_json`` per-file before calling
    ``upsert_user_state_entry``. Kept as a helper so the permission
    handler can reuse the same shape.
    """
    return {
        "repo_id": ctx.repo_id,
        "pack": ctx.pack_name,
        "requested_ref": ctx.pack_requested_ref,
        "resolved_commit": ctx.pack_resolved_commit,
    }
