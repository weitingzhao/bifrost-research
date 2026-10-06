"""Dagster owns every research slot; CronJob manifests may not come back (TD-124, TD-176).

Until 2026-10-06, 25 suspended engine CronJobs shipped from k8s/ and were re-pinned
on every release. Twelve still set RESEARCH_WATCHLIST to a 26-name list that had
drifted from the universe rule (SATS had been renamed ECHO), so unsuspending one
would have run a second writer on the wrong universe next to Dagster.

What is left, and why:
  - research-harness runs outside Dagster and must stay active.
  - TRIGGER_TEMPLATES are suspended and only used as Job templates by
    bifrost-platform api/internal/research/cronjob_trigger.go
    (POST /research/cronjobs/{name}/trigger). They go when that whitelist goes.

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
TRIGGER_TEMPLATES = {
    "bifrost-analytics-daily",
    "research-engines-event-radar",
    "research-engines-forecast",
    "research-engines-momentum",
    "research-gex-intraday",
    "research-iv-percentile",
    "research-terrain-intraday",
}


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


def test_only_the_argued_cronjobs_exist() -> None:
    assert set(_cronjobs()) == ACTIVE | TRIGGER_TEMPLATES


def test_the_harness_runs_and_the_templates_never_do() -> None:
    cronjobs = _cronjobs()
    for name in ACTIVE:
        assert cronjobs[name]["spec"].get("suspend") is not True, name
    for name in TRIGGER_TEMPLATES:
        assert cronjobs[name]["spec"].get("suspend") is True, name


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
    assert default("ALLOW_TEMPLATE") == TRIGGER_TEMPLATES
