"""claude -p subprocess invocation and result classification.

Role definitions are injected into the prompt as <role> blocks by stepio, so
`--agent` is deliberately NOT used (it would apply the definition twice);
frontmatter model/tools are resolved by wfrun and passed explicitly.

Verified against claude CLI v2.1.214+ (2026-07, re-verified against an
isolated v2.1.214 install and v2.1.218; see reliability-spec.md §13):
- `--output-format json` yields {type, subtype, is_error, result,
  total_cost_usd, num_turns, session_id, terminal_reason, api_error_status,
  permission_denials, ...}. `subtype` does NOT distinguish success from
  error ("success" is observed together with is_error=true) -- classify
  on `terminal_reason` / `api_error_status` instead (reliability-spec.md
  §13.9.1).
- Calls run with `--output-format stream-json --verbose`, whose `type:"result"`
  events have that same shape, one per turn. A refused call inside an agent
  the child started in the background is listed on the result of the turn
  that started it and never on the last one, which is all `json` prints
  (measured on v2.1.270, docs/dev/test-constraints.md). So refusals are the
  union over every result event; every other field is read from the last
  one, exactly what `json` gave.
- `--json-schema` puts the validated object in `structured_output` (the raw
  JSON string also appears in `result`).
- Startup failures may exit non-zero with a fully-formed error JSON on
  stdout (not just empty stdout + stderr) -- e.g. an unknown --model yields
  exit 1 with {is_error:true, terminal_reason:"api_error",
  api_error_status:404} on stdout. JSON parsing is therefore attempted
  before looking at the exit code.

CliResult.error_class (reliability-spec.md §3.1, §13.5) is one of: `env`
(CLI not found / unparseable output / non-retryable api_error status --
never retried), `timeout` (retried), `behavioral` (empty body, is_error
with a non-api_error terminal_reason, or a schema violation -- retried),
`guardrail` (a compliant `ERROR:`-prefixed step response -- not retried,
but `on-error="debug"` may still fire), `refusal` (a `[BLOCKED:` mode/rules
refusal -- neither retried nor debugged), `decision` (a `DECISION:` request
for human/llm adjudication -- neither retried nor debugged; see
xml-wf-decision-request.md §1/§3), `denied` (a refused call listed in
`permission_denials` of any result event -- neither retried nor debugged),
`unchecked` (a result that would have passed carries no
`permission_denials` field, so refused calls could not be checked --
neither retried nor debugged), `transient` (a retryable
upstream api_error by status code -- retried but never debugged, since
treating it as a fixable failure is what caused the P3/C3 retry-storm
incident this spec responds to). `classify_result()` is the single place
that assigns it **on this path only** -- the run-llm `record` path classifies
independently in `stepio.record_result` and the pi backend in
`pi_cli.classify_result_pi`; a new class must be added to all three
(xml-wf-decision-request.md §3). `is_retryable()`/`is_debuggable()` are the
executor-facing predicates.

Windows launch path (reliability-spec.md §13): the npm distribution's
`claude` is a `.cmd`/`.bat` shim. `subprocess.run(["claude", ...])` without
shell=True never resolves it (PATHEXT is not consulted), so this module
resolves an executable path once via `_resolve_claude_bin()` and launches
that directly -- bypassing cmd.exe, which otherwise silently corrupts argv
containing `& | % ^ < >` or newlines (measured in reliability-spec.md
§13.2). The system prompt is passed via `--append-system-prompt-file`, a
run-dir file the caller already owns (`system_prompt_file=`), rather than
inline, both to avoid argv length limits and to keep multi-line/metachar
payloads out of argv entirely.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import decision as decision_mod
from . import procbind
from .guardrails import ASK_PROMPT, ASK_SCHEMA
from .modes import blocked_line, strip_mode_line

# Design defaults for reliability-spec.md §13.5's `transient` class -- only
# 404 has been observed directly (unknown --model); the rest follow common
# "retry-safe" HTTP semantics for upstream API errors. Kept as a single
# constant so real observations can replace it without touching call sites.
TRANSIENT_API_ERROR_STATUSES = {429, 500, 502, 503, 504, 529}

# error_class values for which an identical-prompt retry is pointless: `env`
# (a config/CLI problem retry can't fix), `guardrail`/`refusal` (the step
# agent already gave its final, deterministic answer to this exact prompt),
# `decision` (the step is asking for adjudication -- re-running the identical
# prompt answers nothing, and the step may have side effects that a second
# execution would repeat; this holds for a MALFORMED payload too, which is
# why the class is pinned on the prefix alone, xml-wf-decision-request.md §1),
# `denied` (a permission problem retry can't fix), `unchecked` (the CLI's
# output lacks the field refusals are read from; a rerun lacks it too).
# Everything else -- including error_class=None, which is what pre-Phase-2.1
# CliResult construction (and any caller that never classifies) produces --
# stays retryable, matching prior behavior (reliability-spec.md §3.2/§13.5).
NON_RETRYABLE_CLASSES = {"env", "guardrail", "refusal", "decision", "denied",
                         "unchecked"}

# error_class values for which `on-error="debug"` must not fire: `refusal`/
# `denied` are the step agent's or the permission system's final word on
# THIS request, not a bug a debug re-diagnosis could fix; `decision` is a
# request for a judgment, so handing it to debug would have the debug role
# diagnose a failure that does not exist (xml-wf-decision-request.md §3);
# `transient` is an upstream API hiccup -- classifying it as a fixable
# "failed" and handing it to the recovery loop is the exact
# misclassification that drove the P3/C3 retry-storm incident this spec
# responds to (reliability-spec.md §3.2, §13.5); and `unchecked` is a CLI
# format change no step-level fix can repair, so a paid diagnosis is waste.
NON_DEBUGGABLE_CLASSES = {"refusal", "decision", "denied", "transient",
                          "unchecked"}


def is_retryable(error_class: str | None) -> bool:
    return error_class not in NON_RETRYABLE_CLASSES


def is_debuggable(error_class: str | None) -> bool:
    return error_class not in NON_DEBUGGABLE_CLASSES

# The print-mode flags every call carries. Streaming is what makes the result
# of every turn readable (classify_result); nothing is persisted, since the
# verdict comes from this output alone. Shared with evals/deny_canary.py, so
# the canary launches exactly what wfrun launches.
PRINT_ARGS = ("-p", "--output-format", "stream-json", "--verbose",
              "--no-session-persistence")

# cmd.exe metacharacters (plus newlines) that a Windows npm .cmd/.bat shim's
# argv parsing corrupts (measured in reliability-spec.md §13.2).
_SHIM_HOSTILE_CHARS = set("&|%^<>\n\r")

_resolution_cache: dict[str, object] = {}


def _is_stub(path: str) -> bool:
    """True if `path` is upstream's --omit=optional placeholder stub.

    install.cjs uses the same 4096-byte threshold to tell the ~500-byte
    stub script apart from the real (hundreds-of-MB) native binary; see
    reliability-spec.md §13.9.2 (confirmed by installing with
    --omit=optional: the stub is a 500-byte shell script, not a PE).
    """
    try:
        return os.path.getsize(path) < 4096
    except OSError:
        return True


def _resolve_claude_bin() -> tuple[str | None, bool]:
    """Resolve the claude executable once per process.

    Returns (path, via_shim):
    - path is None only when `claude` is not found on PATH at all -- the
      sole legitimate `env` (CLI-not-found) case (reliability-spec.md
      §13.3.1). Any override is done via PATH ordering, not an env var
      (decided; see reliability-spec.md §13.3.1 / §13.7).
    - via_shim is True when only the Windows npm .cmd/.bat launcher could
      be resolved (its cmd.exe layer mangles argv -- §13.2); callers must
      route hostile-metachar payloads through a file-based flag or refuse
      to launch (§13.3.3) rather than pass them on argv.
    """
    if "result" in _resolution_cache:
        return _resolution_cache["result"]

    which_path = shutil.which("claude")
    if which_path is None:
        result = (None, False)
    else:
        ext = os.path.splitext(which_path)[1].lower()
        if ext not in (".cmd", ".bat"):
            result = (which_path, False)
        else:
            sibling = os.path.join(
                os.path.dirname(which_path), "node_modules",
                "@anthropic-ai", "claude-code", "bin", "claude.exe")
            if os.path.isfile(sibling) and not _is_stub(sibling):
                result = (sibling, False)
            else:
                result = (which_path, True)

    _resolution_cache["result"] = result
    return result


def _supports_system_prompt_file(claude_bin: str) -> bool:
    """Capability probe for --append-system-prompt-file (§13.3.4).

    Verified present down to v2.1.214 (reliability-spec.md §13.8), but the
    probe stays as a fallback for older/unknown installs. No API call: an
    unknown flag is rejected before any model request, and a missing-file
    error on a known flag is likewise instant. Both verdict messages arrive
    on stderr (measured on v2.1.214 and v2.1.218).

    Fail-closed: only a definite "the flag parsed, the file was missing"
    answer counts as support. An indeterminate probe (CLI crash, unexpected
    wording) downgrades to inline `--append-system-prompt`, which still
    works everywhere and, on a shim, is caught by the metachar check in
    run_claude rather than corrupting the prompt silently.

    Cached per executable: `wfrun dispatch`'s wrapper (§5.1) may resolve a
    different binary than the caller did.
    """
    cache_key = f"supports_system_prompt_file::{claude_bin}"
    if cache_key in _resolution_cache:
        return _resolution_cache[cache_key]
    probe_path = os.path.join(tempfile.gettempdir(),
                              "__wfrun_probe_nonexistent__.txt")
    supported = False
    try:
        # encoding/errors: see the comment in _launch. Low risk here (the
        # verdict wording is ASCII) but the contract holds file-wide -- a
        # decode crash in a capability probe would be just as unclassified.
        probe = subprocess.run(
            [claude_bin, "-p", "--append-system-prompt-file", probe_path],
            input="", capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15)
        stderr = (probe.stderr or "").lower()
        if "unknown option" in stderr:
            supported = False
        elif "not found" in stderr or "does not exist" in stderr:
            # The only path we passed is the probe path, so a not-found
            # complaint means the flag itself was understood. Verified
            # wording: "Error: Append system prompt file not found: <path>".
            supported = True
    except (OSError, subprocess.TimeoutExpired):
        supported = False
    _resolution_cache[cache_key] = supported
    return supported


def _has_hostile_metachars(text: str) -> bool:
    return any(ch in _SHIM_HOSTILE_CHARS for ch in text)


@dataclass
class CliResult:
    ok: bool
    text: str = ""
    structured: dict | list | None = None
    cost_usd: float = 0.0
    num_turns: int = 0
    exit_code: int = 0
    stderr: str = ""
    error: str | None = None  # human-readable classification when ok is False
    error_class: str | None = None  # machine classification (reliability-spec.md §13/§3.1); always set when ok is False for results produced by classify_result()
    raw: dict | None = field(default=None, repr=False)  # the last result event
    stream: str = field(default="", repr=False)  # the whole stream-json stdout; "" for a single JSON object


_DENIED_COMMAND_LIMIT = 200


def _denied_command_fragment(denials: list) -> str:
    """The first denial's own command text, truncated, for the error string.

    `tool_name` alone cannot distinguish the two ways a step gets denied, and
    that ambiguity has cost real measurement time: an eval logged ten `denied`
    samples as "sonnet reached for Bash outside the tools grant" and stayed
    stuck there, because widening `tools=` did not stop them. Measured
    2026-08-12: a command containing `${VAR:-}` is refused by the permission
    classifier even when the tool IS granted, since the expansion cannot be
    matched against an allow prefix. Reaching for an ungranted tool and
    sending an unmatchable command look identical through `tool_name`; the
    command text tells them apart, and the payload already carries it
    (`permission_denials[].tool_input.command`) -- it was simply discarded.

    First denial only, truncated to 200 chars: the full record is kept
    untruncated in the run dir (executor writes `res.raw` per attempt), so
    this string is for the report/console surface and for eval sample files,
    which keep no raw. `description` is the fallback because a denial without
    a command still names what it tried.

    Degrades silently to "" on any shape that is not a dict carrying text --
    a diagnostic must never be the thing that raises.
    """
    for denial in denials:
        if not isinstance(denial, dict):
            continue
        tool_input = denial.get("tool_input")
        if not isinstance(tool_input, dict):
            continue
        for key in ("command", "description"):
            text = tool_input.get(key)
            if isinstance(text, str) and text.strip():
                text = " ".join(text.split())
                if len(text) > _DENIED_COMMAND_LIMIT:
                    text = text[:_DENIED_COMMAND_LIMIT] + "..."
                return f"; first denial: {text}"
    return ""


def _parse_output(stdout: str) -> tuple[list[dict], bool]:
    """(result events, whether stdout was a stream).

    A single JSON object is the `--output-format json` shape and is the only
    result event (an object with no `type` counts, as older callers write
    them). In a stream, one unparseable line voids the whole parse: a result
    event missed there could be the one that lists a refusal.
    """
    try:
        single = json.loads(stdout)
    except json.JSONDecodeError:
        single = None
    if isinstance(single, dict):
        return ([single] if single.get("type", "result") == "result" else []), False
    events = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return [], True
        if isinstance(event, dict) and event.get("type") == "result":
            events.append(event)
    return events, True


def _denial_union(events: list[dict]) -> list:
    """Every refused call listed on any result event, in event order."""
    union = []
    for event in events:
        denials = event.get("permission_denials")
        if isinstance(denials, list):
            union.extend(denials)
    return union


def classify_result(returncode: int, stdout: str, stderr: str, *,
                    schema: str | None = None) -> CliResult:
    """Turn a completed claude -p invocation's (returncode, stdout, stderr)
    into a CliResult. Pure function of those three inputs plus `schema` --
    shared, per reliability-spec.md §3.1, with `wfrun dispatch`'s wrapper
    (§5.1) and with `wait`, which reads the same fields back from the
    wrapper's exit.json/result.json files rather than from a live
    subprocess.Popen result.

    Classification order (reliability-spec.md §3.1, §13.5, §13.9.1):
    unparseable stdout or no result event -> env; permission_denials on any
    result event (independent of is_error, since it has been observed with
    is_error=False) -> denied; is_error -> transient/env (api_error, by
    api_error_status) or behavioral (any other terminal_reason); otherwise
    inspect the response body -> guardrail (ERROR:) / refusal ([BLOCKED:) /
    decision (DECISION:) / behavioral (empty body, or schema given but no
    structured output); last, a result that would pass while some result
    event lacks the permission_denials field -> unchecked. `subtype` is never
    consulted -- it is not a reliable success/error signal (§13.9.1).

    The three body prefixes are peers and ALL precede the schema fallback
    (xml-wf-decision-request.md §3): a `schema=` step's DECISION: text has no
    structured output, so a later check would classify it `behavioral` and
    retry it -- re-running a step that may already have written its
    deliverable.
    """
    events, is_stream = _parse_output(stdout)
    if not events:
        return CliResult(ok=False, exit_code=returncode, stderr=stderr,
                         error_class="env",
                         error=f"claude exited {returncode} with unparseable "
                               f"output: {stderr.strip()[:500] or stdout.strip()[:500]!r}")
    raw = events[-1]

    result = CliResult(
        ok=True,
        text=(raw.get("result") or ""),
        structured=raw.get("structured_output"),
        cost_usd=float(raw.get("total_cost_usd") or 0.0),
        num_turns=int(raw.get("num_turns") or 0),
        exit_code=returncode,
        stderr=stderr,
        raw=raw,
        stream=stdout if is_stream else "",
    )

    denials = _denial_union(events)
    if denials:
        # Machine-readable and independent of is_error (observed with
        # is_error=False, returncode=0 -- reliability-spec.md Phase 0 O1).
        result.ok = False
        result.error_class = "denied"
        tools = sorted({d.get("tool_name") for d in denials
                        if isinstance(d, dict) and d.get("tool_name")})
        result.error = ("claude reported permission_denials"
                        + (f" for: {', '.join(tools)}" if tools else "")
                        + _denied_command_fragment(denials))
        return result

    # _common.md (mode injection) mandates a leading [Mode: x] line; it must
    # not defeat the ERROR: protocol or the structured-output fallback.
    body = strip_mode_line(result.text)
    if raw.get("is_error"):
        result.ok = False
        terminal_reason = raw.get("terminal_reason")
        if terminal_reason == "api_error":
            status = raw.get("api_error_status")
            # Unknown/missing status is fail-closed to "env" (not retried):
            # treating an unrecognized status as retryable is exactly the
            # misclassification that drove the P3/C3 retry-storm incident
            # this spec responds to (reliability-spec.md §13.5).
            result.error_class = ("transient" if status in TRANSIENT_API_ERROR_STATUSES
                                  else "env")
            result.error = (f"claude reported api_error "
                            f"(status={status}, terminal_reason=api_error)")
        else:
            # Any other CLI-level failure (budget_exhausted,
            # structured_output_retry_exhausted, tool_deferred_unavailable,
            # turn_setup_failed, ...): not further classified, but retryable
            # like other transient/technical hiccups.
            result.error_class = "behavioral"
            result.error = f"claude reported is_error (terminal_reason={terminal_reason})"
        return result

    if body.lstrip().startswith("ERROR:"):
        # Guardrail protocol: the step agent hit a blocker and stopped. An
        # identical retry would hit the same guardrail again -- not
        # retryable, but `on-error="debug"` may still supply a fix.
        result.ok = False
        result.error_class = "guardrail"
        result.error = body.strip()
        return result

    blocked = blocked_line(result.text)
    if blocked is not None:
        # Mode/rules refusal (_meta protocol): a constraint blocked the
        # task. Not retryable, and not debuggable either (current
        # behavior, kept as-is).
        result.ok = False
        result.error_class = "refusal"
        result.error = blocked[:500]
        return result

    claimed, _preamble = decision_mod.claim_decision_body(body)
    if claimed is not None:
        # Decision protocol: the step hit a fork it may not resolve alone.
        # On a first-token match the class is pinned on the prefix alone --
        # whether the five fields are actually well formed is decided later,
        # by whoever holds the step definition (xml-wf-decision-request.md
        # §1). A payload below preamble prose is claimed only when it parses
        # whole (D9). Doing it in this order is what keeps a malformed
        # payload out of the retry loop.
        result.ok = False
        result.error_class = "decision"
        result.error = claimed.strip()
        return result

    if body.strip() == "" and result.structured is None:
        # New in Phase 2.1 (reliability-spec.md §3.1): a compliant-looking
        # but empty response is a CLI/model hiccup, not a step failure --
        # retryable like other `behavioral` cases.
        result.ok = False
        result.error_class = "behavioral"
        result.error = "empty result"
        return result

    if schema and result.structured is None:
        # --json-schema should force structured output; treat absence as failure.
        try:
            result.structured = json.loads(body)
        except json.JSONDecodeError:
            result.ok = False
            result.error_class = "behavioral"
            result.error = "schema was specified but no structured output was returned"

    if result.ok and any("permission_denials" not in event for event in events):
        # Every result measured carries the field, error results included
        # (docs/dev/test-constraints.md), so its absence is a format change and
        # an empty union proves nothing. Only a would-be success is withheld:
        # a result that already failed keeps its own class.
        result.ok = False
        result.error_class = "unchecked"
        result.error = ("claude result carries no permission_denials field, "
                        "so refused tool calls could not be checked")
    return result


def _run_with_tree_kill(cmd: list[str], prompt: str, timeout: int,
                        cwd: str | None) -> subprocess.CompletedProcess:
    """Like `subprocess.run(cmd, ..., timeout=timeout)`, but on timeout also
    kills the whole process tree, not just the immediate `claude` process.

    DEFENSE IN DEPTH, not a confirmed-bug fix (reliability-spec.md §5.3):
    a real-CLI E2E in this session (claude v2.1.218, win32-x64) timed out a
    `claude -p` call whose Bash tool had started a long-running child
    (`sleep 120`, including an explicitly detached/`nohup`+`disown` variant)
    and found ZERO surviving descendants even with plain `subprocess.run`'s
    default timeout handling (no tree-kill at all) -- claude.exe apparently
    already tears down its own children when killed (most likely a Windows
    Job Object with kill-on-close). The originally assumed leak did not
    reproduce. This function is kept anyway because: (a) it costs nothing
    when no timeout fires, (b) the guarantee may not hold on other
    platforms, sandboxes, or future CLI versions, and (c) `wfrun dispatch`'s
    wrapper (§5.1) self-enforces the step timeout with no orchestrator
    polling to ever notice a leak if the guarantee ever breaks.

    Windows: `taskkill /T /F` on the process's own PID -- the mechanism
    validated in reliability-spec.md Phase 0 O3, but that test killed a
    *shell-launched* process chain (cmd.exe -> node.exe -> claude.exe) from
    outside, a different scenario from a `claude -p` process's own
    Bash-tool-spawned children (this function's actual target), which this
    session found did not need it.
    POSIX: kill the process group via SIGKILL -- the analogous mechanism,
    NOT measured in this session (no POSIX host was available); a design
    default pending real verification, same as the Windows path above.

    That tree-kill covers only this process's OWN timeout. A hard kill of
    this process is covered by launching through `procbind.popen_bound`
    (I2): the child cannot keep working after its parent is gone.
    """
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    # encoding/errors: see the comment at the sibling `_run_bound` call in
    # _launch. Reached by `wfrun dispatch` (kill_tree=True) and by every pi
    # step, which routes here unconditionally, so it carries the same UTF-8
    # contract.
    proc = procbind.popen_bound(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, cwd=cwd,
                                encoding="utf-8", errors="replace",
                                creationflags=creationflags,
                                start_new_session=(os.name != "nt"))
    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True)
        else:
            import signal
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _run_bound(cmd: list[str], *, input: str, timeout: int,
               cwd: str | None) -> subprocess.CompletedProcess:
    """`subprocess.run(cmd, input=input, capture_output=True, text=True,
    timeout=timeout, cwd=cwd)`, with the child launched through
    `procbind.popen_bound` so a hard kill of this process ends it too (I2).

    Everything else is `subprocess.run`'s own behaviour, kept on purpose: on
    timeout only the child itself is killed (the tree-kill is
    `kill_tree=True`'s job), and TimeoutExpired is re-raised carrying the
    output collected so far.
    """
    # encoding/errors: see the comment at this helper's call in _launch.
    with procbind.popen_bound(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, cwd=cwd,
                              encoding="utf-8", errors="replace") as proc:
        try:
            stdout, stderr = proc.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            if os.name == "nt":
                # Windows reads the pipes on helper threads; only a second
                # communicate() collects what they read (as subprocess.run).
                exc.stdout, exc.stderr = proc.communicate()
            else:
                proc.wait()
            raise
        except BaseException:
            proc.kill()
            raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _launch(prompt: str, *, system_prompt: str | None = None,
           system_prompt_file: str | Path | None = None,
           model: str | None = None, effort: str | None = None,
           tools: str | None = None, schema: str | None = None,
           timeout: int = 600, cwd: str | None = None,
           permission_mode: str | None = None,
           kill_tree: bool = False) -> CliResult | tuple[int, str, str]:
    """Resolve the executable, build argv, and execute -- the "command
    construction" + "execution" thirds of reliability-spec.md §3.1's
    three-way split (classification is `classify_result()`).

    `system_prompt_file`, when given, is a run-dir file the CALLER already
    wrote and owns (reliability-spec.md §13.3.2/I1) -- this function never
    creates or deletes a system-prompt file itself. It is resolved to an
    absolute path (the child runs with `cwd=`, which may differ from this
    process's cwd) and passed via `--append-system-prompt-file` when the
    resolved claude binary supports that flag; otherwise this falls back to
    inline, using `system_prompt` if given or else the file's own text.

    Returns either an early CliResult (the call was rejected before/without
    launching, or launching itself failed: env/timeout) or a
    (returncode, stdout, stderr) tuple for the caller to classify. Split out
    from `run_claude()` so `wfrun`'s `dispatch` wrapper (reliability-spec.md
    §5.1) can execute with `kill_tree=True` and persist the raw
    (returncode, stdout, stderr) to exit.json/result.json before handing
    the same three values to `classify_result()` itself.
    """
    claude_bin, via_shim = _resolve_claude_bin()
    if claude_bin is None:
        return CliResult(ok=False, exit_code=-1, error_class="env",
                         error="claude CLI not found on PATH")

    # Every check that can reject the call is done BEFORE cmd is built, so
    # no early return can launch a half-built argv (the checks are pure;
    # only the capability probe shells out, and it is cached).
    resolved_file = Path(system_prompt_file).resolve() if system_prompt_file else None
    has_content = bool(system_prompt) or (
        resolved_file is not None and resolved_file.stat().st_size > 0)
    use_prompt_file = (has_content and resolved_file is not None
                      and _supports_system_prompt_file(claude_bin))
    if schema and via_shim and _has_hostile_metachars(schema):
        # --json-schema has no file-based form (reliability-spec.md
        # §13.3.3): refuse rather than launch a corrupted schema.
        return CliResult(
            ok=False, exit_code=-1, error_class="env",
            error="claude resolved via the Windows npm .cmd/.bat shim; "
                  "--json-schema has no file-based form and this schema "
                  "contains shell metacharacters (& | % ^ < > or a "
                  "newline) that the shim's cmd.exe layer would "
                  "silently corrupt. Put a non-shim claude earlier on "
                  "PATH.")
    if use_prompt_file and via_shim and _has_hostile_metachars(str(resolved_file)):
        # The file itself is safe, but its PATH travels through the shim's
        # cmd.exe layer on argv same as any other flag value, and a run-dir
        # path (unlike the old %TEMP% mkstemp path) may contain & | % ^ < >
        # (reliability-spec.md §13.3.3 -- decided: loud failure).
        return CliResult(
            ok=False, exit_code=-1, error_class="env",
            error="claude resolved via the Windows npm .cmd/.bat shim; "
                  "the system prompt file path contains shell "
                  "metacharacters (& | % ^ < > or a newline) that the "
                  "shim's cmd.exe layer would silently corrupt. Put a "
                  "non-shim claude earlier on PATH.")
    inline_text = None
    if has_content and not use_prompt_file:
        inline_text = system_prompt if system_prompt else resolved_file.read_text(
            encoding="utf-8")
        if via_shim and _has_hostile_metachars(inline_text):
            # File-based flag unsupported and the only resolvable claude is
            # the argv-mangling shim: refuse rather than launch corrupted
            # (reliability-spec.md §13.3.3 -- decided: loud failure).
            return CliResult(
                ok=False, exit_code=-1, error_class="env",
                error="claude resolved via the Windows npm .cmd/.bat shim "
                      "and --append-system-prompt-file is unsupported; the "
                      "system prompt contains shell metacharacters "
                      "(& | % ^ < > or a newline) that the shim's cmd.exe "
                      "layer would silently corrupt. Put a non-shim claude "
                      "earlier on PATH.")

    cmd = [claude_bin, *PRINT_ARGS]

    if has_content:
        # Append (not replace): keeps the CLI's default tool-use scaffolding
        # while placing role/mode/rules in the high-authority channel.
        if use_prompt_file:
            cmd += ["--append-system-prompt-file", str(resolved_file)]
        else:
            cmd += ["--append-system-prompt", inline_text]
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
        if kill_tree:
            proc = _run_with_tree_kill(cmd, prompt, timeout, cwd)
        else:
            # UTF-8 both ways, never strict. `encoding=` governs stdin as
            # well as stdout/stderr, so this fixes two defects at once:
            #
            #  - decode: without it, text mode uses the platform default
            #    (cp932 on JA Windows) and ONE undecodable reply byte kills
            #    subprocess's reader thread. stdout comes back None and
            #    classify_result dies on json.loads(None) -- the whole run
            #    aborts unclassified, so no error_class and no retry/debug/
            #    decision path. Took down two live measurement runs.
            #  - encode: `input=prompt` was likewise cp932-encoded, which
            #    both raises on any character cp932 lacks and mismatches
            #    the child (node reads stdin as UTF-8).
            #
            # errors="replace" over strict: the payload is a JSON envelope
            # whose structure is pure ASCII, so a replaced byte costs one
            # U+FFFD in a message body while every field classification
            # reads survives. Strict would cost the entire run. The
            # substitution is not silent -- on the step and replan paths
            # the executor warns on U+FFFD (ask_llm and the capability
            # probe carry no such check, by design).
            # Same treatment at every site in this file and in pi_cli.py;
            # here it is pinned inside `_run_bound`.
            proc = _run_bound(cmd, input=prompt, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired as e:
        stderr = getattr(e, "stderr", None)
        # Same UTF-8-never-strict contract as the launch call above: a
        # bare .decode() is strict, so an undecodable byte would raise
        # from inside the timeout handler -- reinstating, on the error
        # path, the unclassified crash this whole fix removes.
        stderr = (stderr.decode("utf-8", "replace")
                  if isinstance(stderr, bytes) else (stderr or ""))
        return CliResult(ok=False, exit_code=-1, stderr=stderr,
                         error_class="timeout", error=f"timeout after {timeout}s")
    except FileNotFoundError:
        return CliResult(ok=False, exit_code=-1, error_class="env",
                         error="claude CLI not found on PATH")

    return proc.returncode, proc.stdout, proc.stderr


def run_claude(prompt: str, *, system_prompt: str | None = None,
               system_prompt_file: str | Path | None = None,
               model: str | None = None,
               effort: str | None = None, tools: str | None = None,
               schema: str | None = None, timeout: int = 600,
               cwd: str | None = None,
               permission_mode: str | None = None,
               kill_tree: bool = False) -> CliResult:
    result = _launch(prompt, system_prompt=system_prompt,
                     system_prompt_file=system_prompt_file, model=model,
                     effort=effort, tools=tools, schema=schema, timeout=timeout,
                     cwd=cwd, permission_mode=permission_mode, kill_tree=kill_tree)
    if isinstance(result, CliResult):
        return result
    returncode, stdout, stderr = result
    return classify_result(returncode, stdout, stderr, schema=schema)


# model= takes a post-resolution concrete CLI model name, never a tier.
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
