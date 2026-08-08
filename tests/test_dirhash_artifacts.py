"""anywhere-agents#18: the directory digest must ignore build artifacts,
must not ignore anything else, and must be unambiguous.

The reported failure: a stray ``__pycache__`` inside a deployed skill
directory changes that directory's hash, ``_build_prior_pack_outputs``
then refuses to walk it, every file in the skill falls to
``PRESTATE_UNMANAGED``, and the drift gate aborts the next compose with
``rc=1`` while naming files the user never edited.

Five properties, each with its own class below:

1. An artifact in the deployed tree must not change the hash.
2. A genuine edit to any other file must still change it, so the drift
   gate keeps protecting user work.
3. The write site and the read sites must agree on the same file set,
   or the recorded hash is one no read site can reproduce.
4. A lock recorded by a shipped pre-#18 version must still verify, since
   it used a different encoding and an unfiltered walk.
5. Distinct file sets must produce distinct digests.

Properties 4 and 5 came out of review. The encoding shipped before #18
separated fields with a NUL on the theory that paths cannot contain one.
Paths cannot; **file content can**, so one file holding ``b"x\\0b\\0y"``
and two files holding ``b"x"`` and ``b"y"`` hashed identically, and a
directory swapped between those shapes passed uninstall and was deleted.

The ignore-list is short because it authorizes deletion: anything on it
can be removed by ``pack uninstall`` with no drift warning. Two rounds of
review reproduced silent deletion of names that were on it and should not
have been, so :class:`ContentNotArtifactTests` is the guard that keeps
them off.
"""
from __future__ import annotations

import ast
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from packs import dirhash  # noqa: E402
from packs import source_fetch as sf  # noqa: E402
from packs import state  # noqa: E402
from packs import uninstall as uninstall_mod  # noqa: E402
from packs.handlers import skill as skill_mod  # noqa: E402
import compose_packs  # noqa: E402


def _write(path: Path, content: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode("utf-8")
    path.write_bytes(content)
    return path


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


CLEAN_TREE: dict[str, bytes] = {
    "SKILL.md": b"# implement-review\n\nPhase 1 ...\n",
    "scripts/health-check.py": b"#!/usr/bin/env python3\nprint('ok')\n",
    "scripts/health-check.sh": b"#!/usr/bin/env bash\nexec python3 \"$0.py\"\n",
    "references/checks.md": b"Check 8 scans the dispatch tail.\n",
}

# Regenerable tool state and OS droppings: losing these costs nothing, so
# they are the only things the gate is allowed to ignore.
ARTIFACTS: dict[str, bytes] = {
    "scripts/__pycache__/health-check.cpython-312.pyc": b"\x00\x0f\r\ropaque",
    "references/.pytest_cache/CACHEDIR.TAG": b"Signature: 8a477f597d28d\n",
    "sub/.mypy_cache/cache.json": b"{}\n",
    ".DS_Store": b"\x00\x00\x00\x01Bud1",
}

# Names that look disposable and are not. Each was excluded at some point
# during review and each was observed being deleted with no drift report.
CONTENT_LOOKALIKES: dict[str, bytes] = {
    "notes~": b"editor backup holding the only copy of an edit\n",
    "merge.orig": b"pre-merge original\n",
    "merge.rej": b"rejected hunk the user still needs\n",
    ".draft.swp": b"swap file for an unsaved buffer\n",
    ".git": b"gitdir: ../../.git/modules/vendor/dep\n",
    ".ipynb_checkpoints/draft-checkpoint.ipynb": b'{"cells": []}\n',
    "scripts/legacy.pyc": b"standalone bytecode outside __pycache__\n",
    "docs/.dir-sha256": b"dir-sha256:nested-not-a-marker\n",
    ".coverage": b"SQLite format 3\x00",
}

# A real ``.git/`` directory carries local-only objects, refs, stashes,
# and reflogs. Excluding the directory name let uninstall delete them.
GIT_DIR_STATE: dict[str, bytes] = {
    ".git/refs/stash": b"ref: stash\n",
    ".git/objects/ab/local-only": b"loose object only in this clone\n",
}


def _make_tree(root: Path, files: dict[str, bytes]) -> Path:
    for rel, content in files.items():
        _write(root / rel, content)
    return root


# =====================================================================
# The exclusion predicate
# =====================================================================


class IsArtifactTests(unittest.TestCase):
    def test_tool_caches_are_artifacts_at_any_depth(self) -> None:
        for rel in (
            "__pycache__/x.cpython-312.pyc",
            "scripts/__pycache__/health-check.cpython-312.pyc",
            "a/b/c/__pycache__/deep.pyc",
            ".pytest_cache/CACHEDIR.TAG",
            "sub/.mypy_cache/cache.json",
            "sub/.ruff_cache/content",
        ):
            with self.subTest(rel=rel):
                self.assertTrue(dirhash.is_artifact(rel))

    def test_os_droppings_are_artifacts(self) -> None:
        for rel in (".DS_Store", "sub/Thumbs.db", "desktop.ini"):
            with self.subTest(rel=rel):
                self.assertTrue(dirhash.is_artifact(rel))

    def test_pack_content_is_not_an_artifact(self) -> None:
        for rel in (
            "SKILL.md",
            "scripts/health-check.py",
            "scripts/dispatch-codex.ps1",
            "assets/diagram.png",
            "scripts/_speedup.pyd",
        ):
            with self.subTest(rel=rel):
                self.assertFalse(dirhash.is_artifact(rel))

    def test_exclusion_lists_stay_small(self) -> None:
        """A tripwire, not a style rule. Every name here authorizes
        deletion, so growth should be a deliberate reviewed act."""
        self.assertEqual(len(dirhash.EXCLUDED_DIR_NAMES), 4)
        self.assertEqual(len(dirhash.EXCLUDED_FILE_NAMES), 3)


class ContentNotArtifactTests(unittest.TestCase):
    """The ignore-list authorizes ``shutil.rmtree``, so recovery material
    and fetch-layer metadata must never appear on it. Every name below was
    excluded at some point and reproduced as silently deleted."""

    def test_every_lookalike_is_content(self) -> None:
        for rel in CONTENT_LOOKALIKES:
            with self.subTest(rel=rel):
                self.assertFalse(dirhash.is_artifact(rel))

    def test_git_is_content_as_both_file_and_directory(self) -> None:
        """The file form is a submodule or worktree gitdir pointer. The
        directory form holds local-only objects, refs, and stashes."""
        for rel in (
            ".git",
            "vendor/dep/.git",
            ".git/refs/stash",
            ".git/objects/ab/local-only",
            "vendor/dep/.git/HEAD",
        ):
            with self.subTest(rel=rel):
                self.assertFalse(dirhash.is_artifact(rel))

    def test_dir_sha256_is_content_in_a_deployed_tree(self) -> None:
        """It is product-owned only at an archive-cache root, which is
        handled at the copy site where that root is known."""
        for rel in (".dir-sha256", "docs/.dir-sha256", "a/b/.dir-sha256"):
            with self.subTest(rel=rel):
                self.assertFalse(dirhash.is_artifact(rel))

    def test_standalone_bytecode_is_content(self) -> None:
        """PEP 3147 puts generated bytecode in ``__pycache__``, which the
        directory rule covers. A ``.pyc`` elsewhere was placed on purpose."""
        for rel in ("scripts/legacy.pyc", "vendor/compiled.pyo"):
            with self.subTest(rel=rel):
                self.assertFalse(dirhash.is_artifact(rel))

    def test_coverage_data_is_content(self) -> None:
        """Coverage.py writes ``.coverage`` (and ``.coverage.<host>.<pid>``
        under ``--parallel``) into the working directory. Treating it as
        content means a stray one causes conservative drift: uninstall
        refuses, which is noisy but never destroys a measurement."""
        for rel in (".coverage", ".coverage.host.1234.5678"):
            with self.subTest(rel=rel):
                self.assertFalse(dirhash.is_artifact(rel))


# =====================================================================
# Property 5: distinct file sets, distinct digests
# =====================================================================


class V2EncodingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_nul_in_content_no_longer_collides(self) -> None:
        """The exact case the pre-#18 framing could not tell apart.

        One file holding ``x\\0b\\0y`` and two files holding ``x`` and
        ``y`` both encoded to ``a\\0x\\0b\\0y\\0``.
        """
        one = _make_tree(self.root / "one", {"a": b"x\x00b\x00y"})
        two = _make_tree(self.root / "two", {"a": b"x", "b": b"y"})
        self.assertNotEqual(dirhash.dir_sha256(one), dirhash.dir_sha256(two))

    def test_the_legacy_encoder_still_shows_the_collision(self) -> None:
        """Pins why the transition happened. If this ever stops
        colliding, the legacy encoder was changed and the compatibility
        branch no longer reproduces what old versions recorded."""
        one = _make_tree(self.root / "l-one", {"a": b"x\x00b\x00y"})
        two = _make_tree(self.root / "l-two", {"a": b"x", "b": b"y"})
        self.assertEqual(
            dirhash.legacy_dir_sha256(one), dirhash.legacy_dir_sha256(two)
        )

    def test_path_content_boundary_cannot_be_shifted(self) -> None:
        a = _make_tree(self.root / "a", {"x": b"y/z"})
        b = _make_tree(self.root / "b", {"x/y": b"z"})
        self.assertNotEqual(dirhash.dir_sha256(a), dirhash.dir_sha256(b))

    def test_length_prefixes_separate_adjacent_files(self) -> None:
        """Ambiguity across a file boundary rather than within one.

        Concatenated without framing both trees yield ``abcd``: one is
        ``a``+``b`` then ``c``+``d``, the other is ``a``+``bcd``.
        """
        a = _make_tree(self.root / "c", {"a": b"b", "c": b"d"})
        b = _make_tree(self.root / "d", {"a": b"bcd"})
        self.assertNotEqual(dirhash.dir_sha256(a), dirhash.dir_sha256(b))

    def test_digest_carries_the_domain_tag_and_v2_label(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        expected = hashlib.sha256(dirhash.DIRHASH_V2_DOMAIN).hexdigest()
        self.assertEqual(
            dirhash.dir_sha256(empty), f"{dirhash.V2_PREFIX}{expected}"
        )

    def test_labels_are_distinguishable(self) -> None:
        """The two forms must not share a namespace, or a recorded value
        cannot select the encoder that verifies it and every read has to
        try the colliding legacy encoder."""
        tree = _make_tree(self.root / "lbl", CLEAN_TREE)
        v2, legacy = dirhash.dir_sha256(tree), dirhash.legacy_dir_sha256(tree)
        self.assertTrue(v2.startswith(dirhash.V2_PREFIX))
        self.assertTrue(legacy.startswith(dirhash.LEGACY_PREFIX))
        self.assertFalse(v2.startswith(dirhash.LEGACY_PREFIX))
        self.assertTrue(dirhash.is_dir_digest(v2))
        self.assertTrue(dirhash.is_dir_digest(legacy))

    def test_backslash_filename_does_not_forge_a_separator(self) -> None:
        """On POSIX a backslash is a legal filename character, so joining
        the path into one string made a file named ``a\\b`` encode the
        same as file ``b`` inside directory ``a``. Reproduced on a real
        Linux filesystem before the component-wise framing landed.

        Simulated through the encoder here so the case is covered on
        Windows too, where such a filename cannot be created.
        """
        hasher_one = dirhash.new_dir_hasher()
        dirhash.update_dir_hasher(hasher_one, ("a\\b",), b"same")
        hasher_two = dirhash.new_dir_hasher()
        dirhash.update_dir_hasher(hasher_two, ("a", "b"), b"same")
        self.assertNotEqual(hasher_one.hexdigest(), hasher_two.hexdigest())

    def test_v2_and_legacy_differ_on_the_same_clean_tree(self) -> None:
        tree = _make_tree(self.root / "t", CLEAN_TREE)
        self.assertNotEqual(
            dirhash.dir_sha256(tree), dirhash.legacy_dir_sha256(tree)
        )

    def test_ordering_is_by_posix_relative_path(self) -> None:
        """Same content, built in different creation order, same digest."""
        a = self.root / "o1"
        for rel in ("z.md", "a.md", "m/n.md"):
            _write(a / rel, rel.encode())
        b = self.root / "o2"
        for rel in ("m/n.md", "a.md", "z.md"):
            _write(b / rel, rel.encode())
        self.assertEqual(dirhash.dir_sha256(a), dirhash.dir_sha256(b))


class LegacyEncodingTests(unittest.TestCase):
    """``legacy_dir_sha256`` has one job: reproduce, byte for byte, what a
    shipped pre-#18 version recorded. If it drifts, every lock written by
    an older release silently stops verifying."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tree = _make_tree(
            Path(self.tmp.name).resolve() / "skill",
            {**CLEAN_TREE, **ARTIFACTS},
        )

    def test_matches_the_pre_fix_implementation(self) -> None:
        hasher = hashlib.sha256()
        entries = sorted(
            (p for p in self.tree.rglob("*") if p.is_file()),
            key=lambda p: str(p.relative_to(self.tree)).replace("\\", "/"),
        )
        for child in entries:
            rel_posix = str(child.relative_to(self.tree)).replace("\\", "/")
            hasher.update(rel_posix.encode("utf-8"))
            hasher.update(b"\0")
            hasher.update(child.read_bytes())
            hasher.update(b"\0")
        self.assertEqual(
            f"dir-sha256:{hasher.hexdigest()}",
            dirhash.legacy_dir_sha256(self.tree),
        )

    def test_legacy_walk_is_unfiltered(self) -> None:
        """It must see artifacts, because the version that wrote it did."""
        before = dirhash.legacy_dir_sha256(self.tree)
        (self.tree / "scripts/__pycache__/health-check.cpython-312.pyc").unlink()
        self.assertNotEqual(before, dirhash.legacy_dir_sha256(self.tree))


# =====================================================================
# Properties 1 and 2
# =====================================================================


class DirSha256PropertyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.tree = _make_tree(self.root / "skill", CLEAN_TREE)

    def test_artifact_does_not_change_the_hash(self) -> None:
        before = dirhash.dir_sha256(self.tree)
        _make_tree(self.tree, ARTIFACTS)
        self.assertEqual(before, dirhash.dir_sha256(self.tree))

    def test_editing_a_shipped_file_still_changes_the_hash(self) -> None:
        before = dirhash.dir_sha256(self.tree)
        _write(self.tree / "SKILL.md", b"# implement-review\n\nlocally edited\n")
        self.assertNotEqual(before, dirhash.dir_sha256(self.tree))

    def test_adding_or_deleting_a_shipped_file_changes_the_hash(self) -> None:
        before = dirhash.dir_sha256(self.tree)
        _write(self.tree / "scripts/extra.py", b"print('new')\n")
        self.assertNotEqual(before, dirhash.dir_sha256(self.tree))
        (self.tree / "scripts/extra.py").unlink()
        self.assertEqual(before, dirhash.dir_sha256(self.tree))
        (self.tree / "references/checks.md").unlink()
        self.assertNotEqual(before, dirhash.dir_sha256(self.tree))

    def test_every_lookalike_changes_the_hash(self) -> None:
        before = dirhash.dir_sha256(self.tree)
        for rel, content in CONTENT_LOOKALIKES.items():
            with self.subTest(rel=rel):
                path = _write(self.tree / rel, content)
                self.assertNotEqual(before, dirhash.dir_sha256(self.tree))
                path.unlink()
                self.assertEqual(before, dirhash.dir_sha256(self.tree))

    def test_git_directory_state_changes_the_hash(self) -> None:
        before = dirhash.dir_sha256(self.tree)
        _make_tree(self.tree, GIT_DIR_STATE)
        self.assertNotEqual(before, dirhash.dir_sha256(self.tree))

    def test_directory_holding_only_artifacts_equals_empty(self) -> None:
        only = _make_tree(self.root / "only", ARTIFACTS)
        empty = self.root / "empty2"
        empty.mkdir()
        self.assertEqual(dirhash.dir_sha256(only), dirhash.dir_sha256(empty))


class MatchesAnyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tree = _make_tree(
            Path(self.tmp.name).resolve() / "skill", CLEAN_TREE
        )

    def test_matches_the_current_v2_digest(self) -> None:
        self.assertTrue(
            dirhash.matches_any(self.tree, {dirhash.dir_sha256(self.tree)})
        )

    def test_matches_a_legacy_digest_from_a_polluted_source(self) -> None:
        """Property 4. The old version copied the artifact and baked it
        into the recorded value, so the filtered digest cannot reproduce
        it and the legacy branch is the only thing that can."""
        _make_tree(self.tree, ARTIFACTS)
        legacy = dirhash.legacy_dir_sha256(self.tree)
        self.assertNotEqual(legacy, dirhash.dir_sha256(self.tree))
        self.assertTrue(dirhash.matches_any(self.tree, {legacy}))

    def test_rejects_an_edit_against_a_distinct_legacy_digest(self) -> None:
        """Pollute first, so the two forms genuinely differ and the
        legacy branch is exercised on its own terms."""
        _make_tree(self.tree, ARTIFACTS)
        legacy = dirhash.legacy_dir_sha256(self.tree)
        self.assertNotEqual(legacy, dirhash.dir_sha256(self.tree))
        _write(self.tree / "SKILL.md", b"# edited by the user\n")
        self.assertFalse(dirhash.matches_any(self.tree, {legacy}))

    def test_rejects_an_edited_tree_under_both_forms(self) -> None:
        known = {
            dirhash.dir_sha256(self.tree),
            dirhash.legacy_dir_sha256(self.tree),
        }
        _write(self.tree / "SKILL.md", b"# edited by the user\n")
        self.assertFalse(dirhash.matches_any(self.tree, known))

    def test_rejects_an_empty_known_set(self) -> None:
        self.assertFalse(dirhash.matches_any(self.tree, set()))

    def test_a_v2_lock_never_reaches_the_legacy_encoder(self) -> None:
        """The label is what bounds the collision. A tree whose *legacy*
        digest equals a recorded value must be refused when that value
        carries the v2 label, because only a v2 digest may verify it.
        """
        legacy_of_tree = dirhash.legacy_dir_sha256(self.tree)
        # Same hex, v2 label. Nothing should verify against it.
        relabelled = dirhash.V2_PREFIX + legacy_of_tree.split(":", 1)[1]
        self.assertFalse(dirhash.matches_any(self.tree, {relabelled}))
        # And the genuinely legacy-labelled form still verifies.
        self.assertTrue(dirhash.matches_any(self.tree, {legacy_of_tree}))

    def test_unlabelled_values_are_ignored(self) -> None:
        """A plain file sha in the known set must not be mistaken for a
        directory digest under either encoder."""
        plain = hashlib.sha256(b"whatever").hexdigest()
        self.assertFalse(dirhash.matches_any(self.tree, {plain}))

    def test_legacy_compatibility_is_a_floor_not_a_migration(self) -> None:
        """The documented limit. A legacy digest matches only while the
        tree is byte-identical to what the old version copied, artifact
        included. Once that artifact is rewritten or removed, neither
        form matches and one successful compose is required to
        re-record. The CHANGELOG must not claim otherwise.
        """
        pyc = _write(self.tree / "__pycache__/hook.pyc", b"ORIGINAL BYTECODE")
        legacy = dirhash.legacy_dir_sha256(self.tree)
        self.assertTrue(dirhash.matches_any(self.tree, {legacy}))

        pyc.write_bytes(b"REGENERATED BYTECODE")
        self.assertFalse(dirhash.matches_any(self.tree, {legacy}))

        pyc.unlink()
        self.assertFalse(dirhash.matches_any(self.tree, {legacy}))


# =====================================================================
# Property 3: the write site and the read sites agree
# =====================================================================


class _FakeTxn:
    def __init__(self) -> None:
        self.writes: dict[str, bytes] = {}

    def stage_write(self, path: Path, content: bytes) -> None:
        self.writes[str(path)] = content


class _FakeCtx:
    def __init__(self, pack_source_dir: Path) -> None:
        self.txn = _FakeTxn()
        self.pack_source_dir = pack_source_dir


class WriteReadRoundTripTests(unittest.TestCase):
    """``_stage_dir_copy`` records the hash; the read sites verify it.
    They walk different trees, so they must agree on the file set."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def _stage(self, src: Path, dst: Path, archive_root: Path | None = None):
        ctx = _FakeCtx(archive_root if archive_root is not None else src)
        recorded = skill_mod._stage_dir_copy(src, dst, ctx)
        for path_str, content in ctx.txn.writes.items():
            _write(Path(path_str), content)
        return recorded, ctx

    def _staged_rels(self, ctx: _FakeCtx, dst: Path) -> set[str]:
        return {Path(p).relative_to(dst).as_posix() for p in ctx.txn.writes}

    def test_recorded_hash_matches_the_deployed_tree(self) -> None:
        src = _make_tree(self.root / "src", CLEAN_TREE)
        dst = self.root / "dst"
        recorded, _ = self._stage(src, dst)
        self.assertEqual(recorded, compose_packs._dir_sha256(dst))
        self.assertEqual(recorded, uninstall_mod._dir_sha256(dst))

    def test_artifact_in_source_is_neither_hashed_nor_deployed(self) -> None:
        src = _make_tree(self.root / "src", {**CLEAN_TREE, **ARTIFACTS})
        dst = self.root / "dst"
        recorded, ctx = self._stage(src, dst)
        self.assertEqual(self._staged_rels(ctx, dst), set(CLEAN_TREE))
        self.assertEqual(recorded, compose_packs._dir_sha256(dst))

    def test_lookalike_files_in_source_are_deployed(self) -> None:
        """A pack may ship a file whose name merely looks disposable.
        Dropping it from the copy would be a silent content loss.

        The mapped directory sits *inside* the archive here, which is the
        normal shape: a manifest maps ``skills/demo``, not the cache slot
        itself. So a ``.git`` file under it is content, not the clone
        metadata that ``_is_archive_root_metadata`` removes. The
        archive-root case is covered separately below, and the two
        together are what pin the rules apart.
        """
        archive = self.root / "arc0"
        src = _make_tree(
            archive / "skills" / "demo", {**CLEAN_TREE, **CONTENT_LOOKALIKES}
        )
        _write(archive / ".dir-sha256", b"dir-sha256:slot\n")
        dst = self.root / "dst"
        recorded, ctx = self._stage(src, dst, archive_root=archive)
        staged = self._staged_rels(ctx, dst)
        self.assertEqual(staged, set(CLEAN_TREE) | set(CONTENT_LOOKALIKES))
        self.assertEqual(recorded, compose_packs._dir_sha256(dst))

    def test_archive_root_metadata_is_not_deployed(self) -> None:
        """When a manifest maps the archive root itself, the fetch
        layer's own ``.git/`` and ``.dir-sha256`` must stay behind."""
        src = _make_tree(self.root / "arc", {
            **CLEAN_TREE,
            ".git/HEAD": b"ref: refs/heads/main\n",
            ".git/objects/ab/x": b"loose\n",
            ".dir-sha256": b"dir-sha256:slot\n",
        })
        dst = self.root / "dst2"
        recorded, ctx = self._stage(src, dst, archive_root=src)
        self.assertEqual(self._staged_rels(ctx, dst), set(CLEAN_TREE))
        self.assertEqual(recorded, compose_packs._dir_sha256(dst))

    def test_nested_metadata_names_are_still_deployed(self) -> None:
        """Only the archive **root** is special. A ``.git`` file or a
        nested ``.dir-sha256`` under a mapped subdirectory is content,
        and the archive-cache hash counts it as content too."""
        arc = self.root / "arc2"
        src = _make_tree(arc / "skills" / "demo", {
            **CLEAN_TREE,
            ".git": b"gitdir: ../../.git/modules/x\n",
            "docs/.dir-sha256": b"nested\n",
        })
        _write(arc / ".dir-sha256", b"dir-sha256:slot\n")
        dst = self.root / "dst3"
        recorded, ctx = self._stage(src, dst, archive_root=arc)
        staged = self._staged_rels(ctx, dst)
        self.assertIn(".git", staged)
        self.assertIn("docs/.dir-sha256", staged)
        self.assertEqual(recorded, compose_packs._dir_sha256(dst))

    def test_recorded_hash_survives_pollution_of_the_deployed_tree(self) -> None:
        src = _make_tree(self.root / "src", CLEAN_TREE)
        dst = self.root / "dst"
        recorded, _ = self._stage(src, dst)
        _make_tree(dst, ARTIFACTS)
        self.assertTrue(dirhash.matches_any(dst, {recorded}))


# =====================================================================
# The reported failure, at the layer that produced rc=1
# =====================================================================


def _lock_with_skill_dir(dir_rel: str, input_sha: str) -> dict:
    return {
        "version": state.SCHEMA_VERSION,
        "packs": {
            "aa-core-skills": {
                "files": [
                    {
                        "role": "active-skill",
                        "host": "claude-code",
                        "source_path": "skills/implement-review",
                        "input_sha256": input_sha,
                        "output_paths": [dir_rel + "/"],
                        "output_scope": "project-local",
                        "effective_update_policy": "locked",
                    }
                ]
            }
        },
    }


class DriftGateWithArtifactTests(unittest.TestCase):
    SKILL_DIR = ".claude/skills/implement-review"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.tree = _make_tree(self.root / self.SKILL_DIR, CLEAN_TREE)
        self.recorded = dirhash.dir_sha256(self.tree)

    def _prior(self) -> dict[str, str]:
        return compose_packs._build_prior_pack_outputs(
            root=self.root,
            previous_pack_lock=_lock_with_skill_dir(
                self.SKILL_DIR, self.recorded
            ),
        )

    def _shipped_abs(self) -> list[str]:
        return [str((self.tree / rel).resolve()) for rel in CLEAN_TREE]

    def test_clean_tree_walks(self) -> None:
        prior = self._prior()
        for abs_path in self._shipped_abs():
            self.assertIn(abs_path, prior)

    def test_pycache_no_longer_drops_the_skill_to_unmanaged(self) -> None:
        """Before the fix this returned ``{}`` and the compose aborted."""
        _make_tree(self.tree, ARTIFACTS)
        prior = self._prior()
        for abs_path in self._shipped_abs():
            self.assertIn(abs_path, prior)

    def test_shipped_file_shas_are_correct_despite_the_artifact(self) -> None:
        _make_tree(self.tree, ARTIFACTS)
        prior = self._prior()
        for rel, content in CLEAN_TREE.items():
            self.assertEqual(
                prior[str((self.tree / rel).resolve())], _sha256(content)
            )

    def test_artifact_is_not_registered_as_a_pack_output(self) -> None:
        _make_tree(self.tree, ARTIFACTS)
        prior = self._prior()
        self.assertEqual(len(prior), len(CLEAN_TREE))
        for rel in ARTIFACTS:
            self.assertNotIn(str((self.tree / rel).resolve()), prior)

    def test_user_edit_still_falls_to_unmanaged(self) -> None:
        _write(self.tree / "SKILL.md", b"# locally edited by the user\n")
        self.assertEqual(self._prior(), {})

    def test_user_edit_plus_artifact_still_falls_to_unmanaged(self) -> None:
        _make_tree(self.tree, ARTIFACTS)
        _write(self.tree / "SKILL.md", b"# locally edited by the user\n")
        self.assertEqual(self._prior(), {})

    def test_each_lookalike_falls_to_unmanaged(self) -> None:
        for rel, content in CONTENT_LOOKALIKES.items():
            with self.subTest(rel=rel):
                path = _write(self.tree / rel, content)
                self.assertEqual(self._prior(), {})
                path.unlink()

    def test_git_directory_state_falls_to_unmanaged(self) -> None:
        _make_tree(self.tree, GIT_DIR_STATE)
        self.assertEqual(self._prior(), {})

    def test_legacy_lock_from_a_polluted_source_still_walks(self) -> None:
        _make_tree(self.tree, ARTIFACTS)
        self.recorded = dirhash.legacy_dir_sha256(self.tree)
        prior = self._prior()
        for abs_path in self._shipped_abs():
            self.assertIn(abs_path, prior)


# =====================================================================
# Second symptom: uninstall
# =====================================================================


class UninstallTests(unittest.TestCase):
    SKILL_DIR = ".claude/skills/implement-review"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.tree = _make_tree(self.root / self.SKILL_DIR, CLEAN_TREE)
        self.recorded = dirhash.dir_sha256(self.tree)

    def _delete(self, expected: str, root: Path | None = None):
        outcome = uninstall_mod.UninstallOutcome(status="ok")
        uninstall_mod._delete_project_local(
            self.SKILL_DIR, expected, root or self.root, outcome
        )
        return outcome

    def test_clean_tree_is_removed(self) -> None:
        outcome = self._delete(self.recorded)
        self.assertEqual(outcome.drift_paths, [])
        self.assertFalse(self.tree.exists())

    def test_pycache_no_longer_blocks_removal(self) -> None:
        _make_tree(self.tree, ARTIFACTS)
        outcome = self._delete(self.recorded)
        self.assertEqual(outcome.drift_paths, [])
        self.assertFalse(self.tree.exists())

    def test_user_edit_still_blocks_removal(self) -> None:
        _write(self.tree / "SKILL.md", b"# locally edited by the user\n")
        outcome = self._delete(self.recorded)
        self.assertEqual(outcome.drift_paths, [self.SKILL_DIR])
        self.assertTrue(self.tree.exists())

    def _case_root(self, rel: str) -> tuple[Path, Path, str]:
        case = self.root / f"case-{abs(hash(rel)) % 10**9}"
        tree = _make_tree(case / self.SKILL_DIR, CLEAN_TREE)
        return case, tree, dirhash.dir_sha256(tree)

    def test_each_lookalike_blocks_removal(self) -> None:
        """Every one of these was previously deleted with drift_paths==[]."""
        for rel, content in CONTENT_LOOKALIKES.items():
            with self.subTest(rel=rel):
                case, tree, recorded = self._case_root(rel)
                _write(tree / rel, content)
                outcome = self._delete(recorded, root=case)
                self.assertEqual(outcome.drift_paths, [self.SKILL_DIR])
                self.assertTrue(tree.exists())
                self.assertTrue((tree / rel).exists())

    def test_git_directory_state_blocks_removal(self) -> None:
        """A real ``.git/`` holds commits and stashes that exist nowhere
        else. Reproduced as deleted with no drift before the narrowing."""
        for rel, content in GIT_DIR_STATE.items():
            with self.subTest(rel=rel):
                case, tree, recorded = self._case_root(rel)
                _write(tree / rel, content)
                outcome = self._delete(recorded, root=case)
                self.assertEqual(outcome.drift_paths, [self.SKILL_DIR])
                self.assertTrue((tree / rel).exists())

    def test_swapped_tree_with_a_colliding_legacy_digest_is_refused(self) -> None:
        """The v2 encoding closes the deletion path the collision opened.

        Under the old framing, recording ``a=b"x", b=b"y"`` and then
        replacing it with ``a=b"x\\0b\\0y"`` passed with drift_paths==[]
        and the directory was removed.
        """
        case = self.root / "collide"
        tree = case / self.SKILL_DIR
        _write(tree / "a", b"x")
        _write(tree / "b", b"y")
        recorded = dirhash.dir_sha256(tree)
        (tree / "b").unlink()
        _write(tree / "a", b"x\x00b\x00y")
        outcome = self._delete(recorded, root=case)
        self.assertEqual(outcome.drift_paths, [self.SKILL_DIR])
        self.assertTrue(tree.exists())

    def test_legacy_digest_still_verifies_an_unchanged_tree(self) -> None:
        _make_tree(self.tree, ARTIFACTS)
        legacy = dirhash.legacy_dir_sha256(self.tree)
        outcome = self._delete(legacy)
        self.assertEqual(outcome.drift_paths, [])
        self.assertFalse(self.tree.exists())


# =====================================================================
# The archive cache keeps its own rule and its own encoder copy
# =====================================================================


class ArchiveCacheTests(unittest.TestCase):
    """``source_fetch`` must not adopt the deployed-tree predicate. Its
    hash guards the pack cache, where any file can be a manifest's
    ``files[].from`` source."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_marker_and_top_level_git_are_excluded(self) -> None:
        cache = _make_tree(self.root / "cache", {
            "SKILL.md": b"content\n",
            ".dir-sha256": b"dir-sha256:stale\n",
            ".git/HEAD": b"ref: refs/heads/main\n",
        })
        rels = {p.relative_to(cache).as_posix()
                for p in sf._iter_content_files(cache)}
        self.assertEqual(rels, {"SKILL.md"})

    def test_manifest_named_source_is_covered(self) -> None:
        """A hook mapping may read ``hook.py.orig`` and deploy it."""
        cache = _make_tree(self.root / "cache2", {
            "manifest.yaml": b"files:\n  - from: hook.py.orig\n",
            "hook.py.orig": b"ORIGINAL CACHE BYTES\n",
        })
        before = sf._compute_dir_sha256(cache)
        _write(cache / "hook.py.orig", b"MUTATED CACHE BYTES\n")
        self.assertNotEqual(before, sf._compute_dir_sha256(cache))

    def test_every_lookalike_is_covered(self) -> None:
        for rel, content in CONTENT_LOOKALIKES.items():
            if rel == ".git":
                continue  # top-level component, the pre-existing clone rule
            with self.subTest(rel=rel):
                cache = _make_tree(
                    self.root / f"c-{abs(hash(rel)) % 10**9}",
                    {"pack.yaml": b"x\n"},
                )
                before = sf._compute_dir_sha256(cache)
                _write(cache / rel, content)
                self.assertNotEqual(before, sf._compute_dir_sha256(cache))

    def test_nul_in_content_no_longer_collides(self) -> None:
        one = _make_tree(self.root / "one", {"a": b"x\x00b\x00y"})
        two = _make_tree(self.root / "two", {"a": b"x", "b": b"y"})
        self.assertNotEqual(
            sf._compute_dir_sha256(one), sf._compute_dir_sha256(two)
        )

    def test_encoder_matches_dirhash_on_the_same_file_set(self) -> None:
        """Pins the deliberate duplication. The two modules carry
        separate copies of the v2 framing because source_fetch is
        vendored as a standalone subset and must not import the
        deployed-tree predicate. Equal digests on an artifact-free tree
        prove the copies have not drifted."""
        tree = _make_tree(self.root / "same", CLEAN_TREE)
        self.assertEqual(dirhash.dir_sha256(tree), sf._compute_dir_sha256(tree))

    def test_source_fetch_does_not_import_the_shared_predicate(self) -> None:
        """A static guard against re-unifying the two filters.

        The check walks the AST rather than scanning text: a substring
        scan matched the module's own docstring, which names
        ``packs.dirhash.is_artifact`` in the warning telling the next
        editor not to make this substitution. That is the same
        documentation-echo shape as the check-8 false positive in
        anywhere-agents#17.
        """
        source = ROOT / "scripts" / "packs" / "source_fetch.py"
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module.split(".")[-1])
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                imported.update(a.name.split(".")[-1] for a in node.names)
        self.assertNotIn("dirhash", imported)


if __name__ == "__main__":
    unittest.main()
