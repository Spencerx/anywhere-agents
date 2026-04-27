"""Tests for v0.5.0 pack-management CLI subcommands.

Covers:
- ``pack add`` remote-manifest expansion (one row per remote pack)
- ``pack add --pack <name>`` filter
- ``pack add --type rule`` excluding active packs
- ``pack add`` warning + skip for missing remote pack name
- ``pack update <name>`` thin-wheel flow (resolve ref + invoke composer)
- ``pack list --drift`` audit
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch, MagicMock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "pypi"))
sys.path.insert(0, str(ROOT / "scripts"))


def _build_archive(archive_dir: pathlib.Path) -> object:
    """Build a PackArchive pointing at the given directory."""
    from anywhere_agents.packs import source_fetch
    return source_fetch.PackArchive(
        url="https://github.com/yzhao062/agent-pack",
        ref="v0.1.0",
        resolved_commit="ab" * 20,
        method="anonymous",
        archive_dir=archive_dir,
        canonical_id="yzhao062/agent-pack",
        cache_key="abcd1234/" + "ab" * 20,
    )


_THREE_PACK_MANIFEST = (
    "version: 2\n"
    "packs:\n"
    "  - name: profile\n"
    "    description: x\n"
    "    source: {repo: https://github.com/yzhao062/agent-pack, ref: v0.1.0}\n"
    "    passive: [{files: [{from: docs/rule-pack.md, to: AGENTS.md}]}]\n"
    "  - name: paper-workflow\n"
    "    description: y\n"
    "    source: {repo: https://github.com/yzhao062/agent-pack, ref: v0.1.0}\n"
    "    passive: [{files: [{from: docs/paper-workflow.md, to: AGENTS.md}]}]\n"
    "  - name: acad-skills\n"
    "    description: z\n"
    "    source: {repo: https://github.com/yzhao062/agent-pack, ref: v0.1.0}\n"
    "    hosts: [claude-code]\n"
    "    active: [{kind: skill, required: false, files: [{from: skills/x/, to: .claude/skills/x/}]}]\n"
)


class TestPackAddRemoteManifest(unittest.TestCase):
    def test_pack_add_expands_multi_pack_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive_dir = pathlib.Path(d) / "archive"
            archive_dir.mkdir()
            (archive_dir / "pack.yaml").write_text(_THREE_PACK_MANIFEST)
            user_path = pathlib.Path(d) / "user-config.yaml"
            argv = [
                "pack", "add", "https://github.com/yzhao062/agent-pack",
                "--ref", "v0.1.0",
            ]
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import source_fetch
            with patch.object(
                source_fetch, "fetch_pack",
                return_value=_build_archive(archive_dir),
            ):
                _pack_main(user_path, argv[1:])
            written = yaml.safe_load(user_path.read_text())
            names = [e["name"] for e in written["packs"]]
            self.assertEqual(set(names), {"profile", "paper-workflow", "acad-skills"})
            for entry in written["packs"]:
                self.assertEqual(
                    entry["source"],
                    {"url": "https://github.com/yzhao062/agent-pack", "ref": "v0.1.0"},
                )

    def test_pack_add_with_pack_filter_only_writes_named_pack(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive_dir = pathlib.Path(d) / "archive"
            archive_dir.mkdir()
            (archive_dir / "pack.yaml").write_text(_THREE_PACK_MANIFEST)
            user_path = pathlib.Path(d) / "user-config.yaml"
            argv = [
                "pack", "add", "https://github.com/yzhao062/agent-pack",
                "--ref", "v0.1.0", "--pack", "profile",
            ]
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import source_fetch
            with patch.object(
                source_fetch, "fetch_pack",
                return_value=_build_archive(archive_dir),
            ):
                rc = _pack_main(user_path, argv[1:])
            self.assertEqual(rc, 0)
            written = yaml.safe_load(user_path.read_text())
            self.assertEqual(len(written["packs"]), 1)
            self.assertEqual(written["packs"][0]["name"], "profile")

    def test_pack_add_with_type_rule_skips_active_packs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive_dir = pathlib.Path(d) / "archive"
            archive_dir.mkdir()
            (archive_dir / "pack.yaml").write_text(_THREE_PACK_MANIFEST)
            user_path = pathlib.Path(d) / "user-config.yaml"
            argv = [
                "pack", "add", "https://github.com/yzhao062/agent-pack",
                "--ref", "v0.1.0", "--type", "rule",
            ]
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import source_fetch
            with patch.object(
                source_fetch, "fetch_pack",
                return_value=_build_archive(archive_dir),
            ):
                rc = _pack_main(user_path, argv[1:])
            self.assertEqual(rc, 0)
            written = yaml.safe_load(user_path.read_text())
            names = {e["name"] for e in written["packs"]}
            # acad-skills declares active:, so it's excluded by --type rule.
            self.assertEqual(names, {"profile", "paper-workflow"})

    def test_pack_add_handles_missing_remote_pack_warning(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive_dir = pathlib.Path(d) / "archive"
            archive_dir.mkdir()
            (archive_dir / "pack.yaml").write_text(_THREE_PACK_MANIFEST)
            user_path = pathlib.Path(d) / "user-config.yaml"
            argv = [
                "pack", "add", "https://github.com/yzhao062/agent-pack",
                "--ref", "v0.1.0", "--pack", "nonexistent",
            ]
            err_buf = io.StringIO()
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import source_fetch
            with patch.object(
                source_fetch, "fetch_pack",
                return_value=_build_archive(archive_dir),
            ):
                with redirect_stderr(err_buf):
                    rc = _pack_main(user_path, argv[1:])
            self.assertEqual(rc, 0)
            self.assertIn("nonexistent", err_buf.getvalue())
            self.assertIn("warning", err_buf.getvalue().lower())
            # No row written for the missing pack — and because no prior
            # config existed, no file should have been created at all
            # (avoid leaving an empty 'packs: []' artifact).
            self.assertFalse(user_path.exists())

    def test_pack_add_handles_auth_failure(self) -> None:
        """auth chain exhaustion -> clean error message + rc=2, no traceback."""
        with tempfile.TemporaryDirectory() as d:
            user_path = pathlib.Path(d) / "user-config.yaml"
            argv = [
                "pack", "add", "https://github.com/yzhao062/agent-pack",
                "--ref", "v0.1.0",
            ]
            err_buf = io.StringIO()
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import auth, source_fetch
            with patch.object(
                source_fetch, "fetch_pack",
                side_effect=auth.AuthChainExhaustedError(
                    "https://github.com/yzhao062/agent-pack", "v0.1.0", [],
                ),
            ):
                with redirect_stderr(err_buf):
                    rc = _pack_main(user_path, argv[1:])
            self.assertEqual(rc, 2)
            self.assertIn("could not fetch", err_buf.getvalue())
            # No user-config file should have been created from a failed fetch.
            self.assertFalse(user_path.exists())

    def test_pack_add_handles_malformed_remote_manifest(self) -> None:
        """schema.ParseError on remote pack.yaml -> rc=2 with clean message."""
        with tempfile.TemporaryDirectory() as d:
            archive_dir = pathlib.Path(d) / "archive"
            archive_dir.mkdir()
            # Write a manifest that will fail schema.parse_manifest().
            (archive_dir / "pack.yaml").write_text("not: a: valid: manifest:\n")
            user_path = pathlib.Path(d) / "user-config.yaml"
            argv = [
                "pack", "add", "https://github.com/yzhao062/agent-pack",
                "--ref", "v0.1.0",
            ]
            err_buf = io.StringIO()
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import schema, source_fetch
            with patch.object(
                source_fetch, "fetch_pack",
                return_value=_build_archive(archive_dir),
            ), patch.object(
                schema, "parse_manifest",
                side_effect=schema.ParseError("bad shape"),
            ):
                with redirect_stderr(err_buf):
                    rc = _pack_main(user_path, argv[1:])
            self.assertEqual(rc, 2)
            self.assertIn("malformed", err_buf.getvalue())

    def test_pack_add_with_name_on_multipack_warns_and_uses_original_names(self) -> None:
        """--name on a multi-pack manifest is silently dropped pre-fix; now warns."""
        with tempfile.TemporaryDirectory() as d:
            archive_dir = pathlib.Path(d) / "archive"
            archive_dir.mkdir()
            (archive_dir / "pack.yaml").write_text(_THREE_PACK_MANIFEST)
            user_path = pathlib.Path(d) / "user-config.yaml"
            argv = [
                "pack", "add", "https://github.com/yzhao062/agent-pack",
                "--ref", "v0.1.0", "--name", "custom-name",
            ]
            err_buf = io.StringIO()
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import source_fetch
            with patch.object(
                source_fetch, "fetch_pack",
                return_value=_build_archive(archive_dir),
            ):
                with redirect_stderr(err_buf):
                    rc = _pack_main(user_path, argv[1:])
            self.assertEqual(rc, 0)
            self.assertIn("--name", err_buf.getvalue())
            self.assertIn("ignored", err_buf.getvalue())
            written = yaml.safe_load(user_path.read_text())
            names = {e["name"] for e in written["packs"]}
            # Original names preserved; "custom-name" must NOT appear.
            self.assertEqual(names, {"profile", "paper-workflow", "acad-skills"})
            self.assertNotIn("custom-name", names)

    def test_pack_add_with_type_rule_filters_all_writes_nothing(self) -> None:
        """When --type rule filters out every pack and no prior config exists,
        do not create an empty packs: [] file."""
        single_active_only = (
            "version: 2\n"
            "packs:\n"
            "  - name: skills-only\n"
            "    description: q\n"
            "    source: {repo: https://github.com/yzhao062/agent-pack, ref: v0.1.0}\n"
            "    hosts: [claude-code]\n"
            "    active: [{kind: skill, required: false, files: [{from: s/, to: t/}]}]\n"
        )
        with tempfile.TemporaryDirectory() as d:
            archive_dir = pathlib.Path(d) / "archive"
            archive_dir.mkdir()
            (archive_dir / "pack.yaml").write_text(single_active_only)
            user_path = pathlib.Path(d) / "user-config.yaml"
            argv = [
                "pack", "add", "https://github.com/yzhao062/agent-pack",
                "--ref", "v0.1.0", "--type", "rule",
            ]
            err_buf = io.StringIO()
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import source_fetch
            with patch.object(
                source_fetch, "fetch_pack",
                return_value=_build_archive(archive_dir),
            ):
                with redirect_stderr(err_buf):
                    rc = _pack_main(user_path, argv[1:])
            self.assertEqual(rc, 0)
            self.assertIn("nothing written", err_buf.getvalue())
            # No file should have been created.
            self.assertFalse(user_path.exists())


class TestPackUpdate(unittest.TestCase):
    def _seed_user_config(self, root: pathlib.Path, ref: str = "v0.1.0") -> pathlib.Path:
        cfg = root / "user-config.yaml"
        cfg.write_text(yaml.safe_dump({
            "packs": [{
                "name": "profile",
                "source": {
                    "url": "https://github.com/yzhao062/agent-pack",
                    "ref": ref,
                },
            }],
        }))
        return cfg

    def test_pack_update_invokes_project_composer(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            cfg = self._seed_user_config(root)
            project = root / "project"
            composer = project / ".agent-config" / "repo" / "scripts" / "compose_packs.py"
            composer.parent.mkdir(parents=True, exist_ok=True)
            composer.write_text("# placeholder composer\n")
            argv = ["pack", "update", "profile", "--ref", "v0.2.0"]

            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import auth
            mock_proc = MagicMock(returncode=0)
            with patch("anywhere_agents.cli.os.getcwd", return_value=str(project)), \
                 patch("anywhere_agents.cli.Path") as mocked_path, \
                 patch.object(auth, "resolve_ref_with_auth_chain",
                              return_value=("cd" * 20, "anonymous")), \
                 patch("anywhere_agents.cli.subprocess.run",
                       return_value=mock_proc) as run_mock:
                # Make Path.cwd() return our project dir; everything else
                # passes through.
                mocked_path.cwd.return_value = project
                mocked_path.side_effect = lambda *a, **kw: pathlib.Path(*a, **kw)
                rc = _pack_main(cfg, argv[1:])
            self.assertEqual(rc, 0)
            # The user config should now record the new ref.
            written = yaml.safe_load(cfg.read_text())
            self.assertEqual(
                written["packs"][0]["source"]["ref"],
                "v0.2.0",
            )
            # subprocess.run was called with the composer path and the
            # ANYWHERE_AGENTS_UPDATE=apply env var.
            args, kwargs = run_mock.call_args
            self.assertIn("compose_packs.py", args[0][1])
            self.assertEqual(kwargs["env"]["ANYWHERE_AGENTS_UPDATE"], "apply")

    def test_pack_update_missing_pack_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            cfg = self._seed_user_config(root)
            argv = ["pack", "update", "ghost"]
            from anywhere_agents.cli import _pack_main
            err_buf = io.StringIO()
            with redirect_stderr(err_buf):
                rc = _pack_main(cfg, argv[1:])
            self.assertEqual(rc, 2)
            self.assertIn("ghost", err_buf.getvalue())

    def test_pack_update_missing_composer_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            cfg = self._seed_user_config(root)
            project = root / "project"
            project.mkdir()  # No .agent-config/repo/.
            argv = ["pack", "update", "profile", "--ref", "v0.2.0"]
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import auth
            err_buf = io.StringIO()
            with patch("anywhere_agents.cli.Path") as mocked_path, \
                 patch.object(auth, "resolve_ref_with_auth_chain",
                              return_value=("cd" * 20, "anonymous")):
                mocked_path.cwd.return_value = project
                mocked_path.side_effect = lambda *a, **kw: pathlib.Path(*a, **kw)
                with redirect_stderr(err_buf):
                    rc = _pack_main(cfg, argv[1:])
            self.assertEqual(rc, 2)
            self.assertIn("composer not found", err_buf.getvalue())

    def test_pack_update_resolve_failure_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            cfg = self._seed_user_config(root)
            pre_content = cfg.read_text()
            argv = ["pack", "update", "profile"]
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import auth
            err_buf = io.StringIO()
            with patch.object(
                auth, "resolve_ref_with_auth_chain",
                side_effect=auth.AuthChainExhaustedError(
                    "https://github.com/yzhao062/agent-pack", "v0.1.0", [],
                ),
            ):
                with redirect_stderr(err_buf):
                    rc = _pack_main(cfg, argv[1:])
            self.assertEqual(rc, 2)
            self.assertIn("could not resolve", err_buf.getvalue())
            # Locked-in invariant: a failed resolve must not modify the
            # user-config file. Guards against future refactors that move
            # _write_user_config before the resolve call.
            post_content = cfg.read_text()
            self.assertEqual(
                pre_content, post_content,
                "user config must not be modified when resolve fails",
            )


class TestPackListDrift(unittest.TestCase):
    def _seed_lock(self, project: pathlib.Path, recorded_commit: str) -> None:
        agent_dir = project / ".agent-config"
        agent_dir.mkdir(parents=True, exist_ok=True)
        lock = {
            "version": 1,
            "packs": {
                "profile": {
                    "source_url": "https://github.com/yzhao062/agent-pack",
                    "requested_ref": "v0.1.0",
                    "resolved_commit": recorded_commit,
                },
            },
        }
        (agent_dir / "pack-lock.json").write_text(json.dumps(lock))

    def test_pack_list_drift_reports_changed_commit(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d)
            self._seed_lock(project, recorded_commit="aa" * 20)
            argv = ["pack", "list", "--drift"]
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import auth
            out_buf, err_buf = io.StringIO(), io.StringIO()
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                with patch.object(
                    auth, "resolve_ref_with_auth_chain",
                    return_value=("bb" * 20, "anonymous"),
                ):
                    with redirect_stdout(out_buf), redirect_stderr(err_buf):
                        rc = _pack_main(pathlib.Path(d) / "x.yaml", argv[1:])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc, 0)
            self.assertIn("profile", out_buf.getvalue())
            self.assertIn("aaaaaaa", out_buf.getvalue())
            self.assertIn("bbbbbbb", out_buf.getvalue())

    def test_pack_list_drift_no_drift_when_commit_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d)
            self._seed_lock(project, recorded_commit="aa" * 20)
            argv = ["pack", "list", "--drift"]
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import auth
            out_buf = io.StringIO()
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                with patch.object(
                    auth, "resolve_ref_with_auth_chain",
                    return_value=("aa" * 20, "anonymous"),
                ):
                    with redirect_stdout(out_buf):
                        rc = _pack_main(pathlib.Path(d) / "x.yaml", argv[1:])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc, 0)
            self.assertIn("no drift", out_buf.getvalue())

    def test_pack_list_drift_continues_on_resolve_failure(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d)
            self._seed_lock(project, recorded_commit="aa" * 20)
            argv = ["pack", "list", "--drift"]
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import auth
            err_buf, out_buf = io.StringIO(), io.StringIO()
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                with patch.object(
                    auth, "resolve_ref_with_auth_chain",
                    side_effect=auth.AuthChainExhaustedError(
                        "https://github.com/yzhao062/agent-pack", "v0.1.0", [],
                    ),
                ):
                    with redirect_stdout(out_buf), redirect_stderr(err_buf):
                        rc = _pack_main(pathlib.Path(d) / "x.yaml", argv[1:])
            finally:
                os.chdir(cwd_before)
            # Resolve failure on a single entry does NOT crash the whole
            # subcommand; rc is 0 (read-only audit best-effort).
            self.assertEqual(rc, 0)
            self.assertIn("could not resolve", err_buf.getvalue())
            self.assertIn("profile", err_buf.getvalue())

    def test_pack_list_drift_corrupt_pack_lock_returns_2(self) -> None:
        """Corrupt pack-lock JSON must not be silently treated as 'no drift'."""
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d)
            agent_dir = project / ".agent-config"
            agent_dir.mkdir(parents=True, exist_ok=True)
            # Write malformed JSON that json.loads cannot parse.
            (agent_dir / "pack-lock.json").write_text("{ not valid json {{")
            argv = ["pack", "list", "--drift"]
            from anywhere_agents.cli import _pack_main
            err_buf, out_buf = io.StringIO(), io.StringIO()
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                with redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = _pack_main(pathlib.Path(d) / "x.yaml", argv[1:])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc, 2)
            # Must not lie about state.
            self.assertNotIn("no drift", out_buf.getvalue())
            self.assertIn("cannot read", err_buf.getvalue())


class TestPackUpdateCredentialURLRejected(unittest.TestCase):
    """Codex Round 2 H3-B: ``pack update`` must reject a
    credential-bearing URL recorded in user-config WITHOUT calling
    ``resolve_ref_with_auth_chain`` (which would leak the token into
    git argv) AND without echoing the raw token in stderr."""

    def test_pack_update_credential_url_rejected_before_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            cfg = root / "user-config.yaml"
            # Legacy hand-edited user-config with a token in the URL.
            cfg.write_text(yaml.safe_dump({
                "packs": [{
                    "name": "profile",
                    "source": {
                        "url": "https://ghp_legacy_secret@github.com/yzhao062/agent-pack",
                        "ref": "v0.1.0",
                    },
                }],
            }))
            argv = ["pack", "update", "profile", "--ref", "v0.2.0"]
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import auth
            err_buf = io.StringIO()
            with patch.object(
                auth, "resolve_ref_with_auth_chain",
            ) as resolve:
                with redirect_stderr(err_buf):
                    rc = _pack_main(cfg, argv[1:])
            # Reject path: rc=2, ``resolve_ref_with_auth_chain`` not
            # called, raw token absent from stderr.
            self.assertEqual(rc, 2)
            resolve.assert_not_called()
            self.assertNotIn("ghp_legacy_secret", err_buf.getvalue())
            self.assertIn("<redacted>", err_buf.getvalue())


class TestPackListDriftCredentialURLRejected(unittest.TestCase):
    """Codex Round 2 H3-B: ``pack list --drift`` must reject a
    credential-bearing URL recorded in pack-lock for a single entry
    WITHOUT calling ``resolve_ref_with_auth_chain`` AND continuing
    audit of the remaining entries (read-only audit best-effort)."""

    def test_pack_list_drift_credential_url_skips_entry(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d)
            agent_dir = project / ".agent-config"
            agent_dir.mkdir(parents=True, exist_ok=True)
            # pack-lock with one credential-URL entry plus one clean
            # entry; the audit should skip the credential entry and
            # still process the clean one.
            lock = {
                "version": 1,
                "packs": {
                    "tainted": {
                        "source_url": "https://ghp_legacy_secret@github.com/x/y",
                        "requested_ref": "v0.1.0",
                        "resolved_commit": "aa" * 20,
                    },
                    "profile": {
                        "source_url": "https://github.com/yzhao062/agent-pack",
                        "requested_ref": "v0.1.0",
                        "resolved_commit": "aa" * 20,
                    },
                },
            }
            (agent_dir / "pack-lock.json").write_text(json.dumps(lock))
            argv = ["pack", "list", "--drift"]
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import auth
            err_buf, out_buf = io.StringIO(), io.StringIO()
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                # Patch the resolver so we can assert how many times it
                # was actually called.
                with patch.object(
                    auth, "resolve_ref_with_auth_chain",
                    return_value=("aa" * 20, "anonymous"),
                ) as resolve:
                    with redirect_stdout(out_buf), redirect_stderr(err_buf):
                        rc = _pack_main(pathlib.Path(d) / "x.yaml", argv[1:])
            finally:
                os.chdir(cwd_before)
            # Audit returns rc=0 (best-effort).
            self.assertEqual(rc, 0)
            # ``resolve_ref_with_auth_chain`` called for the clean entry
            # only — the tainted one was rejected before resolve.
            self.assertEqual(resolve.call_count, 1)
            args, _kwargs = resolve.call_args
            self.assertEqual(args[0], "https://github.com/yzhao062/agent-pack")
            # Stderr names the rejected entry but does NOT echo the raw
            # token bytes.
            self.assertIn("tainted", err_buf.getvalue())
            self.assertIn("unsafe source URL", err_buf.getvalue())
            self.assertNotIn("ghp_legacy_secret", err_buf.getvalue())


class TestPackAddNameOnSinglePackManifest(unittest.TestCase):
    """Codex Round 2 M5: ``pack add --name custom`` on a single-pack
    remote manifest must write the user-config entry with
    ``name: "custom"`` (output name) while looking up the pack in the
    remote manifest under its ORIGINAL name. Pre-fix the lookup used
    ``args.name`` (the override), so the pack was 'missing' and nothing
    was written."""

    def test_pack_add_with_name_on_single_pack_renames_output(self) -> None:
        single_pack_manifest = (
            "version: 2\n"
            "packs:\n"
            "  - name: profile\n"
            "    description: x\n"
            "    source: {repo: https://github.com/yzhao062/agent-pack, ref: v0.1.0}\n"
            "    passive: [{files: [{from: docs/profile.md, to: AGENTS.md}]}]\n"
        )
        with tempfile.TemporaryDirectory() as d:
            archive_dir = pathlib.Path(d) / "archive"
            archive_dir.mkdir()
            (archive_dir / "pack.yaml").write_text(single_pack_manifest)
            user_path = pathlib.Path(d) / "user-config.yaml"
            argv = [
                "pack", "add", "https://github.com/yzhao062/agent-pack",
                "--ref", "v0.1.0", "--name", "custom",
            ]
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import source_fetch
            with patch.object(
                source_fetch, "fetch_pack",
                return_value=_build_archive(archive_dir),
            ):
                rc = _pack_main(user_path, argv[1:])
            self.assertEqual(rc, 0)
            written = yaml.safe_load(user_path.read_text())
            # Exactly one entry written, named ``custom`` (the override),
            # pointing at the source URL.
            self.assertEqual(len(written["packs"]), 1)
            entry = written["packs"][0]
            self.assertEqual(entry["name"], "custom")
            self.assertEqual(
                entry["source"],
                {
                    "url": "https://github.com/yzhao062/agent-pack",
                    "ref": "v0.1.0",
                },
            )


class TestVendorPacksOutput(unittest.TestCase):
    """Vendor script must produce LF-terminated files on every platform."""

    def test_vendor_output_has_no_crlf_on_any_platform(self) -> None:
        # Run vendor() to its real destination and inspect bytes. The
        # destination is gitignored as "AM" already; content equality
        # against scripts/packs/*.py guarantees byte-for-byte parity.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "vendor_packs",
            ROOT / "scripts" / "vendor-packs.py",
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.vendor()
        # Read each vendored file as bytes and assert no CR.
        dst = ROOT / "packages" / "pypi" / "anywhere_agents" / "packs"
        for name in ("__init__.py", "auth.py", "source_fetch.py", "schema.py"):
            content = (dst / name).read_bytes()
            self.assertNotIn(b"\r", content, f"{name} contains CR")


if __name__ == "__main__":
    unittest.main()
