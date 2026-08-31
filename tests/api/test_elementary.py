"""Elementary status + file serving (no DB)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from bifrost_research.api.app import create_app


def test_elementary_status_missing(tmp_path: Path, monkeypatch) -> None:
    report = tmp_path / "elementary_report.html"
    monkeypatch.setenv("ELEMENTARY_REPORT_PATH", str(report))
    client = TestClient(create_app())
    resp = client.get("/analytics/elementary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["present"] is False


def test_elementary_file_serves_html(tmp_path: Path, monkeypatch) -> None:
    report = tmp_path / "elementary_report.html"
    report.write_text("<html><body>edr</body></html>", encoding="utf-8")
    (tmp_path / "asset.js").write_text("window.edr=1", encoding="utf-8")
    monkeypatch.setenv("ELEMENTARY_REPORT_PATH", str(report))
    client = TestClient(create_app())

    status = client.get("/analytics/elementary")
    assert status.json()["present"] is True

    html = client.get("/analytics/elementary/files/elementary_report.html")
    assert html.status_code == 200
    assert "edr" in html.text
    assert "text/html" in html.headers["content-type"]

    default = client.get("/analytics/elementary/files")
    assert default.status_code == 200
    assert default.text == html.text

    asset = client.get("/analytics/elementary/files/asset.js")
    assert asset.status_code == 200
    assert "window.edr" in asset.text


def test_elementary_file_rejects_traversal(tmp_path: Path, monkeypatch) -> None:
    report = tmp_path / "elementary_report.html"
    report.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setenv("ELEMENTARY_REPORT_PATH", str(report))
    client = TestClient(create_app())
    resp = client.get("/analytics/elementary/files/../secret")
    assert resp.status_code == 404
