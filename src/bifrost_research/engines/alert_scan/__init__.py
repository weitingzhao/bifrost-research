"""Alert scan package."""

__all__ = ["run"]


def __getattr__(name: str):
    if name == "run":
        from bifrost_research.engines.alert_scan.entry import run

        return run
    raise AttributeError(name)
