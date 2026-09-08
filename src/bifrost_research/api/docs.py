"""Documents that ship with the package, served as they are.

The Research blueprint is one file with three readers: an agent calibrating
the code against it, the API, and the page in the Trade frontend. Keeping it
inside the package and serving it from here is what makes those three the same
file — a copy in the frontend would be a second blueprint within a week.

Read-only. D10 BLOCKED.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/research/docs", tags=["research-docs"])

_DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"

#: slug → (file, title). A short allowlist, not a directory listing: only
#: documents meant to be read from the product are reachable by URL.
DOCS: dict[str, tuple[str, str]] = {
    # The target: changes only when the understanding changes.
    "blueprint": ("RESEARCH_BLUEPRINT.md", "Research 蓝图"),
    # The state: contract status, evidence, gaps. Changes every calibration.
    "calibration": ("RESEARCH_CALIBRATION.md", "Research 校准"),
}

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _split_front_matter(text: str) -> tuple[dict[str, str], str]:
    """`key: value` lines between the leading `---` fences, and the body after them."""
    m = _FRONT_MATTER.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    return meta, text[m.end() :]


def read_doc(slug: str) -> dict[str, Any]:
    """The document as the API returns it. Raises KeyError for an unknown slug."""
    filename, title = DOCS[slug]
    text = (_DOCS_DIR / filename).read_text(encoding="utf-8")
    meta, body = _split_front_matter(text)
    return {
        "slug": slug,
        "title": title,
        "version": meta.get("version"),
        "updated": meta.get("updated"),
        "status": meta.get("status"),
        "markdown": body,
        "path": f"bifrost-research/src/bifrost_research/docs/{filename}",
    }


@router.get("/{slug}")
def get_doc(slug: str) -> dict[str, Any]:
    if slug not in DOCS:
        raise HTTPException(status_code=404, detail=f"no such document: {slug!r}")
    return {"ok": True, "data": read_doc(slug)}
