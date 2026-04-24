"""Auth safety preconditions for pack source URLs (v0.4.0 Phase 4).

Implements three preconditions from pack-architecture.md § "Auth safety
preconditions":

1. **Credential-URL rejection** — reject any source URL that embeds
   credentials in the userinfo component (``user@host``,
   ``user:pass@host``, ``<token>@host``). Applied in every config layer
   at parse time, before any network call. SSH transport usernames
   (``git@host:path``, ``ssh://git@host/path``) are allowed because they
   are not credentials.
2. **Noninteractive fetch env** — set ``GIT_TERMINAL_PROMPT=0`` (no
   HTTPS password prompt) and ``GIT_SSH_COMMAND=ssh -o BatchMode=yes
   -o ConnectTimeout=10`` on the composer subprocess so missing keys /
   unknown-host prompts don't hang the bootstrap.
3. **GitHub URL normalization** — extract the canonical
   ``<owner>/<repo>`` identity from any github.com URL form
   (``git@github.com:<owner>/<repo>``, ``https://github.com/<owner>/<repo>``,
   ``ssh://git@github.com/<owner>/<repo>``). Allows the auth chain in
   v0.5.0 to retry alternate methods on the same identity.

Phase 4 ships these preconditions as pure functions; the auth chain
itself (SSH agent → gh CLI → GITHUB_TOKEN → anonymous) lands in v0.5.0.
"""
from __future__ import annotations

import os
import re
from typing import Mapping
from urllib.parse import urlsplit


class CredentialURLError(ValueError):
    """Raised when a source URL embeds credentials in userinfo.

    Per pack-architecture.md:392, credentials in a URL are unsafe in
    config; use ``git@`` SSH, ``gh auth login``, or ``GITHUB_TOKEN`` env
    instead. Raised at parse time, before any network call.
    """


# HTTP(S) URL with any userinfo component — ``user@``, ``user:pass@``,
# ``token@``. The regex looks for ``://`` followed by a non-slash,
# non-at-sign sequence ending in ``@``. SSH URLs (``ssh://git@host``)
# match this pattern too — we filter SSH separately below.
_HTTPS_USERINFO_RE = re.compile(r"^https?://[^/@]+@")

# SSH URL prefixes whose transport username is not a credential. Listed
# explicitly so the rejector can tell "this is SSH transport" from
# "this is HTTPS with a token baked in".
_SSH_TRANSPORT_PREFIXES = ("git@", "ssh://", "git+ssh://")


def reject_credential_url(url: str, *, source_layer: str = "manifest") -> None:
    """Raise ``CredentialURLError`` if ``url`` embeds credentials.

    ``source_layer`` is included in the error message so the user can
    identify which config layer (user-level / project-tracked /
    project-local / env var / manifest) contains the offending URL.

    HTTP(S) URLs with any userinfo are rejected. SSH URLs get per-scheme
    treatment:
    - ``git@host:path`` (scp-style SSH) — transport username only,
      never credentials, passes through.
    - ``ssh://user@host`` / ``git+ssh://user@host`` — transport username
      only (no password component); passes through.
    - ``ssh://user:pass@host`` / ``git+ssh://user:pass@host`` — password
      field present in userinfo; rejected per pack-architecture.md:392
      ("SSH URLs are rejected only if they embed password-like secret
      material").
    """
    if not isinstance(url, str) or not url:
        return

    # scp-style SSH (``git@host:path``) carries no password component.
    if url.startswith("git@"):
        return

    # ssh:// / git+ssh://: parse via urlsplit and reject only if
    # password is present in userinfo. Username alone is a transport
    # identifier, not a credential.
    if url.startswith("ssh://") or url.startswith("git+ssh://"):
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            # urlsplit is lenient but invalid inputs still raise.
            # Treat as opaque and refuse rather than silently accept.
            raise CredentialURLError(
                f"{source_layer}: source URL {url!r} is malformed ({exc})"
            ) from exc
        if parsed.password is not None:
            raise CredentialURLError(
                f"{source_layer}: source URL {url!r} contains credentials in "
                "userinfo. Credentials in a URL are unsafe in config; use "
                "'git@' SSH, 'gh auth login', or 'GITHUB_TOKEN' env (v0.5.0+)."
            )
        return

    # HTTP(S): any userinfo ("user@", "user:pass@", "token@") rejects.
    if _HTTPS_USERINFO_RE.match(url):
        raise CredentialURLError(
            f"{source_layer}: source URL {url!r} contains credentials in "
            "userinfo. Credentials in a URL are unsafe in config; use "
            "'git@' SSH, 'gh auth login', or 'GITHUB_TOKEN' env (v0.5.0+)."
        )


def noninteractive_fetch_env(
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an env mapping suitable for spawning a fetch subprocess.

    Starts from ``base_env`` (or ``os.environ`` when ``None``), then
    overlays:

    - ``GIT_TERMINAL_PROMPT=0``: git over HTTPS will not prompt for
      credentials. Missing credentials surface as a clear fetch error
      instead of an interactive prompt hanging the composer.
    - ``GIT_SSH_COMMAND=ssh -o BatchMode=yes -o ConnectTimeout=10``:
      ssh fails fast on missing keys or unknown hosts (no
      "continue connecting (yes/no)?" prompt); ConnectTimeout bounds
      the failure window so a dead host doesn't stall bootstrap.

    Callers pass the returned mapping as the ``env=`` argument to
    ``subprocess.run`` / similar; the returned dict is a fresh copy
    and safe to mutate.
    """
    env = dict(base_env if base_env is not None else os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o ConnectTimeout=10"
    return env


# ======================================================================
# GitHub URL normalization
# ======================================================================

# Accept forms:
#   https://github.com/<owner>/<repo>[.git]
#   http://github.com/<owner>/<repo>[.git]
#   git@github.com:<owner>/<repo>[.git]
#   ssh://git@github.com/<owner>/<repo>[.git]
_GITHUB_HTTPS_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)
_GITHUB_SSH_SCP_RE = re.compile(
    r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"
)
_GITHUB_SSH_URL_RE = re.compile(
    r"^ssh://git@github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)


class GithubURLParseError(ValueError):
    """Raised when a URL is claimed to be a github.com URL but cannot
    be parsed into a canonical owner/repo identity."""


def normalize_github_url(url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from a github.com URL, if the URL is one.

    Returns ``None`` for URLs that are not on ``github.com`` (e.g.,
    GitHub Enterprise, GitLab, self-hosted). Callers fall back to
    URL-shape-based auth method selection for non-github.com hosts;
    Phase 4 scope is ``github.com`` only.

    Raises ``GithubURLParseError`` when the URL IS on github.com but
    doesn't match any recognized shape (malformed).
    """
    if not isinstance(url, str) or not url:
        return None
    # Quick host check — only proceed if the URL claims github.com.
    if "github.com" not in url:
        return None
    for pattern in (_GITHUB_HTTPS_RE, _GITHUB_SSH_SCP_RE, _GITHUB_SSH_URL_RE):
        m = pattern.match(url)
        if m:
            return m.group("owner"), m.group("repo")
    raise GithubURLParseError(
        f"github.com URL {url!r} does not match any recognized shape "
        "(expected https://github.com/<owner>/<repo>, "
        "git@github.com:<owner>/<repo>, or ssh://git@github.com/<owner>/<repo>)"
    )


def canonical_github_identity(url: str) -> str | None:
    """Return ``"<owner>/<repo>"`` for a github.com URL, else ``None``.

    Convenience wrapper around :func:`normalize_github_url` for callers
    that only need the identity string, not the tuple. Returns ``None``
    for non-github.com hosts; propagates ``GithubURLParseError`` for
    malformed github.com URLs.
    """
    result = normalize_github_url(url)
    if result is None:
        return None
    owner, repo = result
    return f"{owner}/{repo}"
