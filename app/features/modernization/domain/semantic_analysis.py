from enum import StrEnum

from app.features.modernization.domain.enums import GenerationStrategy
from app.shared.domain.value_object import ValueObject


class Feature(StrEnum):
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


class DetectedFeature(ValueObject):
    feature: Feature
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
    features: tuple[DetectedFeature, ...] = ()
    risks: tuple[SemanticRisk, ...] = ()
    dependencies: tuple[Dependency, ...] = ()
    parameters: ParameterSummary = ParameterSummary()
    variables: tuple[str, ...] = ()
    recommended_strategy: GenerationStrategy
    recommendations: tuple[Recommendation, ...] = ()

    def has(self, feature: Feature) -> bool:
        return any(detected.feature is feature for detected in self.features)

    @property
    def feature_names(self) -> tuple[Feature, ...]:
        return tuple(detected.feature for detected in self.features)
