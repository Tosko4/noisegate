from __future__ import annotations

from ._version import __version__
from .plugin import register, transform_terminal_output, transform_tool_result

__all__ = [
    "__version__",
    "register",
    "transform_terminal_output",
    "transform_tool_result",
]
