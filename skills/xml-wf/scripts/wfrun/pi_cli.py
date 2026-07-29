"""`pi -p` subprocess invocation for `wfrun ask --backend pi`.

Sibling of `claude_cli.py`, scoped to the single Pi-backed capability xml-wf
needs today: LLM condition judgment for `ask=`. Not a general `pi -p`
wrapper -- see `mode-orchestrator-runs/phase5-item1-cc-inventory-design.md`
for the fuller CC<->Pi feature-parity survey (structured-output forcing,
cost reporting, and general delegation are all still CC-only there).

Differences from claude_cli.py's approach, and why (all measured 2026-07-29
against pi v0.80.6 / win32):

- No `--json-schema` on Pi (`pi --help` has no equivalent) -- forced instead
  by appending an instruction to the prompt, then parsed with a two-pass
  fallback (full-body parse, then brace-extraction). The fallback is load
  bearing, not defensive: the measured replies wrap the object in a
  ```json fence despite the instruction.
- The prompt is passed as a **positional message argument**, NOT via pi's
  `@<file>` include syntax. `@file` attaches the file as *content to reason
  about*, not as the turn's instruction: a probe whose file said "reply with
  exactly this JSON" got back a refusal that named it an embedded-instruction
  (prompt-injection) attempt and asked what the user actually wanted. The
  same text as a positional argument was obeyed.
- stdin is closed (DEVNULL). A `pi -p` call left with stdin open as a pipe
  blocks forever before dispatch (`_projects/pi-extensions-dev/rules.md`,
  "Known pitfalls").
- `cost_usd` is always 0.0 because `--mode text` prints no cost figure. Pi
  *can* report cost, but only in `--mode json`, whose `message_end` /
  `turn_end` lines carry `.message.usage.cost.total`. Switching this module
  to that mode would recover the figure for natively-priced providers
  (measured: `google/gemini-3.1-flash-lite` reported 0.00123875 for a
  one-word turn) but not for the models the canonical names resolve to:
  `pi-claude-agent-sdk/*` is a bridge to the claude binary and pi has no
  price table for it, so it reports a real token count with `cost.total` 0.
  Since ask defaults to those canonical names, text mode loses nothing today
  -- but callers must still not read 0.0 as a measured zero.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

from .guardrails import ASK_PROMPT

# Appended to ASK_PROMPT since pi has no --json-schema equivalent to force
# structured output the way claude_cli.py's run_claude() does.
_JSON_INSTRUCTION = """

Respond with a single JSON object and nothing else -- no explanation, no \
code fence, just: {"answer": true or false, "reason": "..."}"""

_BRACE_RE = re.compile(r"\{.*\}", re.DOTALL)

# Where npm's Windows shim keeps the actual CLI entry, relative to the shim.
_NPM_ENTRY = ("node_modules", "@earendil-works", "pi-coding-agent", "dist",
              "cli.js")

_resolution_cache: dict[str, object] = {}


def resolve_pi_launcher() -> list[str] | None:
    """Resolve the argv prefix that launches pi, once per process.

    Returns the prefix (e.g. `["/usr/local/bin/pi"]`, or
    `["node", ".../dist/cli.js"]` on Windows), or None when pi cannot be
    launched safely.

    Windows: npm installs `pi` as a `.CMD` shim. Launching that shim with a
    multi-line prompt on argv **silently truncates the prompt at the first
    newline** -- measured: a two-line probe reached the model as line one
    only, exit code 0, nothing on stderr. This is the same cmd.exe argv
    corruption reliability-spec.md §13.2 measured for the claude shim, and
    the ask prompt is always multi-line, so every call would be affected.
    The shim itself just runs `node <entry> %*`, so we resolve that entry and
    launch it through node directly, bypassing cmd.exe.

    Fail-closed: if only the shim is reachable (no entry file, or no node on
    PATH), return None rather than launch a call whose prompt would arrive
    truncated -- the same loud-failure choice as §13.3.3.
    """
    if "launcher" in _resolution_cache:
        return _resolution_cache["launcher"]

    which_path = shutil.which("pi")
    if which_path is None:
        launcher = None
    elif os.path.splitext(which_path)[1].lower() not in (".cmd", ".bat"):
        launcher = [which_path]
    else:
        entry = os.path.join(os.path.dirname(which_path), *_NPM_ENTRY)
        node = shutil.which("node")
        launcher = [node, entry] if (node and os.path.isfile(entry)) else None

    _resolution_cache["launcher"] = launcher
    return launcher


def _extract_json(text: str) -> dict | None:
    """Two-pass parse: try the whole (stripped) body first, then fall back
    to the first-brace-to-last-brace span. The fallback is what handles the
    ```json fence the model emits in practice."""
    stripped = text.strip()
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    m = _BRACE_RE.search(stripped)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    return None


def ask_llm_pi(question: str, *, model: str = "haiku", cwd: str | None = None,
              timeout: int = 300) -> tuple[bool | None, str, float]:
    """Pi-backed counterpart to claude_cli.ask_llm(). Same contract: returns
    (answer, reason, cost) with answer=None on failure after one retry.

    cost is always 0.0 (see module docstring) -- kept in the return shape
    for drop-in parity with the cc backend, not because it is measured.
    """
    launcher = resolve_pi_launcher()
    if launcher is None:
        return None, ("pi CLI not launchable: not on PATH, or only the "
                      "Windows npm .CMD shim is reachable (it truncates a "
                      "multi-line prompt at the first newline) and no "
                      "node + dist/cli.js could be resolved to bypass it"), 0.0

    prompt = ASK_PROMPT.format(question=question) + _JSON_INSTRUCTION
    cmd = [*launcher, "-p", "--mode", "text", "--tools", "read",
           "--no-session", "--model", model, prompt]

    reason = "no structured answer"
    for _ in range(2):
        try:
            proc = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, capture_output=True,
                text=True, timeout=timeout, cwd=cwd)
        except subprocess.TimeoutExpired:
            reason = f"timeout after {timeout}s"
            continue
        except FileNotFoundError:
            return None, "pi CLI not found on PATH", 0.0
        if proc.returncode != 0:
            reason = (proc.stderr or "").strip()[:500] or f"pi exited {proc.returncode}"
            continue
        obj = _extract_json(proc.stdout)
        if obj is not None and isinstance(obj.get("answer"), bool):
            return obj["answer"], str(obj.get("reason", "")), 0.0
        reason = "no structured answer"
    return None, reason, 0.0
