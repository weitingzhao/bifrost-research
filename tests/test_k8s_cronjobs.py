"""Dagster owns every research slot; CronJob manifests may not come back (TD-124, TD-176, TD-190).

Until 2026-10-06, 25 suspended engine CronJobs shipped from k8s/ and were re-pinned
on every release. Twelve still set RESEARCH_WATCHLIST to a 26-name list that had
drifted from the universe rule (SATS had been renamed ECHO), so unsuspending one
would have run a second writer on the wrong universe next to Dagster.

What is left, and why:
  - research-harness runs outside Dagster and must stay active. It is the only
    CronJob left.

Until 2026-10-07 seven suspended CronJobs (bifrost-analytics-daily,
research-engines-event-radar/-forecast/-momentum, research-gex-intraday,
research-iv-percentile, research-terrain-intraday) shipped as Job templates for
bifrost-platform's POST /research/cronjobs/{name}/trigger. The route had no
caller and ran engines outside Dagster's ordering; both went (TD-190). No
template may come back, and the verify script may not grow a template list again.

The set may only shrink: a new CronJob fails here until it is argued into this
file, and a name deleted from k8s/ must be deleted here too.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
K8S = ROOT / "k8s"
VERIFY = ROOT / "scripts" / "verify_husbandry_schedulers.sh"

ACTIVE = {"research-harness"}


def _manifests() -> list[Path]:
    # Argo (bifrost-research app) excludes orchestration/** and _archived/**;
    # read them too — a CronJob parked there is one exclude edit from shipping.
    return sorted(p for p in K8S.rglob("*.y*ml") if p.suffix in (".yaml", ".yml"))


def _cronjobs() -> dict[str, dict]:
    found: dict[str, dict] = {}
    for path in _manifests():
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if isinstance(doc, dict) and doc.get("kind") == "CronJob":
                found[doc["metadata"]["name"]] = doc
    return found


def test_only_the_harness_exists() -> None:
    assert set(_cronjobs()) == ACTIVE


def test_the_harness_runs() -> None:
    cronjobs = _cronjobs()
    for name in ACTIVE:
        assert cronjobs[name]["spec"].get("suspend") is not True, name


def test_no_suspended_cronjob_ships() -> None:
    """A suspended CronJob is either dead weight or a Job template for something
    outside Dagster (TD-190); neither may ship."""
    suspended = sorted(n for n, d in _cronjobs().items() if d["spec"].get("suspend") is True)
    assert suspended == []


def test_no_manifest_sets_research_watchlist() -> None:
    """The engines resolve their universe from the rule Dagster uses; a pinned
    list in a manifest drifts (TD-124)."""
    hits = [
        f"{p.relative_to(ROOT)}:{n}"
        for p in _manifests()
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if "RESEARCH_WATCHLIST" in line and not line.lstrip().startswith("#")
    ]
    assert hits == []


def test_the_verify_script_allows_exactly_these() -> None:
    text = VERIFY.read_text(encoding="utf-8")

    def default(var: str) -> set[str]:
        m = re.search(rf'^{var}="\$\{{{var}:-([^}}]*)\}}"$', text, re.M)
        assert m, var
        return set(m.group(1).split())

    assert default("ALLOW_ACTIVE") == ACTIVE
    # The template allow-list went with platform's trigger route (TD-190).
    assert "ALLOW_TEMPLATE" not in text
