#!/usr/bin/env python3
"""Single shared timestamp source for `@log` entries (stdlib only).

`session_init.py` (channel A: injects `iso_ts=` into the `[Progress Session]`
header for the main agent to transcribe) and `session_progress_capture.py`
(channel B: the Stop hook's deterministic apply-path) each write `@log` lines
that must carry the same wall-clock instant format. Prior to this module the
two hooks computed `datetime.datetime.now()` independently and naively (no
tzinfo, no offset in the output), so entries written from environments with
different local timezones were indistinguishable and silently mixed. Both
hooks now import `now_iso()` from here so there is exactly one generation
point to keep in sync.

Usage:

    from tstamp import now_iso
    iso_ts = now_iso()
"""
from __future__ import annotations

import datetime


def now_iso() -> str:
    """Return the current local time as offset-aware ISO8601, second precision.

    Example: '2026-07-14T01:17:28+09:00'.
    """
    return datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()
