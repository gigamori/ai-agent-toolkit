"""The `DECISION:` protocol: payload/answer parsing and the shared vocabulary.

Third response prefix alongside `ERROR:` (guardrail) and `[BLOCKED:` (refusal)
-- a step that hits a fork it may not resolve alone declares it here instead of
silently picking a branch (xml-wf-decision-request.md §1).

Kept dependency-free inside wfrun on purpose: the two classification sites
(`claude_cli.classify_result` for the CLI path, `stepio.record_result` for the
run-llm `record` path -- §3), `pi_cli.classify_result_pi`, the executor and the
`resume --answer` parser all read this one contract, so it must not import any
of them.

Parsing is deliberate, not incidental. The payload's `work-state` and the
answer's `option:` line exist so that Python -- not an LLM -- decides between
continuation forms (a) and (b): whoever wrote the text knows its own state, and
routing a verifiable classification through inference is substrate
mis-allocation (§6, §13.3).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

# The one exception to the rule above: `model` is the pure attribute/vocabulary
# module and imports nothing from wfrun, so the decider vocabulary can be
# shared instead of spelled twice.
from . import model

DECISION_PREFIX = "DECISION:"


class DecisionError(Exception):
    """A decision artifact on disk is missing or unreadable when it is needed."""

WORK_STATES = ("complete", "stopped")

# Why a decision continued as form (b) -- re-run the step -- rather than form
# (a) -- accept the payload's output and move on. The first four are demotions
# from an (a) candidate; the last two were never (a) candidates. They share one
# vocabulary because the only question a human asks the report is "why did this
# step run again?" (xml-wf-decision-request.md §6).
B_REASON_NO_EXPECT_FILE = "no-expect-file"
B_REASON_MISSING_FILE = "missing-file"
B_REASON_MISSING_FILE_AT_RESUME = "missing-file-at-resume"
B_REASON_SCHEMA_STEP = "schema-step"
B_REASON_WORK_STATE_STOPPED = "work-state-stopped"
B_REASON_UNLISTED_OPTION = "unlisted-option"
# The ruling picked an option other than the one the step recommended, so the
# step's `output:` -- which states the value it would produce under its OWN
# recommendation -- no longer corresponds to what was decided. Adopting it
# anyway silently substitutes a value the adjudicator did not choose (measured
# 2026-08-13: recommendation 2 / output 300, ruling option 1 (375), applied
# 300). The chosen option cannot be turned into a value either: option lines
# are free text. So the step re-runs and produces the value itself.
B_REASON_OPTION_NOT_RECOMMENDED = "option-not-recommended"

B_REASONS = (
    B_REASON_NO_EXPECT_FILE,
    B_REASON_MISSING_FILE,
    B_REASON_MISSING_FILE_AT_RESUME,
    B_REASON_SCHEMA_STEP,
    B_REASON_WORK_STATE_STOPPED,
    B_REASON_UNLISTED_OPTION,
    B_REASON_OPTION_NOT_RECOMMENDED,
)

_KEY_LINE_RE = re.compile(
    r"^[ \t]*(fork|options|recommendation|work-state|output)[ \t]*:[ \t]?(.*)$")
_OPTION_LINE_RE = re.compile(r"^[ \t]*(\d+)\.[ \t]*(.*)$")
_ANSWER_LINE_RE = re.compile(r"^[ \t]*option[ \t]*:[ \t]*(.*)$", re.IGNORECASE)


def starts_with_decision(body: str) -> bool:
    """True when `body` opens the decision channel.

    Matched the same way the `ERROR:` protocol is (first-token anchored on the
    already-mode-line-stripped body), so the three prefixes stay mutually
    exclusive and a response merely *mentioning* the token is not caught.
    """
    return body.lstrip().startswith(DECISION_PREFIX)


@dataclass
class DecisionPayload:
    summary: str
    fork: str
    options: list[str]
    recommendation: int | None  # 1-based; None is an explicit "none"
    work_state: str
    output: str | None = None

    @property
    def work_complete(self) -> bool:
        return self.work_state == "complete"


@dataclass
class DecisionAnswer:
    option: int | None  # 1-based; None is an explicit "none" (unlisted answer)
    text: str           # free-form remainder (rationale / instructions)


def parse_payload(text: str) -> tuple[DecisionPayload | None, list[str]]:
    """(payload, errors) for a `DECISION:` body. Non-empty errors => malformed.

    Malformed is NOT the same as "not a decision": the caller has already
    classified this response as `decision` on the prefix alone (§1), and a
    malformed payload stays in that class so it can never be retried or
    debugged. This function only decides whether it is *answerable*.
    """
    errors: list[str] = []
    lines = text.splitlines()

    start = 0
    while start < len(lines) and lines[start].strip() == "":
        start += 1
    if start >= len(lines) or not lines[start].lstrip().startswith(DECISION_PREFIX):
        return None, [f"first non-empty line must start with '{DECISION_PREFIX}'"]
    summary = lines[start].lstrip()[len(DECISION_PREFIX):].strip()
    if not summary:
        errors.append(f"'{DECISION_PREFIX}' line carries no summary")

    # Split the remainder into key sections. A key line owns every following
    # line until the next key line, so `fork:` may wrap over its 1-3 lines and
    # `options:` may carry its numbered items underneath.
    sections: dict[str, list[str]] = {}
    order: list[str] = []
    current: str | None = None
    for line in lines[start + 1:]:
        match = _KEY_LINE_RE.match(line)
        if match:
            key, inline = match.group(1), match.group(2)
            if key in sections:
                errors.append(f"duplicate key '{key}:'")
            else:
                order.append(key)
            sections[key] = [inline] if inline.strip() else []
            current = key
        elif current is not None:
            sections[current].append(line)
        elif line.strip():
            errors.append(f"unexpected text before the first key line: {line.strip()[:60]!r}")

    for key in ("fork", "options", "recommendation", "work-state"):
        if key not in sections:
            errors.append(f"missing required key '{key}:'")

    fork = "\n".join(sections.get("fork", [])).strip()
    if "fork" in sections and not fork:
        errors.append("'fork:' is empty")

    options = _parse_options(sections.get("options", []), errors)

    recommendation = _parse_recommendation(
        sections.get("recommendation"), len(options), errors)

    work_state = "\n".join(sections.get("work-state", [])).strip()
    if "work-state" in sections and work_state not in WORK_STATES:
        errors.append(f"'work-state:' must be one of {'/'.join(WORK_STATES)}, "
                      f"got {work_state[:40]!r}")

    output = "\n".join(sections.get("output", [])).strip() or None
    if work_state == "complete" and not output:
        errors.append("'output:' is required when work-state is 'complete'")

    if errors:
        return None, errors
    return DecisionPayload(summary=summary, fork=fork, options=options,
                           recommendation=recommendation, work_state=work_state,
                           output=output), []


def _parse_options(body: list[str], errors: list[str]) -> list[str]:
    """Numbered `1.` / `2.` items, sequential from 1.

    The numbering is the payload's own -- the run report only transcribes it --
    because the answer selects by number and a number that exists solely in the
    report would drift for anyone reading the permanent payload file instead
    (§1). Sequentiality is checked for the same reason: a gap silently shifts
    every later option.
    """
    options: list[str] = []
    expected = 1
    for line in body:
        match = _OPTION_LINE_RE.match(line)
        if match:
            number = int(match.group(1))
            if number != expected:
                errors.append(f"'options:' must be numbered sequentially from 1; "
                              f"expected {expected}., got {number}.")
            options.append(match.group(2).strip())
            expected += 1
        elif line.strip():
            if options:
                options[-1] = (options[-1] + " " + line.strip()).strip()
            else:
                errors.append(f"'options:' text before the first numbered item: "
                              f"{line.strip()[:60]!r}")
    if body and len(options) < 2:
        errors.append(f"'options:' needs at least 2 items, got {len(options)}")
    for index, option in enumerate(options, start=1):
        if not option:
            errors.append(f"'options:' item {index} is empty")
    return options


def _parse_recommendation(body: list[str] | None, option_count: int,
                          errors: list[str]) -> int | None:
    if body is None:
        return None
    raw = "\n".join(body).strip()
    if raw.lower() == "none":
        return None
    try:
        number = int(raw)
    except ValueError:
        errors.append(f"'recommendation:' must be an option number or 'none', "
                      f"got {raw[:40]!r}")
        return None
    if option_count and not 1 <= number <= option_count:
        errors.append(f"'recommendation: {number}' is outside the "
                      f"1..{option_count} options")
        return None
    return number


def parse_answer(text: str, option_count: int) -> tuple[DecisionAnswer | None, list[str]]:
    """(answer, errors) for a decision answer file.

    Three rejections, all raised before anything executes (§13.3): a missing or
    unparseable `option:` line, a number outside the payload's own range, and
    `option: none` with no free text -- an unlisted answer carrying no guidance
    would leave the (b) re-run with nothing new to go on.
    """
    lines = text.splitlines()
    start = 0
    while start < len(lines) and lines[start].strip() == "":
        start += 1
    if start >= len(lines):
        return None, ["answer file is empty; expected a leading 'option: <N|none>' line"]

    match = _ANSWER_LINE_RE.match(lines[start])
    if not match:
        return None, ["first non-empty line must be 'option: <N|none>', got "
                      f"{lines[start].strip()[:60]!r}"]

    raw = match.group(1).strip()
    body = "\n".join(lines[start + 1:]).strip()

    if raw.lower() == "none":
        if not body:
            return None, ["'option: none' needs free-form text saying what to do "
                          "instead; without it the re-run has nothing new to act on"]
        return DecisionAnswer(option=None, text=body), []

    try:
        number = int(raw)
    except ValueError:
        return None, [f"'option:' must be a number or 'none', got {raw[:40]!r}"]
    if not 1 <= number <= option_count:
        return None, [f"'option: {number}' is outside the 1..{option_count} "
                      "options this request offers"]
    return DecisionAnswer(option=number, text=body), []


def answer_b_reason(answer: DecisionAnswer, recommendation: int | None) -> str | None:
    """Why the ruling itself rules form (a) out, or None (§6).

    Form (a) adopts the payload's `output:` verbatim, and that value states
    what the step would produce under ITS OWN recommendation. So (a) is only
    coherent when the ruling agrees with that recommendation; any other choice
    -- including an unlisted one -- must re-run the step so the value matches
    what was actually decided.

    Lives here so the three adjudication sites share one rule: `resume
    --answer` (__main__._ingest_answers), run-llm's `record --answer`
    (stepio.adjudicate_answer) and the in-process llm adjudicator
    (executor._handle_decision, §15.1). A fourth copy is how the seven-value
    vocabulary drifts.
    """
    if answer.option is None:
        return B_REASON_UNLISTED_OPTION
    if recommendation is None or answer.option != recommendation:
        return B_REASON_OPTION_NOT_RECOMMENDED
    return None


def request_id(step_id: str, cycle: int, seq: int) -> str:
    """Stable identity for one decision request.

    Mirrors the A layer's per-cycle `<id>_cNN_` stem (`__main__._a_layer_paths`)
    so both ledgers read alike. `cycle` counts visits to the step node and
    `seq` counts decisions within one visit (§13.1).
    """
    return f"{step_id}_c{cycle:02d}_d{seq:02d}"


def decisions_dir(run_dir: str | Path) -> Path:
    return Path(run_dir) / "decisions"


def request_path(run_dir: str | Path, rid: str) -> Path:
    return decisions_dir(run_dir) / f"{rid}_request.md"


def answer_path(run_dir: str | Path, rid: str) -> Path:
    """Where the answer is *expected* -- the run report names this path so the
    human has somewhere obvious to write, but `--answer` accepts any path."""
    return decisions_dir(run_dir) / f"{rid}_answer.md"


# ---------------------------------------------------------------------------
# run-llm ledger (xml-wf-decision-request.md §14). Batch keeps decision state
# in events.jsonl; run-llm has no such stream, so the `decisions/` directory
# IS the ledger and every question about it -- what number comes next, which
# request is still open, which rulings are settled -- is answered by reading
# that directory rather than by the orchestrator remembering. Keeping those
# derivations here is what lets §14's "the orchestrator holds no new memory"
# invariant hold: an LLM that only ever passes paths cannot mis-enumerate.
# ---------------------------------------------------------------------------

REQUEST_SUFFIX = "_request.md"
ANSWER_SUFFIX = "_answer.md"
VERDICT_SUFFIX = "_verdict.json"

_SEQ_RE = re.compile(r"_d(\d+)\Z")


def verdict_marker(dec_dir: str | Path, rid: str) -> Path:
    """The file whose existence means "this request has been adjudicated".

    Doubles as the double-answer guard (§14.2 step 4) and as the filter for
    `settled_pairs`, so one artifact carries both duties and they cannot
    disagree with each other.
    """
    return Path(dec_dir) / f"{rid}{VERDICT_SUFFIX}"


def request_ids(dec_dir: str | Path, prefix: str) -> list[str]:
    """Recorded request ids under `prefix`, in allocation order.

    `prefix` is the step id on the run-llm B layer and `<id>_cNN` on the A
    layer, which is what keeps the two apart: `s1_d01` never matches
    `s1_c01_d*` and vice versa. Zero-padded sequence numbers make the plain
    name sort the allocation order.
    """
    try:
        names = sorted(p.name for p in
                       Path(dec_dir).glob(f"{prefix}_d*{REQUEST_SUFFIX}"))
    except OSError:
        return []
    return [name[:-len(REQUEST_SUFFIX)] for name in names]


def allocate_request_id(dec_dir: str | Path, prefix: str) -> str:
    """The next request id for `prefix`, numbered past the highest on disk.

    Highest-plus-one rather than count-plus-one so a removed file can never
    make a new request reuse a retired id.
    """
    highest = 0
    for rid in request_ids(dec_dir, prefix):
        match = _SEQ_RE.search(rid)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{prefix}_d{highest + 1:02d}"


def step_request_ids(dec_dir: str | Path, step_id: str) -> list[str]:
    """Every recorded request for `step_id`, both layers, in allocation order.

    The B layer files under `<id>_dNN` and the A layer under `<id>_cNN_dNN`.
    Adjudication has to reach both: `record --answer` is the only settling
    verb, and a caller that knew only the bare id would leave every A-layer
    request permanently unanswerable -- detected, filed, and stuck.
    """
    seen = dict.fromkeys(request_ids(dec_dir, step_id))
    try:
        cycles = sorted({p.name.split("_d")[0] for p in
                         Path(dec_dir).glob(f"{step_id}_c*_d*{REQUEST_SUFFIX}")})
    except OSError:
        cycles = []
    for cycle_prefix in cycles:
        seen.update(dict.fromkeys(request_ids(dec_dir, cycle_prefix)))
    return list(seen)


def pending_request_id(dec_dir: str | Path, prefix: str) -> str | None:
    """The newest recorded request with no verdict marker, or None.

    At most one request per step is ever open (§13.1), so "newest unsettled"
    is the whole answer rather than a heuristic.
    """
    for rid in reversed(request_ids(dec_dir, prefix)):
        if not verdict_marker(dec_dir, rid).is_file():
            return rid
    return None


def pending_step_request_id(dec_dir: str | Path, step_id: str) -> str | None:
    """The step's open request, whichever layer filed it."""
    for rid in reversed(step_request_ids(dec_dir, step_id)):
        if not verdict_marker(dec_dir, rid).is_file():
            return rid
    return None


def settled_request_ids(dec_dir: str | Path, prefix: str) -> list[str]:
    """Adjudicated request ids under `prefix`, in allocation order.

    Enumerated from the verdict markers, NOT from the request files: the
    marker is what makes a ruling settled, so a missing request file has to
    surface as an error in `settled_pairs` rather than making the whole
    ruling disappear from the re-run prompt.
    """
    try:
        names = sorted(p.name for p in
                       Path(dec_dir).glob(f"{prefix}_d*{VERDICT_SUFFIX}"))
    except OSError:
        return []
    return [name[:-len(VERDICT_SUFFIX)] for name in names]


def llm_adjudications(dec_dir: str | Path, prefix: str) -> int:
    """How many of `prefix`'s settled requests an llm decider ruled on (§7).

    run-llm's cap tally. There is no events.jsonl here, so the verdict markers
    are the ledger -- the same artifacts that already carry the double-answer
    guard. Scope follows the prefix and therefore the layer: `<id>_cNN` counts
    one A-layer visit, a bare step id counts the B layer, which has no cycle
    concept at all and so counts for the whole run (§15.7). Unreadable markers
    are skipped rather than assumed: an unparseable file is not evidence that
    an llm ruled.
    """
    total = 0
    for rid in settled_request_ids(dec_dir, prefix):
        try:
            marker = json.loads(
                verdict_marker(dec_dir, rid).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if isinstance(marker, dict) and marker.get("decider") == model.DECIDER_LLM:
            total += 1
    return total


def settled_pairs(dec_dir: str | Path, prefix: str) -> list[tuple[str, str]]:
    """[(request body, answer body), ...] for every settled request, in order.

    Feeds the form-(b) re-run prompt (§14.3) with the same all-rulings
    guarantee the batch path gets from its event log: dropping an earlier
    ruling would let the step walk back into a fork already settled, so an
    unreadable artifact raises instead of being skipped.
    """
    pairs: list[tuple[str, str]] = []
    for rid in settled_request_ids(dec_dir, prefix):
        marker = verdict_marker(dec_dir, rid)
        try:
            recorded = json.loads(marker.read_text(encoding="utf-8"))
            request = (Path(dec_dir) / f"{rid}{REQUEST_SUFFIX}").read_text(
                encoding="utf-8")
            answer = Path(recorded["answer_path"]).read_text(encoding="utf-8")
        except (OSError, json.JSONDecodeError, KeyError) as e:
            raise DecisionError(
                f"decision {rid}: settled, but its request/answer can no "
                f"longer be read ({e}); the re-run would silently lose that "
                "ruling") from e
        pairs.append((request, answer))
    return pairs


def render_options(payload: DecisionPayload) -> list[str]:
    """The numbered options, for transcription into the run report (§8)."""
    return [f"  {i}. {opt}" for i, opt in enumerate(payload.options, start=1)]
