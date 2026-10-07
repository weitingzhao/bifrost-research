"""The Dagster daemon is a singleton, and its Deployment has to roll like one."""

from __future__ import annotations

import re
from pathlib import Path

K8S = Path(__file__).resolve().parents[2] / "k8s"
MANIFEST = K8S / "orchestration" / "dagster.yaml"
_IMAGE = re.compile(r"bifrost-research:([A-Za-z0-9._-]+)")


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


def test_k8s_holds_one_dagster_instance_config() -> None:
    """TD-120: an unmounted second file can drop run_monitoring and hide zombie runs."""
    assert list(K8S.rglob("dagster_instance.yaml")) == []
    holders = [
        path
        for path in K8S.rglob("*.yaml")
        if "name: dagster-instance" in path.read_text() and "run_monitoring:" in path.read_text()
    ]
    assert holders == [MANIFEST]


def test_daemon_image_tag_is_the_api_pin_plus_dagster() -> None:
    """TD-120: dagster-daemon (and the webserver) must be '<api pin>-dagster'."""
    api_tags = _IMAGE.findall((K8S / "api" / "deployment.yaml").read_text())
    assert api_tags == [api_tags[0]]
    pin = api_tags[0]
    assert not pin.endswith("-dagster")
    for name in ("dagster-daemon", "dagster-webserver"):
        tags = _IMAGE.findall(_deployment(name))
        assert tags == [f"{pin}-dagster"], name


def test_deployments_do_not_mark_the_research_secret_optional() -> None:
    """TD-121: a Deployment that envFroms bifrost-research-secrets must fail closed.

    Per-key secretKeyRef optionals (write tokens) stay; the whole-secret ref does not.
    Checksum annotations are LANE-R1.
    """
    offenders: list[str] = []
    seen: list[str] = []
    for path in sorted(K8S.rglob("*.yaml")):
        for doc in path.read_text().split("\n---"):
            if "kind: Deployment" not in doc:
                continue
            name = ""
            for line in doc.splitlines():
                if line.startswith("  name: "):
                    name = line.split(":", 1)[1].strip()
                    break
            if "envFrom:" not in doc or "name: bifrost-research-secrets" not in doc:
                continue
            seen.append(f"{path.name}:{name}")
            if re.search(
                r"envFrom:\n(?:[^\n]*\n){0,8}?[ ]+name: bifrost-research-secrets\n[ ]+optional:\s*true",
                doc,
            ):
                offenders.append(f"{path.name}:{name}")
    assert offenders == []
    assert {
        "deployment.yaml:research-api",
        "deployment.yaml:research-mcp",
        "dagster.yaml:dagster-webserver",
        "dagster.yaml:dagster-daemon",
    } <= set(seen)
