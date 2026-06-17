from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["RCOLConverter"]


def __getattr__(name: str) -> Any:
    if name == "RCOLConverter":
        module = import_module(".api", __name__)
        value = getattr(module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
