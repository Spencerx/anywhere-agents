#!/usr/bin/env python3
"""Vendor scripts/packs/{auth,source_fetch,schema}.py into the PyPI package.

Source of truth: scripts/packs/*.py
Vendored copies: packages/pypi/anywhere_agents/packs/*.py

CI runs this and fails if the vendored copies don't match.
"""
import pathlib, shutil, sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "scripts" / "packs"
DST = REPO / "packages" / "pypi" / "anywhere_agents" / "packs"
MODULES = ("auth.py", "source_fetch.py", "schema.py")


def vendor():
    DST.mkdir(parents=True, exist_ok=True)
    (DST / "__init__.py").write_text(
        '"""Vendored copies of scripts/packs/* for the installed CLI."""\n',
        encoding="utf-8",
        newline="\n",
    )
    for name in MODULES:
        src_file = SRC / name
        dst_file = DST / name
        # Rewrite imports: scripts.packs.X → anywhere_agents.packs.X
        body = src_file.read_text(encoding="utf-8")
        body = body.replace(
            "from scripts.packs",
            "from anywhere_agents.packs",
        )
        body = body.replace(
            "import scripts.packs",
            "import anywhere_agents.packs",
        )
        dst_file.write_text(body, encoding="utf-8", newline="\n")


def _vendored_text(src_file: pathlib.Path) -> str:
    body = src_file.read_text(encoding="utf-8")
    body = body.replace("from scripts.packs", "from anywhere_agents.packs")
    body = body.replace("import scripts.packs", "import anywhere_agents.packs")
    return body


def check() -> None:
    """Fail with non-zero exit if vendored copies don't match the source.

    Compares the destination files byte-for-byte against the rewrite
    that would be produced by `vendor()`. Used by CI vendor-sync job.
    """
    errors = []
    expected_init = '"""Vendored copies of scripts/packs/* for the installed CLI."""\n'
    init_path = DST / "__init__.py"
    if not init_path.exists() or init_path.read_text(encoding="utf-8") != expected_init:
        errors.append(str(init_path))
    for name in MODULES:
        expected = _vendored_text(SRC / name)
        actual_path = DST / name
        actual = actual_path.read_text(encoding="utf-8") if actual_path.exists() else ""
        if actual != expected:
            errors.append(str(actual_path))
    if errors:
        print("vendored pack modules are out of sync with scripts/packs/*:", file=sys.stderr)
        for path in errors:
            print(f"  {path}", file=sys.stderr)
        print("Run `python scripts/vendor-packs.py` to regenerate.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    if sys.argv[1:2] == ["check"]:
        check()
    else:
        vendor()
