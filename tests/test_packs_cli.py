"""Tests for the anywhere-agents CLI subcommands (pack add/remove/list, uninstall).

Imports the PyPI-package CLI directly; exercises pack management by
setting the user-level config path via env vars and invoking main()
with argv lists.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Add both the PyPI package and the scripts dir to sys.path so the CLI
# imports work (cli.py + packs.uninstall).
sys.path.insert(0, str(ROOT / "packages" / "pypi"))
sys.path.insert(0, str(ROOT / "scripts"))

from anywhere_agents import cli  # noqa: E402


def _invoke(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Invoke cli.main(argv) with an optional env override; capture I/O."""
    original_env = dict(os.environ)
    if env is not None:
        os.environ.clear()
        os.environ.update(env)
    out_buf, err_buf = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = cli.main(argv)
    except SystemExit as exc:
        rc = int(exc.code) if exc.code is not None else 0
    finally:
        os.environ.clear()
        os.environ.update(original_env)
    return rc, out_buf.getvalue(), err_buf.getvalue()


class _TmpHome(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Force the user-level config into a temp dir via env vars.
        if sys.platform == "win32":
            self.env = {
                "APPDATA": str(self.root / "AppData"),
                "PATH": os.environ.get("PATH", ""),
            }
            self.expected_config = (
                self.root / "AppData" / "anywhere-agents" / "config.yaml"
            )
        else:
            self.env = {
                "HOME": str(self.root),
                "PATH": os.environ.get("PATH", ""),
            }
            self.expected_config = (
                self.root / ".config" / "anywhere-agents" / "config.yaml"
            )


class PackAddTests(_TmpHome):
    def test_first_add_seeds_agent_style_default(self) -> None:
        """First `pack add` on an empty user-level config auto-seeds
        agent-style as the default + the user's added pack. This
        prevents silent opt-out of the default rule pack."""
        rc, _, err = _invoke(
            ["pack", "add", "https://github.com/me/cool-pack", "--name", "cool"],
            env=self.env,
        )
        self.assertEqual(rc, 0, msg=err)
        self.assertTrue(self.expected_config.exists())

        import yaml
        data = yaml.safe_load(self.expected_config.read_text(encoding="utf-8"))
        names = [p["name"] for p in data["packs"]]
        self.assertIn("agent-style", names)
        self.assertIn("cool", names)

    def test_credential_url_rejected(self) -> None:
        rc, _, err = _invoke(
            ["pack", "add", "https://ghp_xyz@github.com/me/pack"],
            env=self.env,
        )
        self.assertEqual(rc, 2)
        self.assertIn("credentials", err)

    def test_second_add_appends(self) -> None:
        _invoke(["pack", "add", "https://github.com/me/a"], env=self.env)
        rc, _, _ = _invoke(
            ["pack", "add", "https://github.com/me/b"], env=self.env
        )
        self.assertEqual(rc, 0)
        import yaml
        data = yaml.safe_load(self.expected_config.read_text(encoding="utf-8"))
        names = [p["name"] for p in data["packs"]]
        self.assertEqual(sorted(names), ["a", "agent-style", "b"])

    def test_add_updates_existing_same_name(self) -> None:
        _invoke(
            ["pack", "add", "https://github.com/me/foo", "--ref", "v1"],
            env=self.env,
        )
        _invoke(
            ["pack", "add", "https://github.com/me/foo", "--ref", "v2"],
            env=self.env,
        )
        import yaml
        data = yaml.safe_load(self.expected_config.read_text(encoding="utf-8"))
        foo_entries = [p for p in data["packs"] if p.get("name") == "foo"]
        self.assertEqual(len(foo_entries), 1)
        self.assertEqual(foo_entries[0]["ref"], "v2")


class LegacyAliasMigrationTests(_TmpHome):
    """Regression for Round 1 Codex High #3: `pack add` / `pack remove`
    on a user-level file that contains only legacy `rule_packs:` must
    migrate the existing entries into `packs:` rather than silently
    shadow them. Without the migration, the composer's preference for
    `packs:` over `rule_packs:` drops the legacy selections."""

    def _prewrite_legacy(self) -> None:
        import yaml
        self.expected_config.parent.mkdir(parents=True, exist_ok=True)
        self.expected_config.write_text(
            yaml.safe_dump(
                {"rule_packs": [{"name": "legacy-pack"}]},
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def test_add_migrates_legacy(self) -> None:
        self._prewrite_legacy()
        rc, _, err = _invoke(
            ["pack", "add", "https://github.com/me/new-pack"], env=self.env,
        )
        self.assertEqual(rc, 0, msg=err)
        import yaml
        data = yaml.safe_load(self.expected_config.read_text(encoding="utf-8"))
        # rule_packs: gone; packs: contains legacy-pack + new-pack.
        self.assertNotIn("rule_packs", data)
        self.assertIn("packs", data)
        names = [p.get("name") if isinstance(p, dict) else p for p in data["packs"]]
        self.assertIn("legacy-pack", names)
        self.assertIn("new-pack", names)

    def test_remove_migrates_legacy(self) -> None:
        self._prewrite_legacy()
        rc, _, err = _invoke(
            ["pack", "remove", "legacy-pack"], env=self.env,
        )
        self.assertEqual(rc, 0, msg=err)
        import yaml
        data = yaml.safe_load(self.expected_config.read_text(encoding="utf-8"))
        self.assertNotIn("rule_packs", data)
        self.assertEqual(data.get("packs", []), [])


class PackRemoveTests(_TmpHome):
    def test_remove_existing(self) -> None:
        _invoke(
            ["pack", "add", "https://github.com/me/foo"], env=self.env,
        )
        rc, _, err = _invoke(["pack", "remove", "foo"], env=self.env)
        self.assertEqual(rc, 0, msg=err)
        import yaml
        data = yaml.safe_load(self.expected_config.read_text(encoding="utf-8"))
        names = [p.get("name") if isinstance(p, dict) else p for p in data["packs"]]
        self.assertNotIn("foo", names)

    def test_remove_nonexistent_is_noop(self) -> None:
        rc, _, _ = _invoke(["pack", "remove", "never-added"], env=self.env)
        self.assertEqual(rc, 0)


class PackListTests(_TmpHome):
    def test_list_empty(self) -> None:
        rc, out, _ = _invoke(["pack", "list"], env=self.env)
        self.assertEqual(rc, 0)
        self.assertIn("(not created yet)", out)

    def test_list_after_add(self) -> None:
        _invoke(
            ["pack", "add", "https://github.com/me/my-pack"], env=self.env
        )
        rc, out, _ = _invoke(["pack", "list"], env=self.env)
        self.assertEqual(rc, 0)
        self.assertIn("my-pack", out)
        self.assertIn("agent-style", out)  # seeded on first add


class BootstrapBackwardCompatTests(unittest.TestCase):
    def test_version_flag_unchanged(self) -> None:
        rc, out, _ = _invoke(["--version"])
        # argparse exits via SystemExit; _invoke catches it.
        self.assertEqual(rc, 0)
        self.assertIn("anywhere-agents", out)

    def test_dry_run_flag_unchanged(self) -> None:
        """The bootstrap path must still respect --dry-run without
        reaching out to the network or spawning subprocesses."""
        rc, _, err = _invoke(["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("Would fetch", err)


class UninstallWiringTests(_TmpHome):
    def test_uninstall_without_bootstrap_surfaces_hint(self) -> None:
        """`uninstall --all` in a directory without .agent-config/repo/
        must not crash — it exits 2 with an actionable hint."""
        # Change cwd to the tmp root (no .agent-config/repo/).
        import os as _os
        original = _os.getcwd()
        try:
            _os.chdir(self.root)
            rc, _, err = _invoke(["uninstall", "--all"], env=self.env)
        finally:
            _os.chdir(original)
        self.assertEqual(rc, 2)
        self.assertIn("requires a project bootstrapped", err)


if __name__ == "__main__":
    unittest.main()
