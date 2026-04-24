"""Tests for scripts/packs/config.py (XDG paths + 4-layer merge + env var)."""
from __future__ import annotations

import sys
import tempfile
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from packs import config  # noqa: E402


class UserConfigHomeTests(unittest.TestCase):
    def test_posix_xdg_wins(self) -> None:
        env = {"XDG_CONFIG_HOME": "/tmp/xdg", "HOME": "/home/u"}
        if sys.platform == "win32":
            self.skipTest("POSIX-only")
        self.assertEqual(
            config.user_config_home(env),
            Path("/tmp/xdg") / "anywhere-agents",
        )

    def test_posix_home_fallback(self) -> None:
        env = {"HOME": "/home/u"}
        if sys.platform == "win32":
            self.skipTest("POSIX-only")
        self.assertEqual(
            config.user_config_home(env),
            Path("/home/u/.config/anywhere-agents"),
        )

    def test_windows_appdata(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows-only")
        env = {"APPDATA": "C:\\Users\\u\\AppData\\Roaming"}
        self.assertEqual(
            config.user_config_home(env),
            Path("C:\\Users\\u\\AppData\\Roaming") / "anywhere-agents",
        )

    def test_missing_returns_none(self) -> None:
        self.assertIsNone(config.user_config_home({}))


class LoadSaveConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_absent_file_returns_none(self) -> None:
        self.assertIsNone(config.load_config_file(self.root / "none.yaml"))

    def test_round_trip(self) -> None:
        path = self.root / "config.yaml"
        payload = {"packs": [{"name": "foo", "ref": "main"}]}
        config.save_config_file(path, payload)
        loaded = config.load_config_file(path)
        self.assertEqual(loaded, payload)

    def test_malformed_yaml_raises(self) -> None:
        path = self.root / "config.yaml"
        path.write_text("key: [unclosed\n", encoding="utf-8")
        with self.assertRaisesRegex(config.ConfigError, r"malformed YAML"):
            config.load_config_file(path)

    def test_non_mapping_top_level_rejects(self) -> None:
        path = self.root / "config.yaml"
        path.write_text("- just\n- a list\n", encoding="utf-8")
        with self.assertRaisesRegex(config.ConfigError, r"must be a mapping"):
            config.load_config_file(path)


class EnvVarGrammarTests(unittest.TestCase):
    def test_empty_env_returns_empty(self) -> None:
        add, sub = config.parse_env_var({})
        self.assertEqual(add, [])
        self.assertEqual(sub, [])

    def test_add_and_subtract(self) -> None:
        env = {"AGENT_CONFIG_PACKS": "foo,-bar,baz"}
        add, sub = config.parse_env_var(env)
        self.assertEqual(add, ["foo", "baz"])
        self.assertEqual(sub, ["bar"])

    def test_url_rejected(self) -> None:
        env = {"AGENT_CONFIG_PACKS": "https://example.com/foo"}
        with self.assertRaisesRegex(config.ConfigError, r"names-only"):
            config.parse_env_var(env)

    def test_legacy_env_accepted_with_warning(self) -> None:
        env = {"AGENT_CONFIG_RULE_PACKS": "foo,bar"}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            add, sub = config.parse_env_var(env)
        self.assertEqual(add, ["foo", "bar"])
        self.assertTrue(
            any(issubclass(w.category, DeprecationWarning) for w in caught)
        )

    def test_canonical_wins_over_legacy(self) -> None:
        env = {
            "AGENT_CONFIG_PACKS": "new",
            "AGENT_CONFIG_RULE_PACKS": "old",
        }
        add, _ = config.parse_env_var(env)
        self.assertEqual(add, ["new"])


class FourLayerMergeTests(unittest.TestCase):
    def test_no_signals_returns_default(self) -> None:
        result = config.resolve_selections(
            default_selections=[{"name": "agent-style"}],
        )
        self.assertEqual(result, [{"name": "agent-style"}])

    def test_no_signals_no_default_empty(self) -> None:
        result = config.resolve_selections()
        self.assertEqual(result, [])

    def test_user_level_sets_base(self) -> None:
        result = config.resolve_selections(
            user_level={"packs": [{"name": "a"}, {"name": "b"}]},
        )
        self.assertEqual(
            sorted(p["name"] for p in result), ["a", "b"]
        )

    def test_more_specific_overrides_same_name(self) -> None:
        result = config.resolve_selections(
            user_level={"packs": [{"name": "a", "ref": "user"}]},
            project_tracked={"packs": [{"name": "a", "ref": "tracked"}]},
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["ref"], "tracked")

    def test_explicit_empty_clears_earlier_layers(self) -> None:
        result = config.resolve_selections(
            user_level={"packs": [{"name": "a"}]},
            project_tracked={"packs": []},
        )
        self.assertEqual(result, [])

    def test_env_var_overlay_adds_after_clear(self) -> None:
        result = config.resolve_selections(
            user_level={"packs": [{"name": "a"}]},
            project_tracked={"packs": []},
            env_add=["new-from-env"],
        )
        self.assertEqual([p["name"] for p in result], ["new-from-env"])

    def test_env_subtract_removes_from_resolved(self) -> None:
        result = config.resolve_selections(
            user_level={"packs": [{"name": "a"}, {"name": "b"}]},
            env_subtract=["a"],
        )
        self.assertEqual([p["name"] for p in result], ["b"])

    def test_legacy_rule_packs_key_accepted(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            result = config.resolve_selections(
                user_level={"rule_packs": [{"name": "legacy-pack"}]},
            )
        self.assertEqual([p["name"] for p in result], ["legacy-pack"])

    def test_packs_wins_when_both_keys_present(self) -> None:
        result = config.resolve_selections(
            user_level={
                "packs": [{"name": "new"}],
                "rule_packs": [{"name": "old"}],
            },
        )
        self.assertEqual([p["name"] for p in result], ["new"])

    def test_short_form_name_normalized_to_dict(self) -> None:
        result = config.resolve_selections(
            user_level={"packs": ["a", "b"]},
        )
        self.assertEqual(result, [{"name": "a"}, {"name": "b"}])


if __name__ == "__main__":
    unittest.main()
