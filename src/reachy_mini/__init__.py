"""Reachy Mini SDK."""

__all__ = ["ReachyMini", "ReachyMiniApp", "__version__"]


def __getattr__(name: str):
    """Load public SDK objects only when callers ask for them."""
    if name == "ReachyMini":
        from reachy_mini.reachy_mini import ReachyMini

        globals()[name] = ReachyMini
        return ReachyMini

    if name == "ReachyMiniApp":
        from reachy_mini.apps.app import ReachyMiniApp

        globals()[name] = ReachyMiniApp
        return ReachyMiniApp

    if name == "__version__":
        from importlib.metadata import version

        package_version = version("reachy_mini")
        globals()[name] = package_version
        return package_version

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
