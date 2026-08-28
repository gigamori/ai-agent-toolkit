"""Autonomous Debugging Protocol, modernized.

v1 delegated every failure to an LLM debug agent that could RETRY / RESOLVE /
FAIL. v2 keeps only the useful half:

- deterministic retries happen first (executor, `retry` attribute);
- on `on-error="debug"`, a debug role (.claude/agents/debug.md) diagnoses the
  failure and may grant exactly ONE extra attempt with a fix instruction
  appended to the task. RESOLVE (fabricating substitute output) is gone.

Like step roles, the debug definition's body is injected into the prompt as a
<role> block and its frontmatter model/tools are passed explicitly.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from . import model, modelmap
from .agents import discover_agents
from .claude_cli import CliResult, run_claude
from .guardrails import DEBUG_PROMPT, DEBUG_SCHEMA


@dataclass
class Diagnosis:
    action: str  # "RETRY" | "FAIL"
    reason: str
    fix_instruction: str | None = None
    cost_usd: float = 0.0


def diagnose(step: model.Step, prompt: str, failure: CliResult, *,
             cwd: str | None = None, debug_role: str = model.DEBUG_ROLE,
             timeout: int = 600) -> Diagnosis:
    debug_def = discover_agents(cwd or ".").get(debug_role)
    if debug_def is None:
        return Diagnosis(action="FAIL",
                         reason=f"debug role '{debug_role}' not found in "
                                ".claude/agents (project) or the user agents dir "
                                "($CLAUDE_CONFIG_DIR or ~/.claude)/agents")
    debug_prompt = DEBUG_PROMPT.format(
        step_xml=json.dumps(asdict(step), ensure_ascii=False, indent=2),
        prompt=prompt[:8000],
        exit_code=failure.exit_code,
        result=(failure.error or "") + "\n" + failure.text[:4000],
        stderr=failure.stderr[:2000],
    )
    res = run_claude(debug_prompt,
                     system_prompt=f"<role>\n{debug_def.prompt}\n</role>",
                     model=modelmap.resolve(debug_def.model, "cc", allow_legacy=True),
                     tools=debug_def.tools,
                     schema=DEBUG_SCHEMA, timeout=timeout, cwd=cwd)
    if not res.ok or not isinstance(res.structured, dict):
        return Diagnosis(action="FAIL",
                         reason=f"debug agent unavailable: {res.error}",
                         cost_usd=res.cost_usd)
    return Diagnosis(
        action=res.structured.get("action", "FAIL"),
        reason=res.structured.get("reason", ""),
        fix_instruction=res.structured.get("fix_instruction"),
        cost_usd=res.cost_usd,
    )
