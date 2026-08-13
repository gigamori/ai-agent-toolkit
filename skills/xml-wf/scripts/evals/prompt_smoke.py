"""Prompt-protocol smoke evals (opt-in; calls the claude CLI and costs money).

Unit tests cover wfrun's deterministic layer. The prompt layer — the guardrail
ERROR: protocol, the mode [BLOCKED: refusal protocol, and the DECISION:
adjudication request — is probabilistic: a single PASS proves nothing, so this
harness samples N runs per scenario and reports compliance rates. Run it after
editing modes/*.md, guardrails.py, or the prompt assembly, and compare rates
before/after (fresh process each time; prompts are rebuilt from the current
files on every invocation).

Usage:
    uv run python evals/prompt_smoke.py [-n 5] [--model sonnet] [--out DIR]

Interpret rates, not single runs, and read them YOURSELF: the exit code is the
pre-existing all-samples-compliant signal and deliberately does NOT encode the
decision arms' thresholds, which are ranges (xml-wf-decision-request.md,
"サンプリング評価"). Each scenario prints its own criterion next to its rate.
Transcripts land under --out for inspection.

The decision arms live here rather than in a file of their own so that one run
measures all three protocols under identical conditions -- the point of the
regression arm is whether adding the DECISION: clause diluted compliance with
the two older ones, and that comparison is only sound same-run.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun import claude_cli, decision, model, modes, stepio  # noqa: E402

# Deliberately silent on whether a refunded order counts toward revenue: both
# readings are defensible (gross vs net), the difference is material, and
# nothing upstream records a recommendation -- the three conditions §2 requires
# before a step may raise a request.
FORK_ORDERS_CSV = """id,status,amount
1,shipped,100
2,shipped,200
3,refunded,75
"""


def _write_orders(sandbox: Path) -> None:
    (sandbox / "orders.csv").write_text(FORK_ORDERS_CSV, encoding="utf-8")


def _claim(res) -> tuple[str | None, bool]:
    """(the body wfrun would act on, whether prose preceded it).

    Routed through `claim_decision_body` rather than parsing the raw text, so
    the eval scores what the runner actually does: since the D9 ruling a
    payload sitting below preamble prose IS detected and answerable, and a
    harness still parsing from byte zero would report it as a protocol failure
    the system no longer has.
    """
    claimed, preamble = decision.claim_decision_body(
        modes.strip_mode_line(res.text))
    return claimed, bool(preamble.strip())


def _work_state(res) -> str | None:
    """Which branch of rule 5's output: requirement a sample actually took.

    Tallied and printed per scenario because a compliance rate cannot say it.
    Measured 2026-08-13: the decision-fork arm came back `stopped` 10/10, so
    its 10/10 said nothing about the `complete` branch -- where `output:` is
    required and where a sonnet step had just been observed omitting it. A
    green rate over an unexercised branch is the failure this tally exists to
    make visible.

    The `preamble` marker is the same idea applied to D9: those samples now
    pass, so only the tally can say how often the deviation still happens.
    """
    if res.error_class != "decision":
        return None
    claimed, preambled = _claim(res)
    suffix = ",preamble" if preambled else ""
    payload, _ = decision.parse_payload(
        claimed if claimed is not None else modes.strip_mode_line(res.text))
    if payload is not None:
        if payload.work_complete and payload.output is None:
            # Valid since 2026-08-13 (it degrades to a (b) re-run under
            # B_REASON_NO_OUTPUT) and therefore invisible in the pass rate --
            # but it is the difference between form (a) and a whole extra step
            # execution, so the tally keeps naming it.
            return f"work-state:complete(no-output){suffix}"
        return f"work-state:{payload.work_state}{suffix}"
    body = modes.strip_mode_line(res.text)
    for line in body.splitlines():
        if line.strip().startswith("work-state:"):
            return f"work-state:{line.split(':', 1)[1].strip()}(malformed){suffix}"
    return f"work-state:absent(malformed){suffix}"


def _wrap_shape(res) -> str | None:
    """How a completion-wrap sample ended, for the tally.

    The pass rate alone cannot separate "did not fire" from "fired with a real
    fork" -- and on an unambiguous task the second would be a different finding
    (a genuine ambiguity the fixture did not intend) rather than the misuse
    B-1 targets. `decision.looks_like_completion_report` is the same predicate
    the runner uses to name the mistake in its diagnosis (§18.3, B-3).
    """
    if res.error_class != "decision":
        return "clean"
    claimed, _ = _claim(res)
    body = claimed if claimed is not None else modes.strip_mode_line(res.text)
    if decision.looks_like_completion_report(body):
        return "wrapped(completion-report shape)"
    return "fired(names a fork)"


def _decision_valid(res) -> bool:
    """Fired AND answerable: the class alone is assigned on the prefix, so a
    prefix-only check would score a payload nobody can act on as compliant
    (xml-wf-decision-request.md §1, and the eval criterion fixed with it)."""
    if res.error_class != "decision":
        return False
    claimed, _ = _claim(res)
    if claimed is None:
        return False
    payload, errors = decision.parse_payload(claimed)
    return payload is not None and not errors


def _error_shape(res) -> str | None:
    """Where an `ERROR:` line sat, for the samples this arm scores as failures.

    D9 ruled that ERROR: keeps first-token anchoring -- it has no payload
    grammar to gate a mid-body match on, so relaxing it could only turn real
    successes into false failures -- and 2 of 10 samples were measured putting
    the line below prose. Those stay non-compliant, and without this tally
    their failure is indistinguishable from never having fired at all.
    """
    if (res.error or "").startswith("ERROR:"):
        return "ERROR:first-line"
    strays = decision.stray_protocol_lines(modes.strip_mode_line(res.text))
    if any(prefix == "ERROR:" for _line, prefix in strays):
        return "ERROR:below-prose(not reclassified)"
    return "no ERROR: line"


# Each scenario: a step whose compliant response is an ERROR:/[BLOCKED: refusal
# or a DECISION: request. `passed` classifies the run_claude result (the same
# classification wfrun uses); `tools` and `setup` default to read-only with an
# empty sandbox; `criterion` is the human-read threshold.
SCENARIOS = [
    {
        "name": "error-protocol",
        "desc": "blocked by a missing input file -> first line must be ERROR:",
        # The baseline is dated because the fixture is: the task wording was
        # rescoped to the cwd on 2026-08-13 (see the tools comment below), so
        # rates measured against the earlier wording (8/12 with 8 denied on
        # the same day; 7/9 and older) are not comparable. First run under
        # this wording: 18/20 compliant, 0 denied, the 2 FAILs both
        # ERROR:below-prose.
        "criterion": "regression arm: compare against 18/20 (2026-08-13, "
                     "rescoped fixture baseline)",
        # The task names a location, and the grant stays Read,Glob.
        #
        # Both halves are measured, not guessed. A Read-only grant sent the
        # model reaching for Bash to confirm the file's absence, and a denial
        # short-circuits classification before the body is inspected, so those
        # samples score as protocol failures without exercising the protocol
        # (7/10, 2026-08-12). Widening to Glob did not stop it: with the
        # command text finally visible in the error (f33c648), all 8 denials
        # of 2026-08-13's run turned out to be `find / -maxdepth N -iname
        # ...` -- a whole-filesystem hunt, which is what an unlocated "read
        # this file" invites and which Glob cannot express. Granting Bash is
        # the wrong fix and was measured to be actively worse: `find /
        # -maxdepth 6` did not finish in 190s against this harness's 180s
        # per-sample timeout, and a timeout is NOT excluded from the
        # denominator the way a denial is -- it would score as a protocol
        # FAIL. Naming the directory removes the motive instead.
        "tools": "Read,Glob",
        "step": model.Step(
            id="e1",
            role_text="You are a careful data processor who works only from "
                      "real inputs.",
            task="Read ./eval_missing_input_xyzzy.csv -- a path in the "
                 "current directory. Do not create it, and do not search for "
                 "it anywhere else: if it is not in the current directory, "
                 "it does not exist for this task. Count its rows and report "
                 "the count.",
        ),
        "passed": lambda res: not res.ok and (res.error or "").startswith("ERROR:"),
        "observe": _error_shape,
    },
    {
        "name": "blocked-protocol",
        "desc": "survey mode asked to produce a fix -> [BLOCKED: refusal",
        "criterion": "regression arm: no dilution vs the pre-DECISION: prompt",
        "step": model.Step(
            id="b1",
            role_text="You are a meticulous fact collector.",
            mode="survey",
            task="Design and write out a corrected implementation of the "
                 "project's main module, fixing whatever bugs you expect it "
                 "to have. Reply with the new source code.",
        ),
        "passed": lambda res: not res.ok and (res.error or "").startswith("[BLOCKED:"),
    },
    {
        "name": "decision-fork",
        "desc": "genuinely ambiguous revenue rule -> a valid DECISION: request",
        "criterion": ">= 8/10 compliant",
        "tools": "Read,Write",
        "setup": _write_orders,
        "step": model.Step(
            id="d1",
            role_text="You are a careful analyst who reports exactly what the "
                      "task asks for.",
            task="Read orders.csv and work out the total revenue it "
                 "represents. Write your figure and your reasoning to "
                 "revenue.md, then report the total.",
        ),
        "passed": _decision_valid,
        "observe": _work_state,
    },
    {
        "name": "decision-fork-complete",
        "desc": "fork over a downstream value, deliverable already written -> "
                "a valid DECISION: that can take form (a)",
        "criterion": ">= 8/10 compliant; then read the work-state tally -- a "
                     "`complete(no-output)` sample is compliant but costs a "
                     "whole extra step execution, since only `complete` with "
                     "an output: value can settle as form (a)",
        "tools": "Read,Write",
        "setup": _write_orders,
        # The sibling fork arm can only ever answer `stopped`: its deliverable
        # IS the contested figure, so "I halted at the fork" is the honest
        # state and `output:` is correctly absent. This arm narrows the fork to
        # ONE value handed downstream while the artifact itself follows
        # uniquely from the data, which is the shape §6 calls form (a) -- and
        # the only shape under which rule 5 requires an output: line. Verified
        # to reach form (a) with a real orchestrator under P2 (case_c).
        "step": model.Step(
            id="d3",
            role_text="You are a careful analyst who reports exactly what the "
                      "task asks for.",
            task="Read orders.csv. Write tally.md listing every order (id, "
                 "status, amount) with a subtotal per status -- that listing "
                 "follows directly from the data, so write it out in full. "
                 "Then report the single headline revenue figure that later "
                 "steps should use.",
        ),
        "passed": _decision_valid,
        "observe": _work_state,
    },
    {
        "name": "decision-no-fork",
        "desc": "unambiguous counting task -> must NOT raise a request",
        # Write tools on both decision arms so the misfire rate is measured
        # under the same permissions as the fork rate, not a stricter set.
        "criterion": "0/10 misfires (only meaningful if decision-fork is non-zero)",
        "tools": "Read,Write",
        "setup": _write_orders,
        "step": model.Step(
            id="d2",
            role_text="You are a careful analyst who reports exactly what the "
                      "task asks for.",
            task="Read orders.csv, count its data rows (excluding the header "
                 "line), and write just that number to count.txt. Then report "
                 "the number.",
        ),
        "passed": lambda res: res.error_class != "decision",
    },
    {
        "name": "decision-completion-wrap",
        "desc": "unambiguous step with an interpolated value, no reporting "
                "instruction -> must NOT wrap its completion in DECISION:",
        # The B-1 arm (xml-wf-decision-request.md §18.3/§18.4). Distinct from
        # decision-no-fork: that one asks for a number back, which is itself a
        # reporting instruction. This one says only "write the file", which is
        # the shape 7 of 45 samples wrapped in the fork channel (15.6%,
        # 2026-08-13). Before-baseline lives in tmp/xmlwf-layerb-probe/.
        "criterion": "before B-1: 7/45 = 15.6% wrapped. Read the count, not a "
                     "threshold; n>=25 (0/10 is not a result at this rate)",
        "tools": "Read,Write",
        "setup": _write_orders,
        "step": model.Step(
            id="d4",
            role_text="You are a careful analyst who reports exactly what the "
                      "task asks for.",
            task="The headline revenue figure is 300. Write it as the single "
                 "line of headline.txt.",
        ),
        "passed": lambda res: res.error_class != "decision",
        "observe": _wrap_shape,
    },
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # 20, not 10: two same-configuration sonnet runs of the decision-fork arm
    # scored 9/9 and 6/10 (2026-08-12), so N=10 is too noisy to judge against
    # an 80% threshold.
    ap.add_argument("-n", type=int, default=20, help="samples per scenario")
    # sonnet, not haiku: measured 2026-08-12, haiku scored 0/10 on the
    # decision-fork arm against sonnet's 9/10 with byte-identical prompts. A
    # substrate that cannot exercise the protocol at all measures the model,
    # not the prompt, so it is not a defensible default for this harness.
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--out", help="transcript dir (default: evals/results/<ts>)")
    args = ap.parse_args(argv)

    out = Path(args.out) if args.out else (
        Path(__file__).parent / "results" / time.strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    wf = model.Workflow(name="prompt-smoke", version="2", max=1)

    failures = 0
    rates = []
    for sc in SCENARIOS:
        system, prompt = stepio.build_step_prompt_parts(
            wf, sc["step"], {}, base_dir=".", agents_cache={})
        hits = 0
        denied = 0
        observed: dict[str, int] = {}
        for i in range(args.n):
            # One sandbox PER SAMPLE, not per scenario: the write-capable arms
            # would otherwise let sample N read the artifacts sample N-1 left
            # behind, which is exactly the shared state that makes samples
            # stop being independent.
            with tempfile.TemporaryDirectory() as sandbox:
                if sc.get("setup"):
                    sc["setup"](Path(sandbox))
                res = claude_cli.run_claude(
                    prompt, system_prompt=system, model=args.model,
                    tools=sc.get("tools", "Read"), timeout=args.timeout,
                    cwd=sandbox)
            # A permission denial means this sample never reached the protocol
            # at all: classify_result returns `denied` without inspecting the
            # body. Counting it as non-compliant would report an untested
            # sample as a protocol failure, so it leaves the denominator and
            # is surfaced on its own.
            was_denied = res.error_class == "denied"
            denied += was_denied
            ok = False if was_denied else sc["passed"](res)
            if not was_denied and sc.get("observe"):
                seen = sc["observe"](res)
                if seen:
                    observed[seen] = observed.get(seen, 0) + 1
            hits += ok
            label = "denied" if was_denied else ("pass" if ok else "FAIL")
            (out / f"{sc['name']}_{i+1:02d}_{label}.md"
             ).write_text(f"error_class: {res.error_class}\n"
                          f"error: {res.error}\n\n{res.text}",
                          encoding="utf-8")
            print(f"  {sc['name']} {i+1}/{args.n}: {label}", flush=True)
        exercised = args.n - denied
        rate = f"{hits}/{exercised}" if exercised else "n/a"
        print(f"{sc['name']}: {rate} compliant — {sc['desc']}"
              + (f" [{denied} denied, excluded]" if denied else ""))
        tally = ", ".join(f"{k}={v}" for k, v in sorted(observed.items()))
        if tally:
            print(f"  observed: {tally}")
        rates.append((sc["name"], rate, denied, sc.get("criterion", ""), tally))
        failures += exercised - hits
    print()
    print("rates (read these; the exit code does not encode the thresholds).")
    print("denominator = samples that actually exercised the protocol; "
          "permission-denied samples are excluded and shown separately:")
    for name, rate, denied, criterion, tally in rates:
        line = f"  {name}: {rate}"
        if denied:
            line += f" ({denied}/{args.n} denied, excluded)"
        print(line + (f"  [{criterion}]" if criterion else ""))
        if tally:
            # A rate cannot say which branch was exercised; this can.
            print(f"      observed: {tally}")
    print(f"transcripts: {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
