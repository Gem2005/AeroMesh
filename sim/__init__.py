from __future__ import annotations

__all__ = ["Bridge"]


def __getattr__(name: str):
    if name == "Bridge":
        from bridge import Bridge
        return Bridge
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

