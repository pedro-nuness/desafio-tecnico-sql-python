"""Dependency rule checks: inner layers never import vendors or outer layers."""

import ast
from pathlib import Path

import pytest

APP = Path(__file__).parents[2] / "app"

VENDORS = {"openai", "anthropic", "google", "sqlalchemy", "asyncpg", "pglast", "ruff", "alembic"}

FORBIDDEN: dict[str, set[str]] = {
    "domain": VENDORS | {"langgraph", "fastapi", "app.application", "app.infrastructure",
                         "app.graph", "app.api", "app.bootstrap", "app.config"},
    "application": VENDORS | {"langgraph", "fastapi", "app.infrastructure", "app.graph",
                              "app.api", "app.bootstrap"},
    "prompts": VENDORS | {"langgraph", "app.infrastructure", "app.graph", "app.api"},
    "graph": VENDORS | {"fastapi", "app.infrastructure", "app.api", "app.bootstrap"},
}  # fmt: skip


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _violations(layer: str) -> list[str]:
    found = []
    for path in (APP / layer).rglob("*.py"):
        for imported in _imports(path):
            for forbidden in FORBIDDEN[layer]:
                if imported == forbidden or imported.startswith(forbidden + "."):
                    found.append(f"{path.relative_to(APP)} imports {imported}")
    return found


@pytest.mark.parametrize("layer", sorted(FORBIDDEN))
def test_layer_respects_dependency_rule(layer: str) -> None:
    assert _violations(layer) == []


def test_only_the_llm_adapters_import_openai() -> None:
    offenders = [
        str(path.relative_to(APP))
        for path in APP.rglob("*.py")
        if any(i == "openai" or i.startswith("openai.") for i in _imports(path))
    ]
    assert offenders == [str(Path("infrastructure/llm/openai_provider.py"))]


def test_only_the_parsing_adapter_imports_pglast() -> None:
    offenders = [
        str(path.relative_to(APP))
        for path in APP.rglob("*.py")
        if any(i == "pglast" or i.startswith("pglast.") for i in _imports(path))
    ]
    assert offenders == [str(Path("infrastructure/parsing/pglast_parser.py"))]
