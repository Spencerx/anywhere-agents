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
# Scheme matching is case-insensitive: URL schemes are case-insensitive
# per RFC 3986, so ``HTTPS://`` must reject the same as ``https://``.
_HTTPS_USERINFO_RE = re.compile(r"^https?://[^/@]+@", re.IGNORECASE)

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

    # URL schemes are case-insensitive per RFC 3986; lowercase the
    # prefix for scheme detection so ``Git@host``/``SSH://`` route
    # through the same branches as their lowercase forms.
    scheme_url = url.lower()

    # scp-style SSH (``git@host:path``) carries no password component.
    if scheme_url.startswith("git@"):
        return

    # ssh:// / git+ssh://: parse via urlsplit and reject only if
    # password is present in userinfo. Username alone is a transport
    # identifier, not a credential.
    if scheme_url.startswith("ssh://") or scheme_url.startswith("git+ssh://"):
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            # urlsplit is lenient but invalid inputs still raise.
            # Treat as opaque and refuse rather than silently accept.
            raise CredentialURLError(
                f"{source_layer}: source URL {redact_url_userinfo(url)!r} is "
                f"malformed ({exc})"
            ) from exc
        if parsed.password is not None:
            raise CredentialURLError(
                f"{source_layer}: source URL {redact_url_userinfo(url)!r} "
                "contains credentials in userinfo. Credentials in a URL are "
                "unsafe in config; use 'git@' SSH, 'gh auth login', or "
                "'GITHUB_TOKEN' env (v0.5.0+)."
            )
        return

    # HTTP(S): any userinfo ("user@", "user:pass@", "token@") rejects.
    if _HTTPS_USERINFO_RE.match(url):
        raise CredentialURLError(
            f"{source_layer}: source URL {redact_url_userinfo(url)!r} "
            "contains credentials in userinfo. Credentials in a URL are "
            "unsafe in config; use 'git@' SSH, 'gh auth login', or "
            "'GITHUB_TOKEN' env (v0.5.0+)."
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


# ======================================================================
# Per-method probes (no network, fast) - v0.5.0
# ======================================================================

import subprocess


def ssh_agent_available() -> bool:
    """Return True iff ``ssh-add -l`` reports at least one identity.

    Used by the auth chain to skip the ssh path when no key is loaded
    (avoids a slow ssh handshake that would fail anyway).
    """
    try:
        result = subprocess.run(
            ["ssh-add", "-l"],
            capture_output=True,
            text=True,
            timeout=5,
            env=noninteractive_fetch_env(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def gh_cli_authenticated() -> bool:
    """Return True iff ``gh auth status`` reports a logged-in github.com host."""
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=5,
            env=noninteractive_fetch_env(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def github_token_available() -> bool:
    """Return True iff ``GITHUB_TOKEN`` env var is set and non-empty."""
    return bool(os.environ.get("GITHUB_TOKEN", "").strip())


# ======================================================================
# Secret redaction helpers (used by auth chain failure paths)
# ======================================================================

# Recognized GitHub token prefixes (PAT, OAuth, user-to-server, refresh,
# server-to-server, fine-grained PAT). Matched as whole tokens with
# trailing alphanumeric/underscore characters.
_TOKEN_PREFIX_PATTERN = re.compile(
    r"\b(?:ghp_|gho_|ghu_|ghr_|ghs_|github_pat_)[A-Za-z0-9_]+",
)
_BEARER_PATTERN = re.compile(r"Bearer\s+[A-Za-z0-9._\-]+")


def redact_url_userinfo(url: str) -> str:
    """Redact userinfo (user / password / token) in a URL.

    HTTPS forms with any userinfo become ``https://<redacted>@host/path``.
    SSH transport-only (``git@host:...``) is unchanged because the
    username is a transport identifier, not a credential.
    SSH with password (``ssh://user:pass@host/...``) redacts the password
    component but leaves the username visible.
    """
    if not isinstance(url, str) or not url:
        return url

    # URL schemes are case-insensitive per RFC 3986; lowercase only the
    # prefix used for scheme detection while leaving the rebuilt URL's
    # original case intact.
    lower_url = url.lower()

    # scp-style SSH (git@host:path) has transport username only.
    if lower_url.startswith("git@") and "://" not in url:
        return url

    # https://[userinfo]@host/...
    https_match = re.match(r"^(https?)://([^/@]+)@(.+)$", url, flags=re.IGNORECASE)
    if https_match:
        scheme, _userinfo, rest = https_match.groups()
        return f"{scheme}://<redacted>@{rest}"

    # ssh://user:pass@host/... or git+ssh://user:pass@host/...
    ssh_match = re.match(r"^(ssh|git\+ssh)://([^:@]+):([^@]+)@(.+)$", url, flags=re.IGNORECASE)
    if ssh_match:
        scheme, user, _pass, rest = ssh_match.groups()
        return f"{scheme}://{user}:<redacted>@{rest}"

    # ssh://user@host/... (no password) - leave unchanged.
    return url


def redact_secret_text(
    text: str,
    *,
    known_secrets=(),
) -> str:
    """Redact known secrets and recognized GitHub token patterns from text.

    Used to sanitize subprocess stderr/stdout, exception messages, and
    state-file content before display or persistence.
    """
    if not isinstance(text, str):
        return text
    out = text
    for secret in known_secrets:
        if secret:
            out = out.replace(secret, "<redacted>")
    out = _TOKEN_PREFIX_PATTERN.sub("<redacted>", out)
    out = _BEARER_PATTERN.sub("Bearer <redacted>", out)
    return out


# ======================================================================
# Auth chain (resolver + fetch drivers + orchestrator) - v0.5.0
# ======================================================================


class AuthChainExhaustedError(Exception):
    """Raised when all auth methods in the chain failed.

    Carries per-method outcomes so the caller can render a composite
    error with each method's status + reason. When ``explicit_method``
    is set on the caller, the chain is bypassed entirely; the message
    notes the explicit-method context so the failure cause is clear.
    """

    def __init__(
        self,
        url: str,
        ref: str,
        attempts: list,
        *,
        explicit_method: str | None = None,
    ) -> None:
        self.url = url
        self.ref = ref
        # Each attempt is a (method, status_string) tuple.
        self.attempts = list(attempts)
        self.explicit_method = explicit_method
        super().__init__(self._format())

    def _format(self) -> str:
        safe_url = redact_url_userinfo(self.url)
        if self.explicit_method:
            header = (
                f"Failed to resolve {safe_url}@{self.ref} "
                f"(explicit method={self.explicit_method!r}; "
                f"chain bypassed, no fallback)"
            )
        else:
            header = f"Failed to resolve {safe_url}@{self.ref}"
        lines = [header]
        for method, status in self.attempts:
            lines.append(f"  {method:11s} : {status}")
        return "\n".join(lines)


_AUTH_CHAIN_ORDER = ("ssh", "gh", "github_token", "anonymous")


def _to_https_url(url: str) -> str:
    """Convert any github.com URL form to ``https://github.com/owner/repo``.

    Non-github URLs pass through unchanged. Used by methods that drive
    git over HTTPS (gh, github_token, anonymous) so they can accept
    SSH-style URLs in pack manifests.
    """
    owner_repo = canonical_github_identity(url)
    if owner_repo:
        return f"https://github.com/{owner_repo}"
    return url


def _git_ls_remote(url: str, ref: str, method: str) -> tuple:
    """Run ``git ls-remote <url> <ref>`` with method-specific config.

    Returns ``(success, sha_or_empty, error_or_empty)``. Stderr is run
    through :func:`redact_secret_text` before being returned so token
    material from credential-helper output cannot leak into composite
    error messages.

    Codex Round 2 H (defense-in-depth): validates the URL via
    :func:`reject_credential_url` before any git invocation, so a caller
    that bypasses :func:`resolve_ref_with_auth_chain` cannot leak
    credentials embedded in URL userinfo into ``git ls-remote`` argv.
    """
    reject_credential_url(url, source_layer="auth._git_ls_remote")
    args = ["git", "ls-remote"]
    env = noninteractive_fetch_env()

    if method == "ssh":
        # SSH: rewrite to git@github.com:owner/repo form for github.com.
        owner_repo = canonical_github_identity(url)
        target = f"git@github.com:{owner_repo}.git" if owner_repo else url
    elif method == "gh":
        args = [
            "git",
            "-c",
            "credential.helper=!gh auth git-credential",
            "ls-remote",
        ]
        target = url if url.startswith("http") else _to_https_url(url)
    elif method == "github_token":
        # Token via inline credential helper that reads from env (no
        # argv leak). ls-remote uses the same helper-string approach as
        # clone, since ls-remote also goes through git's credential
        # machinery.
        args = [
            "git",
            "-c",
            f"credential.helper={_token_credential_helper()}",
            "ls-remote",
        ]
        # The credential helper needs GITHUB_TOKEN in env; ensure it is
        # present in the subprocess env even when caller's base env
        # already has it (noninteractive_fetch_env starts from
        # os.environ so this is idempotent).
        env["GITHUB_TOKEN"] = os.environ.get("GITHUB_TOKEN", "")
        target = url if url.startswith("http") else _to_https_url(url)
    else:  # anonymous
        target = url if url.startswith("http") else _to_https_url(url)

    # Query both <ref> and <ref>^{} so annotated tags resolve to their
    # peeled commit sha (matching what `git rev-parse HEAD` returns
    # after `clone --branch <tag>`). Without the peeled query, an
    # annotated tag's ls-remote line carries the tag-object sha; that
    # mis-compares against the cloned archive's commit sha downstream
    # and would trigger spurious locked-drift detection. Branches and
    # lightweight tags only produce the bare-ref line, so the peeled
    # query is harmless there.
    peeled_arg = f"{ref}^{{}}" if not ref.endswith("^{}") else ref
    try:
        result = subprocess.run(
            args + [target, ref, peeled_arg],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, "", redact_secret_text(str(e))

    if result.returncode != 0:
        return False, "", redact_secret_text((result.stderr or "").strip())

    # ls-remote output: one line per matching ref, "<sha>\t<refname>".
    # Prefer the peeled (refname ending in "^{}") line when present;
    # fall back to the first valid bare-ref line otherwise.
    peeled_sha = None
    fallback_sha = None
    for line in (result.stdout or "").strip().split("\n"):
        parts = line.split("\t")
        if len(parts) != 2 or len(parts[0]) != 40:
            continue
        sha, refname = parts
        if refname.endswith("^{}"):
            peeled_sha = sha
        elif fallback_sha is None:
            fallback_sha = sha
    chosen = peeled_sha or fallback_sha
    if chosen is None:
        return False, "", (
            f"unexpected ls-remote output: {(result.stdout or '').strip()!r}"
        )
    return True, chosen, ""


def resolve_ref_with_auth_chain(
    url: str,
    ref: str,
    *,
    explicit_method: str | None = None,
) -> tuple:
    """Resolve ``(url, ref)`` to a 40-char commit sha via the auth chain.

    Returns ``(resolved_commit, method_that_succeeded)``.

    With ``explicit_method=None``, walks ``_AUTH_CHAIN_ORDER`` and
    returns on the first method that succeeds. Methods whose probe
    fails are skipped and recorded in the composite error. With
    ``explicit_method`` set, the chain is bypassed entirely: only that
    method is attempted, and failure raises (no anonymous fallback).

    Codex Round 2 H3-A defense-in-depth: validates the URL via
    :func:`reject_credential_url` before any network call, so a caller
    that forgets to pre-validate (CLI, future plugin, anyone) cannot
    leak credentials embedded in URL userinfo into ``git ls-remote``
    argv. ``CredentialURLError`` propagates to the caller.
    """
    reject_credential_url(url, source_layer="auth.resolve_ref_with_auth_chain")
    methods = (explicit_method,) if explicit_method else _AUTH_CHAIN_ORDER
    attempts: list = []

    for method in methods:
        # Probe (fast skip for unavailable methods). With
        # explicit_method, the probe is informational only; we still
        # attempt the method so the failure cause is the actual
        # ls-remote error, not a probe-skip.
        if method == "ssh" and not ssh_agent_available() and not explicit_method:
            attempts.append(
                (method, "skipped (ssh-add -l reported no identities)"),
            )
            continue
        if method == "gh" and not gh_cli_authenticated() and not explicit_method:
            attempts.append(
                (method, "skipped (gh auth status returned non-zero)"),
            )
            continue
        if method == "github_token" and not github_token_available() and not explicit_method:
            attempts.append(
                (method, "skipped (GITHUB_TOKEN env var not set)"),
            )
            continue

        success, sha, err = _git_ls_remote(url, ref, method)
        if success:
            return sha, method
        attempts.append(
            (method, f"attempted: {err or 'unknown failure'}"),
        )

    raise AuthChainExhaustedError(
        url, ref, attempts, explicit_method=explicit_method,
    )


def _token_credential_helper() -> str:
    """Return a git-credential-helper inline script that reads GITHUB_TOKEN.

    Used by ``ls-remote`` (Task 2.2) where a credential helper string is
    needed without writing a separate file. The returned string is
    embedded in ``git -c credential.helper=<this>``. The token is
    expanded by the shell at git's invocation time, NOT at script-write
    time, so the value never appears in argv.
    """
    return "!f() { echo username=x-access-token; echo password=$GITHUB_TOKEN; }; f"


# ----------------------------------------------------------------------
# Per-method fetch drivers (Task 2.3) - GIT_ASKPASS for token, no argv leak
# ----------------------------------------------------------------------

import pathlib
import shutil
import tempfile


def _git_rev_parse_head(repo_dir) -> str:
    """Return the 40-char HEAD commit of a clone."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    return (result.stdout or "").strip()


def _write_git_askpass_helper(token_env_var: str = "GITHUB_TOKEN"):
    """Write a temporary GIT_ASKPASS helper script that reads the token from env.

    The helper outputs ``x-access-token`` for the username prompt and
    the value of the ``token_env_var`` env var for the password prompt.
    The token is NEVER passed via argv: only the env-var NAME appears
    in the helper's body, and the shell (POSIX) or cmd.exe (Windows)
    expands it at git's invocation time, not at Python's script-write
    time.

    Returns the path to the helper script. Caller is responsible for
    cleaning up the parent tempdir after the subprocess finishes.
    """
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="aa-askpass-"))
    try:
        if os.name == "nt":
            # Windows .bat: %~1 is the prompt arg. findstr returns 0 when
            # the substring is found. %{token_env_var}% expands at
            # cmd.exe runtime to the env value, never at script-write time.
            helper = tmp / "askpass.bat"
            helper.write_text(
                "@echo off\r\n"
                "echo %~1 | findstr /C:\"Username\" >nul && "
                "(echo x-access-token) "
                "|| (echo %{token_env_var}%)\r\n".format(
                    token_env_var=token_env_var
                )
            )
        else:
            # POSIX shell: ${GITHUB_TOKEN} expands at script-run time.
            # The Python f-string injects only the var NAME into the
            # script body; expansion to value happens when git invokes
            # the script, NOT when Python writes it.
            helper = tmp / "askpass.sh"
            helper.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  *Username*) printf '%s\\n' x-access-token ;;\n"
                f"  *) printf '%s\\n' \"${{{token_env_var}}}\" ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            helper.chmod(0o700)
    except BaseException:
        # Mid-write failure (full disk, permission error, encoding glitch)
        # would otherwise leak the tempdir, since the caller's finally
        # only fires when this function returns a non-None helper path.
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return helper


def fetch_with_method(
    url: str,
    ref: str,
    method: str,
    *,
    dest=None,
):
    """Run a ``git clone`` subprocess for one auth method.

    Returns a :class:`source_fetch.PackArchive` with the resolved commit
    sha, the on-disk archive directory, the method that ran, and the
    cache key. Raises :class:`subprocess.CalledProcessError` on clone
    failure (the orchestrator catches and aggregates).

    Method-specific behavior:

    - ``ssh``: rewrite URL to ``git@github.com:<owner>/<repo>.git``
      (for github.com URLs) and let SSH's ambient agent provide the key.
    - ``gh``: ``git -c credential.helper=!gh auth git-credential clone ...``;
      gh CLI provides the token via stdin (no argv leak).
    - ``github_token``: write a temporary GIT_ASKPASS helper script,
      set ``GIT_ASKPASS`` env to its path. Token is read from the
      ``GITHUB_TOKEN`` env var by the helper at git's invocation time;
      the value NEVER appears in the clone argv.
    - ``anonymous``: plain HTTPS clone with no credential config.

    Codex Round 2 H (defense-in-depth): validates the URL via
    :func:`reject_credential_url` before any git invocation, so a caller
    that bypasses :func:`fetch_with_auth_chain` (e.g., the CI
    token-smoke workflow that calls this directly) cannot leak
    credentials embedded in URL userinfo into ``git clone`` argv.
    """
    reject_credential_url(url, source_layer="auth.fetch_with_method")
    from scripts.packs import source_fetch  # forward import to avoid cycles

    auto_created_dest = dest is None
    if dest is None:
        dest = pathlib.Path(tempfile.mkdtemp(prefix="aa-clone-"))
    else:
        dest = pathlib.Path(dest)

    base_args = ["git"]
    env = noninteractive_fetch_env()
    askpass_helper = None

    if method == "ssh":
        owner_repo = canonical_github_identity(url)
        target = f"git@github.com:{owner_repo}.git" if owner_repo else url
    elif method == "gh":
        base_args = [
            "git",
            "-c",
            "credential.helper=!gh auth git-credential",
        ]
        target = _to_https_url(url) if not url.startswith("http") else url
    elif method == "github_token":
        askpass_helper = _write_git_askpass_helper()
        env["GIT_ASKPASS"] = str(askpass_helper)
        env["GIT_TERMINAL_PROMPT"] = "0"
        # Ensure the token is forwarded to the subprocess (the askpass
        # helper reads it via env-var expansion at git's invocation time).
        env["GITHUB_TOKEN"] = os.environ.get("GITHUB_TOKEN", "")
        target = _to_https_url(url) if not url.startswith("http") else url
    elif method == "anonymous":
        target = _to_https_url(url) if url.startswith("git@") else url
    else:
        raise ValueError(f"unknown auth method: {method!r}")

    # Pin core.autocrlf=false + core.eol=lf so the working-tree bytes match
    # what's recorded by source_fetch's dir-sha256 marker. Without this,
    # Windows's default core.autocrlf=true would inject CRLFs at checkout
    # time, and a later integrity check (recompute on cache hit) would
    # disagree with the marker recorded at fetch-time, forcing spurious
    # re-clones. The flags must precede the `clone` subcommand.
    clone_args = base_args + [
        "-c", "core.autocrlf=false",
        "-c", "core.eol=lf",
        "clone",
        "--depth=1",
        "--filter=blob:none",
        "--branch", ref,
        target,
        str(dest),
    ]

    try:
        subprocess.run(
            clone_args,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        resolved_commit = _git_rev_parse_head(dest)
    except BaseException:
        # Clone or rev-parse failed; clean up an auto-created dest so
        # the orchestrator's fall-through to the next method does not
        # leak a tempdir per attempted method.
        if auto_created_dest:
            shutil.rmtree(dest, ignore_errors=True)
        raise
    finally:
        if askpass_helper is not None:
            shutil.rmtree(askpass_helper.parent, ignore_errors=True)

    canonical = canonical_github_identity(url)
    return source_fetch.PackArchive(
        url=url,
        ref=ref,
        resolved_commit=resolved_commit,
        method=method,
        archive_dir=dest,
        canonical_id=canonical,
        cache_key=source_fetch.compute_cache_key(url, resolved_commit),
    )


# ----------------------------------------------------------------------
# Chain orchestrator (Task 2.4) - first-success-wins, fail-closed on explicit
# ----------------------------------------------------------------------


def fetch_with_auth_chain(
    url: str,
    ref: str,
    *,
    explicit_method: str | None = None,
):
    """Walk the auth chain; return the first :class:`PackArchive` that succeeds.

    Order (when ``explicit_method=None``):
    ``ssh`` -> ``gh`` -> ``github_token`` -> ``anonymous``. Methods whose
    probe returns False are skipped (logged in the composite error). With
    ``explicit_method`` set, the chain is bypassed entirely; only that
    method is attempted, and failure raises with no anonymous fallback.

    Raises :class:`AuthChainExhaustedError` when every attempted method
    fails (or when an explicit method fails). The composite error
    aggregates per-method outcomes with stderr passed through
    :func:`redact_secret_text` so token material cannot leak.

    Codex Round 2 H3-A defense-in-depth: validates the URL via
    :func:`reject_credential_url` before any network call, so a caller
    that forgets to pre-validate cannot leak credentials embedded in
    URL userinfo into ``git clone`` argv. ``CredentialURLError``
    propagates to the caller.
    """
    reject_credential_url(url, source_layer="auth.fetch_with_auth_chain")
    methods = (explicit_method,) if explicit_method else _AUTH_CHAIN_ORDER
    attempts: list = []

    for method in methods:
        # Probe (fast skip for unavailable methods). With explicit_method,
        # the probe is informational only; we still attempt the method
        # so the failure cause is the actual clone error.
        if method == "ssh" and not ssh_agent_available() and not explicit_method:
            attempts.append(
                (method, "skipped (ssh-add -l reported no identities)"),
            )
            continue
        if method == "gh" and not gh_cli_authenticated() and not explicit_method:
            attempts.append(
                (method, "skipped (gh auth status returned non-zero)"),
            )
            continue
        if method == "github_token" and not github_token_available() and not explicit_method:
            attempts.append(
                (method, "skipped (GITHUB_TOKEN env var not set)"),
            )
            continue

        try:
            archive = fetch_with_method(url, ref, method)
            return archive
        except subprocess.CalledProcessError as e:
            stderr = redact_secret_text((e.stderr or "").strip())
            attempts.append(
                (method, f"attempted, exit {e.returncode}: {stderr}"),
            )
            continue
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            attempts.append(
                (method, f"attempted: {redact_secret_text(str(e))}"),
            )
            continue

    raise AuthChainExhaustedError(
        url, ref, attempts, explicit_method=explicit_method,
    )
