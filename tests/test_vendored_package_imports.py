"""The vendored PyPI package must actually import, and its module list
must cover its own import closure.

``scripts/vendor-packs.py check`` compares the vendored copies against a
text rewrite of ``scripts/packs/*``. Text equality says nothing about
whether the result is importable: adding an import of a module that is
not in ``MODULES`` produces a vendored tree that is byte-for-byte
"in sync" and raises ``ImportError`` the moment the installed CLI runs.

That is exactly what happened while fixing anywhere-agents#18. A
``from . import dirhash`` was added to ``source_fetch.py``, the vendoring
copied the import but not the module, and the whole test suite still
passed because nothing imports the vendored tree.

Two layers guard it, because either alone has a blind spot:

* A runtime import of every vendored module, from a working directory
  outside the repository, so the source checkout cannot satisfy an
  import that an installed wheel would fail. Without that, a
  ``scripts.packs.*`` spelling resolves from the repo and the test
  passes while the wheel is broken. The subprocess proves its own
  isolation rather than trusting an interpreter flag; see
  :func:`_import_in_isolated_subprocess` for why the flags are not
  usable here.
* A static import-closure assertion. The runtime layer only executes
  eager imports; ``auth.py`` imports ``source_fetch`` inside a function
  to avoid a cycle, so dropping ``source_fetch`` from ``MODULES`` would
  not surface by importing ``auth`` alone. The AST walk sees imports at
  any nesting depth.

``MODULES`` stays hand-maintained rather than computed so the shipped
surface stays reviewable; these tests are what make that safe.
"""
from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPI = ROOT / "packages" / "pypi"
VENDORED = PYPI / "anywhere_agents" / "packs"
SRC = ROOT / "scripts" / "packs"


def _load_vendor_module():
    spec = importlib.util.spec_from_file_location(
        "vendor_packs", ROOT / "scripts" / "vendor-packs.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _module_stems() -> list[str]:
    return sorted(
        p.stem for p in VENDORED.glob("*.py") if p.name != "__init__.py"
    )


def _import_in_isolated_subprocess(names: list[str]) -> subprocess.CompletedProcess:
    """Import each vendored module where the source checkout cannot help.

    Isolation is proven inside the subprocess rather than delegated to an
    interpreter flag. Flags were tried first and are the wrong tool: ``-P``
    (drop the cwd entry, which is the part that matters here) is 3.11+ and
    the CI floor is 3.9, while ``-I`` additionally implies ``-s`` and drops
    user site-packages. That last one is not hypothetical: on the aarch64
    Linux box used for cross-platform validation, PyYAML was user-installed,
    so ``-I`` made ``schema.py``'s ``import yaml`` fail and the test
    reported a missing-from-MODULES error that had nothing to do with
    MODULES.

    What remains is ``-E`` (ignore ``PYTHON*`` env vars, available on every
    supported version), a neutral working directory so the implicit
    ``sys.path`` entry points at nothing, and an explicit check that the
    checkout really is out of reach.
    """
    body = "\n".join(f"import anywhere_agents.packs.{n}" for n in names)
    script = textwrap.dedent(
        """
        import sys
        sys.path.insert(0, {pypi!r})

        try:
            import scripts.packs.auth  # noqa: F401
        except ImportError:
            pass
        else:
            sys.stderr.write("ISOLATION-FAILED: source checkout is importable\\n")
            raise SystemExit(3)

        {body}
        print("OK")
        """
    ).format(pypi=str(PYPI), body=body)
    with tempfile.TemporaryDirectory() as neutral:
        return subprocess.run(
            [sys.executable, "-E", "-c", script],
            capture_output=True,
            text=True,
            cwd=neutral,
        )


def _sibling_imports(source: Path) -> set[str]:
    """Return sibling module names ``source`` imports, at any depth.

    Five spellings reach a sibling and all five have to be recognised:

    * ``from scripts.packs import x`` (what the vendoring rewrites)
    * ``from scripts.packs.x import Y`` (absolute submodule)
    * ``from . import x``
    * ``from .x import y``
    * ``import scripts.packs.x``

    The absolute-submodule form was missed in the first version. It is
    the one a lazy import naturally takes when it wants a name rather
    than a module, so leaving it out defeated the check for exactly the
    case the check exists to catch.

    Function-local imports count: ``ast.walk`` visits every node, not
    only module-level ones.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "scripts.packs" or (
                node.level and not node.module
            ):
                found.update(a.name for a in node.names)
            elif node.level and node.module:
                found.add(node.module.split(".")[0])
            elif node.level == 0 and node.module and node.module.startswith(
                "scripts.packs."
            ):
                found.add(node.module.split(".")[2])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("scripts.packs."):
                    found.add(alias.name.split(".")[2])
    return found


class VendoredPackageImportTests(unittest.TestCase):
    def test_vendored_dir_exists(self) -> None:
        self.assertTrue(
            VENDORED.is_dir(), f"vendored package missing at {VENDORED}"
        )

    def test_every_vendored_module_imports_in_isolation(self) -> None:
        names = _module_stems()
        self.assertTrue(names, "no vendored modules found")
        result = _import_in_isolated_subprocess(names)
        self.assertNotEqual(
            result.returncode,
            3,
            "the source checkout was importable from the subprocess, so "
            "this test could pass on an import spelling an installed wheel "
            f"cannot satisfy.\nstderr:\n{result.stderr}",
        )
        self.assertEqual(
            result.returncode,
            0,
            "vendored package failed to import out-of-tree; a module "
            "reachable from a vendored import is probably missing from "
            f"MODULES in scripts/vendor-packs.py.\nstderr:\n{result.stderr}",
        )

    def test_the_isolation_check_is_not_vacuous(self) -> None:
        """Guard the guard. The in-subprocess control only means anything
        if the checkout genuinely IS importable when isolation is dropped;
        otherwise it would pass on any machine for the wrong reason."""
        result = subprocess.run(
            [sys.executable, "-c", "import scripts.packs.auth; print('reachable')"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        self.assertEqual(
            result.returncode,
            0,
            "expected the source checkout to be importable from the repo "
            "root; if it is not, the isolation control in "
            "test_every_vendored_module_imports_in_isolation proves nothing."
            f"\nstderr:\n{result.stderr}",
        )

    def test_vendor_module_list_covers_the_vendored_dir(self) -> None:
        """``MODULES`` and the on-disk vendored tree must agree, so a
        stale extra copy cannot linger after a module is dropped."""
        mod = _load_vendor_module()
        self.assertEqual(
            sorted(name[: -len(".py")] for name in mod.MODULES),
            _module_stems(),
        )

    def test_module_list_is_closed_under_sibling_imports(self) -> None:
        """Every sibling a listed module imports must itself be listed,
        including imports nested inside functions."""
        mod = _load_vendor_module()
        listed = {name[: -len(".py")] for name in mod.MODULES}
        for name in sorted(mod.MODULES):
            source = SRC / name
            with self.subTest(module=name):
                missing = _sibling_imports(source) - listed
                self.assertEqual(
                    missing,
                    set(),
                    f"{source.relative_to(ROOT)} imports {sorted(missing)}, "
                    "which is not in MODULES; the vendored package would "
                    "raise ImportError once installed",
                )

    def test_closure_check_sees_function_local_imports(self) -> None:
        """Guard the guard: the AST walk must not be module-level only,
        or ``auth.py``'s cycle-avoiding local import would be invisible."""
        found = _sibling_imports(SRC / "auth.py")
        self.assertIn(
            "source_fetch",
            found,
            "auth.py imports source_fetch inside a function; if this "
            "assertion fails the closure check has regressed to "
            "module-level imports only and no longer protects anything",
        )

    def test_closure_check_sees_every_sibling_spelling(self) -> None:
        """Guard the guard, part two. The absolute-submodule form was
        missed originally, and a runtime import of ``auth`` alone would
        not have surfaced it either, so both layers were blind at once."""
        import tempfile

        spellings = {
            "from scripts.packs import source_fetch":
                "def f():\n    from scripts.packs import source_fetch\n",
            "from scripts.packs.source_fetch import PackArchive":
                "def f():\n"
                "    from scripts.packs.source_fetch import PackArchive\n",
            "import scripts.packs.source_fetch":
                "def f():\n    import scripts.packs.source_fetch\n",
            "from . import source_fetch":
                "from . import source_fetch\n",
            "from .source_fetch import PackArchive":
                "from .source_fetch import PackArchive\n",
        }
        with tempfile.TemporaryDirectory() as td:
            probe = Path(td) / "probe.py"
            for label, body in spellings.items():
                with self.subTest(spelling=label):
                    probe.write_text(body, encoding="utf-8")
                    self.assertIn("source_fetch", _sibling_imports(probe))


if __name__ == "__main__":
    unittest.main()
