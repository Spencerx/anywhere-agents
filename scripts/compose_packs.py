#!/usr/bin/env python3
"""Unified pack composer for anywhere-agents (v0.4.0+).

Entry point invoked by bootstrap.sh / bootstrap.ps1. Handles both v1
(legacy passive-only) and v2 (unified passive + active) manifests:

- v1 manifests: delegate to ``scripts/compose_rule_packs.py`` so v0.3.x
  consumer-visible output stays byte-identical during the BC window.
- v2 manifests: parse with ``scripts.packs.schema``, resolve selections,
  route passive entries through the v2 passive adapter, dispatch active
  entries via the kind-handler registry, and stage all writes through
  ``scripts.packs.transaction`` for per-file atomic commits. State files
  (``pack-lock.json``, ``pack-state.json``) are written at end on success.

The v2 composition flow does not yet acquire per-user / per-repo locks
(Phase 4 wires them in) and does not yet invoke startup reconciliation
(also Phase 4). Phase 3's contract is "active-kind dispatch works + 4
shipped skills convert to pack-emitted"; the lifecycle-robustness
additions layer on in Phase 4+.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

import compose_rule_packs as legacy  # noqa: E402
from packs import dispatch  # noqa: E402
from packs import handlers  # noqa: E402 — side-effect: registers handlers
from packs import passive as passive_mod  # noqa: E402
from packs import schema  # noqa: E402
from packs import state as state_mod  # noqa: E402
from packs import transaction as txn_mod  # noqa: E402


def _validated_state_bytes(
    write_fn: Callable[[Path, dict[str, Any]], None], payload: dict[str, Any]
) -> bytes:
    """Run the state-file ``write_fn`` against a temp path so schema
    validation errors surface before we stage the content. Returns the
    bytes written — caller stages them through the composer transaction
    so all writes (outputs + state files) share one commit boundary.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "state.json"
        write_fn(tmp, payload)
        return tmp.read_bytes()


# Default v2 selections applied when the consumer provides no signal.
# Includes the v0.3.x-default agent-style rule pack plus the bundled
# aa-core-skills pack so shipped-skill pointers are emitted under the
# same default-on behavior as v0.3.x.
DEFAULT_V2_SELECTIONS: list[dict[str, str]] = [
    {"name": "agent-style"},
    {"name": "aa-core-skills"},
]


def _resolve_manifest_path(root: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    bootstrap_dir = root / ".agent-config" / "repo" / "bootstrap"
    candidate = bootstrap_dir / "packs.yaml"
    if candidate.exists():
        return candidate
    return bootstrap_dir / "rule-packs.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="unified pack composer for anywhere-agents (v0.4.0+)"
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--print-yaml", metavar="PACK", default=None)
    args = parser.parse_args(argv)

    if args.print_yaml:
        return legacy.main(argv)

    root = args.root.resolve()
    manifest_path = _resolve_manifest_path(root, args.manifest)

    if not manifest_path.exists():
        # Nothing to compose; legacy.main emits the appropriate error.
        return legacy.main(argv)

    try:
        parsed = schema.parse_manifest(manifest_path)
    except schema.ParseError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1

    if parsed["version"] == 1:
        # Legacy passive-only manifest: delegate.
        return legacy.main(argv)

    # v2 manifest: full composition here.
    return _do_compose_v2(root, parsed, args.no_cache)


def _do_compose_v2(
    root: Path, parsed: dict, no_cache: bool
) -> int:
    # ----- resolve selections (reuse legacy's config parsing) -----
    try:
        tracked = legacy.parse_user_config(root / "agent-config.yaml")
        local = legacy.parse_user_config(root / "agent-config.local.yaml")
    except legacy.RulePackError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1

    env_val = os.environ.get("AGENT_CONFIG_RULE_PACKS", "") or os.environ.get(
        "AGENT_CONFIG_PACKS", ""
    )
    env_list = legacy.parse_env_packs(env_val) if env_val else []

    try:
        selections = legacy.resolve_selections(
            tracked, local, env_list, default=DEFAULT_V2_SELECTIONS
        )
    except legacy.RulePackError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1

    # ----- upstream AGENTS.md -----
    upstream_path = root / ".agent-config" / "AGENTS.md"
    if not upstream_path.exists():
        sys.stderr.write(
            f"error: upstream AGENTS.md not found at {upstream_path}; "
            "bootstrap should fetch it before invoking this helper\n"
        )
        return 1
    upstream = upstream_path.read_text(encoding="utf-8")

    # Opt-out: selections empty → write verbatim upstream and exit.
    if not selections:
        try:
            legacy.atomic_write(root / "AGENTS.md", upstream)
        except OSError as exc:
            sys.stderr.write(
                f"error: failed to write verbatim AGENTS.md: {exc}\n"
            )
            return 1
        return 0

    # ----- state setup -----
    project_lock_path = root / ".agent-config" / "pack-lock.json"
    project_state_path = root / ".agent-config" / "pack-state.json"
    user_state_path = Path.home() / ".claude" / "pack-state.json"

    pack_lock = state_mod.empty_pack_lock()
    project_state = state_mod.empty_project_state()
    try:
        user_state = state_mod.load_user_state(user_state_path)
    except state_mod.StateError as exc:
        sys.stderr.write(
            f"warning: user state at {user_state_path} unreadable "
            f"({exc}); starting fresh\n"
        )
        user_state = state_mod.empty_user_state()

    # ----- compose -----
    composed_agents = upstream
    cache_dir = root / ".agent-config" / "rule-packs"

    packs_by_name = {p["name"]: p for p in parsed["packs"]}
    # Staging dir name matches Phase 2's reconciliation scan pattern
    # `*.staging-*` so a crashed composer is recoverable on next startup
    # once Phase 4 wires reconciliation into bootstrap.
    staging_dir = root / ".agent-config" / f"pack-compose.staging-{os.getpid()}"
    lock_path = root / ".agent-config" / ".pack-lock.lock"

    try:
        with txn_mod.Transaction(staging_dir, lock_path) as txn:
            for selection in selections:
                pack_name = selection["name"]
                pack = packs_by_name.get(pack_name)
                if pack is None:
                    sys.stderr.write(
                        f"error: pack {pack_name!r} not found in manifest\n"
                    )
                    return 1

                ctx = _build_ctx(
                    root=root,
                    pack=pack,
                    selection=selection,
                    txn=txn,
                    pack_lock=pack_lock,
                    project_state=project_state,
                    user_state=user_state,
                )

                # Passive entries first (concatenate into AGENTS.md).
                for passive_entry in pack.get("passive", []) or []:
                    composed_agents = passive_mod.handle_passive_entry(
                        passive_entry,
                        pack,
                        ctx,
                        upstream_agents_md=composed_agents,
                        cache_dir=cache_dir,
                        no_cache=no_cache,
                    )

                # Then active entries (dispatch by kind).
                for active_entry in pack.get("active", []) or []:
                    dispatch.dispatch_active(active_entry, ctx)

                ctx.finalize_pack_lock()

            # Stage all writes — state files + AGENTS.md — through the
            # same transaction so a state-validation error surfaces
            # before any output commits and partial state cannot leak.
            txn.stage_write(
                project_lock_path,
                _validated_state_bytes(state_mod.save_pack_lock, pack_lock),
            )
            txn.stage_write(
                project_state_path,
                _validated_state_bytes(state_mod.save_project_state, project_state),
            )
            if user_state.get("entries"):
                txn.stage_write(
                    user_state_path,
                    _validated_state_bytes(state_mod.save_user_state, user_state),
                )
            txn.stage_write(
                root / "AGENTS.md", composed_agents.encode("utf-8")
            )
    except (
        state_mod.StateError,
        dispatch.DispatchError,
        legacy.RulePackError,
        FileNotFoundError,
        ValueError,
    ) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    except OSError as exc:
        sys.stderr.write(f"error: composition failed: {exc}\n")
        return 1

    return 0


def _build_ctx(
    *,
    root: Path,
    pack: dict,
    selection: dict,
    txn: txn_mod.Transaction,
    pack_lock: dict,
    project_state: dict,
    user_state: dict,
) -> dispatch.DispatchContext:
    """Assemble a DispatchContext for one pack's composition."""
    source = pack.get("source")
    if isinstance(source, dict):
        source_url = source.get("repo") or source.get("url") or "bundled:aa"
        pack_ref = selection.get("ref") or source.get("ref") or "bundled"
    else:
        # Source absent or string: treat as bundled.
        source_url = source if isinstance(source, str) and source else "bundled:aa"
        pack_ref = selection.get("ref") or pack.get("default-ref") or "bundled"

    # Pack-level hosts default (pack-architecture.md:199) is explicitly
    # threaded into the context so dispatch._effective_hosts() can
    # inherit it when an active entry omits its own hosts:.
    pack_hosts_default = pack.get("hosts")

    return dispatch.DispatchContext(
        pack_name=pack["name"],
        pack_source_url=source_url,
        pack_requested_ref=pack_ref,
        # Phase 3 uses requested_ref as resolved_commit; true SHA
        # resolution (git ls-remote) lands in Phase 4/5 for private
        # sources.
        pack_resolved_commit=pack_ref,
        pack_update_policy=pack.get("update_policy", "locked"),
        pack_source_dir=root / ".agent-config" / "repo",
        project_root=root,
        user_home=Path.home(),
        repo_id=str(root),
        txn=txn,
        pack_lock=pack_lock,
        project_state=project_state,
        user_state=user_state,
        pack_hosts=pack_hosts_default,
    )


if __name__ == "__main__":
    sys.exit(main())
