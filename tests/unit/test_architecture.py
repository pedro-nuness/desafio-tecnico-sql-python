"""Dependency rule checks for Package by Feature architecture."""

import ast
from pathlib import Path

import pytest

APP = Path(__file__).parents[2] / "app"

VENDORS = {
    "openai",
    "openrouter",
    "anthropic",
    "google",
    "sqlalchemy",
    "asyncpg",
    "pglast",
    "ruff",
    "alembic",
}

# Features depend on the LLMProvider port only; adapters are wired by the composition root.
LLM_ADAPTERS = {
    "app.shared.integrations.llm.factory",
    "app.shared.integrations.llm.openai",
    "app.shared.integrations.llm.openrouter",
}

FORBIDDEN: dict[str, set[str]] = {
    # shared/integrations holds vendor adapters; the rest of shared stays vendor-free.
    "shared": {"fastapi", "langgraph", "app.core", "app.features"},
    "shared/domain": VENDORS,
    "shared/resilience": VENDORS,
    "core/config": VENDORS | {"fastapi", "langgraph", "app.features"},
    "core/database": {"fastapi", "langgraph", "app.features", "openai", "pglast", "ruff"},
    "features/modernization/domain": VENDORS
    | LLM_ADAPTERS
    | {
        "langgraph",
        "fastapi",
        "app.core.bootstrap",
        "app.features.modernization.application",
        "app.features.modernization.infrastructure",
        "app.features.modernization.graph",
        "app.features.modernization.api",
    },
    "features/modernization/application": VENDORS
    | LLM_ADAPTERS
    | {
        "langgraph",
        "fastapi",
        "app.core.bootstrap",
        "app.features.modernization.infrastructure",
        "app.features.modernization.graph",
        "app.features.modernization.api",
    },
    "features/modernization/prompts": VENDORS
    | LLM_ADAPTERS
    | {
        "langgraph",
        "fastapi",
        "app.core.bootstrap",
        "app.features.modernization.infrastructure",
        "app.features.modernization.graph",
        "app.features.modernization.api",
    },
    "features/modernization/graph": VENDORS
    | LLM_ADAPTERS
    | {
        "fastapi",
        "app.core.bootstrap",
        "app.features.modernization.infrastructure",
        "app.features.modernization.api",
    },
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _violations(rel_path: str) -> list[str]:
    found = []
    target_dir = APP / Path(rel_path)
    for path in target_dir.rglob("*.py"):
        for imported in _imports(path):
            for forbidden in FORBIDDEN[rel_path]:
                if imported == forbidden or imported.startswith(forbidden + "."):
                    found.append(f"{path.relative_to(APP)} imports {imported}")
    return found


@pytest.mark.parametrize("target", sorted(FORBIDDEN))
def test_package_by_feature_respects_dependency_rule(target: str) -> None:
    assert _violations(target) == []


@pytest.mark.parametrize("vendor", ["openai", "openrouter"])
def test_only_the_vendor_adapter_imports_its_sdk(vendor: str) -> None:
    offenders = [
        str(path.relative_to(APP)).replace("\\", "/")
        for path in APP.rglob("*.py")
        if any(i == vendor or i.startswith(vendor + ".") for i in _imports(path))
    ]
    assert sorted(offenders) == [
        "core/exception_handlers.py",
        f"shared/integrations/llm/{vendor}/provider.py",
    ]


def test_only_the_parsing_adapter_imports_pglast() -> None:
    offenders = [
        str(path.relative_to(APP)).replace("\\", "/")
        for path in APP.rglob("*.py")
        if any(i == "pglast" or i.startswith("pglast.") for i in _imports(path))
    ]
    assert sorted(offenders) == [
        "core/exception_handlers.py",
        "features/modernization/infrastructure/parsing/pglast_parser.py",
    ]


def test_no_init_py_files_exist_in_app() -> None:
    inits = list(APP.rglob("__init__.py"))
    assert inits == []


def test_app_has_no_local_exception_handlers() -> None:
    offenders = [
        str(path.relative_to(APP))
        for path in APP.rglob("*.py")
        if any(
            isinstance(node, ast.ExceptHandler)
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    ]
    assert offenders == []
