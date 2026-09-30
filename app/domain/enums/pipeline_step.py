from enum import StrEnum


class PipelineStep(StrEnum):
    PARSING = "parsing"
    SEMANTIC_ANALYSIS = "semantic_analysis"
    GENERATION = "generation"
    VALIDATION = "validation"
