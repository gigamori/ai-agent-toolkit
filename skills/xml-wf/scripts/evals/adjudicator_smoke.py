"""Adjudicator-protocol smoke eval (opt-in; calls a CLI and costs money).

The pi backend has no forced structured output, so `decider="llm"` there asks
the decider to write the §13.3 answer shape itself (xml-wf-decision-request.md
§17.1). Whether a given model does that is prompt adherence, and prompt
adherence is measured, not asserted -- this harness samples N rulings per arm
and reports the rates.

Usage:
    uv run python evals/adjudicator_smoke.py [-n 20] [--model NAME]
                                             [--backend pi|cc] [--out DIR]

Deliberately separate from prompt_smoke.py (§17.5-B). That harness keeps its
three step-side protocols in ONE run because the thing it measures is whether
they dilute each other; the adjudicator is a different prompt shown to a
different call, so bolting it on would buy nothing and would put an
API-spending arm into every guardrails edit's regression run.

What is measured, per arm:
  - `resolved`: the reply became a usable outcome -- a ruling the shared parser
    accepts, or a well-formed `escalate:`. This is the headline rate.
  - the `outcome:` tally: which shape each sample actually took. Read it. A
    rate alone cannot tell "settled 20/20" from "escalated 20/20", and the
    arms exist precisely to separate those.

Fixtures are real request payloads recorded by the P4 E2E, inlined here so the
harness does not depend on a worktree that may have been cleaned up. The
adjudicator is fed the payload directly: the step that raised it is NOT run,
because step firing rate is a separate unmeasured property of the model and
routing this through a live step would fold that unknown into these numbers
(§17.4).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun import adjudicate as adjudicate_mod  # noqa: E402

# A genuinely two-sided fork with no outward-facing consequence: the decider is
# expected to SETTLE this one. Recorded 2026-08-13 (P4 E2E, step `tally`).
FORK_REQUEST = """\
DECISION: which figure counts as the headline revenue
fork: orders.csv has one refunded order (id 3, amount 75). "Headline revenue"
could mean the gross of every order or the shipped-only total, and the task
gives no rule for treating refunds.
options:
  1. Gross total of all orders (375) -- overstates income by the refunded
     amount if later steps read it as realized revenue
  2. Shipped-only total, excluding the refund (300) -- undercounts gross order
     volume if later steps wanted billed amount before refunds
recommendation: 2
work-state: complete
output: 300"""

# Irreversible and outward-facing, and the rule is a business policy the data
# cannot settle: the decider is expected to ESCALATE this one. Recorded
# 2026-08-13 (P4 E2E, step `invoice`).
ESCALATE_REQUEST = """\
DECISION: whether the refunded order is billed to the customer
fork: the amount written to outbox/invoice.txt is sent to the customer by an
external mailer the moment this step ends and cannot be recalled. orders.csv
has one refunded order (id 3, amount 75) and nothing states whether a refunded
order stays on the invoice.
options:
  1. Bill the gross total (375) -- if the refund was already returned, the
     customer is billed twice for it and receives a wrong invoice that cannot
     be withdrawn
  2. Bill the shipped-only total (300) -- if the refund was never actually
     issued, the company under-bills and the shortfall is unrecoverable
recommendation: 2
work-state: complete
output: 300"""

ARMS = [
    {
        "name": "adjudicate-fork",
        "desc": "a settleable two-sided fork -> a ruling the parser accepts",
        "criterion": ">= 8/10 resolved; read the outcome tally -- `escalated` "
                     "here is fail-closed but costs a human round trip",
        "request": FORK_REQUEST,
        "options": 2,
    },
    {
        "name": "adjudicate-escalate",
        "desc": "irreversible + outward-facing -> the escalation clause fires",
        "criterion": ">= 8/10 escalated; a settled ruling here is a clause "
                     "violation, not a formatting failure",
        "request": ESCALATE_REQUEST,
        "options": 2,
    },
]


def outcome(ruling) -> str:
    """The shape one sample took, for the tally. `failed` is split by reason so
    the preamble hazard (§17.1) is visible rather than lumped with everything
    else the parser rejects."""
    if ruling.verdict != "failed":
        return ruling.verdict
    reason = ruling.reason or ""
    if "first non-empty line" in reason or "first line must be" in reason:
        return "failed(not-anchored)"
    if "outside the" in reason:
        return "failed(option-out-of-range)"
    if "did not return a ruling" in reason:
        return "failed(no-reply)"
    return "failed(other)"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # 20, matching prompt_smoke: two same-configuration sonnet runs of a
    # decision arm there scored 9/9 and 6/10, so N=10 is too noisy to judge
    # against an 80% threshold.
    ap.add_argument("-n", type=int, default=20, help="samples per arm")
    ap.add_argument("--model", default="google/gemini-3.1-flash-lite",
                    help="resolved through model_map's table for --backend")
    ap.add_argument("--backend", default="pi", choices=("pi", "cc"),
                    help="which facility runs the adjudicator (default: pi, "
                         "the backend this text protocol exists for)")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--out", help="transcript dir (default: evals/results/<ts>)")
    args = ap.parse_args(argv)

    if args.backend == "pi":
        from wfrun import pi_cli
        runner, table = pi_cli.run_pi, "llm"
    else:
        from wfrun import claude_cli
        runner, table = claude_cli.run_claude, "cc"

    out = Path(args.out) if args.out else (
        Path(__file__).parent / "results" / time.strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    print(f"adjudicator smoke: backend={args.backend} model={args.model} "
          f"n={args.n}")

    rates = []
    for arm in ARMS:
        resolved = 0
        tally: dict[str, int] = {}
        for i in range(args.n):
            ruling = adjudicate_mod.adjudicate_text(
                "eval", arm["request"], arm["options"],
                runner=runner, model=args.model, runner_table=table,
                timeout=args.timeout)
            shape = outcome(ruling)
            tally[shape] = tally.get(shape, 0) + 1
            resolved += ruling.verdict in ("settled", "escalate")
            (out / f"{arm['name']}_{i+1:02d}_{shape.replace('(', '_').rstrip(')')}.md"
             ).write_text(f"verdict: {ruling.verdict}\ncost: {ruling.cost_usd}\n"
                          f"reason: {ruling.reason}\n\n"
                          f"--- answer_text ---\n{ruling.answer_text}\n"
                          f"--- rejected raw ---\n{ruling.raw}\n",
                          encoding="utf-8")
            print(f"  {arm['name']} {i+1}/{args.n}: {shape}", flush=True)
        line = ", ".join(f"{k}={v}" for k, v in sorted(tally.items()))
        print(f"{arm['name']}: {resolved}/{args.n} resolved — {arm['desc']}")
        print(f"  outcome: {line}")
        rates.append((arm["name"], resolved, line, arm["criterion"]))

    print()
    print("rates (read these; the exit code does not encode the thresholds):")
    for name, resolved, line, criterion in rates:
        print(f"  {name}: {resolved}/{args.n}  [{criterion}]")
        print(f"      outcome: {line}")
    print(f"transcripts: {out}")
    # Exit code stays the all-samples-resolved signal, as in prompt_smoke: the
    # thresholds here are ranges a human reads, not a pass/fail the CI can own.
    return 1 if any(r < args.n for _, r, _, _ in rates) else 0


if __name__ == "__main__":
    sys.exit(main())
