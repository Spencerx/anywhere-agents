"""passive.py archive adapter tests for v0.5.0.

Tests the new ``_resolve_passive_body`` helper that branches between the
v0.5.0 archive-backed read (when ``ctx.pack_source_dir`` points at a
``PackArchive`` directory containing the file at ``mapping['from']``) and
the v0.4.0 legacy raw-URL fetch (when ``ctx.pack_source_dir`` is None or
does not contain the requested file — preserves bundled-pack behavior
such as the agent-style rule pack).
"""
from __future__ import annotations

import hashlib
import pathlib
import sys
import tempfile
import unittest
import unittest.mock
from unittest.mock import MagicMock

ROOT = pathlib.Path(__file__).resolve().parents[1]
# Both paths needed: ``ROOT`` so ``from scripts.packs import passive``
# resolves, and ``ROOT/scripts`` so ``passive.py``'s internal
# ``import compose_rule_packs as _legacy`` resolves when the test is
# invoked directly (``python tests/test_packs_passive_archive.py``).
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from scripts.packs import passive  # noqa: E402


class TestPassiveArchiveAdapter(unittest.TestCase):
    """Cover the inline-source archive adapter and the legacy fallback."""

    def test_reads_from_archive_when_pack_source_dir_set(self) -> None:
        """When ``ctx.pack_source_dir`` contains the requested file,
        ``_resolve_passive_body`` reads its bytes directly from disk and
        hashes them — no network, no legacy URL derivation."""
        with tempfile.TemporaryDirectory() as d:
            archive = pathlib.Path(d)
            (archive / "doc.md").write_text("# hello\n", encoding="utf-8")
            ctx = MagicMock(pack_source_dir=archive)
            mapping = {"from": "doc.md", "to": "AGENTS.md"}
            pack_manifest = {
                "name": "test",
                "source": {"repo": "https://example/x", "ref": "v1"},
            }
            body, sha = passive._resolve_passive_body(
                mapping, pack_manifest, ctx,
                cache_dir=pathlib.Path(d), no_cache=False,
            )
            self.assertEqual(body, "# hello\n")
            self.assertEqual(len(sha), 64)
            self.assertEqual(
                sha,
                hashlib.sha256(b"# hello\n").hexdigest(),
            )

    def test_falls_back_to_legacy_when_pack_source_dir_none(self) -> None:
        """When ``ctx.pack_source_dir`` is ``None`` (v0.3.x bundled
        agent-style flow), the adapter delegates to the legacy raw-URL
        fetch path that already passes for the agent-style rule pack."""
        with tempfile.TemporaryDirectory() as d:
            ctx = MagicMock(pack_source_dir=None)
            mapping = {"from": "rule-pack.md", "to": "AGENTS.md"}
            pack_manifest = {
                "name": "agent-style",
                "source": {
                    "repo": "https://github.com/yzhao062/agent-style",
                    "ref": "v0.3.2",
                },
            }
            with unittest.mock.patch.object(
                passive._legacy, "fetch_rule_pack",
                return_value=("legacy content", "deadbeef" * 8),
            ) as legacy_fetch:
                with unittest.mock.patch.object(passive._legacy, "validate_ref"):
                    with unittest.mock.patch.object(
                        passive._legacy, "validate_rule_pack"
                    ):
                        body, sha = passive._resolve_passive_body(
                            mapping, pack_manifest, ctx,
                            cache_dir=pathlib.Path(d), no_cache=False,
                        )
            self.assertEqual(body, "legacy content")
            self.assertEqual(sha, "deadbeef" * 8)
            legacy_fetch.assert_called_once()

    def test_falls_back_to_legacy_when_archive_missing_file(self) -> None:
        """``ctx.pack_source_dir`` is set but does not contain
        ``mapping['from']``: this is the bundled-pack contract where
        ``pack_source_dir`` points at the consumer's
        ``.agent-config/repo/`` (used by active handlers) but the
        passive rule-pack body is upstream-only. The adapter must fall
        through to the legacy raw-URL fetch instead of crashing on a
        missing file."""
        with tempfile.TemporaryDirectory() as d:
            archive = pathlib.Path(d) / "repo_dir"
            archive.mkdir()
            # Note: no file at archive/rule-pack.md — file missing on disk.
            ctx = MagicMock(pack_source_dir=archive)
            mapping = {"from": "rule-pack.md", "to": "AGENTS.md"}
            pack_manifest = {
                "name": "agent-style",
                "source": {
                    "repo": "https://github.com/yzhao062/agent-style",
                    "ref": "v0.3.2",
                },
            }
            with unittest.mock.patch.object(
                passive._legacy, "fetch_rule_pack",
                return_value=("legacy content", "abc" + "d" * 61),
            ) as legacy_fetch:
                with unittest.mock.patch.object(passive._legacy, "validate_ref"):
                    with unittest.mock.patch.object(
                        passive._legacy, "validate_rule_pack"
                    ):
                        body, sha = passive._resolve_passive_body(
                            mapping, pack_manifest, ctx,
                            cache_dir=pathlib.Path(d), no_cache=False,
                        )
            self.assertEqual(body, "legacy content")
            legacy_fetch.assert_called_once()

    def test_archive_read_records_correct_sha256(self) -> None:
        """The hash returned for the archive-backed read is the sha256 of
        the file body bytes (UTF-8). Regression guard: the lock-file
        ``input_sha256`` field downstream depends on this being the
        content hash, not a path hash or the file's git blob hash."""
        with tempfile.TemporaryDirectory() as d:
            archive = pathlib.Path(d)
            text = "line1\nline2\n"
            (archive / "x.md").write_text(text, encoding="utf-8")
            ctx = MagicMock(pack_source_dir=archive)
            body, sha = passive._resolve_passive_body(
                {"from": "x.md", "to": "AGENTS.md"},
                {
                    "name": "agent-pack",
                    "source": {"repo": "https://example/y", "ref": "v0.1"},
                },
                ctx,
                cache_dir=pathlib.Path(d),
                no_cache=False,
            )
            self.assertEqual(body, text)
            self.assertEqual(
                sha,
                hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )


if __name__ == "__main__":
    unittest.main()
