"""Deterministic semantic analysis over the parser-agnostic IR.

Pure domain logic: no I/O, no parser library. It is intentionally not behind a port —
there is a single, deterministic implementation and nothing external to swap.
"""

from collections import defaultdict
from collections.abc import Iterator

from app.features.modernization.domain.enums import GenerationStrategy
from app.features.modernization.domain.parsing import (
    LOOP_KINDS,
    DeclarationKind,
    ParameterMode,
    ParsedProcedure,
    SqlCommand,
    SqlFragment,
    Statement,
    StatementKind,
)
from app.features.modernization.domain.semantic_analysis import (
    Dependency,
    DependencyKind,
    DetectedFeature,
    Feature,
    ParameterSummary,
    Recommendation,
    RiskSeverity,
    SemanticAnalysis,
    SemanticRisk,
)

AGGREGATE_FUNCTIONS = frozenset(
    {
        "count", "sum", "avg", "min", "max", "array_agg", "string_agg", "json_agg",
        "jsonb_agg", "json_object_agg", "jsonb_object_agg", "bool_and", "bool_or",
        "every", "stddev", "variance", "percentile_cont", "percentile_disc", "mode",
    }
)  # fmt: skip

BUILTIN_FUNCTIONS = AGGREGATE_FUNCTIONS | frozenset(
    {
        "abs", "ceil", "floor", "round", "trunc", "greatest", "least", "coalesce", "nullif",
        "now", "current_timestamp", "current_date", "clock_timestamp", "date_trunc",
        "date_part", "extract", "age", "make_interval", "to_char", "to_date", "to_timestamp",
        "lower", "upper", "length", "substr", "substring", "trim", "btrim", "concat",
        "concat_ws", "format", "replace", "split_part", "position", "left", "right",
        "lpad", "rpad", "md5", "gen_random_uuid", "random", "generate_series", "unnest",
        "array_length", "cardinality", "array_append", "row_number", "rank", "dense_rank",
        "lag", "lead", "first_value", "last_value", "nextval", "currval", "setval",
        "jsonb_build_object", "jsonb_build_array", "json_build_object", "json_build_array",
        "to_jsonb", "to_json", "jsonb_set", "jsonb_insert", "jsonb_extract_path",
        "jsonb_extract_path_text", "jsonb_array_elements", "jsonb_array_elements_text",
        "jsonb_each", "jsonb_each_text", "jsonb_object_keys", "jsonb_typeof",
        "jsonb_strip_nulls", "jsonb_array_length", "jsonb_path_query", "row_to_json",
        "quote_ident", "quote_literal", "quote_nullable",
    }
)  # fmt: skip

PROCEDURAL_FEATURES = frozenset(
    {
        Feature.LOOP,
        Feature.CONDITIONAL,
        Feature.EXCEPTION_HANDLING,
        Feature.RAISE,
        Feature.CURSOR,
        Feature.TRANSACTION_CONTROL,
        Feature.DYNAMIC_SQL,
        Feature.GET_DIAGNOSTICS,
    }
)

STATEMENT_FEATURES: dict[StatementKind, Feature] = {
    StatementKind.IF: Feature.CONDITIONAL,
    StatementKind.CASE: Feature.CONDITIONAL,
    StatementKind.COMMIT: Feature.TRANSACTION_CONTROL,
    StatementKind.ROLLBACK: Feature.TRANSACTION_CONTROL,
    StatementKind.RAISE: Feature.RAISE,
    StatementKind.GET_DIAGNOSTICS: Feature.GET_DIAGNOSTICS,
    StatementKind.RETURN_QUERY: Feature.RETURN_QUERY,
    StatementKind.OPEN_CURSOR: Feature.CURSOR,
    StatementKind.FETCH: Feature.CURSOR,
    StatementKind.CLOSE_CURSOR: Feature.CURSOR,
    StatementKind.FOR_CURSOR: Feature.CURSOR,
    StatementKind.CALL: Feature.FUNCTION_CALLS,
}

DATABASE_ROUND_TRIP_KINDS = frozenset(
    {StatementKind.SQL, StatementKind.PERFORM, StatementKind.CALL, StatementKind.DYNAMIC_SQL}
)
DML_COMMANDS = frozenset(
    {SqlCommand.INSERT, SqlCommand.UPDATE, SqlCommand.DELETE, SqlCommand.MERGE}
)


def is_builtin_function(name: str) -> bool:
    schema, _, bare = name.rpartition(".")
    return schema == "pg_catalog" or bare.lower() in BUILTIN_FUNCTIONS


class _FeatureCollector:
    def __init__(self) -> None:
        self._lines: dict[Feature, list[int]] = defaultdict(list)
        self._counts: dict[Feature, int] = defaultdict(int)

    def add(self, feature: Feature, line: int | None = None, occurrences: int = 1) -> None:
        self._counts[feature] += occurrences
        if line is not None and line not in self._lines[feature]:
            self._lines[feature].append(line)

    def __contains__(self, feature: Feature) -> bool:
        return feature in self._counts

    def result(self) -> tuple[DetectedFeature, ...]:
        return tuple(
            DetectedFeature(feature=feature, occurrences=count, lines=tuple(self._lines[feature]))
            for feature, count in sorted(self._counts.items())
        )


class SemanticAnalyzer:
    def analyze(self, procedure: ParsedProcedure) -> SemanticAnalysis:
        features = _FeatureCollector()
        parameters = _summarize_parameters(procedure)
        variables = _collect_declarations(procedure, features)
        if parameters.inputs or parameters.in_out:
            features.add(
                Feature.IN_PARAMETERS, occurrences=len(parameters.inputs + parameters.in_out)
            )
        if parameters.outputs or parameters.in_out:
            features.add(
                Feature.OUT_PARAMETERS, occurrences=len(parameters.outputs + parameters.in_out)
            )
        if any("jsonb" in parameter.data_type.lower() for parameter in procedure.parameters):
            features.add(Feature.JSONB)

        for statement, _ in procedure.iter_statements():
            _collect_statement_features(statement, features)
        for fragment, line in _fragments_with_lines(procedure):
            _collect_sql_features(fragment, line, features)

        risks = _detect_risks(procedure, features)
        strategy = _recommend_strategy(procedure, features)
        return SemanticAnalysis(
            features=features.result(),
            risks=risks,
            dependencies=_collect_dependencies(procedure),
            parameters=parameters,
            variables=variables,
            recommended_strategy=strategy,
            recommendations=_recommend(features, risks),
        )


def _summarize_parameters(procedure: ParsedProcedure) -> ParameterSummary:
    def names(*modes: ParameterMode) -> tuple[str, ...]:
        return tuple(p.name for p in procedure.parameters if p.mode in modes)

    return ParameterSummary(
        inputs=names(ParameterMode.IN, ParameterMode.VARIADIC),
        outputs=names(ParameterMode.OUT, ParameterMode.TABLE),
        in_out=names(ParameterMode.INOUT),
    )


def _collect_declarations(
    procedure: ParsedProcedure, features: _FeatureCollector
) -> tuple[str, ...]:
    variables: list[str] = []
    for declaration in procedure.declarations:
        if declaration.kind is DeclarationKind.CURSOR:
            features.add(Feature.CURSOR, declaration.line)
            continue
        variables.append(declaration.name)
        features.add(Feature.VARIABLES, declaration.line)
        if declaration.data_type and "jsonb" in declaration.data_type.lower():
            features.add(Feature.JSONB, declaration.line)
    return tuple(variables)


def _collect_statement_features(statement: Statement, features: _FeatureCollector) -> None:
    if statement.kind in LOOP_KINDS:
        features.add(Feature.LOOP, statement.line)
    if _is_dynamic_sql(statement):
        features.add(Feature.DYNAMIC_SQL, statement.line)
    if feature := STATEMENT_FEATURES.get(statement.kind):
        features.add(feature, statement.line)
    if statement.exception_handlers:
        features.add(Feature.EXCEPTION_HANDLING, statement.line)


def _is_dynamic_sql(statement: Statement) -> bool:
    if statement.kind in {StatementKind.DYNAMIC_SQL, StatementKind.FOR_DYNAMIC}:
        return True
    # RETURN QUERY EXECUTE: the query is an expression, not parsed SQL
    return statement.kind is StatementKind.RETURN_QUERY and statement.sql is None


def _collect_sql_features(
    fragment: SqlFragment, line: int | None, features: _FeatureCollector
) -> None:
    if fragment.command in DML_COMMANDS:
        features.add(Feature.DML, line)
    if fragment.locking_clauses:
        features.add(Feature.ROW_LOCKING, line)
    if fragment.uses_jsonb:
        features.add(Feature.JSONB, line)
    if fragment.has_cte:
        features.add(Feature.CTE, line)
    if fragment.has_recursive_cte:
        features.add(Feature.RECURSIVE_CTE, line)
    if fragment.has_join:
        features.add(Feature.JOIN, line)
    if any(f.rpartition(".")[2].lower() in AGGREGATE_FUNCTIONS for f in fragment.functions):
        features.add(Feature.AGGREGATION, line)
    if any(not is_builtin_function(f) for f in fragment.functions):
        features.add(Feature.FUNCTION_CALLS, line)


def _fragments_with_lines(procedure: ParsedProcedure) -> Iterator[tuple[SqlFragment, int | None]]:
    for declaration in procedure.declarations:
        if declaration.cursor_sql is not None:
            yield declaration.cursor_sql, declaration.line
    for statement, _ in procedure.iter_statements():
        if statement.sql is not None:
            yield statement.sql, statement.line


def _detect_risks(
    procedure: ParsedProcedure, features: _FeatureCollector
) -> tuple[SemanticRisk, ...]:
    risks: list[SemanticRisk] = []
    for statement, loop_depth in procedure.iter_statements():
        if statement.kind in DATABASE_ROUND_TRIP_KINDS and loop_depth > 0:
            risks.append(
                SemanticRisk(
                    code="N_PLUS_ONE",
                    severity=RiskSeverity.HIGH,
                    message=(
                        f"{statement.kind.value.upper()} executed inside a loop: one database "
                        "round-trip per iteration. Prefer a single set-based statement."
                    ),
                    line=statement.line,
                )
            )
        if _is_dynamic_sql(statement):
            risks.append(
                SemanticRisk(
                    code="DYNAMIC_SQL",
                    severity=RiskSeverity.HIGH,
                    message=(
                        "Dynamic SQL cannot be analyzed statically and is prone to SQL "
                        "injection; the Python version must use bind parameters/quoted identifiers."
                    ),
                    line=statement.line,
                )
            )
        if statement.kind in {StatementKind.COMMIT, StatementKind.ROLLBACK}:
            risks.append(
                SemanticRisk(
                    code="TRANSACTION_CONTROL",
                    severity=RiskSeverity.MEDIUM,
                    message=(
                        f"{statement.kind.value.upper()} inside the routine: transaction "
                        "boundaries must move to the Python caller / unit of work."
                    ),
                    line=statement.line,
                )
            )
        for handler in statement.exception_handlers:
            reraises = any(
                inner.kind is StatementKind.RAISE for inner, _ in _walk_all(handler.body)
            )
            if "others" in (c.lower() for c in handler.conditions) and not reraises:
                risks.append(
                    SemanticRisk(
                        code="SWALLOWED_EXCEPTION",
                        severity=RiskSeverity.MEDIUM,
                        message="WHEN OTHERS handler does not re-raise: errors are swallowed.",
                        line=statement.line,
                    )
                )
    for fragment, line in _fragments_with_lines(procedure):
        if fragment.parse_error:
            risks.append(
                SemanticRisk(
                    code="UNPARSED_SQL",
                    severity=RiskSeverity.MEDIUM,
                    message=f"Embedded SQL could not be analyzed: {fragment.parse_error}",
                    line=line,
                )
            )
    if Feature.ROW_LOCKING in features:
        risks.append(
            SemanticRisk(
                code="ROW_LOCKING",
                severity=RiskSeverity.MEDIUM,
                message="Row locks (FOR UPDATE/SHARE) only hold inside one transaction; "
                "lock and subsequent writes must share the same connection/transaction.",
            )
        )
    if Feature.GET_DIAGNOSTICS in features:
        risks.append(
            SemanticRisk(
                code="DIAGNOSTICS_SEMANTICS",
                severity=RiskSeverity.LOW,
                message="GET DIAGNOSTICS must be mapped to driver equivalents (e.g. rowcount).",
            )
        )
    if Feature.RECURSIVE_CTE in features:
        risks.append(
            SemanticRisk(
                code="RECURSIVE_CTE",
                severity=RiskSeverity.LOW,
                message="Recursive CTE should stay in SQL; re-implementing it in Python is costly.",
            )
        )
    external = [f for f in procedure.called_functions if not is_builtin_function(f)]
    if external:
        risks.append(
            SemanticRisk(
                code="EXTERNAL_ROUTINE_DEPENDENCY",
                severity=RiskSeverity.LOW,
                message=f"Depends on other database routines: {', '.join(sorted(external))}.",
            )
        )
    return tuple(risks)


def _walk_all(statements: tuple[Statement, ...]) -> Iterator[tuple[Statement, int]]:
    for statement in statements:
        yield from statement.walk()


def _recommend_strategy(
    procedure: ParsedProcedure, features: _FeatureCollector
) -> GenerationStrategy:
    has_sql = next(procedure.sql_fragments(), None) is not None
    is_procedural = any(feature in features for feature in PROCEDURAL_FEATURES)
    if not has_sql:
        return GenerationStrategy.PYTHON_REIMPLEMENTATION
    if not is_procedural:
        return GenerationStrategy.DATABASE_DELEGATED
    return GenerationStrategy.HYBRID


def _collect_dependencies(procedure: ParsedProcedure) -> tuple[Dependency, ...]:
    tables = [Dependency(name=t, kind=DependencyKind.TABLE) for t in procedure.referenced_tables]
    functions = [
        Dependency(name=f, kind=DependencyKind.FUNCTION)
        for f in procedure.called_functions
        if not is_builtin_function(f)
    ]
    return tuple(tables + functions)


def _recommend(
    features: _FeatureCollector, risks: tuple[SemanticRisk, ...]
) -> tuple[Recommendation, ...]:
    risk_codes = {risk.code for risk in risks}
    rules: list[tuple[bool, Recommendation]] = [
        (
            any(
                f in features for f in (Feature.DML, Feature.AGGREGATION, Feature.JOIN, Feature.CTE)
            ),
            Recommendation(
                code="KEEP_SET_BASED_SQL",
                message="Keep joins, aggregates, CTEs and bulk DML as parameterized SQL.",
            ),
        ),
        (
            "N_PLUS_ONE" in risk_codes,
            Recommendation(
                code="REWRITE_ROW_BY_ROW",
                message="Collapse per-row statements inside loops into one set-based statement.",
            ),
        ),
        (
            Feature.TRANSACTION_CONTROL in features or Feature.ROW_LOCKING in features,
            Recommendation(
                code="CALLER_OWNS_TRANSACTION",
                message="Receive an open connection; do not commit inside the function.",
            ),
        ),
        (
            Feature.RAISE in features or Feature.EXCEPTION_HANDLING in features,
            Recommendation(
                code="TYPED_EXCEPTIONS",
                message="Map RAISE EXCEPTION to typed exceptions and RAISE NOTICE to logging.",
            ),
        ),
        (
            Feature.OUT_PARAMETERS in features,
            Recommendation(
                code="RESULT_TYPE_FOR_OUT_PARAMS",
                message="Return OUT/INOUT parameters as an immutable result type.",
            ),
        ),
        (
            Feature.RETURN_QUERY in features,
            Recommendation(
                code="TYPED_ROWS",
                message="Return RETURN QUERY rows as a sequence of typed rows.",
            ),
        ),
        (
            Feature.GET_DIAGNOSTICS in features,
            Recommendation(
                code="ROWCOUNT",
                message="Use the driver's rowcount instead of GET DIAGNOSTICS ROW_COUNT.",
            ),
        ),
        (
            Feature.DYNAMIC_SQL in features,
            Recommendation(
                code="SAFE_DYNAMIC_SQL",
                message="Whitelist identifiers and bind values; never interpolate into SQL.",
            ),
        ),
    ]
    return tuple(recommendation for applies, recommendation in rules if applies)
