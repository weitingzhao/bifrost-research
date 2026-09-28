"""The Dagster daemon is a singleton, and its Deployment has to roll like one."""

from __future__ import annotations

from pathlib import Path

MANIFEST = Path(__file__).resolve().parents[2] / "k8s" / "orchestration" / "dagster.yaml"


def _deployment(name: str) -> str:
    for doc in MANIFEST.read_text().split("\n---"):
        if "kind: Deployment" in doc and f"  name: {name}\n" in doc:
            return doc
    raise AssertionError(f"no Deployment {name} in {MANIFEST}")


def test_the_daemon_is_replaced_not_rolled() -> None:
    """2026-09-28, 0.150.2-dagster: RollingUpdate kept the old and new daemon
    running side by side for 10-20s, long enough for a schedule tick to be
    launched by both. Recreate stops the old one before the new one starts."""
    daemon = _deployment("dagster-daemon")
    assert "replicas: 1" in daemon
    assert "strategy:\n    type: Recreate" in daemon
    assert "rollingUpdate" not in daemon
