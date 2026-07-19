"""Per-runner model-name resolution (model_map.json).

The XML's `model=` vocabulary is canonical — haiku / sonnet / opus as
difficulty classes, the anchor words a builder judges best against. Binding
them to the models that actually run is a run-time concern that differs by
execution facility:

- runner "cc":  everything dispatched through the local claude CLI — run-cc
  steps, ask= judgments (both runners: `wfrun ask` also calls the CLI),
  debug diagnoses, replan builders
- runner "llm": run-llm step delegation via the orchestrator's subagent
  facility (`wfrun prompt` prints the resolved name on the dispatch line)

The bundled map is the identity, so zero-config behavior is unchanged. Names
absent from a table pass through untouched (lint nudges with
`model-not-canonical`). Resolution is one deterministic table lookup at
dispatch — the orchestrating LLM never translates names.
"""
from __future__ import annotations

import json
from pathlib import Path

MAP_PATH = Path(__file__).parent / "model_map.json"
CANONICAL_MODELS = ("haiku", "sonnet", "opus")
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
            path: str | Path | None = None) -> str | None:
    """Map a canonical model name for the given runner ("cc" | "llm").
    None and unmapped names pass through unchanged."""
    if not model:
        return model
    return load_map(path).get(runner, {}).get(model, model)
