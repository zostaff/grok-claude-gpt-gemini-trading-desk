"""An architecture test: the domain must not import I/O.

This is the property the whole layout exists to protect. Enforcing it in a test rather
than in a code-review convention means it cannot quietly rot -- the day someone reaches
for httpx inside the consensus engine, this fails.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

DOMAIN = pathlib.Path(__file__).resolve().parents[3] / "src" / "trading_desk" / "domain"

FORBIDDEN = {
    "httpx", "websockets", "requests", "aiohttp", "socket", "urllib",
    "anthropic", "openai", "google", "PIL",
}


def _imported_roots(path: pathlib.Path) -> set[str]:
    """Every top-level module name imported by one file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("path", sorted(DOMAIN.glob("*.py")), ids=lambda p: p.name)
def test_domain_module_imports_no_io(path: pathlib.Path):
    offenders = _imported_roots(path) & FORBIDDEN
    assert not offenders, f"{path.name} imports I/O: {sorted(offenders)}"


def test_domain_does_not_import_adapters():
    """The dependency arrow points inward: adapters know the domain, never the reverse."""
    for path in DOMAIN.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "adapters" not in source, f"{path.name} references the adapters package"
