"""`pi -p` subprocess invocation for `wfrun ask --backend pi`.

Sibling of `claude_cli.py`, scoped to the single Pi-backed capability xml-wf
needs today: LLM condition judgment for `ask=`. Not a general `pi -p`
wrapper -- see `mode-orchestrator-runs/phase5-item1-cc-inventory-design.md`
for the fuller CC<->Pi feature-parity survey (structured-output forcing,
cost reporting, and general delegation are all still CC-only there).

Differences from claude_cli.py's approach, and why:
- No `--json-schema` on Pi (`pi --help` has no equivalent) -- forced instead
  by appending an instruction to the prompt, then parsed with a two-pass
  fallback (full-body parse, then brace-extraction) since the model may wrap
  the JSON in prose or a code fence despite the instruction.
- Prompt is passed via `@<file>` (pi's file-include syntax), not stdin -- a
  `pi -p` call left with stdin open as a pipe blocks forever before dispatch
  (a documented, reproduced pitfall for this CLI; see
  `_projects/pi-extensions-dev/rules.md`, "Known pitfalls"). Passing the
  prompt as a file and explicitly closing stdin (DEVNULL) avoids it.
- `cost_usd` is always 0.0: `--mode text` (used here) reports no cost figure,
  and `--mode json`'s event-stream usage schema is unverified (see the design
  doc above). Callers must not treat 0.0 as a real zero-cost measurement.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

from .guardrails import ASK_PROMPT

# Appended to ASK_PROMPT since pi has no --json-schema equivalent to force
# structured output the way claude_cli.py's run_claude() does.
_JSON_INSTRUCTION = """

Respond with a single JSON object and nothing else -- no explanation, no \
code fence, just: {"answer": true or false, "reason": "..."}"""

_BRACE_RE = re.compile(r"\{.*\}", re.DOTALL)

_resolution_cache: dict[str, object] = {}


def _resolve_pi_bin() -> str | None:
    """Resolve the `pi` executable once per process.

    Unlike claude_cli.py's `_resolve_claude_bin()`, no shim/metachar handling
    is needed: this module's argv is a fixed flag set plus a temp-file path
    and a model name, none of which can contain shell metacharacters (see
    the design doc's "binary 解決" section for why this differs from the
    claude case).
    """
    if "path" in _resolution_cache:
        return _resolution_cache["path"]
    path = shutil.which("pi")
    _resolution_cache["path"] = path
    return path


def _extract_json(text: str) -> dict | None:
    """Two-pass parse: try the whole (stripped) body first, then fall back
    to the first-brace-to-last-brace span (handles a stray code fence or
    a leading/trailing sentence the model added despite the instruction)."""
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
    pi_bin = _resolve_pi_bin()
    if pi_bin is None:
        return None, "pi CLI not found on PATH", 0.0

    prompt = ASK_PROMPT.format(question=question) + _JSON_INSTRUCTION

    fd, prompt_file = tempfile.mkstemp(prefix="wfrun-ask-", suffix=".md")
    # UTF-8 explicitly -- same cp932-corruption concern as claude_cli.py's
    # sys_prompt_file (questions and interpolated vars routinely carry
    # non-ASCII text on JA Windows).
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(prompt)

    try:
        cmd = [pi_bin, f"@{prompt_file}", "-p", "--mode", "text",
               "--tools", "read", "--no-session", "--model", model]
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
    finally:
        try:
            os.remove(prompt_file)
        except OSError:
            pass
