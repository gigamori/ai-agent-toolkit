"""Run-state persistence: append-only events.jsonl + state.json snapshot.

Resume model (event sourcing): a resumed run replays events.jsonl against the
same workflow XML and params. While the recorded sequence matches the
execution path, step results and condition verdicts are reused instead of
re-executed; at the first mismatch or recorded failure, replay stops and
execution continues live. Deterministic control flow plus reused ask-verdicts
guarantee the same path up to that point.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class RunState:
    def __init__(self, run_dir: str | Path):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "outputs").mkdir(exist_ok=True)
        (self.run_dir / "steps").mkdir(exist_ok=True)
        self._events_path = self.run_dir / "events.jsonl"
        self._lock = threading.Lock()

    def event(self, kind: str, **data) -> dict:
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, **data}
        with self._lock:
            with self._events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def snapshot(self, *, status: str, variables: dict, step_count: int,
                 cost_usd: float, error: str | None = None):
        payload = {
            "status": status,
            "vars": {k: _jsonable(v) for k, v in variables.items()},
            "step_count": step_count,
            "cost_usd": round(cost_usd, 6),
            "error": error,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        with self._lock:
            (self.run_dir / "state.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _jsonable(value):
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def load_events(run_dir: str | Path) -> list[dict]:
    path = Path(run_dir) / "events.jsonl"
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


class ReplayCursor:
    """Strict in-order replay of recorded events.

    take(kind, key): if the next replayable event matches (kind, key) and
    succeeded, consume and return it; otherwise replay is disabled for the
    rest of the run (we fell off the recorded path).

    take_group(ids): for <parallel> — consume the consecutive run of matching
    successful step events regardless of their relative order.
    """

    def __init__(self, events: list[dict]):
        # Only successful events enter the replay stream. Failure records and
        # post-failure live events from earlier resumes thus concatenate into
        # the exact success-order of the execution path, which makes repeated
        # resume idempotent.
        self._events = [e for e in events
                        if e["kind"] in ("step", "cond", "replan")
                        and e.get("status") == "success"]
        self._pos = 0
        self.active = bool(self._events)

    def _peek(self):
        return self._events[self._pos] if self._pos < len(self._events) else None

    def take(self, kind: str, key: str) -> dict | None:
        if not self.active:
            return None
        event = self._peek()
        if event and event["kind"] == kind and event.get("key") == key:
            self._pos += 1
            return event
        self.active = False
        return None

    def take_group(self, kind: str, keys: set[str]) -> dict[str, dict]:
        found: dict[str, dict] = {}
        if not self.active:
            return found
        while True:
            event = self._peek()
            if (event and event["kind"] == kind and event.get("key") in keys
                    and event.get("key") not in found):
                found[event["key"]] = event
                self._pos += 1
            else:
                break
        if len(found) != len(keys):
            # Partial group: the missing members run live, and replay cannot
            # continue past an incomplete barrier.
            self.active = False
        return found
