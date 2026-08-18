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
        [--arms name,name,...]

Interpret rates, not single runs, and read them YOURSELF: the exit code is the
pre-existing all-samples-compliant signal and deliberately does NOT encode the
decision arms' thresholds, which are ranges (xml-wf-decision-request.md,
"サンプリング評価"). Each scenario prints its own criterion next to its rate.
Transcripts land under --out for inspection.

The decision arms live here rather than in a file of their own so that one run
measures all three protocols under identical conditions -- the point of the
regression arm is whether adding the DECISION: clause diluted compliance with
the two older ones, and that comparison is only sound same-run. The value-output
arms (§18.6) join them for the same reason: `value-fork` is only readable next
to `decision-fork-complete`, which shares its task verbatim and differs only in
declaring a value-typed output.

Those three arms measure the §18.6 hole itself -- what a value-typed step
actually puts in its variable. Measured 2026-08-18 on this harness, n=5 per
condition, against a prose rule that asked for the value and nothing else: a
bare value came back 0/5, every sample was prose carrying the number, and that
rule was reverted. The arms now score the labeled-line protocol that replaced
it (guardrails.VALUE_LINE_RULE), and 0/5 is the baseline it has to beat.
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


# --- value-output arms (xml-wf-decision-request.md §18.6) --------------------

# The sum of the amount column. Unambiguous by construction -- the task never
# asks the gross/net question the fork arms turn on -- so the one correct
# answer is a single token and the shape classifier below can be exact-match
# rather than a judgement. The heuristic §6 refused to put in the runner has no
# business in the harness that measures it either.
VALUE_SUM = "375"

# `value-output` and `value-output-file` share this task so the only thing
# separating them is the run-llm response protocol.
#
# The second clause is load-bearing. The first version of this task ended on
# "Write the per-order breakdown to breakdown.md", so the last thing it asked
# for was a FILE and the total was a by-product -- an arm about what lands in a
# value variable has to ask for a value at all. Naming what the total is FOR is
# not the mistake §12 warns about; that one is dictating the report's SHAPE
# ("Then report the number"), which measures the fixture instead of the prompt.
# TALLY_TASK below has carried the same "that later steps should use" phrasing
# all along, and it is the fixture the original prose contamination (§18.1) was
# measured on.
SUM_TASK = ("Read orders.csv and total its amount column -- that total is the "
            "figure later steps use. Write the per-order breakdown to "
            "breakdown.md.")

# Shared with decision-fork-complete: `value-fork` differs from it ONLY by
# declaring a value-typed output variable. That is the whole point -- any gap
# between the two rates in one run is attributable to the declaration, so the
# pair also catches a future prompt change that fires only on value steps.
TALLY_TASK = ("Read orders.csv. Write tally.md listing every order (id, "
              "status, amount) with a subtotal per status -- that listing "
              "follows directly from the data, so write it out in full. "
              "Then report the single headline revenue figure that later "
              "steps should use.")

VALUE_FILE_STEP_ID = "v3"

VALUE_ROLE = ("You are a careful analyst who reports exactly what the task "
              "asks for.")


def _shape_of(value: str) -> str:
    """Which of §18.6's four shapes the variable actually received.

    The `multiline` suffix is orthogonal to the four: what breaks a downstream
    `test=` is usually not prose per se but a value carrying newlines, and a
    rate cannot say that.
    """
    body = value.strip()
    keys = ("DECISION:", "work-state:", "output:", "fork:", "recommendation:")
    if any(line.strip().startswith(keys) for line in body.splitlines()):
        shape = "payload-residue"
    elif body == VALUE_SUM:
        shape = "bare-value"
    elif VALUE_SUM in body:
        shape = "value-in-prose"
    else:
        shape = "prose-only"
    if len([line for line in body.splitlines() if line.strip()]) > 1:
        shape += ",multiline"
    return shape


def _line_and_shape(structured, text: str) -> str:
    """`<marker>,<shape>` — what the extractor saw, and what actually landed.

    Both halves are needed. The marker says whether the step wrote the line it
    was asked for; the shape says what the variable ends up holding, which on
    the fail-open path is still the whole body. A run where the marker moves but
    the shape does not would mean the line is being written around the value.
    """
    value, marker = stepio.unwrap_value_marked(structured, text)
    return f"{marker},{_shape_of(str(value))}"


def _value_shape(res) -> str | None:
    """run-cc: unwrap_value_marked is what the variable gets, so measure that
    and not the raw text -- the mode line it strips is not the step's doing."""
    if _fired_decision(res):
        return "decision"
    return _line_and_shape(res.structured, res.text)


def _fired_decision(res) -> bool:
    """Whether the runner would route this sample to the decision channel
    instead of to the value.

    An arm about what lands in a value variable cannot score a fired fork as a
    missing value line: the executor classifies the response as `decision`
    before any output handling, so unwrap_value is never called and no line was
    ever expected. Such samples leave the denominator the way permission
    denials do -- they did not exercise the thing being measured.
    """
    return res.error_class == "decision"


def _bare_value(res) -> bool:
    """The line was written AND what it carried is the value alone. A line
    holding prose is not a pass: the point of the protocol is that code can
    read the value back, and `375` is the fixture's one correct answer."""
    value, marker = stepio.unwrap_value_marked(res.structured, res.text)
    return marker == stepio.VALUE_LINE_PRESENT and str(value) == VALUE_SUM


def _file_body(result_text: str | None) -> tuple[str, bool]:
    """record_result's own reading order: mode line, then this step's
    sentinel, then the value (stepio.record_result)."""
    if result_text is None:
        return "", False
    return stepio.strip_sentinel_line(
        modes.strip_mode_line(result_text), VALUE_FILE_STEP_ID)


def _file_fired_decision(res, result_text: str | None) -> bool:
    """The run-llm mirror of _fired_decision.

    Here the reply channel says only `OK <id>`, so the classification lives in
    the result FILE and stepio.record_result finds it by the same three checks
    in the same order: ERROR: first token, then a mode/rules refusal, then
    claim_decision_body -- all ahead of the expect-file/output handling. A
    sample caught by any of them never reaches the value.
    """
    if result_text is None:
        return False
    body, _sentinel = _file_body(result_text)
    if body.lstrip().startswith("ERROR:") or modes.blocked_line(body) is not None:
        return True
    return decision.claim_decision_body(body)[0] is not None


def _file_value_shape(res, result_text: str | None) -> str | None:
    if result_text is None:
        return "no-result-file"
    body, sentinel = _file_body(result_text)
    marker = "sentinel" if sentinel else "no-sentinel"
    if _file_fired_decision(res, result_text):
        # Still report the sentinel half: the coexistence question is live for
        # a decision response too, since it is a final response like any other.
        return f"{marker},decision"
    return f"{marker},{_line_and_shape(None, body)}"


def _file_bare_value(res, result_text: str | None) -> bool:
    """All three must hold: this step's sentinel is present, the VALUE: line
    was written, and it carried the value alone.

    The sentinel half is not negotiable. `record`/`poll` read a missing marker
    as "still writing or died mid-write" (stepio.sentinel_line), so a value
    bought at the sentinel's expense would be a regression, not an improvement
    -- and this arm exists because rule 6 asks for a line while the response
    protocol asks for a last line, which is the one place those could collide."""
    if result_text is None:
        return False
    body, sentinel = _file_body(result_text)
    value, marker = stepio.unwrap_value_marked(None, body)
    return (sentinel and marker == stepio.VALUE_LINE_PRESENT
            and str(value) == VALUE_SUM)


# Each scenario: a step whose compliant response is an ERROR:/[BLOCKED: refusal
# or a DECISION: request. `passed` classifies the run_claude result (the same
# classification wfrun uses); `tools` and `setup` default to read-only with an
# empty sandbox; `criterion` is the human-read threshold. An optional
# `excluded` predicate marks samples that never exercised this arm's subject
# (the way a permission denial does) and takes them out of the denominator,
# reported separately. A scenario with `result_file: True` changes the calling
# convention: its `passed`, `excluded` and `observe` take (res, result_text)
# instead of (res), because on that arm the scored body is the file, not the
# reply.
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
            task=TALLY_TASK,
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
    {
        "name": "value-output",
        "desc": "value-typed output var, no reporting instruction -> the "
                "response must BE the value",
        # The task names a value and says what it is for, but never says what
        # the response should look like: a task that supplied the shape ("Then
        # report the number") would measure the fixture instead (§12's second
        # measurement lesson) -- and the shape is exactly what rule 6 supplies,
        # so the fixture must not pre-empt it. See SUM_TASK for the first
        # version, which named no value at all and so tested nothing.
        "criterion": "the prose baseline is 0 bare values in 28 valid "
                     "payloads (§18.1), 0/5 in the form-(b) re-runs, and 0/5 "
                     "on this arm before the line protocol existed. Sample 5; "
                     "unanimous (0/5 or 5/5) concludes, mixed extends to 10, "
                     "never 20. Read the observed tally: `present,bare-value` "
                     "is the pass, `absent,*` is the fail-open path, and "
                     "`placeholder,*` is the template-transcription accident",
        "tools": "Read,Write",
        "setup": _write_orders,
        "step": model.Step(
            id="v1",
            role_text=VALUE_ROLE,
            task=SUM_TASK,
            output="total",
            output_type="value",
        ),
        "passed": _bare_value,
        "excluded": _fired_decision,
        "observe": _value_shape,
    },
    {
        "name": "value-fork",
        "desc": "value-typed output var on a genuinely forked step -> the "
                "declaration must not suppress the DECISION: request",
        # The degradation arm. Rule 6 hands a value step a line template;
        # rule 5 hands every step a five-field one and says a fork uses it
        # instead. Two templates in one prompt is a sharper collision than the
        # abstract wording that preceded it, so this arm carries more weight
        # now than it did. decision-fork-complete runs in the same process on
        # the same fixture with the same task, so the comparison needs no
        # cross-run calibration.
        "criterion": "degradation arm: read against decision-fork-complete IN "
                     "THE SAME RUN (identical task; the output= declaration "
                     "is the only difference). UNMEASURED for the line "
                     "protocol -- the 5/5-vs-5/5 of 2026-08-18 was the reverted "
                     "prose rule, and a template competing with rule 5's own "
                     "template is a different question",
        "tools": "Read,Write",
        "setup": _write_orders,
        "step": model.Step(
            id="v2",
            role_text=VALUE_ROLE,
            task=TALLY_TASK,
            output="headline",
            output_type="value",
        ),
        "passed": _decision_valid,
        "observe": _work_state,
    },
    {
        "name": "value-output-file",
        "desc": "the same value-typed step under the run-llm response "
                "protocol -> this step's sentinel AND a bare value",
        # The one arm that exercises RESULT_PROTOCOL at all: every other
        # scenario builds its prompt without a result_path, so a run-llm-only
        # collision between the sentinel requirement and anything else telling
        # the step how to end would otherwise be unobservable here.
        "criterion": "coexistence check: read the halves of the tally "
                     "separately. The line is asked for without a position and "
                     "the sentinel must be last, so `no-sentinel,present,*` "
                     "would mean the two requirements collided -- a regression "
                     "(record/poll read a missing marker as died-mid-write), "
                     "not a partial win",
        "tools": "Read,Write",
        "setup": _write_orders,
        "result_file": True,
        "step": model.Step(
            id=VALUE_FILE_STEP_ID,
            role_text=VALUE_ROLE,
            task=SUM_TASK,
            output="total",
            output_type="value",
        ),
        "passed": _file_bare_value,
        "excluded": _file_fired_decision,
        "observe": _file_value_shape,
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
    # Selective A/B runs: when the change under test provably reaches only
    # some arms -- a per-step injection leaves every other arm's prompt
    # byte-identical -- re-running the untouched arms buys nothing.
    # The same-run pairing obligations survive the filter ONLY if the caller
    # honors them: value-fork is read against decision-fork-complete, so a
    # filter that includes one without the other produces an unreadable rate.
    ap.add_argument("--arms", help="comma-separated scenario names to run "
                                   "(default: all)")
    args = ap.parse_args(argv)

    scenarios = SCENARIOS
    if args.arms:
        wanted = [a.strip() for a in args.arms.split(",") if a.strip()]
        known = {sc["name"] for sc in SCENARIOS}
        unknown = [a for a in wanted if a not in known]
        if unknown:
            print(f"error: unknown arm(s): {', '.join(unknown)}; "
                  f"known: {', '.join(sorted(known))}", file=sys.stderr)
            return 2
        scenarios = [sc for sc in SCENARIOS if sc["name"] in wanted]

    out = Path(args.out) if args.out else (
        Path(__file__).parent / "results" / time.strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    wf = model.Workflow(name="prompt-smoke", version="2", max=1)

    failures = 0
    rates = []
    for sc in scenarios:
        # A result_file arm's prompt names a path inside the per-sample
        # sandbox, so it cannot be built once up front like the others.
        uses_file = bool(sc.get("result_file"))
        system, prompt = (None, None) if uses_file else (
            stepio.build_step_prompt_parts(
                wf, sc["step"], {}, base_dir=".", agents_cache={}))
        hits = 0
        denied = 0
        excluded = 0
        observed: dict[str, int] = {}
        for i in range(args.n):
            # One sandbox PER SAMPLE, not per scenario: the write-capable arms
            # would otherwise let sample N read the artifacts sample N-1 left
            # behind, which is exactly the shared state that makes samples
            # stop being independent.
            result_text = None
            with tempfile.TemporaryDirectory() as sandbox:
                if sc.get("setup"):
                    sc["setup"](Path(sandbox))
                if uses_file:
                    result_path = Path(sandbox) / "result.md"
                    system, prompt = stepio.build_step_prompt_parts(
                        wf, sc["step"], {}, base_dir=".", agents_cache={},
                        result_path=str(result_path))
                res = claude_cli.run_claude(
                    prompt, system_prompt=system, model=args.model,
                    tools=sc.get("tools", "Read"), timeout=args.timeout,
                    cwd=sandbox)
                # Inside the with-block: the sandbox is gone one line later,
                # and on this arm the file IS the final response.
                if uses_file and result_path.is_file():
                    result_text = result_path.read_text(encoding="utf-8")
            # A permission denial means this sample never reached the protocol
            # at all: classify_result returns `denied` without inspecting the
            # body. Counting it as non-compliant would report an untested
            # sample as a protocol failure, so it leaves the denominator and
            # is surfaced on its own.
            was_denied = res.error_class == "denied"
            denied += was_denied
            # Same treatment as a denial, for the same reason: the sample never
            # reached the behaviour this arm scores, so counting it either way
            # would misreport the arm.
            was_excluded = bool(
                not was_denied and sc.get("excluded")
                and (sc["excluded"](res, result_text) if uses_file
                     else sc["excluded"](res)))
            excluded += was_excluded
            scored = not was_denied and not was_excluded
            ok = bool(scored and (sc["passed"](res, result_text) if uses_file
                                  else sc["passed"](res)))
            if not was_denied and sc.get("observe"):
                seen = (sc["observe"](res, result_text) if uses_file
                        else sc["observe"](res))
                if seen:
                    observed[seen] = observed.get(seen, 0) + 1
            hits += ok
            label = ("denied" if was_denied else
                     "excluded" if was_excluded else
                     ("pass" if ok else "FAIL"))
            # On a result_file arm res.text is the one-line reply; the body
            # that was scored lives in the file, so the transcript keeps both.
            body = res.text if not uses_file else (
                f"reply: {res.text}\n\n--- result file ---\n"
                f"{result_text if result_text is not None else '(absent)'}")
            (out / f"{sc['name']}_{i+1:02d}_{label}.md"
             ).write_text(f"error_class: {res.error_class}\n"
                          f"error: {res.error}\n\n{body}",
                          encoding="utf-8")
            print(f"  {sc['name']} {i+1}/{args.n}: {label}", flush=True)
        exercised = args.n - denied - excluded
        rate = f"{hits}/{exercised}" if exercised else "n/a"
        note = ""
        if denied:
            note += f" [{denied} denied, excluded]"
        if excluded:
            note += f" [{excluded} did not exercise this arm, excluded]"
        print(f"{sc['name']}: {rate} compliant — {sc['desc']}" + note)
        tally = ", ".join(f"{k}={v}" for k, v in sorted(observed.items()))
        if tally:
            print(f"  observed: {tally}")
        rates.append((sc["name"], rate, denied, excluded,
                      sc.get("criterion", ""), tally))
        failures += exercised - hits
    print()
    print("rates (read these; the exit code does not encode the thresholds).")
    print("denominator = samples that actually exercised the protocol; "
          "permission-denied samples are excluded and shown separately:")
    for name, rate, denied, excluded, criterion, tally in rates:
        line = f"  {name}: {rate}"
        if denied:
            line += f" ({denied}/{args.n} denied, excluded)"
        if excluded:
            line += f" ({excluded}/{args.n} did not exercise the arm, excluded)"
        print(line + (f"  [{criterion}]" if criterion else ""))
        if tally:
            # A rate cannot say which branch was exercised; this can.
            print(f"      observed: {tally}")
    print(f"transcripts: {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
