"""``passive:`` slot handler for v2 manifests (v0.4.0 Phase 3 + v0.5.0 archive adapter).

Passive entries inject content into ``AGENTS.md`` (or another project-
local text file). Semantically equivalent to the v0.3.x rule-pack
composer — fetch a document at a pinned git ref, validate against the
routing-marker grammar, and wrap the content in begin/end markers so
re-composition is byte-stable.

Phase 3 reuses the legacy fetch + validation primitives from
``scripts/compose_rule_packs.py`` rather than duplicating them, so the
v2 adapter is mostly plumbing from the new structured source shape
(``{repo, ref}``) back to the legacy URL + ref arguments.

v0.5.0 (Codex Round 1 H4 / spec § D) adds an archive adapter: when the
composer fetched a remote pack via ``source_fetch.fetch_pack``, the
``DispatchContext.pack_source_dir`` points at the on-disk archive and
the passive body is read from there directly — no second network call.
The legacy raw-URL fetch path is preserved verbatim for v1 / bundled
packs (e.g., bundled agent-style) where ``pack_source_dir`` is ``None``.

Only the ``AGENTS.md`` target is supported in Phase 3 since that is the
only shipped use case (agent-style rule pack). Passive entries with
other target paths raise ``ValueError`` — they would require a different
begin/end marker scheme and are future work.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

# Import the legacy composer's fetch + compose helpers. scripts/ is on
# sys.path when this package is imported by compose_packs.py.
import compose_rule_packs as _legacy  # type: ignore[import-not-found]  # noqa: E402

from .dispatch import DispatchContext


def _resolve_passive_body(
    mapping: dict[str, Any],
    pack_manifest: dict[str, Any],
    ctx: DispatchContext,
    *,
    cache_dir: Path,
    no_cache: bool,
) -> tuple[str, str]:
    """Resolve one passive file mapping from archive or v0.4 raw URL path.

    When ``ctx.pack_source_dir`` is set AND contains the requested file
    (v0.5.0 inline-source path, populated by the composer from a
    ``PackArchive.archive_dir``), read ``mapping['from']`` from that
    directory and hash the bytes — no network. Otherwise fall back to
    v0.4.0 legacy raw-URL fetch (which preserves bundled agent-style
    behavior: the bundled composer sets ``pack_source_dir`` to
    ``<consumer>/.agent-config/repo/`` for active handlers, but bundled
    passive content lives upstream and is fetched fresh each run).

    The "exists" check (rather than a strict ``is not None`` test) lets
    a single bundled pack mix active entries (which read from
    ``pack_source_dir``) with passive entries (which fall through to the
    legacy fetch path when the rule-pack body is not co-located with the
    consumer's repo cache).

    Returns ``(body, sha256)`` where ``sha256`` is the hex digest of the
    UTF-8 encoded body. Caller wraps body in begin/end markers and
    records ``input_sha256`` in the pack-lock entry.
    """
    from_path = mapping["from"]

    pack_source_dir = getattr(ctx, "pack_source_dir", None)
    if pack_source_dir is not None:
        archive_file = pack_source_dir / from_path
        if archive_file.exists():
            # v0.5.0 archive-backed read. Bytes hashed are exactly what
            # we write into AGENTS.md, so the lock-file input_sha256
            # stays a tamper-evident witness of the consumed content.
            body = archive_file.read_text(encoding="utf-8")
            sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
            return body, sha
        # File not present in pack_source_dir → fall through to the
        # legacy raw-URL fetch path. This is the v0.4.0 bundled-pack
        # contract: agent-style's pack_source_dir is the consumer's
        # `.agent-config/repo/` (used by active handlers) but its
        # passive rule-pack body is upstream.

    # v0.4.0 legacy raw-URL path (bundled agent-style etc.).
    pack_name = pack_manifest["name"]
    source = pack_manifest.get("source")
    repo_url = source.get("repo") if isinstance(source, dict) else source
    ref = (
        source.get("ref") if isinstance(source, dict)
        else pack_manifest.get("default-ref")
    )
    if not isinstance(ref, str) or not ref:
        raise ValueError(
            f"pack {pack_name!r} passive entry: 'source.ref' required for "
            "legacy fetch"
        )
    fetch_url = _derive_raw_url(repo_url, ref, from_path)
    _legacy.validate_ref(pack_name, ref)
    cache_md = cache_dir / f"{quote(pack_name, safe='')}-{quote(ref, safe='')}.md"
    content, sha = _legacy.fetch_rule_pack(fetch_url, ref, cache_md, no_cache)
    _legacy.validate_rule_pack(pack_name, content)
    return content, sha


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

    The body for each mapping comes from
    :func:`_resolve_passive_body`, which branches on
    ``ctx.pack_source_dir``: a fetched archive when present (v0.5.0
    inline-source path), the legacy raw-URL fetch otherwise (v0.4.0 /
    bundled-pack path).
    """
    pack_name = pack_manifest["name"]
    source = pack_manifest.get("source")
    if source is None:
        raise ValueError(
            f"pack {pack_name!r} passive entry requires a pack-level "
            "'source' (repo + ref), but none was provided"
        )

    # v2 source is a dict {repo, ref, ...} — the schema parser already
    # rejects shapes that can't be resolved here. The body resolver
    # validates ``ref`` only on the legacy raw-URL path; archive reads
    # do not need a ref because the archive is already a snapshot at a
    # specific commit. Capture the requested ref here for begin/end
    # marker formatting (v1-byte-identical output).
    if isinstance(source, dict):
        ref = source.get("ref")
    else:
        ref = pack_manifest.get("default-ref")
    # Marker formatting requires a non-empty ref. The archive path lifts
    # the ref from the source dict (already populated by the composer's
    # selection layer); the legacy path enforces this anyway downstream.
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

        content, sha = _resolve_passive_body(
            mapping, pack_manifest, ctx,
            cache_dir=cache_dir, no_cache=no_cache,
        )

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
