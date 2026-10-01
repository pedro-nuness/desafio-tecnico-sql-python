"""Catalogs the semantic analysis reads: PostgreSQL built-ins, IR -> feature mappings and the
text of every risk and recommendation. Data only (plus the built-in lookup)."""

from collections.abc import Mapping
from typing import NamedTuple

from app.features.modernization.domain.parsing import SqlCommand, StatementKind
from app.features.modernization.domain.semantic_analysis import Feature, RiskSeverity

# --------------------------------------------------------------------------- PostgreSQL

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
        "now", "current_timestamp", "current_date", "clock_timestamp", "date", "date_trunc",
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


def is_builtin_function(name: str) -> bool:
    schema, _, bare = name.rpartition(".")
    return schema == "pg_catalog" or bare.lower() in BUILTIN_FUNCTIONS


# --------------------------------------------------------------------------- IR -> features

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

# --------------------------------------------------------------------------- risks


class RiskSpec(NamedTuple):
    severity: RiskSeverity
    message: str
    """str.format template; the placeholders are filled by risks.py."""


RISKS: Mapping[str, RiskSpec] = {
    "N_PLUS_ONE": RiskSpec(
        RiskSeverity.HIGH,
        "{count} SQL statement{plural} executed on every iteration of this {loop} "
        "(line{plural} {lines}): one database round-trip each per row. Prefer set-based "
        "statements.",
    ),
    "DYNAMIC_SQL": RiskSpec(
        RiskSeverity.HIGH,
        "Dynamic SQL cannot be analyzed statically and is prone to SQL injection; the Python "
        "version must use bind parameters/quoted identifiers.",
    ),
    "TRANSACTION_CONTROL": RiskSpec(
        RiskSeverity.MEDIUM,
        "{command} inside the routine: transaction boundaries must move to the Python caller "
        "/ unit of work.",
    ),
    "SWALLOWED_EXCEPTION": RiskSpec(
        RiskSeverity.MEDIUM,
        "WHEN OTHERS handler does not re-raise: errors are swallowed.",
    ),
    "UNPARSED_SQL": RiskSpec(
        RiskSeverity.MEDIUM,
        "Embedded SQL could not be analyzed: {error}",
    ),
    "ROW_LOCKING": RiskSpec(
        RiskSeverity.MEDIUM,
        "Row locks (FOR UPDATE/SHARE) only hold inside one transaction; lock and subsequent "
        "writes must share the same connection/transaction.",
    ),
    "DIAGNOSTICS_SEMANTICS": RiskSpec(
        RiskSeverity.LOW,
        "GET DIAGNOSTICS must be mapped to driver equivalents (e.g. rowcount).",
    ),
    "RECURSIVE_CTE": RiskSpec(
        RiskSeverity.LOW,
        "Recursive CTE should stay in SQL; re-implementing it in Python is costly.",
    ),
    "EXTERNAL_ROUTINE_DEPENDENCY": RiskSpec(
        RiskSeverity.LOW,
        "Depends on other database routines: {routines}.",
    ),
}

# --------------------------------------------------------------------------- recommendations

RECOMMENDATIONS: Mapping[str, str] = {
    "KEEP_SET_BASED_SQL": "Keep joins, aggregates, CTEs and bulk DML as parameterized SQL.",
    "REWRITE_ROW_BY_ROW": "Collapse per-row statements inside loops into one set-based statement.",
    "CALLER_OWNS_TRANSACTION": "Receive an open connection; do not commit inside the function.",
    "TYPED_EXCEPTIONS": "Map RAISE EXCEPTION to typed exceptions and RAISE NOTICE to logging.",
    "RESULT_TYPE_FOR_OUT_PARAMS": "Return OUT/INOUT parameters as an immutable result type.",
    "TYPED_ROWS": "Return RETURN QUERY rows as a sequence of typed rows.",
    "ROWCOUNT": "Use the driver's rowcount instead of GET DIAGNOSTICS ROW_COUNT.",
    "SAFE_DYNAMIC_SQL": "Whitelist identifiers and bind values; never interpolate into SQL.",
}
