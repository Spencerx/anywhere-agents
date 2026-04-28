"""``kind: command`` handler — forward-compat slot in v0.4.0 (Phase 3).

Per pack-architecture.md:485-486 + plan PLAN-aa-v0.4.0.md:183, the
``command`` kind is a schema slot reserved for a future release. v0.4.0
parses the entry, emits the prescribed warning, and performs NO
filesystem writes or state mutations. Full install support (emitting a
standalone ``.claude/commands/<name>.md`` pointer unrelated to any
``kind: skill``) lands when a shipped pack first uses it.
"""
from __future__ import annotations

import sys
from typing import Any

from ..dispatch import DispatchContext

# The exact warning string v0.4.0 promises to emit. Integration tests
# assert on this verbatim; pack-architecture.md:486 calls it the
# "no-op + warn" slot and the test expects stability across releases.
WARNING_MESSAGE = (
    "no-op at v0.4.0; full support in a later release"
)


def handle_command(entry: dict[str, Any], ctx: DispatchContext) -> None:
    """Emit the no-op warning; do not write anything to disk or state.

    Specifically:
      - No ``stage_write`` / ``stage_delete`` call against the transaction.
      - No file-entry record added to pack-lock.
      - No entry added to project-state or user-state.

    Tests monkeypatch ``sys.stderr`` to verify the warning text and
    assert that no ``.claude/commands/<name>.md`` file materializes.
    """
    sys.stderr.write(
        f"warning: pack {ctx.pack_name!r} kind: command entry — "
        f"{WARNING_MESSAGE}\n"
    )
