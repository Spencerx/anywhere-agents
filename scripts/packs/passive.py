"""``passive:`` slot handler for v2 manifests (v0.4.0 Phase 3).

Passive entries inject content into ``AGENTS.md`` (or another project-
local text file). Semantically equivalent to the v0.3.x rule-pack
composer — fetch a document at a pinned git ref, validate against the
routing-marker grammar, and wrap the content in begin/end markers so
re-composition is byte-stable.

Phase 3 reuses the legacy fetch + validation primitives from
``scripts/compose_rule_packs.py`` rather than duplicating them, so the
v2 adapter is mostly plumbing from the new structured source shape
(``{repo, ref}``) back to the legacy URL + ref arguments.

Only the ``AGENTS.md`` target is supported in Phase 3 since that is the
only shipped use case (agent-style rule pack). Passive entries with
other target paths raise ``ValueError`` — they would require a different
begin/end marker scheme and are future work.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Import the legacy composer's fetch + compose helpers. scripts/ is on
# sys.path when this package is imported by compose_packs.py.
import compose_rule_packs as _legacy  # type: ignore[import-not-found]  # noqa: E402

from .dispatch import DispatchContext


def handle_passive_entry(
    entry: dict[str, Any],
    pack_manifest: dict[str, Any],
    ctx: DispatchContext,
    *,
    upstream_agents_md: str,
    cache_dir: Path,
    no_cache: bool,
) -> str:
    """Fetch + append passive content for one ``passive[i]`` entry.

    Returns the updated ``upstream_agents_md`` text (caller concatenates
    across passive entries and writes once at the end).

    Records a pack-lock entry per file mapping with ``role: passive``.
    """
    pack_name = pack_manifest["name"]
    source = pack_manifest.get("source")
    if source is None:
        raise ValueError(
            f"pack {pack_name!r} passive entry requires a pack-level "
            "'source' (repo + ref), but none was provided"
        )

    # v2 source is a dict {repo, ref, ...} — the schema parser already
    # rejects shapes that can't be resolved here. Use .get for defensive
    # access so a malformed pack surfaces a named ValueError instead of
    # a KeyError from the handler.
    repo_url = source.get("repo") if isinstance(source, dict) else source
    ref = (
        source.get("ref") if isinstance(source, dict)
        else pack_manifest.get("default-ref")
    )
    if not isinstance(ref, str) or not ref:
        raise ValueError(
            f"pack {pack_name!r} passive entry: 'source.ref' required"
        )

    files = entry["files"]
    composed = upstream_agents_md
    for mapping in files:
        from_path = mapping["from"]
        to_path = mapping["to"]
        if to_path != "AGENTS.md":
            raise ValueError(
                f"pack {pack_name!r} passive entry targets {to_path!r}; "
                "Phase 3 supports only 'AGENTS.md' as the target path"
            )

        fetch_url = _derive_raw_url(repo_url, ref, from_path)
        _legacy.validate_ref(pack_name, ref)
        # Cache filename matches the legacy composer's naming.
        from urllib.parse import quote
        cache_name = f"{quote(pack_name, safe='')}-{quote(ref, safe='')}.md"
        cache_md = cache_dir / cache_name
        content, sha = _legacy.fetch_rule_pack(
            fetch_url, ref, cache_md, no_cache
        )
        _legacy.validate_rule_pack(pack_name, content)

        # Append with begin/end markers directly so v2 output is
        # byte-identical to the v1 legacy composer's output for the
        # same pack content.
        begin = _legacy.BEGIN_FMT.format(name=pack_name, ref=ref, sha=sha)
        end = _legacy.END_FMT.format(name=pack_name)
        composed = (
            composed.rstrip()
            + "\n\n"
            + begin
            + "\n"
            + content.rstrip()
            + "\n"
            + end
            + "\n"
        )

        ctx.record_lock_file(
            {
                "role": "passive",
                "host": None,
                "source_path": from_path,
                "input_sha256": sha,
                "output_paths": [to_path],
                "output_scope": "project-local",
                "effective_update_policy": ctx.pack_update_policy,
            }
        )

    return composed


def _derive_raw_url(repo_url: str, ref: str, path: str) -> str:
    """Translate a structured source into a fetchable raw URL.

    Phase 3 supports ``github.com`` only. Other hosts would need a
    per-host mapping table that doesn't exist yet; callers fail with a
    clear error rather than guessing.
    """
    parsed = urlparse(repo_url)
    if parsed.netloc == "github.com":
        # https://github.com/<owner>/<repo> → https://raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>
        owner_repo = parsed.path.strip("/")
        if owner_repo.endswith(".git"):
            owner_repo = owner_repo[:-4]
        if owner_repo.count("/") != 1:
            raise ValueError(
                f"invalid github.com repo URL {repo_url!r}: expected "
                "'https://github.com/<owner>/<repo>' shape"
            )
        return f"https://raw.githubusercontent.com/{owner_repo}/{ref}/{path}"
    if parsed.netloc == "raw.githubusercontent.com":
        # Already a raw URL; assume path is <owner>/<repo>/<ref>/... and
        # the caller just wants us to substitute the ref and append path.
        # For safety we error out — the v2 manifest should use the
        # canonical github.com form, not a pre-resolved raw URL.
        raise ValueError(
            f"v2 manifest 'source.repo' should be 'https://github.com/...' "
            f"(got {repo_url!r}); raw URLs are derived by the composer"
        )
    raise ValueError(
        f"v2 passive fetch: unsupported source host {parsed.netloc!r}. "
        "Phase 3 ships github.com support only; other hosts need a "
        "host-specific URL mapping (future work)."
    )
