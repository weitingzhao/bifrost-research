"""What the objective and objective-run repositories share: the connection
shape and the two value coercions both write with."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Protocol


class Connection(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def _serialize_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)
