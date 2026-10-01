"""What the behavior harness produces: the outcome of one dataset case."""

from app.shared.domain.value_object import ValueObject


class CaseResult(ValueObject):
    """One input of the evaluation dataset, run on the original and on the generated code."""

    name: str
    passed: bool
    detail: str
    """Why it is (not) equivalent, e.g. which table differs."""
    original: str
    """Observed outcome of the original routine (rows or error), for the report."""
    generated: str
    holdout: bool = False
    """Never shown to the LLM (the repair loop only sees the other cases)."""
