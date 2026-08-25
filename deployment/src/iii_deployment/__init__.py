"""Shared, ROS-independent III deployment contracts."""

from typing import Any

__all__ = ["CommandResult", "Finding", "NextAction", "Outcome"]
__version__ = "0.1.0"


def __getattr__(name: str) -> Any:
    """Load the CLI-owned envelope only when a caller requests result types.

    Trusted governance scripts intentionally import policy modules from a
    minimal checkout which does not contain the CLI package. Those read-only
    modules must remain usable without eagerly importing an unrelated frontend.
    """

    if name not in __all__:
        raise AttributeError(name)
    from . import result

    return getattr(result, name)
