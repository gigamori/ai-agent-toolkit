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

import re
from dataclasses import dataclass
from pathlib import Path

DECISION_PREFIX = "DECISION:"

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

B_REASONS = (
    B_REASON_NO_EXPECT_FILE,
    B_REASON_MISSING_FILE,
    B_REASON_MISSING_FILE_AT_RESUME,
    B_REASON_SCHEMA_STEP,
    B_REASON_WORK_STATE_STOPPED,
    B_REASON_UNLISTED_OPTION,
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


def render_options(payload: DecisionPayload) -> list[str]:
    """The numbered options, for transcription into the run report (§8)."""
    return [f"  {i}. {opt}" for i, opt in enumerate(payload.options, start=1)]
