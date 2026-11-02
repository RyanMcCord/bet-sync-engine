"""The hard rules in METHODOLOGY.md that ruff can't check, enforced as tests.

1. The hexagonal layers: each layer imports only what it's allowed to.
2. Every module, class and function has a docstring, including private and nested functions.

If one of these fails, fix the code, not the test. Changing a rule means changing
METHODOLOGY.md and this file together, in the same commit.
"""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "betsync"
STDLIB = set(sys.stdlib_module_names) | {"__future__"}

# Which betsync modules each layer may import. A layer may always import from inside itself.
ALLOWED = {
    "domain": set(),
    "ports": {"domain"},
    "application": {"domain", "ports"},
    "adapters": {"domain", "ports"},
    "adapters.api": {"domain", "ports", "application"},   # the driving adapter calls the use cases
    "simulator": {"domain"},                               # reference data only (team names)
    "composition": None,                                   # __main__ and config: may wire anything
}
# Layers that may use only the standard library (plus betsync modules they're allowed).
STDLIB_ONLY = {"domain", "ports", "application"}


def layer_of(path: Path) -> str:
    """The layer a source file belongs to."""
    parts = path.relative_to(PACKAGE).with_suffix("").parts
    if parts[0] in ("__main__", "config", "__init__"):
        return "composition"
    if parts[0] == "adapters" and len(parts) > 1 and parts[1] == "api":
        return "adapters.api"
    return parts[0]


def adapter_of(path: Path) -> str | None:
    """Which adapter a file is part of (providers, sqlite_store, api), or None outside adapters."""
    parts = path.relative_to(PACKAGE).with_suffix("").parts
    return parts[1] if parts[0] == "adapters" and len(parts) > 1 and parts[1] != "__init__" else None


def module_name(path: Path) -> str:
    """The dotted module name of a source file, e.g. betsync.adapters.api."""
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imports(path: Path):
    """Every module a file imports, as absolute dotted names (relative imports resolved)."""
    tree = ast.parse(path.read_text())
    package = module_name(path).split(".")
    if path.name != "__init__.py":
        package = package[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                yield ".".join(base + ([node.module] if node.module else []))
            else:
                yield node.module


def target_layer(module: str) -> str | None:
    """The layer an imported betsync module belongs to, or None if it isn't a betsync module."""
    parts = module.split(".")
    if parts[0] != "betsync":
        return None
    if len(parts) == 1:
        return "composition"
    if parts[1] in ("__main__", "config"):
        return "composition"
    if parts[1] == "adapters" and len(parts) > 2 and parts[2] == "api":
        return "adapters.api"
    return parts[1]


def source_files():
    """Every Python file in the betsync package."""
    return sorted(PACKAGE.rglob("*.py"))


class HexagonalLayerTests(unittest.TestCase):
    """Each layer depends only on the layers inside it."""

    def test_layers_import_only_what_they_are_allowed(self):
        """domain imports no other layer; ports only domain; application only domain and ports;
        adapters only domain and ports (the API may also call application); the simulator only
        domain.
        """
        for path in source_files():
            layer = layer_of(path)
            allowed = ALLOWED[layer]
            if allowed is None:
                continue
            for module in imports(path):
                target = target_layer(module)
                if target is None or target == layer or (layer == "adapters.api" and target == "adapters"):
                    continue
                with self.subTest(file=str(path.relative_to(ROOT)), imports=module):
                    self.assertIn(target, allowed, f"the {layer} layer may not import from {target}")

    def test_inner_layers_use_only_the_standard_library(self):
        """domain, ports and application never import third-party packages like FastAPI or requests."""
        for path in source_files():
            if layer_of(path) not in STDLIB_ONLY:
                continue
            for module in imports(path):
                top = module.split(".")[0]
                with self.subTest(file=str(path.relative_to(ROOT)), imports=module):
                    self.assertTrue(top == "betsync" or top in STDLIB,
                                    f"{layer_of(path)} may only use the standard library, not {module!r}")

    def test_adapters_never_import_each_other(self):
        """providers, sqlite_store and api are independent: none imports another."""
        for path in source_files():
            mine = adapter_of(path)
            if mine is None:
                continue
            for module in imports(path):
                parts = module.split(".")
                if parts[:2] == ["betsync", "adapters"] and len(parts) > 2:
                    with self.subTest(file=str(path.relative_to(ROOT)), imports=module):
                        self.assertEqual(parts[2], mine, "adapters may not import other adapters")

    def test_the_conflict_rule_is_only_called_by_the_application(self):
        """decide() is domain logic; the use case runs it, never an adapter."""
        for path in source_files():
            if layer_of(path) in ("domain", "application"):
                continue
            tree = ast.parse(path.read_text())
            names = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
            with self.subTest(file=str(path.relative_to(ROOT))):
                self.assertNotIn("decide", names, "only the application may run the conflict rule")


class DocstringTests(unittest.TestCase):
    """Every module, class and function has a docstring."""

    def test_everything_is_documented(self):
        """Private and nested functions included, in the package and in the tests."""
        files = source_files() + sorted((ROOT / "tests").rglob("*.py"))
        for path in files:
            tree = ast.parse(path.read_text())
            with self.subTest(file=str(path.relative_to(ROOT))):
                self.assertTrue(ast.get_docstring(tree), "module has no docstring")
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    with self.subTest(file=str(path.relative_to(ROOT)), line=node.lineno, name=node.name):
                        self.assertTrue(ast.get_docstring(node), f"{node.name} has no docstring")


if __name__ == "__main__":
    unittest.main()
