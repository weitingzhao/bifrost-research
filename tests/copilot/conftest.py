"""Shared harness fixtures.

`fake_conn` was defined inside test_harness_runtime and a second test file that
wanted it had to import it — which shadows the parameter of the same name. A
fixture used by more than one module belongs here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_conn() -> Any:
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=MagicMock())
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn
