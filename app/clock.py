"""Shift clock.

PropertyOps is built for one person alone at the desk, and that person may be on
shift at any hour. `PO_SIM_HOUR` pins the hour so time-of-day policy (for example
the after-10pm Gold ceiling) is reproducible. Unset it and the real clock is used.
"""
from __future__ import annotations

import os
from datetime import datetime


def now() -> datetime:
    raw = os.getenv("PO_SIM_HOUR", "").strip()
    if not raw:
        return datetime.now()
    try:
        hour = int(raw) % 24
    except ValueError:
        return datetime.now()
    real = datetime.now()
    return real.replace(hour=hour, minute=real.minute, second=real.second, microsecond=0)
