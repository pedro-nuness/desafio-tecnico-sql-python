from enum import StrEnum

from app.shared.domain.value_object import ValueObject


class GenerationStrategy(StrEnum):
    """Where the relational logic lives after modernization."""

    DATABASE_DELEGATED = "database_delegated"
    """Set-based SQL stays in PostgreSQL; Python only orchestrates the call."""

    PYTHON_REIMPLEMENTATION = "python_reimplementation"
    """Logic is rewritten in Python (no relational work to keep in the database)."""

    HYBRID = "hybrid"
    """Python owns control flow and errors; relational operations remain parameterized SQL."""


class SqlConstruct(StrEnum):
    IN_PARAMETERS = "in_parameters"
    OUT_PARAMETERS = "out_parameters"
    VARIABLES = "variables"
    CURSOR = "cursor"
    LOOP = "loop"
    CONDITIONAL = "conditional"
    TRANSACTION_CONTROL = "transaction_control"
    EXCEPTION_HANDLING = "exception_handling"
    RAISE = "raise"
    GET_DIAGNOSTICS = "get_diagnostics"
    ROW_LOCKING = "row_locking"
    JSONB = "jsonb"
    CTE = "cte"
    RECURSIVE_CTE = "recursive_cte"
    FUNCTION_CALLS = "function_calls"
    RETURN_QUERY = "return_query"
    DYNAMIC_SQL = "dynamic_sql"
    DML = "dml"
    AGGREGATION = "aggregation"
    JOIN = "join"


class RiskSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class DependencyKind(StrEnum):
    TABLE = "table"
    FUNCTION = "function"


class DetectedSqlConstruct(ValueObject):
    construct: SqlConstruct
    occurrences: int
    lines: tuple[int, ...] = ()


class SemanticRisk(ValueObject):
    code: str
    severity: RiskSeverity
    message: str
    line: int | None = None


class Dependency(ValueObject):
    name: str
    kind: DependencyKind


class ParameterSummary(ValueObject):
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    in_out: tuple[str, ...] = ()


class Recommendation(ValueObject):
    code: str
    message: str


class SemanticAnalysis(ValueObject):
    constructs: tuple[DetectedSqlConstruct, ...] = ()
    risks: tuple[SemanticRisk, ...] = ()
    dependencies: tuple[Dependency, ...] = ()
    parameters: ParameterSummary = ParameterSummary()
    variables: tuple[str, ...] = ()
    recommended_strategy: GenerationStrategy
    recommendations: tuple[Recommendation, ...] = ()

    def has(self, construct: SqlConstruct) -> bool:
        return any(detected.construct is construct for detected in self.constructs)

    @property
    def construct_names(self) -> tuple[SqlConstruct, ...]:
        return tuple(detected.construct for detected in self.constructs)
