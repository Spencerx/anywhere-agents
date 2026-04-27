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

    p_verify = sub.add_parser(
        "verify",
        help="Audit pack deployment state across user-level + project-level + pack-lock.",
    )
    p_verify.add_argument(
        "--fix",
        action="store_true",
        help="Write missing rule_packs: entries to agent-config.yaml for user-level-only packs.",
    )
    p_verify.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation when applying --fix.",
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
    if args.action == "verify":
        project_root = Path.cwd()
        if args.fix:
            return _pack_verify_fix(path, project_root, args)
        return _pack_verify(path, project_root, args)
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
        log(
            "note: this is user-level config only. To deploy in a project, "
            "add matching `rule_packs:` entries to agent-config.yaml and run "
            "`bash .agent-config/bootstrap.sh`. Run `anywhere-agents pack verify` "
            "to audit the deployment state, or `pack verify --fix` to wire it up."
        )
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


# ----------------------------------------------------------------------
# v0.5.x pack verify: deployment-state audit + opt-in --fix
# ----------------------------------------------------------------------

# State labels — kept stable for test assertions and tooling integration.
_VERIFY_STATE_DEPLOYED = "deployed"
_VERIFY_STATE_USER_ONLY = "user-level only"
_VERIFY_STATE_MISMATCH = "config mismatch"
_VERIFY_STATE_DECLARED = "declared, not bootstrapped"
_VERIFY_STATE_BROKEN = "broken state"
_VERIFY_STATE_LOCK_STALE = "lock schema stale"
_VERIFY_STATE_ORPHAN = "orphan"

_STATE_GLYPHS = {
    _VERIFY_STATE_DEPLOYED: "✅",       # ✅
    _VERIFY_STATE_USER_ONLY: "⚠",      # ⚠
    _VERIFY_STATE_MISMATCH: "\U0001f500",   # 🔀
    _VERIFY_STATE_DECLARED: "\U0001f6ab",   # 🚫
    _VERIFY_STATE_BROKEN: "❌",         # ❌
    _VERIFY_STATE_LOCK_STALE: "\U0001f4dc", # 📜
    _VERIFY_STATE_ORPHAN: "\U0001f47b",     # 👻
}

# Default project selections seeded when no durable config signal exists.
# Mirrors compose_packs.DEFAULT_V2_SELECTIONS so verify and bootstrap see
# the same baseline.
_DEFAULT_V2_SELECTIONS = ("agent-style", "aa-core-skills")
_BUNDLED_IDENTITY_URL = "bundled:aa"
_BUNDLED_IDENTITY_REF = "bundled"


class _VerifyParseError(Exception):
    """Raised when verify cannot parse a config or lock file at all."""


def _normalize_url(url) -> str:
    """Wrapper around the vendored ``normalize_pack_source_url`` helper."""
    if not isinstance(url, str) or not url:
        return ""
    from anywhere_agents.packs import source_fetch
    return source_fetch.normalize_pack_source_url(url)


def _identity_for_default_selection(name, project_root=None):
    """Resolve the upstream identity tuple for a bundled-default pack.

    The composer reads ``.agent-config/repo/bootstrap/packs.yaml`` and
    writes the resulting ``source.repo`` + ``source.ref`` into the lock.
    Verify must mirror that lookup so a default-bootstrapped project's
    project-side identity matches the lock-side identity (otherwise
    ``agent-style`` etc. always show as ``config mismatch``).

    When ``packs.yaml`` is unavailable (pre-bootstrap, or the verify
    flow runs outside a bootstrapped project), fall back to the
    synthetic ``(name, "bundled:aa", "bundled")`` identity. In that
    case there will also be no lock, so the fallback only ever feeds
    the "declared, not bootstrapped" path where identity equality is
    not exercised.
    """
    if project_root is not None:
        manifest = project_root / ".agent-config" / "repo" / "bootstrap" / "packs.yaml"
        # When the manifest is absent (pre-bootstrap, or running outside
        # a bootstrapped project) fall back to the synthetic bundled
        # identity below. When the manifest is present but malformed,
        # propagate the parse error so the verify CLI exits 2 instead
        # of silently mis-classifying default-seeded packs as
        # ``config mismatch``.
        if manifest.exists():
            data = _read_yaml_or_none(manifest) or {}
        else:
            data = {}
        packs = data.get("packs") if isinstance(data, dict) else None
        if isinstance(packs, list):
            for pack in packs:
                if not isinstance(pack, dict) or pack.get("name") != name:
                    continue
                source = pack.get("source")
                if isinstance(source, dict):
                    url = source.get("url") or source.get("repo") or ""
                    ref = source.get("ref") or pack.get("default-ref") or ""
                    if url:
                        return (name, _normalize_url(url), ref, url, ref)
                if isinstance(source, str) and source:
                    ref = pack.get("default-ref") or ""
                    return (name, _normalize_url(source), ref, source, ref)
                # Pack listed in packs.yaml without a remote source ->
                # truly bundled (e.g., aa-core-skills). The lock writer
                # records ``source_url: "bundled:aa"`` for these.
                break
    return (
        name,
        _BUNDLED_IDENTITY_URL,
        _BUNDLED_IDENTITY_REF,
        _BUNDLED_IDENTITY_URL,
        _BUNDLED_IDENTITY_REF,
    )


def _identity_for_user_entry(entry):
    """Return ``(name, normalized_url, ref, raw_url, raw_ref)`` for a
    user/project pack-list entry, or ``None`` if the entry has no name.
    Bundled-default names without a remote source get the synthetic
    bundled identity ``(name, "bundled:aa", "bundled")``.
    """
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    if not name:
        return None
    source = entry.get("source")
    if isinstance(source, dict):
        url = source.get("url") or source.get("repo") or ""
        ref = source.get("ref") or entry.get("ref") or ""
    elif isinstance(source, str):
        url = source
        ref = entry.get("ref") or ""
    else:
        if name in _DEFAULT_V2_SELECTIONS:
            return (
                name,
                _BUNDLED_IDENTITY_URL,
                _BUNDLED_IDENTITY_REF,
                _BUNDLED_IDENTITY_URL,
                _BUNDLED_IDENTITY_REF,
            )
        url = ""
        ref = entry.get("ref") or ""
    return (name, _normalize_url(url), ref, url, ref)


def _identity_for_lock_entry(name, body):
    """Build an identity tuple from a pack-lock ``packs.<name>`` body."""
    raw_url = body.get("source_url", "") or ""
    raw_ref = body.get("requested_ref", "") or ""
    if not raw_url and not raw_ref and name in _DEFAULT_V2_SELECTIONS:
        return (
            name,
            _BUNDLED_IDENTITY_URL,
            _BUNDLED_IDENTITY_REF,
            _BUNDLED_IDENTITY_URL,
            _BUNDLED_IDENTITY_REF,
        )
    return (name, _normalize_url(raw_url), raw_ref, raw_url, raw_ref)


def _read_yaml_or_none(path: Path):
    """Return ``None`` if file absent, ``{}`` if empty, dict otherwise.
    Raises :class:`_VerifyParseError` on malformed YAML or non-mapping
    top-level values.
    """
    if not path.exists():
        return None
    try:
        import yaml
    except ImportError:
        raise _VerifyParseError("PyYAML is required (install: `pip install pyyaml`)")
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return {}
        data = yaml.safe_load(text)
    except Exception as exc:
        raise _VerifyParseError(f"{path} is not valid YAML: {exc}")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise _VerifyParseError(
            f"{path}: top level must be a mapping (got {type(data).__name__})"
        )
    return data


def _load_user_observations(user_config_path):
    """Return a list of identity tuples from user-level config.

    Empty list when the file is absent or has no pack list. Raises
    :class:`_VerifyParseError` on parse failure (caller maps to exit 2).
    """
    if user_config_path is None:
        return []
    data = _read_yaml_or_none(user_config_path)
    if not data:
        return []
    packs = data.get("packs")
    if packs is None:
        packs = data.get("rule_packs")
    if packs is None:
        return []
    if not isinstance(packs, list):
        raise _VerifyParseError(
            f"{user_config_path}: 'packs' must be a list"
        )
    out = []
    for entry in packs:
        if isinstance(entry, str):
            entry = {"name": entry}
        ident = _identity_for_user_entry(entry)
        if ident is not None:
            out.append(ident)
    return out


def _load_project_observations(project_root: Path):
    """Return a list of project identity tuples after default-seeding.

    Mirrors :func:`compose_rule_packs.resolve_selections`'s behavior:

    - If neither ``agent-config.yaml`` nor ``agent-config.local.yaml``
      provides a ``rule_packs:`` signal, seed ``DEFAULT_V2_SELECTIONS``
      as bundled identities.
    - An explicit ``rule_packs: []`` (or null) in either file is a
      durable opt-out; default seeding is suppressed.
    - Otherwise, merge tracked + local with local-overrides-tracked.

    ``AGENT_CONFIG_PACKS`` env var is excluded; it never satisfies
    "deployed" for the verify classifier.
    """
    yaml_path = project_root / "agent-config.yaml"
    local_path = project_root / "agent-config.local.yaml"

    def _signal(path):
        data = _read_yaml_or_none(path)
        if data is None:
            return None  # file absent
        if "rule_packs" not in data:
            return None  # no signal
        raw = data["rule_packs"]
        if raw is None:
            return []  # explicit opt-out
        if not isinstance(raw, list):
            raise _VerifyParseError(
                f"{path}: 'rule_packs' must be a list"
            )
        return raw

    tracked = _signal(yaml_path)
    local = _signal(local_path)

    if tracked is None and local is None:
        return [
            _identity_for_default_selection(name, project_root)
            for name in _DEFAULT_V2_SELECTIONS
        ]

    # Group entries by name within each file so same-name duplicates in
    # one file (e.g., two ``profile`` rows in agent-config.yaml with
    # different refs) survive into the classifier and surface as
    # ``config mismatch``. Across files, local-overrides-tracked: any
    # name present in agent-config.local.yaml replaces the tracked
    # file's entries entirely for that name.
    def _group_by_name(entries):
        grouped: dict[str, list] = {}
        for entry in entries or []:
            if isinstance(entry, str):
                entry = {"name": entry}
            if isinstance(entry, dict) and "name" in entry:
                grouped.setdefault(entry["name"], []).append(entry)
        return grouped

    tracked_by_name = _group_by_name(tracked)
    local_by_name = _group_by_name(local)

    merged_lists: dict[str, list] = {}
    for name, rows in tracked_by_name.items():
        merged_lists[name] = list(rows)
    for name, rows in local_by_name.items():
        merged_lists[name] = list(rows)

    out = []
    for name in merged_lists:
        for entry in merged_lists[name]:
            # Sourceless project entries naming a bundled default (e.g.,
            # ``rule_packs: [{name: agent-style}]``) inherit the upstream
            # identity from packs.yaml so they compare equal to the lock
            # entry the composer writes. Sourceless non-default names
            # fall through to the standard helper (returns a sentinel
            # identity that will compare distinct from any remote
            # source).
            if (
                isinstance(entry, dict)
                and entry.get("name") in _DEFAULT_V2_SELECTIONS
                and entry.get("source") is None
            ):
                ident = _identity_for_default_selection(entry["name"], project_root)
            else:
                ident = _identity_for_user_entry(entry)
            if ident is not None:
                out.append(ident)
    return out


def _load_lock_observations(project_root: Path):
    """Return ``(identities, lock_health)`` from ``pack-lock.json``.

    ``lock_health`` maps name -> one of ``"ok"``, ``"schema_stale"``, or
    ``("broken", [missing_paths])``. Empty pack-lock returns
    ``([], {})``. Raises :class:`_VerifyParseError` on JSON parse
    failure (caller maps to exit 2).
    """
    lock_path = project_root / ".agent-config" / "pack-lock.json"
    if not lock_path.exists():
        return [], {}
    import json
    try:
        text = lock_path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise _VerifyParseError(f"{lock_path} is malformed: {exc}")
    if not isinstance(data, dict):
        raise _VerifyParseError(f"{lock_path}: top level must be a JSON object")
    packs = data.get("packs")
    if not isinstance(packs, dict):
        return [], {}
    identities = []
    health = {}
    for name, body in packs.items():
        if not isinstance(body, dict):
            continue
        ident = _identity_for_lock_entry(name, body)
        if ident is None:
            continue
        identities.append(ident)
        # Composer-written lock entries record outputs as
        # ``body["files"][i]["output_paths"]`` (nested per file entry,
        # see scripts/packs/dispatch.py and scripts/packs/state.py).
        # Treat the absence of ``files`` (or any malformed entry) as
        # ``schema_stale`` so a pre-v0.5 lock with a different shape
        # surfaces as repairable rather than corrupt.
        files = body.get("files")
        paths: list[str] = []
        stale = False
        if isinstance(files, list) and files:
            for file_entry in files:
                if not isinstance(file_entry, dict):
                    stale = True
                    break
                fe_paths = file_entry.get("output_paths")
                if (
                    not isinstance(fe_paths, list)
                    or not fe_paths
                    or not all(isinstance(p, str) and p for p in fe_paths)
                ):
                    stale = True
                    break
                paths.extend(fe_paths)
        elif "output_paths" in body:
            # Hand-edited or pre-composer lock that uses the flat shape.
            # Accept it but require the same per-path validation.
            fe_paths = body.get("output_paths")
            if (
                not isinstance(fe_paths, list)
                or not fe_paths
                or not all(isinstance(p, str) and p for p in fe_paths)
            ):
                stale = True
            else:
                paths = list(fe_paths)
        else:
            stale = True
        if stale or not paths:
            health[name] = "schema_stale"
            continue
        missing = []
        for p in paths:
            full = project_root / p
            if not full.exists():
                missing.append(p)
        if missing:
            health[name] = ("broken", missing)
        else:
            health[name] = "ok"
    return identities, health


def _classify_pack_states(user, project, lock, lock_health):
    """Apply the priority-order classifier from the plan.

    Returns a list of dicts (one per pack name) sorted by name, each
    with keys: ``name``, ``state``, ``u``, ``p``, ``l``, ``sole``,
    ``note``, ``missing_paths``.
    """
    by_name: dict[str, dict[str, Any]] = {}
    intra_layer_dupes: set[str] = set()

    def _add(name: str, layer_key: str, ident: tuple) -> None:
        slot = by_name.setdefault(
            name, {"u": None, "p": None, "l": None}
        )
        existing = slot[layer_key]
        if existing is not None and (existing[1], existing[2]) != (ident[1], ident[2]):
            # Same name appears twice in one layer with distinct
            # normalized identities — treat as a config mismatch even
            # if the other layers are absent (`pack add` can append
            # rows over time, and we want the user to see the dup).
            intra_layer_dupes.add(name)
        slot[layer_key] = ident

    for ident in user:
        _add(ident[0], "u", ident)
    for ident in project:
        _add(ident[0], "p", ident)
    for ident in lock:
        _add(ident[0], "l", ident)

    rows = []
    for name in sorted(by_name.keys()):
        layers = by_name[name]
        u = layers["u"]
        p = layers["p"]
        l = layers["l"]
        norm_set = set()
        for ident in (u, p, l):
            if ident is not None:
                norm_set.add((ident[1], ident[2]))
        is_mismatch = len(norm_set) > 1 or name in intra_layer_dupes

        lh = lock_health.get(name, "ok")
        lh_kind = lh[0] if isinstance(lh, tuple) else lh
        missing_paths = lh[1] if isinstance(lh, tuple) and len(lh) > 1 else []

        if is_mismatch:
            rows.append({
                "name": name,
                "state": _VERIFY_STATE_MISMATCH,
                "u": u, "p": p, "l": l,
                "sole": None,
                "note": (
                    f"lock {lh_kind}"
                    if lh_kind in ("schema_stale", "broken") else None
                ),
                "missing_paths": missing_paths if lh_kind == "broken" else [],
            })
            continue

        U = u is not None
        P = p is not None
        L = l is not None

        if not P:
            if U:
                state = _VERIFY_STATE_USER_ONLY
                note = None
                if L and lh_kind in ("schema_stale", "broken"):
                    note = "lock has missing/legacy output paths; run --fix, then bootstrap"
                sole = u
                rows.append({
                    "name": name, "state": state,
                    "u": u, "p": p, "l": l, "sole": sole,
                    "note": note, "missing_paths": [],
                })
            else:
                state = _VERIFY_STATE_ORPHAN
                note = None
                if lh_kind in ("schema_stale", "broken"):
                    note = f"lock {lh_kind}"
                sole = l
                rows.append({
                    "name": name, "state": state,
                    "u": u, "p": p, "l": l, "sole": sole,
                    "note": note, "missing_paths": missing_paths,
                })
            continue

        if L:
            if lh_kind == "schema_stale":
                state = _VERIFY_STATE_LOCK_STALE
            elif lh_kind == "broken":
                state = _VERIFY_STATE_BROKEN
            else:
                state = _VERIFY_STATE_DEPLOYED
        else:
            state = _VERIFY_STATE_DECLARED

        rows.append({
            "name": name, "state": state,
            "u": u, "p": p, "l": l, "sole": p,
            "note": None,
            "missing_paths": missing_paths if state == _VERIFY_STATE_BROKEN else [],
        })

    return rows


def _format_source(ident):
    """Format an identity for display. Redacts URL userinfo so a
    legacy hand-edited config containing ``https://TOKEN@host/repo``
    never leaks the token to stdout.
    """
    if ident is None:
        return ""
    _, _norm_url, ref, raw_url, raw_ref = ident
    if raw_url == _BUNDLED_IDENTITY_URL:
        return "bundled"
    if raw_url:
        try:
            from anywhere_agents.packs import auth as _pack_auth
            src = _pack_auth.redact_url_userinfo(raw_url)
        except Exception:
            src = raw_url
    else:
        src = ""
    out_ref = raw_ref or ref or ""
    if src and out_ref:
        return f"{src} @ {out_ref}"
    return src or out_ref


def _print_verify_table(rows, env_var_value, file=None):
    """Print the verify output table to stdout."""
    if file is None:
        file = sys.stdout
    if env_var_value:
        print(
            f"note: AGENT_CONFIG_PACKS={env_var_value} "
            "(transient project selection, not durable)",
            file=file,
        )
    if not rows:
        print(
            "No packs declared in user-level, project-level, or pack-lock.",
            file=file,
        )
        return

    name_w = max(4, max(len(r["name"]) for r in rows))
    print(
        f"{'PACK':<{name_w}}  STATUS                       SOURCE",
        file=file,
    )
    for r in rows:
        state = r["state"]
        glyph = _STATE_GLYPHS.get(state, "[?]")
        if state == _VERIFY_STATE_MISMATCH:
            parts = []
            for layer_name, key in (("user", "u"), ("project", "p"), ("lock", "l")):
                ident = r[key]
                if ident is not None:
                    parts.append(f"{layer_name}: {_format_source(ident)}")
            source = "; ".join(parts)
        else:
            source = _format_source(r.get("sole"))
        status = f"{glyph} {state}"
        print(f"{r['name']:<{name_w}}  {status:<27}  {source}", file=file)
        if r.get("missing_paths"):
            for path in r["missing_paths"][:3]:
                print(f"{'':<{name_w}}    missing: {path}", file=file)
            if len(r["missing_paths"]) > 3:
                print(
                    f"{'':<{name_w}}    ... and {len(r['missing_paths']) - 3} more",
                    file=file,
                )
        if r.get("note"):
            print(f"{'':<{name_w}}    note: {r['note']}", file=file)
        if state == _VERIFY_STATE_MISMATCH:
            print(
                f"{'':<{name_w}}    hint: edit agent-config.yaml, then rerun bootstrap",
                file=file,
            )
        elif state == _VERIFY_STATE_ORPHAN:
            print(
                f"{'':<{name_w}}    hint: restore a rule_packs: entry, OR run",
                file=file,
            )
            print(
                f"{'':<{name_w}}          `anywhere-agents uninstall --all` to remove",
                file=file,
            )
            print(
                f"{'':<{name_w}}          all aa-managed outputs. Do not use `pack remove`",
                file=file,
            )
            print(
                f"{'':<{name_w}}          (it edits user-level config only).",
                file=file,
            )

    not_deployed = sum(
        1 for r in rows if r["state"] != _VERIFY_STATE_DEPLOYED
    )
    if not_deployed > 0:
        print("", file=file)
        print(
            f"{not_deployed} of {len(rows)} pack(s) not deployed in this project.",
            file=file,
        )


def _verify_gather(user_config_path, project_root):
    """Collect (rows, env_var_value); raises :class:`_VerifyParseError` on parse error."""
    user = _load_user_observations(user_config_path)
    project = _load_project_observations(project_root)
    lock_idents, lock_health = _load_lock_observations(project_root)
    rows = _classify_pack_states(user, project, lock_idents, lock_health)
    env_var_value = os.environ.get("AGENT_CONFIG_PACKS", "")
    return rows, env_var_value


def _pack_verify(user_config_path, project_root, args):
    """Read-only audit. Exit 0 when every identity is deployed (or
    nothing to check), 1 when any identity is in a non-deployed state,
    2 when a config or lock file is unparseable.
    """
    try:
        rows, env_var_value = _verify_gather(user_config_path, project_root)
    except _VerifyParseError as exc:
        log(f"error: {exc}")
        return 2
    _print_verify_table(rows, env_var_value)

    bad = [r for r in rows if r["state"] != _VERIFY_STATE_DEPLOYED]
    if not bad:
        return 0
    if any(r["state"] == _VERIFY_STATE_USER_ONLY for r in bad):
        print("", file=sys.stdout)
        print(
            "To deploy: run `anywhere-agents pack verify --fix` (writes "
            "rule_packs: entries\nto agent-config.yaml) then "
            "`bash .agent-config/bootstrap.sh`.",
            file=sys.stdout,
        )
    return 1


def _user_only_rule_pack_entry(row):
    """Return the ``rule_packs:`` entry to write for a user-level-only row."""
    u = row.get("u")
    if u is None:
        return None
    name, _norm_url, _ref, raw_url, raw_ref = u
    entry: dict[str, Any] = {"name": name}
    if raw_url:
        source = {"url": raw_url}
        if raw_ref:
            source["ref"] = raw_ref
        entry["source"] = source
    return entry


def _pack_verify_fix(user_config_path, project_root, args):
    """Verify + repair user-level-only entries by writing matching
    ``rule_packs:`` blocks to ``agent-config.yaml``.

    - Atomic write (temp + ``os.replace``).
    - Refuses to overwrite a malformed YAML file (exits 2).
    - Holds the project repo lock for the read-classify-write sequence.
    - Never modifies ``pack-lock.json`` or generated output files.
    - Mismatch / orphan / broken / declared-not-bootstrapped are
      reported but not auto-repaired.
    """
    project_yaml = project_root / "agent-config.yaml"
    if project_yaml.exists():
        try:
            _read_yaml_or_none(project_yaml)
        except _VerifyParseError as exc:
            log(
                f"error: {exc} -- refusing to overwrite. "
                "Fix the YAML manually first."
            )
            return 2

    try:
        rows, env_var_value = _verify_gather(user_config_path, project_root)
    except _VerifyParseError as exc:
        log(f"error: {exc}")
        return 2

    user_only_rows = [
        r for r in rows if r["state"] == _VERIFY_STATE_USER_ONLY
    ]

    _print_verify_table(rows, env_var_value)

    if not user_only_rows:
        print("", file=sys.stdout)
        print(
            "--fix: nothing to repair (no user-level-only packs).",
            file=sys.stdout,
        )
        bad = [r for r in rows if r["state"] != _VERIFY_STATE_DEPLOYED]
        return 0 if not bad else 1

    # H3: reject credential-bearing URLs in user-level config BEFORE
    # printing them or writing them to project YAML, so a hand-edited
    # legacy entry like ``https://ghp_TOKEN@github.com/...`` cannot
    # leak the token to stdout or get persisted to agent-config.yaml.
    from anywhere_agents.packs import auth as _pack_auth_check
    for r in user_only_rows:
        u = r.get("u")
        raw_url = u[3] if (u is not None and len(u) >= 4) else ""
        if not raw_url:
            continue
        try:
            _pack_auth_check.reject_credential_url(
                raw_url, source_layer="user-level config"
            )
        except _pack_auth_check.CredentialURLError as exc:
            log(f"error: {exc}")
            return 2

    print("", file=sys.stdout)
    print("--fix planned changes:", file=sys.stdout)
    for r in user_only_rows:
        entry = _user_only_rule_pack_entry(r)
        if entry:
            # Display via the redacting formatter so even hypothetical
            # credential-bearing rows (which would have been rejected
            # above) cannot leak to stdout.
            display_src = _format_source(r.get("u"))
            print(
                f"  + add to {project_yaml}: name={entry['name']}, source={display_src}",
                file=sys.stdout,
            )

    if not args.yes:
        if sys.stdin.isatty():
            print("", file=sys.stdout)
            try:
                resp = input("Apply these changes? [y/N]: ").strip().lower()
            except EOFError:
                resp = ""
            if resp != "y":
                print(
                    "--fix: aborted by user; nothing written.",
                    file=sys.stdout,
                )
                return 1
        else:
            print("", file=sys.stdout)
            print(
                "--fix: --yes required in non-interactive mode; nothing written.",
                file=sys.stdout,
            )
            return 0

    # Repo lock: prefer the vendored copy in the PyPI package (always
    # available); fall back to the project-local clone as a sanity path
    # so a maintainer running directly off scripts/ still works.
    locks_mod = None
    try:
        from anywhere_agents.packs import locks as locks_mod  # type: ignore[import-not-found]
    except ImportError:
        locks_mod = None
    if locks_mod is None:
        packs_parent = project_root / ".agent-config" / "repo" / "scripts"
        if packs_parent.exists():
            sys.path.insert(0, str(packs_parent))
            try:
                from packs import locks as locks_mod  # type: ignore[import-not-found]
            except ImportError:
                locks_mod = None

    write_summary = {"written": 0, "rows_locked": [], "final_rows": []}

    def _apply_changes() -> int:
        # Re-gather under the lock so a concurrent bootstrap or second
        # `pack verify --fix` cannot interleave with the actual write:
        # the planned-changes table shown above is best-effort, the
        # final classification under lock is authoritative.
        try:
            rows_locked, _env_var_locked = _verify_gather(
                user_config_path, project_root
            )
        except _VerifyParseError as exc:
            log(f"error: {exc}")
            return 2
        write_summary["rows_locked"] = rows_locked
        user_only_locked = [
            r for r in rows_locked if r["state"] == _VERIFY_STATE_USER_ONLY
        ]
        # Re-validate credential URLs under the lock. A concurrent edit
        # to user-level config could have introduced a token URL after
        # the pre-lock check.
        for r in user_only_locked:
            u = r.get("u")
            raw_url = u[3] if (u is not None and len(u) >= 4) else ""
            if not raw_url:
                continue
            try:
                _pack_auth_check.reject_credential_url(
                    raw_url, source_layer="user-level config"
                )
            except _pack_auth_check.CredentialURLError as exc:
                log(f"error: {exc}")
                return 2
        try:
            import yaml
        except ImportError:
            log("error: PyYAML is required (install: `pip install pyyaml`)")
            return 2
        if project_yaml.exists():
            text = project_yaml.read_text(encoding="utf-8")
            data = yaml.safe_load(text) if text.strip() else {}
            if data is None:
                data = {}
            if not isinstance(data, dict):
                log(
                    f"error: {project_yaml}: top level must be a mapping; "
                    "refusing to overwrite."
                )
                return 2
        else:
            data = {}
        existing = data.get("rule_packs")
        if existing is None:
            existing = []
        elif not isinstance(existing, list):
            log(
                f"error: {project_yaml}: 'rule_packs' must be a list; "
                "refusing to overwrite."
            )
            return 2
        existing_names = {
            e.get("name") for e in existing if isinstance(e, dict)
        }
        written_count = 0
        for r in user_only_locked:
            entry = _user_only_rule_pack_entry(r)
            if entry and entry["name"] not in existing_names:
                existing.append(entry)
                existing_names.add(entry["name"])
                written_count += 1
        write_summary["written"] = written_count
        if written_count == 0:
            # No write happened; the pre-write locked state is also the
            # final state for the outer success/failure decision.
            write_summary["final_rows"] = rows_locked
            return 0
        data["rule_packs"] = existing
        out_text = yaml.safe_dump(
            data, sort_keys=False, default_flow_style=False
        )
        project_yaml.parent.mkdir(parents=True, exist_ok=True)
        tmp = project_yaml.with_name(project_yaml.name + ".tmp")
        tmp.write_text(out_text, encoding="utf-8")
        os.replace(str(tmp), str(project_yaml))
        # Re-classify post-write so the outer caller can detect rows
        # that ``--fix`` does not own (config mismatch, broken state,
        # orphan, lock schema stale, or new user-level only entries
        # added concurrently). Without this, writing ``profile`` would
        # report success even when an unrelated pack ``other`` is in
        # ``config mismatch`` under the same lock.
        try:
            final_rows, _final_env = _verify_gather(
                user_config_path, project_root
            )
        except _VerifyParseError as exc:
            log(f"error after write: {exc}")
            return 2
        write_summary["final_rows"] = final_rows
        return 0

    if locks_mod is None:
        # Fail closed: --fix writes durable project state and must not
        # race with a concurrent composer / bootstrap. The vendored
        # locks module is always present in the PyPI package, so this
        # branch only fires under unusual setups (e.g., a partially
        # broken install). Better to surface the problem than silently
        # write without serialization.
        log(
            "error: cannot acquire repo lock (locks module unavailable); "
            "refusing to write."
        )
        return 2
    try:
        lock_path = locks_mod.repo_lock_path(project_root)
        with locks_mod.acquire(lock_path):
            rc = _apply_changes()
    except Exception as exc:
        log(f"error: failed to acquire repo lock: {exc}")
        return 2

    if rc != 0:
        return rc

    written = write_summary["written"]
    final_rows = write_summary["final_rows"]
    # Classify the final locked state once. ``--fix`` only owns
    # user-level-only rows; any row in user_only / mismatch / broken /
    # orphan / lock_stale that survives the write needs further user
    # action (resolve mismatch by hand, re-run bootstrap to repair
    # broken outputs, clean up orphan via uninstall --all, etc.).
    final_problems = [
        r for r in final_rows
        if r["state"] in (
            _VERIFY_STATE_USER_ONLY,
            _VERIFY_STATE_MISMATCH,
            _VERIFY_STATE_BROKEN,
            _VERIFY_STATE_ORPHAN,
            _VERIFY_STATE_LOCK_STALE,
        )
    ]
    print("", file=sys.stdout)
    if written > 0:
        print(
            f"--fix: wrote {written} rule_packs "
            f"entry/entries to {project_yaml}",
            file=sys.stdout,
        )
        if final_problems:
            names = ", ".join(sorted({r["name"] for r in final_problems}))
            print(
                f"--fix: {len(final_problems)} pack(s) still need attention "
                f"({names}); re-run `pack verify` for details.",
                file=sys.stdout,
            )
            return 1
        print(
            "Now run `bash .agent-config/bootstrap.sh` to deploy.",
            file=sys.stdout,
        )
        return 0

    # Zero writes under the lock — distinguish "concurrent writer
    # already covered the planned changes" (success) from "state
    # changed into a different non-deployed state that --fix cannot
    # repair" (failure). Only all-deployed or declared-not-bootstrapped
    # (rule_packs already there, just not bootstrapped yet) is
    # genuinely up-to-date for what --fix is responsible for.
    if final_problems:
        names = ", ".join(sorted({r["name"] for r in final_problems}))
        print(
            f"--fix: state changed under lock; planned changes not applied. "
            f"{len(final_problems)} pack(s) still need attention ({names}). "
            f"Re-run `pack verify` to see the current state.",
            file=sys.stdout,
        )
        return 1
    declared = [
        r for r in final_rows if r["state"] == _VERIFY_STATE_DECLARED
    ]
    if declared:
        print(
            f"--fix: state changed under lock; rule_packs already had "
            f"matching entries (no write needed).",
            file=sys.stdout,
        )
        print(
            "Now run `bash .agent-config/bootstrap.sh` to deploy.",
            file=sys.stdout,
        )
        return 0
    print(
        f"--fix: state already up-to-date under lock; "
        f"no rule_packs entries written to {project_yaml}.",
        file=sys.stdout,
    )
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
