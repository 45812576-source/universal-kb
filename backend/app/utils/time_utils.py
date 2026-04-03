from __future__ import annotations

import datetime as dt


def utcnow() -> dt.datetime:
    """Return timezone-aware UTC now for internal timestamps."""
    return dt.datetime.now(dt.UTC)
