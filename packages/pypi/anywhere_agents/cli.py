"""CLI for anywhere-agents.

Subcommands:
- Default (no subcommand): download + run the anywhere-agents shell
  bootstrap in the current directory. Refreshes AGENTS.md, skills,
  command pointers, and settings from the upstream repo. Same behavior
  as v0.3.x.
- ``pack add/remove/list``: manage the user-level pack config file
  (``$XDG_CONFIG_HOME/anywhere-agents/config.yaml`` on POSIX,
  ``%APPDATA%\\anywhere-agents\\config.yaml`` on Windows). Pack management
  writes only to the user-level file; project-level config is owned by
  consumer repos.
- ``uninstall --all``: remove every aa-pack-owned output from the
  current project via the composer's uninstall engine. Requires the
  project to have been bootstrapped (needs
  ``.agent-config/repo/scripts/packs/``). Exits with one of the six
  codes defined by pack-architecture.md § "CLI contract for ``uninstall
  --all``".

Invariant: when invoked with no subcommand, behavior is identical to
v0.3.x so existing usage continues unchanged.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__

REPO = "yzhao062/anywhere-agents"
BRANCH = "main"

# ----- shared helpers -----


def log(msg: str) -> None:
    print(f"[anywhere-agents] {msg}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Dispatch on the first positional arg.

    - ``pack`` → pack management subcommand.
    - ``uninstall`` → uninstall subcommand.
    - Otherwise (or no args) → default bootstrap behavior.
    """
    raw = argv if argv is not None else sys.argv[1:]
    # Peek at the first non-option arg to decide routing. This keeps
    # ``anywhere-agents --version`` / ``anywhere-agents --dry-run`` /
    # ``anywhere-agents`` on the existing bootstrap path.
    first_pos = next((a for a in raw if not a.startswith("-")), None)
    if first_pos == "pack":
        return _pack_main(None, raw[raw.index("pack") + 1:])
    if first_pos == "uninstall":
        return _uninstall_main(raw[raw.index("uninstall") + 1:])
    return _bootstrap_main(raw)


# ======================================================================
# Default bootstrap subcommand (v0.3.x behavior, unchanged)
# ======================================================================


def bootstrap_url(script_name: str) -> str:
    return f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/bootstrap/{script_name}"


def choose_script() -> tuple[str, list[str]]:
    """Return (script_name, interpreter_argv_prefix) for the current platform."""
    if platform.system() == "Windows":
        interpreter = shutil.which("pwsh") or shutil.which("powershell")
        if interpreter is None:
            raise RuntimeError("PowerShell is required on Windows but was not found on PATH.")
        return "bootstrap.ps1", [interpreter, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
    bash = shutil.which("bash")
    if bash is None:
        raise RuntimeError("bash is required on macOS/Linux but was not found on PATH.")
    return "bootstrap.sh", [bash]


def _bootstrap_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="anywhere-agents",
        description=(
            "Download and run the anywhere-agents shell bootstrap in the "
            "current directory. Refreshes AGENTS.md, skills, command pointers, "
            "and settings from the upstream repo."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would run without fetching or executing.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"anywhere-agents {__version__}",
    )
    args = parser.parse_args(argv)

    try:
        script_name, interpreter_argv = choose_script()
    except RuntimeError as e:
        log(str(e))
        return 2

    url = bootstrap_url(script_name)
    config_dir = Path(".agent-config")
    out_path = config_dir / script_name

    if args.dry_run:
        log(f"Would fetch: {url}")
        log(f"Would write: {out_path}")
        log(f"Would run:   {' '.join(interpreter_argv + [str(out_path)])}")
        return 0

    config_dir.mkdir(parents=True, exist_ok=True)

    log(f"Fetching {script_name} from {url}")
    try:
        urllib.request.urlretrieve(url, out_path)  # noqa: S310 (user-controlled URL is hard-coded)
    except Exception as exc:  # pragma: no cover — network failure path
        log(f"Download failed: {exc}")
        return 1

    log("Running bootstrap (refreshes AGENTS.md, skills, settings)")
    try:
        result = subprocess.run(interpreter_argv + [str(out_path)], check=False)
    except FileNotFoundError as exc:
        log(f"Interpreter not found: {exc}")
        return 2

    if result.returncode != 0:
        log(f"Bootstrap exited with code {result.returncode}")
    return result.returncode


# ======================================================================
# pack subcommand: user-level config management
# ======================================================================


_USER_CONFIG_APP_DIR = "anywhere-agents"
_USER_CONFIG_FILENAME = "config.yaml"


def _user_config_path() -> Path | None:
    """Resolve the user-level config path per XDG / Windows conventions.

    Returns ``None`` if neither ``$HOME``/``$XDG_CONFIG_HOME`` nor
    ``%APPDATA%`` is set — callers surface an actionable error.
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        return Path(appdata) / _USER_CONFIG_APP_DIR / _USER_CONFIG_FILENAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / _USER_CONFIG_APP_DIR / _USER_CONFIG_FILENAME
    home = os.environ.get("HOME")
    if home:
        return Path(home) / ".config" / _USER_CONFIG_APP_DIR / _USER_CONFIG_FILENAME
    return None


def _load_user_config(path: Path) -> dict[str, Any]:
    """Load user-level config YAML. Missing file → empty dict; malformed
    → hard error (refuse to clobber on write)."""
    if not path.exists():
        return {}
    try:
        import yaml
    except ImportError:
        log("error: PyYAML is required for pack management; install with `pip install pyyaml`")
        raise SystemExit(2)
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text) if text.strip() else {}
    except Exception as exc:
        log(f"error: {path} is not valid YAML ({exc}); refusing to overwrite")
        raise SystemExit(2)
    if not isinstance(data, dict):
        log(f"error: {path} must be a mapping at top level (got {type(data).__name__})")
        raise SystemExit(2)
    return data


def _save_user_config(path: Path, data: dict[str, Any]) -> None:
    """Atomic write via temp + os.replace in the same directory."""
    try:
        import yaml
    except ImportError:
        log("error: PyYAML is required for pack management; install with `pip install pyyaml`")
        raise SystemExit(2)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _pack_main(path: Path | None, argv: list[str]) -> int:
    """Pack-management subcommand router.

    ``path`` may be ``None``; in that case the user-level config path is
    resolved from ``$HOME``/``$XDG_CONFIG_HOME``/``%APPDATA%``. Tests pass
    an explicit path to exercise the helpers without env-var fixtures.
    """
    parser = argparse.ArgumentParser(prog="anywhere-agents pack")
    sub = parser.add_subparsers(dest="action", required=True)

    p_add = sub.add_parser("add", help="Add a pack to user-level config")
    p_add.add_argument("source", help="Pack source (GitHub URL or registered name)")
    p_add.add_argument("--name", help="Override derived pack name (single-pack only)")
    p_add.add_argument("--ref", help="Pin to a specific ref (default: main)")
    p_add.add_argument(
        "--pack", action="append", default=[],
        help="Remote pack name to include; repeatable. Default: include all packs in the remote manifest.",
    )
    p_add.add_argument(
        "--type", choices=("skill", "rule"), default=None,
        help="Filter remote packs by slot: 'rule' = passive-only, 'skill' = include active too (default).",
    )

    p_remove = sub.add_parser("remove", help="Remove a pack from user-level config")
    p_remove.add_argument("name", help="Pack name to remove")

    p_list = sub.add_parser("list", help="List packs from user-level + current project")
    p_list.add_argument(
        "--drift", action="store_true",
        help="Read pack-lock entries and report packs whose upstream ref has moved.",
    )

    p_update = sub.add_parser(
        "update",
        help="Refresh a pack's user-config ref pin and re-run the project composer.",
    )
    p_update.add_argument("name", help="Pack name to update")
    p_update.add_argument(
        "--ref",
        help="New ref to pin. Default: keep the existing ref recorded in user-level config.",
    )

    args = parser.parse_args(argv)

    if path is None:
        path = _user_config_path()
    if path is None:
        log("error: cannot resolve user-level config home ($HOME / $XDG_CONFIG_HOME / %APPDATA% all unset)")
        return 2

    if args.action == "add":
        return _pack_add_v0_5(path, args)
    if args.action == "remove":
        return _pack_remove(path, args.name)
    if args.action == "list":
        if args.drift:
            return _pack_list_drift()
        return _pack_list(path)
    if args.action == "update":
        return _pack_update(path, args)
    return 2  # unreachable due to argparse required=True


def _derive_pack_name(source: str, override: str | None) -> str:
    if override:
        return override
    # Strip .git suffix, take last path segment.
    stem = source.rstrip("/")
    if stem.endswith(".git"):
        stem = stem[:-4]
    return stem.rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def _pack_add(path: Path, source: str, name: str | None, ref: str | None) -> int:
    # Credential-URL check — reject HTTP(S) userinfo (tokens baked into
    # URLs) AND SSH URLs with password field in userinfo.
    import re
    from urllib.parse import urlsplit
    if re.match(r"^https?://[^/@]+@", source):
        log("error: credentials in a URL are unsafe; use 'git@' SSH, 'gh auth login', or 'GITHUB_TOKEN' env")
        return 2
    if source.startswith("ssh://") or source.startswith("git+ssh://"):
        try:
            parsed = urlsplit(source)
        except ValueError as exc:
            log(f"error: source URL {source!r} is malformed ({exc})")
            return 2
        if parsed.password is not None:
            log("error: credentials in a URL are unsafe; use 'git@' SSH, 'gh auth login', or 'GITHUB_TOKEN' env")
            return 2

    data = _load_user_config(path)

    # Normalize legacy rule_packs: → packs: on first write per
    # pack-architecture.md:382. The legacy key is accepted for read but
    # any CLI write migrates to the unified name so future reads are
    # consistent. Without this migration, adding a new pack to a file
    # that contained only rule_packs would silently drop the existing
    # legacy entries from effective config resolution.
    if "packs" not in data and "rule_packs" in data:
        legacy = data.pop("rule_packs")
        if legacy is None:
            legacy = []
        if not isinstance(legacy, list):
            log(f"error: {path} has a malformed 'rule_packs' entry (not a list)")
            return 2
        data["packs"] = list(legacy)
    elif "packs" in data and "rule_packs" in data:
        # Both present — packs: wins; drop the legacy alias so it
        # doesn't confuse future readers.
        data.pop("rule_packs", None)

    pack_name = _derive_pack_name(source, name)
    entry: dict[str, Any] = {"name": pack_name, "source": source}
    if ref:
        entry["ref"] = ref

    packs = data.get("packs")
    if packs is None:
        # First-add default preservation: seed with agent-style + the user's pack.
        data["packs"] = [{"name": "agent-style"}, entry]
        log(f"Seeded new user-level config at {path} with default agent-style + {pack_name}")
    elif not isinstance(packs, list):
        log(f"error: {path} has a malformed 'packs' entry (not a list)")
        return 2
    else:
        # Replace existing entry with same name; else append.
        for i, existing in enumerate(packs):
            if isinstance(existing, dict) and existing.get("name") == pack_name:
                packs[i] = entry
                log(f"Updated {pack_name!r} in {path}")
                break
        else:
            packs.append(entry)
            log(f"Added {pack_name!r} to {path}")

    _save_user_config(path, data)
    return 0


# ----------------------------------------------------------------------
# v0.5.0 pack add: remote-manifest expansion
# ----------------------------------------------------------------------


def _load_or_create_user_config(path: Path) -> dict[str, Any]:
    """Return existing user-level config or a fresh empty dict.

    Mirrors :func:`_load_user_config` but tolerates a missing file
    (returns ``{}``) and migrates legacy ``rule_packs:`` to ``packs:``.
    """
    if not path.exists():
        return {}
    data = _load_user_config(path)
    if "packs" not in data and "rule_packs" in data:
        legacy = data.pop("rule_packs")
        if isinstance(legacy, list):
            data["packs"] = list(legacy)
        else:
            data["packs"] = []
    elif "packs" in data and "rule_packs" in data:
        data.pop("rule_packs", None)
    return data


def _write_user_config(path: Path, data: dict[str, Any]) -> None:
    """Atomic write helper for user-level config. Thin wrapper around
    :func:`_save_user_config` to match the helper name used in the
    Phase 9 plan."""
    _save_user_config(path, data)


def _pack_add_v0_5(user_config_path: Path, args) -> int:
    """Extended ``pack add``: fetches the remote pack.yaml, expands to
    one user-level selection per remote pack (filtered by ``--pack`` and
    ``--type``).

    The remote manifest may declare multiple packs (e.g. ``profile`` +
    ``paper-workflow`` + ``acad-skills`` in agent-pack). Each selected
    pack becomes a row in the user-level config keyed by name. Missing
    pack names print a warning to stderr and skip; ``--type rule``
    excludes packs that declare an ``active:`` slot (passive-only filter).
    """
    # Credential-URL safety check first (no network).
    import re
    from urllib.parse import urlsplit
    source = args.source
    if re.match(r"^https?://[^/@]+@", source):
        log("error: credentials in a URL are unsafe; use 'git@' SSH, 'gh auth login', or 'GITHUB_TOKEN' env")
        return 2
    if source.startswith("ssh://") or source.startswith("git+ssh://"):
        try:
            parsed = urlsplit(source)
        except ValueError as exc:
            log(f"error: source URL {source!r} is malformed ({exc})")
            return 2
        if parsed.password is not None:
            log("error: credentials in a URL are unsafe; use 'git@' SSH, 'gh auth login', or 'GITHUB_TOKEN' env")
            return 2

    from anywhere_agents.packs import auth, source_fetch, schema

    try:
        archive = source_fetch.fetch_pack(args.source, args.ref or "main")
    except auth.AuthChainExhaustedError as exc:
        log(f"error: could not fetch {args.source}@{args.ref or 'main'}: {exc}")
        return 2
    except source_fetch.PackLockDriftError as exc:
        log(f"error: pack-lock drift: {exc}")
        return 2

    try:
        remote_manifest = schema.parse_manifest(archive.archive_dir / "pack.yaml")
    except schema.ParseError as exc:
        log(f"error: remote pack.yaml is malformed: {exc}")
        return 2

    remote_packs = remote_manifest.get("packs", [])
    packs_by_name = {p["name"]: p for p in remote_packs}

    # Codex Round 2 M5: keep remote-lookup name and output name distinct.
    # Pre-fix, ``selected_names = [args.name]`` overrode for single-pack
    # manifests, but the remote lookup at the loop also keyed on
    # ``args.name``, so when ``args.name != remote_pack["name"]`` the
    # pack was reported missing and nothing was written. Use a list of
    # ``(remote_name, output_name)`` pairs so the lookup uses the real
    # remote name and the user-config row uses the override.
    if args.pack:
        selected_pairs: list[tuple[str, str]] = [(name, name) for name in args.pack]
        if args.name:
            log(
                f"warning: --name {args.name!r} ignored; "
                f"applies only when remote manifest has exactly 1 pack and no --pack filter"
            )
    elif args.name and len(remote_packs) == 1:
        only_remote_name = remote_packs[0]["name"]
        selected_pairs = [(only_remote_name, args.name)]
    else:
        if args.name:
            log(
                f"warning: --name {args.name!r} ignored; "
                f"applies only when remote manifest has exactly 1 pack and no --pack filter"
            )
        selected_pairs = [(p["name"], p["name"]) for p in remote_packs]

    user_config_existed = user_config_path.exists()
    user_config = _load_or_create_user_config(user_config_path)
    written_names: list[str] = []
    for remote_name, output_name in selected_pairs:
        pack = packs_by_name.get(remote_name)
        if pack is None:
            print(
                f"warning: pack {remote_name!r} not in remote manifest; skipping",
                file=sys.stderr,
            )
            continue
        if args.type == "rule" and pack.get("active"):
            # 'rule' filter excludes active packs (passive-only request).
            continue
        user_config.setdefault("packs", []).append({
            "name": output_name,
            "source": {"url": args.source, "ref": args.ref or "main"},
        })
        written_names.append(output_name)

    if not written_names and not user_config_existed:
        # Filter excluded everything AND there was no pre-existing config —
        # avoid creating an empty 'packs: []' file out of nothing.
        log("warning: no packs matched the filter; nothing written")
        return 0

    _write_user_config(user_config_path, user_config)
    if written_names:
        log(f"Added {len(written_names)} pack(s) to {user_config_path}: {', '.join(written_names)}")
    else:
        log("warning: no packs matched the filter; nothing written")
    return 0


# ----------------------------------------------------------------------
# v0.5.0 pack update: refresh a pinned ref + invoke project composer
# ----------------------------------------------------------------------


def _pack_update(user_config_path: Path, args) -> int:
    """Refresh a pack's user-level ref pin and trigger a project re-compose.

    Codex Round 2 H6 Option B (thin wheel): the PyPI CLI does NOT vendor
    the full compose stack. ``pack update`` rewrites the ref pin and
    delegates the actual update to the project-local composer at
    ``.agent-config/repo/scripts/compose_packs.py`` with
    ``ANYWHERE_AGENTS_UPDATE=apply`` set in the environment.
    """
    from anywhere_agents.packs import auth

    if not user_config_path.exists():
        log(
            f"error: pack {args.name!r} not in user config; use `pack add` first"
        )
        return 2
    user_config = _load_or_create_user_config(user_config_path)
    packs = user_config.get("packs", [])
    if not isinstance(packs, list):
        log(f"error: {user_config_path} has a malformed 'packs' entry (not a list)")
        return 2

    matching = [
        e for e in packs
        if isinstance(e, dict) and e.get("name") == args.name
    ]
    if not matching:
        log(
            f"error: pack {args.name!r} not in user config; use `pack add` first"
        )
        return 2
    entry = matching[0]
    source = entry.get("source")
    if isinstance(source, str):
        url = source
        existing_ref = entry.get("ref") or "main"
        # Promote string-source entries to dict-source on update so the
        # rewrite below has a place to land.
        entry["source"] = {"url": url, "ref": existing_ref}
        source = entry["source"]
    elif isinstance(source, dict):
        url = source.get("url") or source.get("repo")
        existing_ref = source.get("ref") or entry.get("ref") or "main"
        if not isinstance(url, str) or not url:
            log(
                f"error: pack {args.name!r} source has no 'url'/'repo' field"
            )
            return 2
    else:
        log(
            f"error: pack {args.name!r} source is missing or malformed"
        )
        return 2

    new_ref = args.ref or existing_ref

    # Codex Round 2 H3-B: pre-validate the URL so a credential-bearing
    # entry in user-config (legacy hand-edited file with
    # ``https://ghp_TOKEN@github.com/...``) is rejected before any
    # network call AND before the URL appears in any error message.
    try:
        auth.reject_credential_url(url, source_layer="user-config")
    except auth.CredentialURLError as exc:
        log(f"error: {exc}")
        return 2

    try:
        resolved_commit, _method = auth.resolve_ref_with_auth_chain(url, new_ref)
    except auth.CredentialURLError as exc:
        # Defense-in-depth (auth.resolve_ref_with_auth_chain also
        # validates) — keep the redacted CLI error path symmetric.
        log(f"error: {exc}")
        return 2
    except auth.AuthChainExhaustedError as exc:
        safe_url = auth.redact_url_userinfo(url)
        log(f"error: could not resolve {safe_url}@{new_ref}: {exc}")
        return 2
    log(f"resolved {auth.redact_url_userinfo(url)}@{new_ref} -> {resolved_commit[:7]}")

    source["ref"] = new_ref
    # Drop a top-level "ref" key if present so the dict-source ref is the
    # single source of truth.
    entry.pop("ref", None)
    _write_user_config(user_config_path, user_config)

    project_root = Path.cwd()
    composer = project_root / ".agent-config" / "repo" / "scripts" / "compose_packs.py"
    if not composer.exists():
        log(
            f"error: project-local composer not found at {composer}. Run "
            f"`bash .agent-config/bootstrap.sh` first to bootstrap."
        )
        return 2

    env = dict(os.environ, ANYWHERE_AGENTS_UPDATE="apply")
    result = subprocess.run(
        [sys.executable, str(composer)],
        cwd=str(project_root),
        env=env,
        check=False,
    )
    return result.returncode


# ----------------------------------------------------------------------
# v0.5.0 pack list --drift: read-only audit using auth-aware ls-remote
# ----------------------------------------------------------------------


def _read_all_pack_lock_entries() -> list[dict[str, Any]] | None:
    """Read every pack entry from ``.agent-config/pack-lock.json``.

    Returns a list of dicts each with at least ``name``, ``source_url``,
    ``requested_ref``, and ``resolved_commit``. Returns an empty list
    when no pack-lock exists or it has no packs. Returns ``None`` when
    the pack-lock file is present but unreadable / corrupt JSON, so the
    caller can distinguish "no data" from "error reading data".
    """
    lock_path = Path.cwd() / ".agent-config" / "pack-lock.json"
    if not lock_path.exists():
        return []
    import json
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"error: cannot read {lock_path}: {exc}")
        return None
    packs = data.get("packs") if isinstance(data, dict) else None
    if not isinstance(packs, dict):
        return []
    entries: list[dict[str, Any]] = []
    for name, body in packs.items():
        if not isinstance(body, dict):
            continue
        entries.append({
            "name": name,
            "source_url": body.get("source_url", ""),
            "requested_ref": body.get("requested_ref", ""),
            "resolved_commit": body.get("resolved_commit", ""),
        })
    return entries


def _pack_list_drift() -> int:
    """Read pack-lock + run auth-aware ls-remote per entry.

    Read-only audit: prints drifted packs (current → new commit). On
    ``auth.AuthChainExhaustedError`` for a single entry, prints a
    warning to stderr and continues with the remaining entries.
    """
    from anywhere_agents.packs import auth

    entries = _read_all_pack_lock_entries()
    if entries is None:
        # Pack-lock present but unreadable — surface as error rc=2 so
        # users do not interpret silent "no drift" as a clean state.
        return 2
    drifted: list[tuple[str, str, str]] = []
    for entry in entries:
        url = entry["source_url"]
        ref = entry["requested_ref"]
        if not url or not ref:
            continue
        # Codex Round 2 H3-B: pre-validate per entry so a
        # credential-bearing URL recorded in pack-lock (e.g., legacy
        # hand-edited lock from a pre-v0.5.0 release) is rejected for
        # this entry without leaking the token into the audit's stderr.
        try:
            auth.reject_credential_url(url, source_layer="pack-lock")
            new_commit, _ = auth.resolve_ref_with_auth_chain(url, ref)
        except auth.CredentialURLError as exc:
            print(
                f"  {entry['name']:20s} (unsafe source URL: {exc})",
                file=sys.stderr,
            )
            continue
        except auth.AuthChainExhaustedError as exc:
            print(
                f"  {entry['name']:20s} (could not resolve: {exc})",
                file=sys.stderr,
            )
            continue
        if new_commit != entry["resolved_commit"]:
            drifted.append(
                (entry["name"], entry["resolved_commit"], new_commit)
            )
    if not drifted:
        print("no drift")
        return 0
    for name, old, new in drifted:
        print(f"  {name:20s} {old[:7]} -> {new[:7]}")
    return 0


def _pack_remove(path: Path, name: str) -> int:
    if not path.exists():
        log(f"{path} does not exist; nothing to remove")
        return 0
    data = _load_user_config(path)

    # Normalize legacy rule_packs: → packs: (same migration as pack_add
    # so `pack remove` can edit a legacy-only file).
    if "packs" not in data and "rule_packs" in data:
        legacy = data.pop("rule_packs")
        if legacy is None:
            legacy = []
        if not isinstance(legacy, list):
            log(f"error: {path} has a malformed 'rule_packs' entry (not a list)")
            return 2
        data["packs"] = list(legacy)
    elif "packs" in data and "rule_packs" in data:
        data.pop("rule_packs", None)

    packs = data.get("packs", [])
    if not isinstance(packs, list):
        log(f"error: {path} has a malformed 'packs' entry (not a list)")
        return 2
    before = len(packs)
    data["packs"] = [
        p for p in packs
        if not (isinstance(p, dict) and p.get("name") == name)
        and not (isinstance(p, str) and p == name)
    ]
    if len(data["packs"]) == before:
        log(f"Pack {name!r} not found in {path}")
        return 0
    _save_user_config(path, data)
    log(f"Removed {name!r} from {path}")
    return 0


def _pack_list(path: Path) -> int:
    print(f"User-level config: {path}")
    if not path.exists():
        print("  (not created yet)")
    else:
        data = _load_user_config(path)
        packs = data.get("packs", [])
        if not packs:
            print("  (empty)")
        else:
            for p in packs:
                if isinstance(p, str):
                    print(f"  - {p}")
                elif isinstance(p, dict):
                    name = p.get("name", "<no name>")
                    ref = p.get("ref")
                    source = p.get("source")
                    line = f"  - {name}"
                    if ref:
                        line += f" (ref: {ref})"
                    if source:
                        line += f" <- {source}"
                    print(line)

    cwd_tracked = Path.cwd() / "agent-config.yaml"
    cwd_local = Path.cwd() / "agent-config.local.yaml"
    for label, p in [("Project-tracked", cwd_tracked), ("Project-local", cwd_local)]:
        if p.exists():
            print(f"\n{label}: {p}")
            data = _load_user_config(p)
            packs = data.get("packs") or data.get("rule_packs") or []
            if not packs:
                print("  (empty)")
            else:
                for entry in packs:
                    if isinstance(entry, str):
                        print(f"  - {entry}")
                    elif isinstance(entry, dict):
                        print(f"  - {entry.get('name', '<no name>')}")
    return 0


# ======================================================================
# uninstall subcommand: full project uninstall via composer engine
# ======================================================================


# Map uninstall engine outcomes to CLI exit codes per pack-architecture.md
# § "CLI contract for ``uninstall --all``".
_UNINSTALL_EXIT_CODES = {
    "clean": 0,
    "no-op": 0,
    "lock-timeout": 10,
    "drift": 20,
    "malformed-state": 30,
    "partial-cleanup": 40,
}


def _uninstall_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="anywhere-agents uninstall")
    parser.add_argument(
        "--all",
        action="store_true",
        required=True,
        help="Uninstall every aa-pack-owned output from the current project",
    )
    parser.parse_args(argv)

    project_root = Path.cwd().resolve()
    # The uninstall engine lives in the bootstrap-clone at
    # .agent-config/repo/scripts/packs/. Add its parent to sys.path so
    # `from packs import uninstall` resolves.
    packs_parent = project_root / ".agent-config" / "repo" / "scripts"
    if not packs_parent.exists():
        log(
            f"error: {packs_parent} not found; "
            "uninstall --all requires a project bootstrapped with aa v0.4.0+"
        )
        return 2

    sys.path.insert(0, str(packs_parent))
    try:
        from packs import uninstall as uninstall_mod  # type: ignore[import-not-found]
    except ImportError as exc:
        log(f"error: could not import uninstall engine: {exc}")
        return 2

    outcome = uninstall_mod.run_uninstall_all(project_root)

    # Report summary.
    log(f"status: {outcome.status}")
    if outcome.packs_removed:
        log(f"packs removed: {', '.join(outcome.packs_removed)}")
    if outcome.files_deleted:
        log(f"files deleted: {len(outcome.files_deleted)}")
    if outcome.owners_decremented:
        log(f"owners decremented: {len(outcome.owners_decremented)}")
    if outcome.drift_paths:
        log(f"drift: {len(outcome.drift_paths)} path(s) left in place")
        for p in outcome.drift_paths:
            log(f"  - {p}")
    if outcome.lock_holder_pid is not None:
        log(f"lock holder PID: {outcome.lock_holder_pid}")
    for detail in outcome.details:
        log(detail)

    return _UNINSTALL_EXIT_CODES.get(outcome.status, 40)


if __name__ == "__main__":
    sys.exit(main())
