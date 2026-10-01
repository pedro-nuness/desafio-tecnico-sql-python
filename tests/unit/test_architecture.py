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

# Features depend on the LLM port (llm.llm) only; the gateway, its configuration and the
# providers are wired by the composition root.
LLM_ADAPTERS = {
    "app.shared.integrations.llm.config",
    "app.shared.integrations.llm.gateway",
    "app.shared.integrations.llm.registry",
    "app.shared.integrations.llm.openai",
    "app.shared.integrations.llm.openrouter",
}

# The composition root (core/providers.py + bootstrap.py) wires everything; nothing it builds
# may reach back into it. Only entry points (core, features/*/api routes) know the DI library.
COMPOSITION_ROOT = {"app.core.bootstrap", "app.core.providers", "app.core.server"}
# Use cases and the graph open transactions through the port (app.shared.persistence); only
# infrastructure (repositories) knows the SQLAlchemy implementation in core/database.
CORE_INFRASTRUCTURE = {"app.core.database", "app.core.config"}
DI = {"dishka"}

FORBIDDEN: dict[str, set[str]] = {
    # shared/integrations holds vendor adapters; the rest of shared stays vendor-free.
    "shared": {"fastapi", "langgraph", "app.core", "app.features"} | DI,
    "features": COMPOSITION_ROOT,
    "shared/domain": VENDORS,
    "shared/resilience": VENDORS,
    "core/config": VENDORS | {"fastapi", "langgraph", "app.features"},
    "core/database": {"fastapi", "langgraph", "app.features", "openai", "pglast", "ruff"},
    "features/modernization/infrastructure": DI,
    "features/modernization/domain": VENDORS
    | LLM_ADAPTERS
    | DI
    | CORE_INFRASTRUCTURE
    | {
        "langgraph",
        "fastapi",
        "app.features.modernization.application",
        "app.features.modernization.infrastructure",
        "app.features.modernization.graph",
        "app.features.modernization.api",
    },
    "features/modernization/application": VENDORS
    | LLM_ADAPTERS
    | DI
    | CORE_INFRASTRUCTURE
    | {
        "langgraph",
        "fastapi",
        "app.features.modernization.infrastructure",
        "app.features.modernization.graph",
        "app.features.modernization.api",
    },
    "features/modernization/prompts": VENDORS
    | LLM_ADAPTERS
    | DI
    | CORE_INFRASTRUCTURE
    | {
        "langgraph",
        "fastapi",
        "app.features.modernization.infrastructure",
        "app.features.modernization.graph",
        "app.features.modernization.api",
    },
    "features/modernization/graph": VENDORS
    | LLM_ADAPTERS
    | DI
    | CORE_INFRASTRUCTURE
    | {
        "fastapi",
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
    # Not even core/exception_handlers.py: Integration translates SDK errors at the edge.
    assert offenders == [f"shared/integrations/llm/{vendor}/provider.py"]


def test_only_the_parsing_adapter_imports_pglast() -> None:
    offenders = [
        str(path.relative_to(APP)).replace("\\", "/")
        for path in APP.rglob("*.py")
        if any(i == "pglast" or i.startswith("pglast.") for i in _imports(path))
    ]
    assert offenders == ["features/modernization/infrastructure/parsing/pglast_parser.py"]


def test_no_init_py_files_exist_in_app() -> None:
    inits = list(APP.rglob("__init__.py"))
    assert inits == []


# Errors are handled centrally (core/exception_handlers.py). `except` is allowed only where
# the outcome must be observed locally: SDK error translation + retries (Integration),
# breaker state, route failover (LLMGateway), failure persistence, and native errors that
# carry domain meaning (invalid SQL, syntax errors feeding the repair loop, an LLM answer
# off contract).
LOCAL_EXCEPT_ALLOWED = [
    "features/modernization/application/services/code_generation_service.py",
    "features/modernization/graph/builder.py",
    "features/modernization/infrastructure/parsing/pglast_parser.py",
    "features/modernization/infrastructure/validation/python_ast_validator.py",
    "shared/integrations/integration.py",
    "shared/integrations/llm/gateway.py",
    "shared/resilience/circuit_breaker.py",
]


def test_local_exception_handlers_only_where_needed() -> None:
    offenders = sorted(
        str(path.relative_to(APP)).replace("\\", "/")
        for path in APP.rglob("*.py")
        if any(
            isinstance(node, ast.ExceptHandler)
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    )
    assert offenders == LOCAL_EXCEPT_ALLOWED


# The error class decides the HTTP status (see app/shared/errors.py), so a layer may only
# claim the faults it can know about: a dependency failing is known at the integration edge
# (or by the use case validating its answer); "not found" is never a domain rule.
ERROR_CLASSES_ALLOWED_IN = {
    "IntegrationError": ("shared/integrations/", "features/modernization/application/"),
    "NotFoundError": (
        "features/modernization/application/",
        "features/modernization/graph/",
        "features/modernization/infrastructure/",
    ),
}


@pytest.mark.parametrize("error_class", sorted(ERROR_CLASSES_ALLOWED_IN))
def test_error_classes_are_raised_only_by_the_layers_that_own_them(error_class: str) -> None:
    offenders = [
        str(path.relative_to(APP)).replace("\\", "/")
        for path in APP.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == error_class
    ]
    allowed = ERROR_CLASSES_ALLOWED_IN[error_class]
    assert [o for o in offenders if not o.startswith(allowed)] == []
