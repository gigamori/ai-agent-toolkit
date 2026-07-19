"""claude -p subprocess invocation and result classification.

Role definitions are injected into the prompt as <role> blocks by stepio, so
`--agent` is deliberately NOT used (it would apply the definition twice);
frontmatter model/tools are resolved by wfrun and passed explicitly.

Verified against claude CLI v2.1.214 (2026-07):
- `--output-format json` yields {type, subtype, is_error, result,
  total_cost_usd, num_turns, session_id, ...}.
- `--json-schema` puts the validated object in `structured_output` (the raw
  JSON string also appears in `result`).
- Startup failures exit non-zero with empty stdout and the message on stderr.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

from .guardrails import ASK_PROMPT, ASK_SCHEMA
from .modes import blocked_line, strip_mode_line


@dataclass
class CliResult:
    ok: bool
    text: str = ""
    structured: dict | list | None = None
    cost_usd: float = 0.0
    num_turns: int = 0
    exit_code: int = 0
    stderr: str = ""
    error: str | None = None  # classification when ok is False
    raw: dict | None = field(default=None, repr=False)


def run_claude(prompt: str, *, system_prompt: str | None = None,
               model: str | None = None,
               effort: str | None = None, tools: str | None = None,
               schema: str | None = None, timeout: int = 600,
               cwd: str | None = None,
               permission_mode: str | None = None) -> CliResult:
    cmd = ["claude", "-p", "--output-format", "json", "--no-session-persistence"]
    if system_prompt:
        # Append (not replace): keeps the CLI's default tool-use scaffolding
        # while placing role/mode/rules in the high-authority channel.
        cmd += ["--append-system-prompt", system_prompt]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    if tools:
        cmd += ["--allowedTools", tools]
    if schema:
        cmd += ["--json-schema", schema]
    if permission_mode:
        cmd += ["--permission-mode", permission_mode]

    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired as e:
        stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        return CliResult(ok=False, exit_code=-1, stderr=stderr,
                         error=f"timeout after {timeout}s")
    except FileNotFoundError:
        return CliResult(ok=False, exit_code=-1,
                         error="claude CLI not found on PATH")

    if proc.returncode != 0:
        return CliResult(ok=False, exit_code=proc.returncode, stderr=proc.stderr,
                         error=f"claude exited {proc.returncode}: "
                               f"{proc.stderr.strip()[:500] or proc.stdout.strip()[:500]}")

    try:
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return CliResult(ok=False, exit_code=proc.returncode, stderr=proc.stderr,
                         error=f"unparseable claude output: {proc.stdout[:200]!r}")

    result = CliResult(
        ok=True,
        text=(raw.get("result") or ""),
        structured=raw.get("structured_output"),
        cost_usd=float(raw.get("total_cost_usd") or 0.0),
        num_turns=int(raw.get("num_turns") or 0),
        exit_code=proc.returncode,
        stderr=proc.stderr,
        raw=raw,
    )
    # _common.md (mode injection) mandates a leading [Mode: x] line; it must
    # not defeat the ERROR: protocol or the structured-output fallback.
    body = strip_mode_line(result.text)
    if raw.get("is_error"):
        result.ok = False
        result.error = f"claude reported is_error (subtype={raw.get('subtype')})"
    elif body.lstrip().startswith("ERROR:"):
        # Guardrail protocol: the step agent hit a blocker and stopped.
        result.ok = False
        result.error = body.strip()
    elif (blocked := blocked_line(result.text)) is not None:
        # Mode/rules refusal (_meta protocol): a constraint blocked the task.
        result.ok = False
        result.error = blocked[:500]
    elif schema and result.structured is None:
        # --json-schema should force structured output; treat absence as failure.
        try:
            result.structured = json.loads(body)
        except json.JSONDecodeError:
            result.ok = False
            result.error = "schema was specified but no structured output was returned"
    return result


def ask_llm(question: str, *, model: str = "haiku", cwd: str | None = None,
            timeout: int = 300) -> tuple[bool | None, str, float]:
    """LLM condition judgment. Returns (answer, reason, cost).

    answer is None when judgment failed (after one retry).
    """
    prompt = ASK_PROMPT.format(question=question)
    cost = 0.0
    for _ in range(2):
        res = run_claude(prompt, model=model, tools="Read", schema=ASK_SCHEMA,
                         timeout=timeout, cwd=cwd)
        cost += res.cost_usd
        if res.ok and isinstance(res.structured, dict) and "answer" in res.structured:
            return bool(res.structured["answer"]), str(res.structured.get("reason", "")), cost
        reason = res.error or "no structured answer"
    return None, reason, cost
