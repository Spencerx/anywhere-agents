# SPDX-License-Identifier: MIT
"""Read a pack's pinned ref out of the bundled manifest.

Several suites assert behaviour that depends on what ``bootstrap/packs.yaml``
pins ``agent-style`` at. They used to spell that ref as a literal, in nine
code locations across four files, one of them under a comment saying it
matched the manifest. Nothing kept them in sync, so bumping the pin failed
seven tests that were not testing the pin, and the obvious repair (retype the
literal) rebuilds the same trap for the next bump.

Reading the manifest instead makes it the single source of truth. It also uses
the composer's own ``parse_manifest``, so these tests exercise the shipped
loader rather than a second YAML reader that could drift from it.
"""

from __future__ import annotations

import sys
from pathlib import Path


# tests/ -> repo root
ROOT = Path(__file__).resolve().parents[1]
BUNDLED_MANIFEST = ROOT / "bootstrap" / "packs.yaml"


def _ensure_scripts_importable() -> None:
    """Put ``scripts/`` on ``sys.path`` so ``packs.schema`` resolves.

    Callers set this up themselves, but at varying points in their module
    bodies, and this helper is often used at module scope above that setup.
    Doing it here makes the helper independent of import ordering rather than
    correct only for the files that happen to import it late.
    """
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)


def bundled_ref(pack_name: str) -> str:
    """Return the ``source.ref`` that the bundled manifest pins ``pack_name`` at.

    Raises
    ------
    AssertionError
        If the manifest has no such pack, or the pack declares no ref. Both
        are bugs in the manifest rather than conditions a test should skip
        over, and a silent default here would hide exactly the drift this
        helper exists to prevent.
    """
    _ensure_scripts_importable()
    from packs.schema import parse_manifest

    manifest = parse_manifest(BUNDLED_MANIFEST)
    for entry in manifest["packs"]:
        if entry.get("name") != pack_name:
            continue
        source = entry.get("source")
        ref = source.get("ref") if isinstance(source, dict) else None
        assert ref, f"{BUNDLED_MANIFEST}: pack {pack_name!r} declares no source.ref"
        return str(ref)
    raise AssertionError(f"{BUNDLED_MANIFEST}: no pack named {pack_name!r}")


def agent_style_ref() -> str:
    """The ref the bundled manifest pins the ``agent-style`` rule pack at."""
    return bundled_ref("agent-style")
