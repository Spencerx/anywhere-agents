"""Source fetch v0.5.0 - fetch a pack archive from a URL with auth chain.

Phase 3 replaces the Phase 2 stub with the full
validate->resolve->compare->cache->fetch pipeline:

1. ``reject_credential_url`` (parse-time, no network).
2. ``resolve_ref_with_auth_chain`` (one network call) -> resolved sha.
3. Compare the resolved sha against the recorded pack-lock commit;
   under ``update_policy=locked`` drift raises ``PackLockDriftError``.
4. Cache lookup at ``(canonical_id_or_url_hash, resolved_commit)``;
   integrity check via ``dir-sha256``.
5. Cache miss -> ``fetch_with_auth_chain`` into staging; rename
   atomically into the cache slot.
"""
from __future__ import annotations

import hashlib
import pathlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from anywhere_agents.packs import auth


@dataclass(frozen=True)
class PackArchive:
    """A fetched pack: source URL, ref, resolved commit, fetch method,
    on-disk archive directory, canonical github identity (or ``None``
    for non-github hosts), and the cache key under which the archive
    is stored.
    """
    url: str
    ref: str
    resolved_commit: str
    method: str
    archive_dir: Path
    canonical_id: str | None
    cache_key: str


class PackLockDriftError(Exception):
    """Raised when ``update_policy=locked`` detects ref drift vs pack-lock.

    Carries enough context for the compose layer to surface a precise
    remediation message: the offending URL, the requested ref, the
    commit recorded in pack-lock, and the new commit currently at HEAD
    of the remote ref.
    """

    def __init__(self, url: str, ref: str, recorded: str, current: str):
        self.url = url
        self.ref = ref
        self.recorded = recorded
        self.current = current
        super().__init__(
            f"pack-lock drift for {url}@{ref}: recorded {recorded}, "
            f"current upstream {current}. Run `anywhere-agents pack update "
            f"<name>` to accept the new commit, or pin ref to {recorded}."
        )


def normalize_pack_source_url(url: str) -> str:
    """Return a canonical form of ``url`` for identity comparison.

    Used by ``pack verify`` to detect when the same pack name carries
    different source URLs across user-level / project-level / pack-lock
    layers. Two URLs that resolve to the same repository must normalize
    to byte-equal strings; two URLs that resolve to different repositories
    must remain distinct.

    GitHub URLs (`github.com`, case-insensitive host) collapse to
    ``https://github.com/<owner>/<repo>`` with **lowercased owner and
    repo**, no trailing ``.git``, no trailing ``/``. GitHub repository
    URLs are case-insensitive in practice, so ``Owner/Repo`` and
    ``owner/repo`` must compare equal.

    Other hosts get a minimal normalization: lowercase host, strip a
    single trailing ``/``, strip a single trailing ``.git``. Path case is
    **preserved** for non-GitHub hosts (other forges may be
    case-sensitive, e.g., self-hosted Gitea).

    Unparseable URLs are returned unchanged so verify never crashes; it
    just counts them as a distinct identity.
    """
    if not isinstance(url, str) or not url:
        return url
    # auth.normalize_github_url uses a case-sensitive "github.com" host
    # check + literal `github\.com` regex patterns. Pre-lowercase the
    # host token so e.g. https://GitHub.COM/Owner/Repo is recognized
    # without losing owner/repo case (which the regex captures and we
    # lowercase below for case-insensitive identity comparison).
    import re
    candidate = re.sub(r"github\.com", "github.com", url, flags=re.IGNORECASE)
    try:
        github = auth.normalize_github_url(candidate)
    except Exception:
        github = None
    if github is not None:
        owner, repo = github
        return f"https://github.com/{owner.lower()}/{repo.lower()}"
    # Non-GitHub: minimal normalization. Parse host case-insensitively,
    # strip trailing .git and trailing /, leave path case intact.
    from urllib.parse import urlsplit, urlunsplit
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.scheme or not parts.netloc:
        return url
    host = parts.netloc.lower()
    path = parts.path
    if path.endswith("/"):
        path = path[:-1]
    if path.endswith(".git"):
        path = path[:-4]
    return urlunsplit((parts.scheme, host, path, parts.query, parts.fragment))


def compute_cache_key(url: str, resolved_commit: str) -> str:
    """Cache key keying URL+commit to a stable directory layout.

    For github.com URLs, keys by ``canonical_github_identity`` so that
    different URL forms (https vs scp-style SSH) for the same repo
    dedupe into one cache slot. For other hosts, keys by ``sha256(url)``,
    which is URL-shape-sensitive (a deliberate trade-off: we cannot
    canonicalize an arbitrary host without host-specific knowledge).
    """
    canonical = auth.canonical_github_identity(url)
    if canonical:
        prefix = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    else:
        prefix = hashlib.sha256(url.encode()).hexdigest()[:16]
    return f"{prefix}/{resolved_commit}"


def _iter_content_files(archive_dir: pathlib.Path):
    """Yield only content files (skip ``.git/`` metadata and ``.dir-sha256``).

    Without this filter, the dir-sha256 marker file would be included
    in its own hash on subsequent runs, so the cache-hit integrity
    check would always fail. ``.git/`` is clone metadata, not pack
    content, and varies across clones of the same commit.
    """
    for path in archive_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(archive_dir)
        if rel.parts and rel.parts[0] == ".git":
            continue
        if rel.as_posix() == ".dir-sha256":
            continue
        yield path


def _compute_dir_sha256(archive_dir: pathlib.Path) -> str:
    """Stable Merkle hash over the archive directory's content files.

    Produces ``dir-sha256:<hex>``. Hashes the relative posix path, a
    null byte, the file bytes, then a null byte for each file in
    sorted order. The null separators block boundary-collision attacks
    (file ``a`` with content ``b/c`` vs file ``a/b`` with content ``c``).
    """
    paths = sorted(_iter_content_files(archive_dir))
    h = hashlib.sha256()
    for p in paths:
        rel = p.relative_to(archive_dir).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return f"dir-sha256:{h.hexdigest()}"


def load_cached_archive(
    url: str,
    recorded_commit: str,
    *,
    cache_root: pathlib.Path | None = None,
) -> PackArchive | None:
    """Return the cached archive at ``(url, recorded_commit)``, or ``None``.

    No network call. Used by the compose skip-path to revert to a
    pack-lock-recorded commit without re-resolving the ref. Calling
    :func:`fetch_pack` with a 40-char SHA as ``ref`` would re-run
    :func:`auth.resolve_ref_with_auth_chain`, which calls
    ``git ls-remote <url> <sha> <sha>^{}``; ls-remote matches by refname
    and returns empty for a SHA, exhausting the auth chain and raising
    :class:`auth.AuthChainExhaustedError`. This helper sidesteps that by
    going straight to the cache slot.

    Returns ``None`` if the cache slot does not exist or fails its
    ``dir-sha256`` integrity check, in which case the caller falls back
    to whatever archive it already has.
    """
    if cache_root is None:
        cache_root = pathlib.Path(".agent-config/cache")
    cache_key = compute_cache_key(url, recorded_commit)
    cache_dir = cache_root / cache_key
    if not cache_dir.exists():
        return None
    marker = cache_dir / ".dir-sha256"
    if not marker.exists():
        return None
    recorded_sha = marker.read_text()
    if recorded_sha != _compute_dir_sha256(cache_dir):
        return None
    return PackArchive(
        url=url,
        ref=recorded_commit,
        resolved_commit=recorded_commit,
        method="cached",
        archive_dir=cache_dir,
        canonical_id=auth.canonical_github_identity(url),
        cache_key=cache_key,
    )


def fetch_pack(
    url: str,
    ref: str,
    *,
    policy: Literal["auto", "prompt", "locked"] = "prompt",
    explicit_auth: str | None = None,
    pack_lock_recorded_commit: str | None = None,
    cache_root: pathlib.Path | None = None,
) -> PackArchive:
    """Fetch (or cache-hit) the pack archive at ``url@ref``.

    Ordering (Codex Round 1 M5):

    1. Validate URL via :func:`auth.reject_credential_url` (no network).
    2. Resolve ref to commit sha via
       :func:`auth.resolve_ref_with_auth_chain` (one network call).
    3. Compare the resolved sha against ``pack_lock_recorded_commit``;
       under ``policy="locked"`` drift raises :class:`PackLockDriftError`.
       Under ``"auto"`` / ``"prompt"`` the compose layer decides whether
       to apply or defer the new commit.
    4. Cache lookup at ``(canonical_id_or_url_hash, resolved_commit)``.
       Hit -> re-verify the recorded ``dir-sha256``; on match, return
       a :class:`PackArchive` pointing into the cache. On mismatch the
       cache slot is dropped and the fetch proceeds (one retry).
    5. Cache miss -> :func:`auth.fetch_with_auth_chain` clones into
       staging; the staging directory is moved atomically into the
       cache slot and the ``dir-sha256`` recorded for next-run integrity.
    """
    if cache_root is None:
        cache_root = pathlib.Path(".agent-config/cache")
    cache_root.mkdir(parents=True, exist_ok=True)

    # 1. Validate (parse-time, no network).
    auth.reject_credential_url(url, source_layer="source_fetch")

    # 2. Resolve ref -> commit sha.
    resolved_commit, _resolve_method = auth.resolve_ref_with_auth_chain(
        url, ref, explicit_method=explicit_auth,
    )

    # 3. Compare against pack-lock recorded commit.
    if (
        pack_lock_recorded_commit
        and resolved_commit != pack_lock_recorded_commit
    ):
        if policy == "locked":
            raise PackLockDriftError(
                url, ref, pack_lock_recorded_commit, resolved_commit,
            )
        # policy in {"auto", "prompt"}: drift is handled by the compose
        # layer, which decides apply-or-defer. We continue with the
        # newly resolved commit so the compose layer sees both values.

    # 4. Cache lookup.
    cache_key = compute_cache_key(url, resolved_commit)
    cache_dir = cache_root / cache_key
    if cache_dir.exists():
        marker = cache_dir / ".dir-sha256"
        recorded_sha = marker.read_text() if marker.exists() else None
        current_sha = _compute_dir_sha256(cache_dir)
        if recorded_sha == current_sha:
            return PackArchive(
                url=url,
                ref=ref,
                resolved_commit=resolved_commit,
                method="cached",
                archive_dir=cache_dir,
                canonical_id=auth.canonical_github_identity(url),
                cache_key=cache_key,
            )
        # Integrity mismatch: drop the slot and fall through to refetch.
        shutil.rmtree(cache_dir, ignore_errors=True)

    # 5. Cache miss -> fetch.
    archive = auth.fetch_with_auth_chain(
        url, ref, explicit_method=explicit_auth,
    )

    # Codex Round 2 H2 fix: ``resolve_ref_with_auth_chain`` returned the
    # pre-clone SHA at step 2; ``fetch_with_auth_chain`` is a separate
    # network round-trip and may resolve to a different commit if the
    # branch / tag moved between calls. When the cloned tree's HEAD SHA
    # disagrees with the pre-clone SHA, we MUST re-key the cache to the
    # post-clone SHA so the ``dir-sha256`` marker and ``pack-lock.json``
    # attest to the actually-fetched commit. Under ``policy=locked``
    # with a recorded commit, the post-clone SHA is reported as drift
    # (so the operator sees the new commit, not the stale pre-clone one).
    if archive.resolved_commit != resolved_commit:
        if policy == "locked" and pack_lock_recorded_commit is not None:
            shutil.rmtree(archive.archive_dir, ignore_errors=True)
            raise PackLockDriftError(
                url, ref, pack_lock_recorded_commit, archive.resolved_commit,
            )
        # ``policy in {"auto", "prompt"}``: re-key the cache so the slot
        # name matches what is actually inside it. The caller's drift /
        # apply logic already runs on ``archive.resolved_commit``.
        resolved_commit = archive.resolved_commit
        cache_key = compute_cache_key(url, resolved_commit)
        cache_dir = cache_root / cache_key

    cache_dir.parent.mkdir(parents=True, exist_ok=True)
    if cache_dir.exists():
        # Best-effort cleanup of a stale cache_dir from a crashed prior
        # run. Real concurrent-fetch protection comes from the Phase 6
        # outer lock in compose_packs.py; without that lock, the
        # rmtree+move sequence below is itself a TOCTOU and can fail on
        # Windows if a third concurrent fetch repopulates cache_dir.
        shutil.rmtree(cache_dir, ignore_errors=True)
    shutil.move(str(archive.archive_dir), str(cache_dir))
    sha = _compute_dir_sha256(cache_dir)
    (cache_dir / ".dir-sha256").write_text(sha)

    return PackArchive(
        url=url,
        ref=ref,
        resolved_commit=resolved_commit,
        method=archive.method,
        archive_dir=cache_dir,
        canonical_id=auth.canonical_github_identity(url),
        cache_key=cache_key,
    )
