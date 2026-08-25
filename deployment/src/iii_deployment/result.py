"""Compatibility import for the canonical III CLI result contract.

The implementation and schemas are owned by ``tools/III-Drone-CLI``. Keeping
this import path avoids a flag-day migration for existing deployment modules
without creating a second envelope implementation.
"""

from iii.result import (  # noqa: F401
    CommandResult,
    Finding,
    NextAction,
    Outcome,
    RESULT_SCHEMA,
    internal_error_result,
    result_from_exception,
)

__all__ = [
    "CommandResult",
    "Finding",
    "NextAction",
    "Outcome",
    "RESULT_SCHEMA",
    "internal_error_result",
    "result_from_exception",
]
