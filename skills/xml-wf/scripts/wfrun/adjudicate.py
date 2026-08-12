"""The `decider="llm"` adjudicator for the batch path.

An llm decider settles a step's decision request in-process, without stopping
the run -- that is the whole reason `decider="llm"` exists, and why this call
sits inside Executor._handle_decision rather than on the resume path
(xml-wf-decision-request.md §15.1).

Two properties are load-bearing:

- Structured output is forced (DECIDE_SCHEMA), then rendered into the ordinary
  §13.3 answer file, so a human and an llm decider both end up validated by
  decision.parse_answer. One gate, two producers (§15.2).
- Everything that is not a clean ruling -- a dead CLI, a non-object response, a
  verdict of "escalate", an option number that is not one, text the shared
  parser rejects -- returns escalate/failed here and the caller stops the run
  for a human (§5 fail-closed, §15.4).

Like adp.diagnose this hardcodes run_claude, and for the same reason: the
adjudicator is a claude subagent regardless of which backend runs the steps.
The run-llm path does NOT come here -- its orchestrator delegates to a
harness-native subagent so layer B stays free of claude dependencies (§15.7,
decision D8).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import decision as decision_mod
from . import modelmap
from .claude_cli import run_claude
from .guardrails import DECIDE_PROMPT, DECIDE_ROLE, DECIDE_SCHEMA

# Read-only by design: the payload arrives in the prompt, so tools are needed
# only to inspect a declared artifact before ruling on it. A decider that could
# write could repair the deliverable itself, which is what form (b) -- re-run
# the step -- is for (§15.2).
DECIDE_TOOLS = "Read"


@dataclass
class Adjudication:
    """What the decider settled, in the answer-file text the parser accepts.

    `answer_text` is None unless verdict == "settled": escalation and every
    failure mode leave the answer path empty so the human has somewhere
    obvious to write (§15.2).
    """
    verdict: str  # "settled" | "escalate" | "failed"
    answer_text: str | None = None
    reason: str = ""
    raw: str | None = None  # the rendered text a failed parse rejected
    cost_usd: float = 0.0


def render_answer(option: int | None, text: str) -> str:
    """A ruling in the §13.3 answer format: machine-readable first line, then
    free text. The same shape a person writes by hand, on purpose."""
    head = "none" if option is None else str(option)
    return f"option: {head}\n\n{text.strip()}\n"


def adjudicate(step_id: str, request_body: str, option_count: int, *,
               model: str | None = None, cwd: str | None = None,
               timeout: int = 600, runner=run_claude) -> Adjudication:
    """Settle one decision request, or decline to (§15.2).

    `option_count` comes from the parsed payload and bounds the ruling: an
    option number outside it is rejected by parse_answer, exactly as it would
    be from a human.
    """
    res = runner(DECIDE_PROMPT.format(step_id=step_id, request=request_body),
                 system_prompt=f"<role>\n{DECIDE_ROLE}\n</role>",
                 model=modelmap.resolve(model, "cc"),
                 tools=DECIDE_TOOLS, schema=DECIDE_SCHEMA,
                 timeout=timeout, cwd=cwd)
    if not res.ok or not isinstance(res.structured, dict):
        return Adjudication(verdict="failed", cost_usd=res.cost_usd,
                            reason=f"adjudicator unavailable: {res.error}")

    ruling = res.structured
    text = str(ruling.get("text") or "").strip()
    verdict = ruling.get("verdict")
    if verdict == "escalate":
        return Adjudication(verdict="escalate", cost_usd=res.cost_usd,
                            reason=text or "escalated without a reason")

    if verdict == "option":
        # The schema requires `verdict` and `text` only, so a ruling can name
        # an option and omit the number. Caught here rather than at the parser,
        # which would see a rendered "option: None" and report something less
        # useful than the truth (§15.2).
        raw_option = ruling.get("option")
        if not isinstance(raw_option, int) or isinstance(raw_option, bool):
            return Adjudication(
                verdict="failed", cost_usd=res.cost_usd,
                reason=f"verdict 'option' without a usable option number: "
                       f"{raw_option!r}")
        option = raw_option
    elif verdict == "none":
        option = None
    else:
        return Adjudication(verdict="failed", cost_usd=res.cost_usd,
                            reason=f"unknown verdict {verdict!r}")

    answer_text = render_answer(option, text)
    _, errors = decision_mod.parse_answer(answer_text, option_count)
    if errors:
        return Adjudication(verdict="failed", cost_usd=res.cost_usd,
                            raw=answer_text,
                            reason="; ".join(errors))
    return Adjudication(verdict="settled", answer_text=answer_text,
                        cost_usd=res.cost_usd, reason=text)
