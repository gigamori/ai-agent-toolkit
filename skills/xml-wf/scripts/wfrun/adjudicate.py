"""The `decider="llm"` adjudicator for the batch path.

An llm decider settles a step's decision request in-process, without stopping
the run -- that is the whole reason `decider="llm"` exists, and why this call
sits inside Executor._handle_decision rather than on the resume path
(xml-wf-decision-request.md §15.1).

Two adjudicators live here, one per execution facility, and the difference is
only how the ruling is made machine-readable:

- `adjudicate` (run-cc) forces structured output with DECIDE_SCHEMA and renders
  the object into the ordinary §13.3 answer file (§15.2).
- `adjudicate_text` (run-pi, via `adjudicate_pi`) has no forced-structured-
  output facility to use -- `pi_cli.run_pi` rejects `schema=` outright -- so the
  decider writes the §13.3 shape itself (§17.1).

What they share is the property that matters: **every ruling, from a human or
from either decider, is validated by decision.parse_answer**. One gate, three
producers. And everything that is not a clean ruling -- a dead CLI, a non-object
response, an escalation, an option number that is not one, text the shared
parser rejects -- comes back as escalate/failed so the caller stops the run for
a human (§5 fail-closed, §15.4).

`adjudicate` hardcodes run_claude like adp.diagnose does; `adjudicate_text` is
runner-agnostic and `adjudicate_pi` binds it to run_pi. The run-llm path uses
NEITHER: its orchestrator delegates to a harness-native subagent so layer B
stays free of claude dependencies (§15.7, decision D8).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import decision as decision_mod
from . import modelmap, modes
from .claude_cli import run_claude
from .guardrails import (DECIDE_PROMPT, DECIDE_PROMPT_TEXT, DECIDE_ROLE,
                         DECIDE_SCHEMA)

# Case-insensitive to match decision.parse_answer's own `option:` line rule --
# one anchoring convention for both shapes a ruling can take (§17.1).
_ESCALATE_RE = re.compile(r"^[ \t]*escalate[ \t]*:[ \t]*(.*)$", re.IGNORECASE)

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


def adjudicate_text(step_id: str, request_body: str, option_count: int, *,
                    runner, model: str | None = None, runner_table: str = "llm",
                    cwd: str | None = None, timeout: int = 600) -> Adjudication:
    """Settle one request through a facility with no forced structured output.

    The decider writes the §13.3 answer shape itself, so this function only
    routes and validates: an `escalate:` first line declines the fork, anything
    else goes to decision.parse_answer whole (§17.1).

    Deliberately no rescue path for a well-formed ruling that arrived in a
    malformed envelope -- prose above the ruling, or a quoted request that made
    the runner classify the whole response as a fresh DECISION:. Both come back
    `failed`, the run stops, and a human answers. The alternative is a second
    parser competing with the first; the design's answer is to steer the shape
    with DECIDE_PROMPT_TEXT and to measure how often that fails, rather than to
    grow a rescue path per failure mode (§17.1, §17.4).
    """
    res = runner(DECIDE_PROMPT_TEXT.format(step_id=step_id,
                                           request=request_body),
                 system_prompt=f"<role>\n{DECIDE_ROLE}\n</role>",
                 model=modelmap.resolve(model, runner_table),
                 tools=DECIDE_TOOLS, timeout=timeout, cwd=cwd)
    # `res.ok` is required, not inspected around: this runner has already
    # classified the response, and any class it assigned (guardrail, refusal,
    # decision, behavioral) means the reply is not a ruling (§17.1).
    if not res.ok:
        return Adjudication(
            verdict="failed", cost_usd=res.cost_usd,
            reason=f"adjudicator did not return a ruling "
                   f"({res.error_class or 'no class'}): {res.error}")

    body = modes.strip_mode_line(res.text or "").strip()
    if not body:
        return Adjudication(verdict="failed", cost_usd=res.cost_usd,
                            reason="adjudicator returned an empty response")

    first = body.splitlines()[0]
    escalated = _ESCALATE_RE.match(first)
    if escalated:
        reason = escalated.group(1).strip()
        rest = "\n".join(body.splitlines()[1:]).strip()
        return Adjudication(
            verdict="escalate", cost_usd=res.cost_usd,
            # The whole response, not just the first line: unlike run-llm's
            # delegated ruling there is no orchestrator context to protect
            # here, and the human who receives the fork needs to know why it
            # reached them (§15.3, §17.1).
            reason=(f"{reason}\n\n{rest}".strip() if rest else reason)
                   or "escalated without a reason")

    _, errors = decision_mod.parse_answer(body, option_count)
    if errors:
        return Adjudication(verdict="failed", cost_usd=res.cost_usd,
                            raw=body, reason="; ".join(errors))
    return Adjudication(verdict="settled", answer_text=body + "\n",
                        cost_usd=res.cost_usd, reason=body)


def adjudicate_pi(step_id: str, request_body: str, option_count: int, *,
                  model: str | None = None, cwd: str | None = None,
                  timeout: int = 600) -> Adjudication:
    """`adjudicate`'s counterpart for `wfrun run --backend pi` (§17).

    Same signature as `adjudicate`, so Executor's `adjudicate=` injection point
    takes either. Lives here rather than in pi_cli because the protocol -- the
    Adjudication contract, the fail-closed semantics, the rejected-attempt
    convention -- belongs to this module; pi_cli only supplies the process
    launcher (§17.5-A).
    """
    from . import pi_cli  # deferred: cc-only runs never import the pi launcher
    return adjudicate_text(step_id, request_body, option_count,
                           runner=pi_cli.run_pi, model=model,
                           runner_table="llm", cwd=cwd, timeout=timeout)
