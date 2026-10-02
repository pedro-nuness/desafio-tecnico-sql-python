"""What each side did on a case, and whether the two sides are equivalent.

Equivalent means:

- same outcome: both return, or both raise. The Python exception must be a class the
  generated module defines (the prompt contract for RAISE EXCEPTION), so a crash such as a
  driver error or a TypeError never passes for the expected failure;
- same result rows, when the routine returns any (functions, OUT parameters, SETOF);
- same final rows in the compared tables, ignoring new ids and now() timestamps.

Values are compared as canonical text: numbers by value (1500 == 1500.00), dates in ISO.
"""

import dataclasses
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

PREVIEW_CHARS = 240


@dataclass(frozen=True, slots=True)
class Observed:
    """What one side did on one case."""

    rows: tuple[str, ...] | None = None
    """Canonical result rows; None when the routine returns nothing (procedure, no OUT)."""
    state: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """Canonical rows of each compared table after the call (sorted)."""
    error: str | None = None
    deliberate: bool = False
    """The exception is a class the generated module defines (always False for SQL)."""

    def describe(self) -> str:
        if self.error is not None:
            return f"raised {self.error}"
        if self.rows is None:
            return "returned nothing"
        return f"returned {len(self.rows)} row(s): {_preview(self.rows)}"


def compare(original: Observed, generated: Observed) -> tuple[bool, str]:
    """(equivalent, why): the outcome first, then the result rows, then the final state."""
    if original.error is not None and generated.error is not None:
        if generated.deliberate:
            return True, "both raised"
        return False, f"generated code crashed instead of raising its own error: {generated.error}"
    if original.error is not None:
        return False, "the original raised; the generated code returned normally"
    if generated.error is not None:
        return False, f"the generated code raised: {generated.error}"
    if original.rows is not None and original.rows != generated.rows:
        return False, (
            f"result differs: original {_preview(original.rows)} "
            f"vs generated {_preview(generated.rows or ())}"
        )
    diffs = [
        _table_diff(table, rows, generated.state.get(table, ()))
        for table, rows in original.state.items()
        if rows != generated.state.get(table, ())
    ]
    if diffs:
        return False, "; ".join(diffs)
    return True, "same result and same final state"


def _table_diff(table: str, original: tuple[str, ...], generated: tuple[str, ...]) -> str:
    only_original = list((Counter(original) - Counter(generated)).elements())
    only_generated = list((Counter(generated) - Counter(original)).elements())
    parts = [f"{table}: {len(only_original)} row(s) only in the original"]
    if only_original:
        parts.append(f"e.g. {_preview(only_original[:1])}")
    parts.append(f"{len(only_generated)} only in the generated")
    if only_generated:
        parts.append(f"e.g. {_preview(only_generated[:1])}")
    return " ".join(parts)


# ---------------------------------------------------------------------- canonical form


def canonical_rows(value: Any) -> tuple[str, ...]:
    """A return value as canonical rows. Accepted shapes: None (no rows), a scalar, one record
    (dataclass, tuple, mapping, Row) or a list of records."""
    if value is None:
        return ()
    records = value if _is_row_list(value) else [value]
    return tuple(canonical(list(_record(item))) for item in records)


def _is_row_list(value: Any) -> bool:
    if isinstance(value, list):
        return True
    return isinstance(value, tuple) and bool(value) and all(map(_is_record, value))


def _is_record(value: Any) -> bool:
    return (dataclasses.is_dataclass(value) and not isinstance(value, type)) or isinstance(
        value, Mapping | tuple
    )


def _record(value: Any) -> tuple[Any, ...]:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return tuple(getattr(value, f.name) for f in dataclasses.fields(value))
    if isinstance(value, Mapping):
        return tuple(value.values())
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return tuple(value)
    return (value,)


def canonical(value: Any) -> str:
    """Stable text for comparison: numbers by value (1500 == 1500.00 == 1500.0), dates ISO."""
    return json.dumps(_plain(value), sort_keys=True, ensure_ascii=False)


def _plain(value: Any) -> Any:
    match value:
        case bool() | None:
            return value
        case int() | float() | Decimal():
            number = Decimal(str(value)) if isinstance(value, float) else Decimal(value)
            return "0" if number == 0 else format(number.normalize(), "f")
        case datetime() | date():
            return value.isoformat()
        case Mapping():
            return {str(key): _plain(item) for key, item in value.items()}
        case list() | tuple():
            return [_plain(item) for item in value]
        case _:
            return str(value)


def _preview(rows: Sequence[str]) -> str:
    text = ", ".join(rows[:3]) + (" ..." if len(rows) > 3 else "")
    return text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 3] + "..."
