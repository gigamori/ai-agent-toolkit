"""Per-attempt records and the re-dispatch gate of run-llm.

run-llm hands every step to the orchestrator's own subagent facility -- the
Agent tool on Claude Code, the native `subagent` tool on Pi -- so no wfrun
process owns an attempt's lifetime the way `dispatch`'s wrapper does. What
code can still decide about an attempt is decided here:

- Files per attempt. Each attempt gets its own result file, its own copy of
  the prompt it ran, and a completion sentinel carrying its own key, so a
  late write by an earlier attempt can never pass for a later one's, and no
  result is ever deleted to make room for the next.
- The stop record (`observe_stop`, `wfrun stop`). After a stop request the
  attempt is observed for at most a minute. A result completing inside that
  window is adopted (`done`); after it, never. `confirmed` is written only on
  what Claude Code lets code see: TaskStop reported the task stopped (or
  already ended), no transcript of the subagent or of any agent it started
  grew during the window, and none of them holds a call without a result,
  one that started background work, or one refused or interrupted. Anything
  else is `unconfirmed`, and an unconfirmed attempt is never run again.
- The judge (`issue_judge`, `check_judge`; `wfrun judge`). Whether the work
  may run again from the start is judged by one fixed delegation that may
  read the evidence. Before it is delegated, code lists the evidence with
  checksums; its record is accepted only when it allows a restart, cites only
  lines of listed files, leaves them unchanged, arrives within the judge's
  own deadline, ends in the judge's own end marker, states its limits, and
  -- after a deadline -- gives a longer one grounded in a transcript line
  stamped inside the attempt's own run.
  One attempt gets one judge, and every failure of the judge is a refusal.
- A result that completes after its attempt was stopped or recorded is never
  adopted.

The ladder and the budgets (retry, debug, decision, the one re-dispatch after
an abort, the run-wide max) are `dispatch`'s own, applied to run-llm as well by
`__main__._admission_refusal`; nothing here counts them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from . import ccdeny, modes, stepio
from .guardrails import JUDGE_DEADLINE_CLAUSE, JUDGE_NEXT_TIMEOUT_LINE, JUDGE_PROMPT

# Observing a stop is bounded (user ruling 2026-10-01): a stop that is not
# confirmed within this many seconds is reported, never waited out.
STOP_OBSERVE_MAX = 60
STOP_POLL_INTERVAL = 1.0

FACET_CC = "cc"
FACETS = (FACET_CC,)

# What the orchestrator's TaskStop call reported, passed in verbatim: the
# task stopped; it had already ended (its completion notice arrived), which
# leaves nothing to stop; or the call failed or could not be made.
TASK_STOPPED = "stopped"
TASK_COMPLETED = "completed"
TASK_FAILED = "failed"
TASK_STOP_RESULTS = (TASK_STOPPED, TASK_COMPLETED, TASK_FAILED)

# What a stop record never speaks for, per facet. Written into every record.
NOT_OBSERVED = {
    FACET_CC: ("processes that tool calls started and left running -- "
               "detached or not -- and every operation outside files"),
}

# The judge is a fixed delegation: one prompt (guardrails.JUDGE_PROMPT), one
# model class (resolved through model_map.json like a step's), one tool list,
# one deadline. A record written after the deadline is not accepted.
JUDGE_TIMEOUT = 900
JUDGE_MODEL = "ultra"
JUDGE_TOOLS = ("Read", "Grep", "Write")

STOP_DONE = "done"
STOP_CONFIRMED = "confirmed"
STOP_UNCONFIRMED = "unconfirmed"

# Why the attempt was stopped: its deadline passed, or it ended before the
# deadline without a valid result (no reply, or a malformed one).
CAUSE_DEADLINE = "deadline"
CAUSE_ENDED = "ended"

# Ledger flags. A result that completes after its attempt was stopped or
# recorded is never adopted, and once seen, no further attempt of that step is
# admitted: it means something was still writing when the stop was judged.
FLAG_LATE_COMPLETION = "late_completion"
FLAG_STOP_CONTRADICTION = "stop_contradiction"

JUDGE_ALLOW = "allow"
JUDGE_DENY = "deny"
METHOD_RESTART = "restart"

# What each evidence file is, in the manifest the judge cites from.
EVIDENCE_PROMPT = "prompt"
EVIDENCE_RESULT = "result"
EVIDENCE_STOP = "stop"
EVIDENCE_TRANSCRIPT = "transcript"

_REF_RE = re.compile(r"^-\s*(?P<path>.+?)#L(?P<first>\d+)(?:-L?(?P<last>\d+))?\s*(?::.*)?$")


# The clock, behind names tests replace.
def _now() -> float:
    return time.time()


def _monotonic() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def attempt_label(cycle: int, seq: int) -> str:
    return f"c{cycle:02d}a{seq:02d}"


def attempt_files(steps_dir: str | Path, step_id: str, cycle: int, seq: int,
                  result_suffix: str = ".md") -> dict[str, Path]:
    """Every file one run-llm attempt owns. All live in the step's own
    directory, next to the result path the orchestrator passed, so
    `decisions/` (derived from a result file's grandparent) stays where it
    was."""
    stem = f"{step_id}_c{cycle:02d}_a{seq:02d}"
    steps_dir = Path(steps_dir)
    return {
        "result": steps_dir / f"{stem}_result{result_suffix}",
        "prompt": steps_dir / f"{stem}_prompt.md",
        "stop": steps_dir / f"{stem}_stop.json",
        "evidence": steps_dir / f"{stem}_evidence.json",
        "judge_prompt": steps_dir / f"{stem}_judge_prompt.md",
        "judge_handle": steps_dir / f"{stem}_judge_handle.json",
        "judge_record": steps_dir / f"{stem}_judge.md",
    }


def files_of(handle: dict) -> dict[str, Path]:
    result = Path(handle["result_path"])
    return attempt_files(result.parent, handle["step_id"], handle["cycle"],
                         handle["attempt"], result.suffix)


def read_json(path: str | Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path: str | Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stepio.write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))


def new_attempt(step_id: str, result_base: str | Path, cycle: int, seq: int,
                timeout: int, now: float, supersedes: dict | None = None,
                redispatch_record: str | None = None) -> dict:
    """The handle of a new attempt. Written by the caller, never here."""
    result_base = Path(result_base)
    files = attempt_files(result_base.parent, step_id, cycle, seq,
                          result_base.suffix or ".md")
    token = secrets.token_hex(stepio.DELEGATION_TOKEN_BYTES)
    return {
        "step_id": step_id, "cycle": cycle, "attempt": seq, "token": token,
        "dispatched_at": now, "timeout": timeout,
        "result_path": str(files["result"]), "prompt_path": str(files["prompt"]),
        "result_base": str(result_base),
        "sentinel": stepio.sentinel_line(
            step_id, f"{attempt_label(cycle, seq)}-{token}"),
        "delegation": stepio.delegation_description(step_id, cycle, seq, token),
        "supersedes": supersedes, "redispatch_record": redispatch_record,
    }


def is_complete(handle: dict) -> bool:
    """Whether the attempt's own result file ends in its own sentinel."""
    try:
        text = Path(handle["result_path"]).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    _, present = stepio.strip_sentinel_line(
        modes.strip_mode_line(text), handle["step_id"], handle.get("sentinel"))
    return present


def ledger_entry(steps_dir: str | Path, step_id: str, cycle: int, seq: int
                 ) -> dict | None:
    path = stepio.attempt_ledger_path(steps_dir, step_id, cycle)
    for entry in reversed(stepio.load_attempts(path)):
        if isinstance(entry, dict) and entry.get("seq") == seq:
            return entry
    return None


def flag_attempt(handle: dict, flag: str) -> None:
    path = stepio.attempt_ledger_path(Path(handle["result_path"]).parent,
                                      handle["step_id"], handle["cycle"])
    stepio.update_attempt(path, handle["attempt"], {flag: True})


def load_stop(handle: dict) -> dict | None:
    """The attempt's stop record, when one was written for this very attempt
    (its token)."""
    record = read_json(files_of(handle)["stop"])
    if record is None or record.get("token") != handle.get("token"):
        return None
    return record


def _transcripts(handle: dict) -> tuple[list[Path], str]:
    delegation = ccdeny.delegation_transcripts(handle)
    return list(delegation.transcripts), delegation.reason


def _sizes(paths) -> dict[str, tuple[int, int]]:
    sizes = {}
    for path in paths:
        try:
            stat = Path(path).stat()
            sizes[str(path)] = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            sizes[str(path)] = (-1, -1)
    return sizes


def _observe_cc(handle: dict, task_stop: str | None, window: float
                ) -> tuple[str | None, list[str], dict]:
    """(`done` or None, reasons the stop is not confirmed, what was watched)
    over the window. The window always runs to its end unless the result
    completes: `confirmed` needs a whole window in which nothing grew."""
    watched, missing = _transcripts(handle)
    reasons: list[str] = []
    if not watched:
        reasons.append(missing)
    if task_stop not in (TASK_STOPPED, TASK_COMPLETED):
        reasons.append(f"TaskStop reported {task_stop}")
    reasons += [f"at the stop: {call}" for call in ccdeny.open_tool_calls(watched)]
    before = _sizes(watched)
    changed = False
    end = _monotonic() + window
    while not is_complete(handle):
        remaining = end - _monotonic()
        if remaining <= 0:
            break
        _sleep(min(STOP_POLL_INTERVAL, remaining))
        current, _ = _transcripts(handle)
        changed = changed or _sizes(current) != before
    if is_complete(handle):
        return STOP_DONE, reasons, {}
    if changed:
        reasons.append("a transcript changed during the observation window")
    final, _ = _transcripts(handle)
    seen = set(reasons)
    reasons += [reason for reason in (f"at the end of the window: {call}"
                                      for call in ccdeny.open_tool_calls(final))
                if reason not in seen]
    return None, reasons, {"transcripts": [str(path) for path in final]}


def observe_stop(handle: dict, facet: str, task_stop: str | None,
                 window: float) -> dict:
    """Observe one attempt after a stop request and write its stop record.

    `done` when the attempt's own result completes inside the window: that
    completion is adopted (`record` it). `confirmed` when the facet's
    evidence holds (module docstring), with the record naming what it does
    not observe. Everything else is `unconfirmed`, with its reasons.

    An attempt is observed once: a stop record already written for it is
    returned as it is, so asking again cannot turn an unconfirmed stop into
    a confirmed one."""
    prior = load_stop(handle)
    if prior is not None:
        return prior
    files = files_of(handle)
    label = attempt_label(handle["cycle"], handle["attempt"])
    requested_at = _now()
    cause = (CAUSE_DEADLINE
             if requested_at >= handle["dispatched_at"] + handle["timeout"]
             else CAUSE_ENDED)
    if facet != FACET_CC:
        raise ValueError(f"no stop is observed for facet {facet!r}")
    verdict, reasons, watched = _observe_cc(handle, task_stop, window)
    if verdict is None:
        verdict = STOP_UNCONFIRMED if reasons else STOP_CONFIRMED

    entry = ledger_entry(Path(handle["result_path"]).parent, handle["step_id"],
                         handle["cycle"], handle["attempt"])
    if verdict == STOP_DONE and entry is not None:
        verdict = STOP_UNCONFIRMED
        reasons.append(f"the result completed after attempt {label} was "
                       f"recorded {entry.get('class')}; it is not adopted")
        flag_attempt(handle, FLAG_LATE_COMPLETION)

    record = {"step_id": handle["step_id"], "cycle": handle["cycle"],
              "attempt": handle["attempt"], "token": handle["token"],
              "facet": facet, "task_stop": task_stop, "cause": cause,
              "requested_at": requested_at, "window": window,
              "observed_until": _now(), "verdict": verdict, "reasons": reasons,
              "not_observed": NOT_OBSERVED[facet], **watched}
    write_json(files["stop"], record)
    return record


def _evidence_entry(kind: str, path: str | Path) -> dict:
    data = Path(path).read_bytes()
    lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    return {"kind": kind, "path": str(Path(path).resolve()),
            "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
            "lines": lines}


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _evidence_transcripts(handle: dict, stop: dict) -> list[Path] | str:
    """The transcripts of the stopped attempt: Claude Code's transcripts of
    the subagent and every agent it started."""
    label = attempt_label(handle["cycle"], handle["attempt"])
    found, missing = _transcripts(handle)
    return found or (f"attempt {label} has no transcript a judge could read "
                     f"({missing}) -- stop and report")


def issue_judge(handle: dict, stop: dict) -> dict | str:
    """Write the evidence manifest, the prompt and the handle of the one
    judge of a stopped attempt; the manifest, or why no judge is issued.

    The manifest lists every file the judge may cite with its sha256, taken
    before the judge exists. One attempt gets one judge: any of the judge's
    files already on disk refuses the issue."""
    files = files_of(handle)
    label = attempt_label(handle["cycle"], handle["attempt"])
    if any(files[name].exists() for name in
           ("evidence", "judge_prompt", "judge_handle", "judge_record")):
        return (f"a judge was already issued for attempt {label}; one attempt "
                "gets one judge, and its record is final -- stop and report")
    transcripts = _evidence_transcripts(handle, stop)
    if isinstance(transcripts, str):
        return transcripts
    sources = [(EVIDENCE_PROMPT, handle.get("prompt_path")),
               (EVIDENCE_RESULT, handle.get("result_path")),
               (EVIDENCE_STOP, files["stop"])]
    sources += [(EVIDENCE_TRANSCRIPT, path) for path in transcripts]
    evidence = [_evidence_entry(kind, path) for kind, path in sources
                if path and Path(path).is_file()]

    issued_at = _now()
    token = secrets.token_hex(stepio.DELEGATION_TOKEN_BYTES)
    cause = stop.get("cause", CAUSE_ENDED)
    t0, d_prev = handle["dispatched_at"], handle["timeout"]
    sentinel = stepio.sentinel_line(handle["step_id"], f"{label}-judge-{token}")
    judge = {"issued_at": issued_at, "timeout": JUDGE_TIMEOUT,
             "model": JUDGE_MODEL, "tools": list(JUDGE_TOOLS),
             "record_path": str(files["judge_record"]),
             "prompt_path": str(files["judge_prompt"]),
             "handle_path": str(files["judge_handle"]),
             "sentinel": sentinel,
             "description": f"wfrun-judge {handle['step_id']} {label} {token}"}
    manifest = {"step_id": handle["step_id"], "cycle": handle["cycle"],
                "attempt": handle["attempt"], "token": handle["token"],
                "facet": stop.get("facet"), "cause": cause, "t0": t0,
                "d_prev": d_prev, "files": evidence, "judge": judge}
    deadline = cause == CAUSE_DEADLINE
    prompt = JUDGE_PROMPT.format(
        cause=("its deadline passed" if deadline
               else "it ended without a valid result"),
        manifest=files["evidence"],
        files="\n".join(f"- {entry['kind']}: {entry['path']}"
                        for entry in evidence),
        deadline_clause=(JUDGE_DEADLINE_CLAUSE.format(
            d_prev=d_prev, t0=_iso(t0), deadline=_iso(t0 + d_prev))
            if deadline else ""),
        next_timeout_line=JUDGE_NEXT_TIMEOUT_LINE if deadline else "",
        record=files["judge_record"], sentinel=sentinel)

    # The judge's handle has the shape `poll` reads, so the judge's deadline
    # can wake the orchestrator like a step's; it carries no cycle or attempt,
    # so no attempt command (`stop`) takes it for an attempt.
    files["judge_prompt"].write_text(prompt, encoding="utf-8")
    write_json(files["judge_handle"], {
        "step_id": handle["step_id"], "token": token,
        "result_path": str(files["judge_record"]), "sentinel": sentinel,
        "dispatched_at": issued_at, "timeout": JUDGE_TIMEOUT,
        "delegation": judge["description"]})
    write_json(files["evidence"], manifest)
    return manifest


def _same_path(a: str | Path, b: str | Path) -> bool:
    return (os.path.normcase(str(Path(a).resolve()))
            == os.path.normcase(str(Path(b).resolve())))


def _parse_record(text: str) -> tuple[dict, list, list]:
    fields: dict[str, str] = {}
    refs: list[tuple[str, int, int]] = []
    errors: list[str] = []
    in_evidence = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if in_evidence and line.startswith("-"):
            match = _REF_RE.match(line)
            if match is None:
                errors.append(f"unreadable evidence line {line[:80]!r}")
                continue
            first = int(match["first"])
            refs.append((match["path"].strip(), first,
                         int(match["last"] or first)))
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        in_evidence = key == "evidence"
        if not in_evidence:
            fields[key] = value.strip()
    return fields, refs, errors


def _epoch(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.timestamp()


def _cited_times(path: str | Path, first: int, last: int) -> list[float]:
    try:
        lines = Path(path).read_bytes().split(b"\n")
    except OSError:
        return []
    times = []
    for line in lines[first - 1:last]:
        try:
            entry = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        moment = _epoch(entry.get("timestamp")) if isinstance(entry, dict) else None
        if moment is not None:
            times.append(moment)
    return times


def check_judge(handle: dict, reply: str | None) -> tuple[int, str] | str:
    """(timeout of the next attempt, judge record path) when the judge's
    record admits restarting the step, else why not.

    Checked: the reply names this attempt's record and allows; the record
    was written within the judge's deadline and ends in the judge's own end
    marker; it allows a restart and nothing else; it states its limits; it
    cites at least one line range, each inside a manifest file; every
    manifest file is byte-identical to when it was listed; and after a
    deadline it gives a longer deadline, citing a transcript line stamped
    inside the attempt's own run."""
    files = files_of(handle)
    label = attempt_label(handle["cycle"], handle["attempt"])
    manifest = read_json(files["evidence"])
    if manifest is None or manifest.get("token") != handle.get("token"):
        return (f"no judge was issued for attempt {label} -- run `wfrun judge` "
                "first")
    judge = manifest.get("judge") or {}
    record_path = Path(judge.get("record_path", files["judge_record"]))
    words = (reply or "").strip().split(None, 1)
    if len(words) != 2 or not _same_path(words[1].strip(), record_path):
        return (f"the judge's reply is not `{JUDGE_ALLOW} {record_path}`; it is "
                "not a judgment -- stop and report")
    if words[0].lower() == JUDGE_DENY:
        return f"the judge denied re-running attempt {label} -- stop and report"
    if words[0].lower() != JUDGE_ALLOW:
        return (f"the judge's reply is not `{JUDGE_ALLOW} {record_path}`; it is "
                "not a judgment -- stop and report")
    try:
        written = record_path.stat().st_mtime
        text = record_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return f"the judge's record {record_path} cannot be read -- stop and report"
    if written > judge.get("issued_at", 0) + judge.get("timeout", 0):
        return (f"the judge's record {record_path} was written after the "
                "judge's deadline -- stop and report")

    # The end marker is keyed to this judge: a record without it was not
    # finished, or was not written for this judge.
    sentinel = judge.get("sentinel")
    text, sealed = (stepio.strip_sentinel_line(text, handle["step_id"], sentinel)
                    if sentinel else (text, False))
    if not sealed:
        return (f"the judge's record {record_path} does not end in the judge's "
                "end marker, so it is incomplete -- stop and report")
    fields, refs, errors = _parse_record(text)
    if errors:
        return f"the judge's record is malformed ({'; '.join(errors)}) -- stop and report"
    if fields.get("verdict", "").lower() != JUDGE_ALLOW:
        return "the judge's record does not say `verdict: allow` -- stop and report"
    if fields.get("method", "").lower() != METHOD_RESTART:
        return (f"the judge's record proposes method {fields.get('method')!r}; "
                f"only `{METHOD_RESTART}` can be run here -- stop and report")
    if not fields.get("limits"):
        return ("the judge's record does not state its limits (`limits:`) -- "
                "stop and report")
    if not refs:
        return "the judge's record cites no evidence line -- stop and report"
    listed = {os.path.normcase(entry["path"]): entry
              for entry in manifest.get("files", [])
              if isinstance(entry, dict) and "path" in entry}
    for path, first, last in refs:
        entry = listed.get(os.path.normcase(str(Path(path).resolve())))
        if entry is None:
            return (f"the judge's record cites {path}, which is not in the "
                    "evidence manifest -- stop and report")
        if not 1 <= first <= last <= entry.get("lines", 0):
            return (f"the judge's record cites lines {first}-{last} of {path}, "
                    f"which has {entry.get('lines', 0)} -- stop and report")
    for entry in listed.values():
        try:
            current = hashlib.sha256(Path(entry["path"]).read_bytes()).hexdigest()
        except OSError:
            current = None
        if current != entry.get("sha256"):
            return (f"evidence file {entry['path']} changed after it was listed "
                    "-- stop and report")
    timeout = int(manifest.get("d_prev", handle["timeout"]))
    if manifest.get("cause") != CAUSE_DEADLINE:
        return timeout, str(record_path)
    try:
        proposed = int(fields.get("next-timeout", ""))
    except ValueError:
        return ("the attempt hit its deadline and the judge's record gives "
                "no integer `next-timeout` -- stop and report")
    if proposed <= timeout:
        return (f"the judge's next-timeout {proposed} is not longer than the "
                f"deadline the attempt hit ({timeout}) -- stop and report")
    t0 = manifest.get("t0", handle["dispatched_at"])
    progress = any(t0 <= moment <= t0 + timeout
                   for path, first, last in refs
                   if listed[os.path.normcase(str(Path(path).resolve()))]
                   .get("kind") == EVIDENCE_TRANSCRIPT
                   for moment in _cited_times(path, first, last))
    if not progress:
        return ("the judge's record cites no transcript line stamped inside "
                "the attempt's own run to support a longer deadline -- stop "
                "and report")
    return proposed, str(record_path)


def replan_ledger_path(result_base: str | Path, node_id: str) -> Path:
    return Path(result_base).parent / f"{node_id}_replan.json"


def _well_formed(path: str | Path | None) -> bool:
    if not path:
        return False
    try:
        text = Path(path).read_text(encoding="utf-8")
        ET.fromstring(stepio.strip_fences(text))
    except (OSError, UnicodeDecodeError, ET.ParseError):
        return False
    return True


def admit_replan(node, result_base: str | Path, fix: str | None,
                 now: float, parent_max: int, log_base: int) -> dict | str:
    """The next builder attempt of a <replan>, or why it is refused.

    A replan has no sentinel: its output being well-formed XML is what says
    the builder finished. So an earlier attempt whose output is missing or
    not well-formed may still be running, and the builder is not delegated
    again. A finished attempt is followed either by a fix of it (`--fix`,
    within `retry`) or, without one, by the node's next visit.

    The entry carries `caps`: the parent's `max` and this node's `max-steps`
    as the parent's XML states them now, and `log_base`, the steps.log
    executions recorded when the builder was delegated (the builder writes
    none, and a run-llm run is sequential, so that is where the child's own
    steps start). A child's `prompt` reads them back (`continuation_caps`)."""
    entries = [e for e in stepio.load_attempts(
        replan_ledger_path(result_base, node.id)) if isinstance(e, dict)]
    if not entries:
        if fix:
            return (f"replan '{node.id}': --fix has no earlier builder attempt "
                    "to fix")
        cycle, seq = 1, 1
    else:
        last = entries[-1]
        label = attempt_label(last.get("cycle", 1), last.get("attempt", 1))
        if not _well_formed(last.get("output")):
            return (f"replan '{node.id}': attempt {label} left no well-formed "
                    f"XML at {last.get('output')}; whether its builder stopped "
                    "is unknown, so it is not delegated again -- stop and "
                    "report")
        if fix:
            cycle = last.get("cycle", 1)
            seq = sum(1 for e in entries if e.get("cycle") == cycle) + 1
            if seq > node.retry + 1:
                return (f"replan '{node.id}' cycle {cycle}: its retries are "
                        f"spent (retry={node.retry}) -- follow on-error="
                        f"\"{node.on_error}\"")
        else:
            cycle, seq = last.get("cycle", 1) + 1, 1
    base = Path(result_base)
    output = base.with_name(f"{base.stem}_c{cycle:02d}_a{seq:02d}{base.suffix}")
    return {"node_id": node.id, "cycle": cycle, "attempt": seq,
            "output": str(output), "dispatched_at": now,
            "timeout": node.timeout,
            "caps": {"parent_max": parent_max, "max_steps": node.max_steps,
                     "log_base": log_base}}


def continuation_caps(workflow_path: str | Path) -> dict | None:
    """The `caps` of the <replan> attempt that wrote `workflow_path`, or None
    when no ledger beside it names that file as an output or the entry holds
    no integer caps. Matched by file name: the ledger sits in the output's
    directory, and `--result` may have been given relative."""
    path = Path(workflow_path)
    for ledger in sorted(path.parent.glob("*_replan.json")):
        for e in stepio.load_attempts(ledger):
            if not isinstance(e, dict) or Path(str(e.get("output"))).name != path.name:
                continue
            caps = e.get("caps")
            if isinstance(caps, dict) and all(
                    isinstance(caps.get(k), int)
                    for k in ("parent_max", "max_steps", "log_base")):
                return caps
            return None
    return None


def record_replan(result_base: str | Path, entry: dict) -> None:
    path = replan_ledger_path(result_base, entry["node_id"])
    entries = stepio.load_attempts(path)
    entries.append(entry)
    write_json(path, entries)
