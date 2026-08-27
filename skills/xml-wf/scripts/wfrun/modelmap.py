"""Per-runner model-name resolution (model_map.json).

The XML's `model=` vocabulary is canonical — basic / pro / ultra as
difficulty classes, the anchor words a builder judges best against. Binding
them to the models that actually run is a run-time concern that differs by
execution facility:

- runner "cc":  everything dispatched through the local claude CLI — run-cc
  steps, ask= judgments (both runners: `wfrun ask` also calls the CLI),
  debug diagnoses, replan builders
- runner "llm": run-llm step delegation via the orchestrator's subagent
  facility (`wfrun prompt` prints the resolved name on the dispatch line)

The bundled map keeps zero-config dispatch unchanged: each tier binds to the
model its pre-rename name bound to. Names absent from a table pass through
untouched (lint nudges with `model-not-canonical`). Resolution is one
deterministic table lookup at dispatch — the orchestrating LLM never
translates names.
"""
from __future__ import annotations

import json
from pathlib import Path

MAP_PATH = Path(__file__).parent / "model_map.json"
CANONICAL_MODELS = ("basic", "pro", "ultra")

# D4 migration layer: pre-rename tier names, translated to their replacement
# before the runner tables are consulted -- never inside load_map/resolve's
# unconditional path, only when a caller opts in via resolve(...,
# allow_legacy=True). Scoped to `model=` resolution only; `decider-model=`
# must never opt in (see executor.py's and adjudicate.py's call sites).
LEGACY_ALIASES = {"haiku": "basic", "sonnet": "pro", "opus": "ultra"}

RUNNERS = ("cc", "llm")


class ModelMapError(Exception):
    pass


def load_map(path: str | Path | None = None) -> dict[str, dict[str, str]]:
    """The runner tables. A missing file means identity; invalid content is a
    loud error (a hand-edited map must never be silently ignored)."""
    p = Path(path) if path else MAP_PATH
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ModelMapError(f"{p}: invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ModelMapError(f"{p}: top level must be an object")
    tables = {}
    for runner in RUNNERS:
        table = data.get(runner, {})
        if not (isinstance(table, dict)
                and all(isinstance(k, str) and isinstance(v, str)
                        for k, v in table.items())):
            raise ModelMapError(f"{p}: '{runner}' must map names to names")
        tables[runner] = table
    return tables


def resolve(model: str | None, runner: str,
            path: str | Path | None = None,
            allow_legacy: bool = False) -> str | None:
    """Map a canonical model name for the given runner ("cc" | "llm").
    None and unmapped names pass through unchanged.

    allow_legacy=True additionally consults LEGACY_ALIASES first, translating
    a pre-rename tier name (haiku/sonnet/opus) to its D1 replacement before
    the table lookup. Off by default and set only on the `model=` dispatch
    path -- never on `decider-model=`, where a raw model id (e.g. "opus") is
    meant to reach the runner literally, not be redirected to whatever tier
    it collides with is bound to.
    """
    if not model:
        return model
    if allow_legacy:
        model = LEGACY_ALIASES.get(model, model)
    return load_map(path).get(runner, {}).get(model, model)
