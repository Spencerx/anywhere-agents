"""``kind: permission`` handler for the unified pack composer (Phase 3).

Merges declarative JSON into ``~/.claude/settings.json``. Ownership
semantics per pack-architecture.md:406-408 + :621 + :670-671:

- Distinct permission objects from different packs / repos coexist
  freely in the shared ``settings.json`` (the common happy path; same
  file, different logical entries).
- Two repos claiming the *same logical output* (same target path, same
  merge-key path within the JSON) with *matching* expected content:
  JOIN owners, no change to on-disk JSON.
- Two repos claiming the same logical output with *different* expected
  content: fail closed with ``UserLevelOutputConflict`` per pack-
  architecture.md § "Same-path / different-content conflict".

"Logical output" is encoded via the manifest's ``merge:`` field, which
names the merge-point (e.g., ``permissions.ask`` for an entry added to
the ask-array). The handler treats ``(kind, target_path, merge_key)``
as the uniqueness key for ownership; the schema parser doesn't yet
validate ``merge:`` structure, so malformed merge keys surface as
dispatch-time errors.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .. import state as state_mod
from ..dispatch import DispatchContext, resolve_output_path


def handle_permission(entry: dict[str, Any], ctx: DispatchContext) -> None:
    """Dispatch a ``kind: permission`` active entry.

    For each ``files[i]`` mapping:
      - Read the source JSON payload (from ``ctx.pack_source_dir``).
      - Resolve the target ``to:`` path (``~/.claude/settings.json`` in
        the common case).
      - Determine the merge-point in the target JSON via the entry's
        ``merge:`` field (defaults to ``permissions.ask`` for backward
        compatibility with the v0.3.x style but pack-architecture.md:159
        permits arbitrary merge paths).
      - For each JSON value in the source payload: upsert a user-state
        entry keyed by ``(kind, target_path, merge_key)``.
      - Stage the on-disk settings merge through the active transaction.

    On a ``UserLevelOutputConflict``, re-raise immediately so the
    composer aborts without partial install.
    """
    files = entry["files"]
    merge_key = entry.get("merge", "permissions.ask")
    owner_record = _build_owner_record(ctx)
    for mapping in files:
        src_rel = mapping["from"]
        dst_rel = mapping["to"]
        src = (ctx.pack_source_dir / src_rel).resolve()
        if not src.exists() or not src.is_file():
            raise FileNotFoundError(
                f"pack {ctx.pack_name!r} permission entry: source {src} "
                f"does not exist or is not a regular file (manifest "
                f"'from': {src_rel!r})"
            )
        dst, scope = resolve_output_path(dst_rel, ctx)

        # Load source payload as JSON — permission entries must be JSON.
        source_text = src.read_text(encoding="utf-8")
        try:
            source_payload = json.loads(source_text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"pack {ctx.pack_name!r} permission entry: source {src} is "
                f"not valid JSON ({exc})"
            ) from exc

        # Each top-level value under the source payload becomes one
        # owned merge unit in the target. For a source like
        # ``{"permissions": {"ask": ["Bash(git push)"]}}``, iterate the
        # leaf list. For Phase 3 we only support list-valued merges at
        # the specified merge_key.
        values = _extract_merge_values(source_payload, merge_key)

        for value in values:
            value_sha_or_json = value if isinstance(value, (dict, list)) else _canonical_json(value)
            expected = value if isinstance(value, dict) else (value if isinstance(value, list) else value)
            # For owners tracking, use the raw value (dict or scalar)
            # as expected_sha256_or_json per the schema; for scalar /
            # list values, the canonical JSON form is also acceptable.
            # We normalize: dict stays dict; everything else gets
            # serialized to a canonical string so owners-merge compares
            # deterministically across processes.
            expected_key = _canonical_json(value) if not isinstance(value, dict) else value

            result = state_mod.upsert_user_state_entry(
                ctx.user_state,
                kind="active-permission",
                target_path=f"{dst}#{merge_key}#{_canonical_json(value)}",
                expected_sha256_or_json=expected_key,
                owner={**owner_record, "expected_sha256_or_json": expected_key},
            )

            if result == "created":
                # Stage the JSON merge: read current settings, add this
                # value at the merge_key, write back. Multiple values +
                # other handlers may stage writes to the same file; the
                # transaction's per-file atomic rename coalesces them
                # correctly as long as each stage_write observes the
                # most recent staged content.
                _stage_json_merge(dst, merge_key, value, ctx)

        # Record the source file itself in pack-lock; output_paths lists
        # the settings.json path even though the write is a JSON merge,
        # not a file replacement.
        source_sha = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        ctx.record_lock_file(
            {
                "role": "active-permission",
                "host": ctx.current_host,
                "source_path": src_rel,
                "input_sha256": source_sha,
                "output_paths": [str(dst)],
                "output_scope": scope,
                "effective_update_policy": ctx.pack_update_policy,
            }
        )


def _build_owner_record(ctx: DispatchContext) -> dict[str, Any]:
    return {
        "repo_id": ctx.repo_id,
        "pack": ctx.pack_name,
        "requested_ref": ctx.pack_requested_ref,
        "resolved_commit": ctx.pack_resolved_commit,
    }


def _extract_merge_values(payload: Any, merge_key: str) -> list[Any]:
    """Walk ``payload`` down ``merge_key`` (dot-separated) and return the
    list of values to merge. The leaf must be a list."""
    node: Any = payload
    for part in merge_key.split("."):
        if not isinstance(node, dict):
            raise ValueError(
                f"permission source payload: cannot traverse {merge_key!r}; "
                f"stopped at non-object node"
            )
        if part not in node:
            return []  # nothing to merge at this key
        node = node[part]
    if not isinstance(node, list):
        raise ValueError(
            f"permission source payload: merge target {merge_key!r} must "
            f"be a list (got {type(node).__name__})"
        )
    return list(node)


def _canonical_json(value: Any) -> str:
    """Serialize ``value`` deterministically for owners-match comparison."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _stage_json_merge(
    target_path: Path, merge_key: str, value: Any, ctx: DispatchContext
) -> None:
    """Accumulate ``value`` at ``merge_key`` in the in-memory shadow for
    ``target_path``, then stage the shadow as the latest write.

    The shadow (``ctx._pending_json_targets``) is required because
    transaction commit has not run yet when this handler executes, so
    reading from disk would miss any previously-staged writes within the
    same compose run. Without the shadow, a source payload with two
    values at ``permissions.ask`` would stage the first write, then
    overwrite it with a second write containing only the second value.
    """
    key = str(target_path)
    if key in ctx._pending_json_targets:
        # Deep-copy the shadow so downstream mutation doesn't leak back
        # into the shadow before we re-stage.
        current = json.loads(json.dumps(ctx._pending_json_targets[key]))
    else:
        try:
            current_text = target_path.read_text(encoding="utf-8")
            current = json.loads(current_text) if current_text.strip() else {}
        except FileNotFoundError:
            current = {}
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"cannot merge permission into {target_path}: existing content "
                f"is not valid JSON ({exc})"
            ) from exc

    # Navigate to merge_key, creating intermediate dicts as needed.
    node = current
    parts = merge_key.split(".")
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]
    leaf_key = parts[-1]
    arr = node.setdefault(leaf_key, [])
    if not isinstance(arr, list):
        raise ValueError(
            f"cannot merge permission into {target_path}: {merge_key!r} "
            f"exists but is not a list"
        )
    # De-duplicate: check canonical form.
    value_canon = _canonical_json(value)
    if not any(_canonical_json(existing) == value_canon for existing in arr):
        arr.append(value)

    # Update shadow, then stage the latest full-file rewrite.
    ctx._pending_json_targets[key] = current
    new_text = json.dumps(current, indent=2, sort_keys=True) + "\n"
    ctx.txn.stage_write(target_path, new_text.encode("utf-8"))
