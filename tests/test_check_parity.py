"""Section-level AGENTS.md mirror parity for the pack-deployment banner bullet.

The full-file AGENTS.md byte-equality is BY-DESIGN per scripts/check-parity.sh
(both files must exist; full-file identity is not asserted). This test asserts
that the *section* added for the new pack-deployment banner check (item 7
under "How to populate each field") is byte-identical between the aa and ac
copies.

The cross-repo assertion only runs when the ac sibling clone is available
on the maintainer's local filesystem. CI environments have only one repo
on disk, so the cross-repo case is skipped there. The single-repo
"bullet exists in aa AGENTS.md" check always runs.
"""
from __future__ import annotations

import os
import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
AA_AGENTS = REPO_ROOT / "AGENTS.md"
AA_CLAUDE = REPO_ROOT / "CLAUDE.md"
AA_CODEX = REPO_ROOT / "agents" / "codex.md"

_BULLET_HEADING_RE = re.compile(r"^7\. \*\*Pack deployment\*\*", re.MULTILINE)
_NEXT_BOUNDARY_RE = re.compile(r"^(?:\d+\. \*\*|## )", re.MULTILINE)


def _extract_bullet(agents_md_path: Path) -> str | None:
    """Extract the item-7 ``Pack deployment`` bullet from an AGENTS.md file.

    Returns ``None`` when the anchor is not found, so the caller fails
    closed (a future restructure that loses item 7 surfaces as a test
    failure rather than a silent skip).
    """
    text = agents_md_path.read_text(encoding="utf-8")
    start_match = _BULLET_HEADING_RE.search(text)
    if not start_match:
        return None
    start = start_match.start()
    after = text[start_match.end():]
    end_match = _NEXT_BOUNDARY_RE.search(after)
    end = start_match.end() + end_match.start() if end_match else len(text)
    return text[start:end].rstrip()


def _candidate_ac_agents_md_paths() -> list[Path]:
    """Sibling lookup paths for an ac clone (maintainer-local, not CI)."""
    candidates: list[Path] = []
    env = os.environ.get("AGENT_CONFIG_REPO")
    if env:
        candidates.append(Path(env) / "AGENTS.md")
    parent = REPO_ROOT.parent
    candidates.append(parent / "agent-config" / "AGENTS.md")
    return candidates


def _find_ac_agents_md() -> Path | None:
    for c in _candidate_ac_agents_md_paths():
        if c.is_file():
            return c
    return None


def _generated_files_in_repo(repo_root: Path) -> list[tuple[str, Path]]:
    """Return ``(label, path)`` pairs for the three rule files that must
    carry a byte-identical copy of item 7: ``AGENTS.md`` (source of truth)
    and the two generated rule files derived from it. Used by the
    cross-variant drift assertion below.
    """
    return [
        ("AGENTS.md", repo_root / "AGENTS.md"),
        ("CLAUDE.md", repo_root / "CLAUDE.md"),
        ("agents/codex.md", repo_root / "agents" / "codex.md"),
    ]


class BannerBulletPresenceTests(unittest.TestCase):
    def test_bullet_exists_in_aa_agents(self) -> None:
        bullet = _extract_bullet(AA_AGENTS)
        self.assertIsNotNone(
            bullet,
            "expected '7. **Pack deployment**' anchor in aa AGENTS.md "
            "(check that section was not renumbered or removed)",
        )
        self.assertIn("user-level pack(s) not deployed", bullet)
        self.assertIn("normalize_pack_source_url", bullet)

    def test_bullet_has_four_steps(self) -> None:
        bullet = _extract_bullet(AA_AGENTS)
        self.assertIsNotNone(bullet)
        for step in ("a.", "b.", "c.", "d."):
            self.assertIn(step, bullet, f"expected step {step!r} in bullet")

    def test_bullet_byte_identical_across_aa_generated(self) -> None:
        """Item 7 must be byte-identical across aa AGENTS.md, CLAUDE.md,
        and agents/codex.md. The generator copies AGENTS.md content
        unchanged, so any drift means generation was skipped or hand-
        edited the generated files.
        """
        agents_bullet = _extract_bullet(AA_AGENTS)
        claude_bullet = _extract_bullet(AA_CLAUDE)
        codex_bullet = _extract_bullet(AA_CODEX)
        self.assertIsNotNone(agents_bullet, "aa AGENTS.md bullet missing")
        self.assertIsNotNone(claude_bullet, "aa CLAUDE.md bullet missing")
        self.assertIsNotNone(codex_bullet, "aa agents/codex.md bullet missing")
        self.assertEqual(
            agents_bullet,
            claude_bullet,
            "aa AGENTS.md ↔ CLAUDE.md item 7 drift; rerun "
            "`python scripts/generate_agent_configs.py`.",
        )
        self.assertEqual(
            agents_bullet,
            codex_bullet,
            "aa AGENTS.md ↔ agents/codex.md item 7 drift; rerun "
            "`python scripts/generate_agent_configs.py`.",
        )


class BannerBulletMirrorTests(unittest.TestCase):
    """Cross-repo: item 7 must be byte-identical across all six rule files
    (aa AGENTS.md, aa CLAUDE.md, aa agents/codex.md, ac AGENTS.md,
    ac CLAUDE.md, ac agents/codex.md).

    Only runs when an ac sibling clone is available locally. CI has only
    one repo on disk and skips this class; the maintainer's local runs
    and the pre-push smoke catch drift before it ships.
    """

    def setUp(self) -> None:
        ac_agents = _find_ac_agents_md()
        if ac_agents is None:
            self.skipTest(
                "ac sibling clone not found; set AGENT_CONFIG_REPO env or "
                "place the agent-config clone next to anywhere-agents"
            )
        self.ac_root = ac_agents.parent
        self.ac_agents = ac_agents

    def test_bullet_byte_identical_aa_ac(self) -> None:
        aa_bullet = _extract_bullet(AA_AGENTS)
        ac_bullet = _extract_bullet(self.ac_agents)
        self.assertIsNotNone(aa_bullet, "aa bullet anchor missing")
        self.assertIsNotNone(ac_bullet, "ac bullet anchor missing")
        self.assertEqual(
            aa_bullet,
            ac_bullet,
            "pack-deployment bullet drifted between aa and ac AGENTS.md; "
            "they must be byte-identical (mirror parity).",
        )

    def test_bullet_byte_identical_across_all_six(self) -> None:
        """All six rule files (aa+ac × AGENTS/CLAUDE/codex) must share
        byte-identical item 7. Round 2 Codex flagged that the prior
        test only covered AGENTS.md; the generated CLAUDE.md and
        agents/codex.md could drift silently. This test pins all six.
        """
        files = [("aa", label, p) for label, p in _generated_files_in_repo(REPO_ROOT)]
        files += [("ac", label, p) for label, p in _generated_files_in_repo(self.ac_root)]
        bullets = []
        for repo_label, file_label, path in files:
            bullet = _extract_bullet(path)
            self.assertIsNotNone(
                bullet,
                f"{repo_label}/{file_label}: item 7 anchor missing",
            )
            bullets.append((f"{repo_label}/{file_label}", bullet))
        # Pick a reference (aa/AGENTS.md is the source of truth) and
        # diff every other file against it. Reporting the first
        # mismatch is enough to flag the drift; the user can re-run
        # generate_agent_configs.py and the parity script to converge.
        ref_label, ref_bullet = bullets[0]
        for label, bullet in bullets[1:]:
            self.assertEqual(
                ref_bullet,
                bullet,
                f"item 7 drift: {label} differs from {ref_label}; "
                "rerun `python scripts/generate_agent_configs.py` in "
                "the affected repo and `bash scripts/check-parity.sh`.",
            )


if __name__ == "__main__":
    unittest.main()
