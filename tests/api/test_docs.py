"""The blueprint is one file with three readers; this pins that it is served whole."""

from __future__ import annotations

from fastapi.testclient import TestClient

from bifrost_research.api import docs
from bifrost_research.api.app import create_app


def test_the_blueprint_is_served_with_its_front_matter_parsed() -> None:
    client = TestClient(create_app())
    r = client.get("/research/docs/blueprint")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["slug"] == "blueprint"
    assert d["version"] and d["updated"]
    # Front matter is metadata, not text to render.
    assert not d["markdown"].lstrip().startswith("---")
    assert d["markdown"].lstrip().startswith("# Research 蓝图")


def test_the_calibration_anchors_are_present() -> None:
    body = docs.read_doc("blueprint")["markdown"]
    # The numbered contracts are what future calibrations cite. Renaming one
    # silently breaks every reference to it, so their presence is held here.
    for anchor in ("C-F1", "C-R1", "C-A1", "C-C1", "C-U1", "## 3. 深与宽", "## 4. 契约与完成标准"):
        assert anchor in body, anchor


def test_an_unknown_document_is_a_404_not_a_directory_listing() -> None:
    client = TestClient(create_app())
    assert client.get("/research/docs/../../pyproject").status_code in (404, 422)
    assert client.get("/research/docs/nope").status_code == 404
