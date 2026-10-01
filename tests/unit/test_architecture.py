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
    "langfuse",
}

# Features depend on the LLM port (llm.llm) only; the gateway, its configuration and the
# providers are wired by the composition root.
LLM_ADAPTERS = {
    "app.shared.integrations.llm.config",
    "app.shared.integrations.llm.gateway",
    "app.shared.integrations.llm.registry",
    "app.shared.integrations.llm.tracing",
    "app.shared.integrations.llm.openai",
    "app.shared.integrations.llm.openrouter",
}

# The composition root (core/providers.py + bootstrap.py) wires everything; nothing it builds
# may reach back into it. Only entry points (core, features/*/api routes) know the DI library.
COMPOSITION_ROOT = {"app.core.bootstrap", "app.core.providers", "app.core.server"}
# Only infrastructure (repositories, composition root) knows core/database and core/config.
CORE_INFRASTRUCTURE = {"app.core.database", "app.core.config"}
DI = {"dishka"}

FEATURE = "app.features.modernization"
ENTRY = {f"{FEATURE}.routes", f"{FEATURE}.schemas"}
# Business logic and contracts: no vendor, no framework, no infrastructure, no DI.
PURE = (
    VENDORS | LLM_ADAPTERS | DI | CORE_INFRASTRUCTURE | {"fastapi", "langgraph", "langchain_core"}
)
# Implementations (strategies, repository, graph) never know the HTTP layer or the DI library.
IMPLEMENTATION = DI | {"fastapi"} | ENTRY | {f"{FEATURE}.use_cases"}
# What each step produces (its domain.py) and the execution aggregate: plain data and rules.
DOMAIN_FILES = (
    "features/modernization/domain.py",
    "features/modernization/parsing/domain.py",
    "features/modernization/analysis/domain.py",
    "features/modernization/generation/domain.py",
    "features/modernization/validation/domain.py",
    "features/modernization/validation/checks/behavior/domain.py",
    "features/modernization/evaluation/domain.py",
)
DOMAIN_MODULES = {"app." + path.removesuffix(".py").replace("/", ".") for path in DOMAIN_FILES}

FORBIDDEN: dict[str, set[str]] = {
    **{path: PURE | ENTRY for path in DOMAIN_FILES},
    # shared/integrations holds vendor adapters; the rest of shared stays vendor-free.
    "shared": {"fastapi", "langgraph", "app.core", "app.features"} | DI,
    "features": COMPOSITION_ROOT,
    "shared/domain": VENDORS,
    "shared/resilience": VENDORS,
    "core/config": VENDORS | {"fastapi", "langgraph", "app.features"},
    "core/database": {"fastapi", "langgraph", "app.features", "openai", "pglast", "ruff"},
    "features/modernization/use_cases.py": PURE | ENTRY,
    "features/modernization/generation": PURE | IMPLEMENTATION | {f"{FEATURE}.graph"},
    "features/modernization/parsing/parser.py": PURE | IMPLEMENTATION,
    "features/modernization/analysis": PURE | IMPLEMENTATION | {f"{FEATURE}.graph"},
    "features/modernization/validation/validate_code.py": PURE | IMPLEMENTATION,
    "features/modernization/persistence/execution_log.py": PURE
    | IMPLEMENTATION
    | {f"{FEATURE}.graph"},
    "features/modernization/parsing": IMPLEMENTATION | {f"{FEATURE}.graph"},
    "features/modernization/validation": IMPLEMENTATION | {f"{FEATURE}.graph"},
    "features/modernization/persistence": IMPLEMENTATION | {f"{FEATURE}.graph"},
    # The evaluation runs generated code on its own database; it never drives the pipeline.
    "features/modernization/evaluation": IMPLEMENTATION | {f"{FEATURE}.graph"},
    "features/modernization/graph": (VENDORS - {"langgraph"})
    | LLM_ADAPTERS
    | CORE_INFRASTRUCTURE
    | IMPLEMENTATION,
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
    target = APP / Path(rel_path)
    assert target.exists(), f"rule targets a missing path: {rel_path}"
    for path in [target] if target.suffix == ".py" else target.rglob("*.py"):
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


# Inside the feature, each library is known by the implementation that wraps it, only.
FEATURE_VENDOR_HOMES = {
    "pglast": ("features/modernization/parsing/plpgsql.py",),
    "ruff": ("features/modernization/validation/checks/lint.py",),
    "sqlalchemy": (
        "features/modernization/evaluation/models.py",
        "features/modernization/evaluation/repository.py",
        "features/modernization/persistence/models.py",
        "features/modernization/persistence/repository.py",
        "features/modernization/validation/checks/behavior/harness.py",
    ),
    "langgraph": ("features/modernization/graph/builder.py",),
    "langchain_core": ("features/modernization/graph/builder.py",),
}


@pytest.mark.parametrize("vendor", sorted(FEATURE_VENDOR_HOMES))
def test_each_library_is_imported_only_by_its_implementation(vendor: str) -> None:
    offenders = tuple(
        sorted(
            str(path.relative_to(APP)).replace("\\", "/")
            for path in (APP / "features").rglob("*.py")
            if any(i == vendor or i.startswith(vendor + ".") for i in _imports(path))
        )
    )
    assert offenders == FEATURE_VENDOR_HOMES[vendor]


def _feature_imports(path: Path) -> set[str]:
    return {i for i in _imports(path) if i.startswith(FEATURE + ".")}


@pytest.mark.parametrize("entry", ["routes.py", "schemas.py"])
def test_http_layer_only_knows_use_cases_schemas_and_domain(entry: str) -> None:
    allowed = {f"{FEATURE}.use_cases", f"{FEATURE}.schemas"} | DOMAIN_MODULES
    imports = _feature_imports(APP / "features/modernization" / entry)
    assert sorted(imports - allowed) == []


@pytest.mark.parametrize("domain_file", DOMAIN_FILES)
def test_domain_files_only_import_other_domain_files(domain_file: str) -> None:
    """A step's domain.py never reaches into another step's logic, only into its data."""
    assert sorted(_feature_imports(APP / domain_file) - DOMAIN_MODULES) == []


def test_every_use_case_exposes_a_single_async_execute() -> None:
    """Commands/queries are plain data; every other class is one use case = one `execute`."""
    tree = ast.parse((APP / "features/modernization/use_cases.py").read_text(encoding="utf-8"))
    use_cases = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and not node.name.endswith(("Command", "Query"))
    ]
    assert use_cases
    for use_case in use_cases:
        public = [
            node
            for node in use_case.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and not node.name.startswith("_")
        ]
        assert [(type(m), m.name) for m in public] == [(ast.AsyncFunctionDef, "execute")], (
            use_case.name
        )


def test_no_init_py_files_exist_in_app() -> None:
    inits = list(APP.rglob("__init__.py"))
    assert inits == []


# Errors are handled centrally (core/exception_handlers.py). `except` is allowed only where
# the outcome must be observed locally: SDK error translation + retries (Integration),
# breaker state, route failover (LLMGateway), failure persistence, and native errors that
# carry domain meaning (invalid SQL, syntax errors feeding the repair loop, an LLM answer
# off contract), and the evaluation, where a failing call is the observation.
LOCAL_EXCEPT_ALLOWED = [
    "features/modernization/generation/generate_code.py",
    "features/modernization/graph/builder.py",
    "features/modernization/parsing/plpgsql.py",
    "features/modernization/validation/checks/behavior/check.py",
    "features/modernization/validation/checks/behavior/harness.py",
    "features/modernization/validation/checks/syntax.py",
    "shared/integrations/integration.py",
    "shared/integrations/llm/gateway.py",
    "shared/integrations/llm/tracing.py",
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


# The error class decides the HTTP status (see app/shared/errors.py), so a module may only
# claim the faults it can know about: a dependency failing is known at the integration edge
# (or by the generation step validating the LLM answer); "not found" only by persistence.
ERROR_CLASSES_ALLOWED_IN = {
    "IntegrationError": ("shared/integrations/", "features/modernization/generation/"),
    "NotFoundError": ("features/modernization/persistence/",),
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
