"""`pi -p` subprocess invocation: `wfrun ask --backend pi` (`ask_llm_pi`) and
`wfrun run --backend pi` (`run_pi`, the step-execution counterpart to
claude_cli.run_claude -- mode-orchestrator-runs/phase6-run-pi-design.md).

Sibling of `claude_cli.py`. `ask_llm_pi` predates `run_pi`: see
`mode-orchestrator-runs/phase5-item1-cc-inventory-design.md` for the
original CC<->Pi feature-parity survey that scoped this module to condition
judgment only ("not a general pi -p wrapper"); phase6-run-pi-design.md §4
lifted that scope to full step execution. Structured-output forcing
(schema=) is still refused outright -- pi has no equivalent -- but general
delegation and (for natively-priced providers) real cost reporting are
covered now via run_pi.

Differences from claude_cli.py's approach, and why (all measured 2026-07-29
against pi v0.80.6 / win32):

- No `--json-schema` on Pi (`pi --help` has no equivalent) -- forced instead
  by appending an instruction to the prompt, then parsed with a two-pass
  fallback (full-body parse, then brace-extraction). The fallback is load
  bearing, not defensive: the measured replies wrap the object in a
  ```json fence despite the instruction.
- The prompt is passed as a **positional message argument**, NOT via pi's
  `@<file>` include syntax. `@file` attaches the file as *content to reason
  about*, not as the turn's instruction: a probe whose file said "reply with
  exactly this JSON" got back a refusal that named it an embedded-instruction
  (prompt-injection) attempt and asked what the user actually wanted. The
  same text as a positional argument was obeyed.
- stdin is closed (DEVNULL). A `pi -p` call left with stdin open as a pipe
  blocks forever before dispatch (`_projects/pi-extensions-dev/rules.md`,
  "Known pitfalls").
- `cost_usd` is always 0.0 because `--mode text` prints no cost figure. Pi
  *can* report cost, but only in `--mode json`, whose `message_end` /
  `turn_end` lines carry `.message.usage.cost.total`. Switching this module
  to that mode would recover the figure for natively-priced providers
  (measured: `google/gemini-3.1-flash-lite` reported 0.00123875 for a
  one-word turn) but not for the models the canonical names resolve to:
  `pi-claude-agent-sdk/*` is a bridge to the claude binary and pi has no
  price table for it, so it reports a real token count with `cost.total` 0.
  Since ask defaults to those canonical names, text mode loses nothing today
  -- but callers must still not read 0.0 as a measured zero.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

from . import claude_cli, model, stepio
from . import decision as decision_mod
from .guardrails import ASK_PROMPT
from .modes import blocked_line, strip_mode_line

# Appended to ASK_PROMPT since pi has no --json-schema equivalent to force
# structured output the way claude_cli.py's run_claude() does.
_JSON_INSTRUCTION = """

Respond with a single JSON object and nothing else -- no explanation, no \
code fence, just: {"answer": true or false, "reason": "..."}"""

_BRACE_RE = re.compile(r"\{.*\}", re.DOTALL)

# Where npm's Windows shim keeps the actual CLI entry, relative to the shim.
_NPM_ENTRY = ("node_modules", "@earendil-works", "pi-coding-agent", "dist",
              "cli.js")

_resolution_cache: dict[str, object] = {}


def resolve_pi_launcher() -> list[str] | None:
    """Resolve the argv prefix that launches pi, once per process.

    Returns the prefix (e.g. `["/usr/local/bin/pi"]`, or
    `["node", ".../dist/cli.js"]` on Windows), or None when pi cannot be
    launched safely.

    Windows: npm installs `pi` as a `.CMD` shim. Launching that shim with a
    multi-line prompt on argv **silently truncates the prompt at the first
    newline** -- measured: a two-line probe reached the model as line one
    only, exit code 0, nothing on stderr. This is the same cmd.exe argv
    corruption reliability-spec.md §13.2 measured for the claude shim, and
    the ask prompt is always multi-line, so every call would be affected.
    The shim itself just runs `node <entry> %*`, so we resolve that entry and
    launch it through node directly, bypassing cmd.exe.

    Fail-closed: if only the shim is reachable (no entry file, or no node on
    PATH), return None rather than launch a call whose prompt would arrive
    truncated -- the same loud-failure choice as §13.3.3.
    """
    if "launcher" in _resolution_cache:
        return _resolution_cache["launcher"]

    which_path = shutil.which("pi")
    if which_path is None:
        launcher = None
    elif os.path.splitext(which_path)[1].lower() not in (".cmd", ".bat"):
        launcher = [which_path]
    else:
        entry = os.path.join(os.path.dirname(which_path), *_NPM_ENTRY)
        node = shutil.which("node")
        launcher = [node, entry] if (node and os.path.isfile(entry)) else None

    _resolution_cache["launcher"] = launcher
    return launcher


def _extract_json(text: str) -> dict | None:
    """Two-pass parse: try the whole (stripped) body first, then fall back
    to the first-brace-to-last-brace span. The fallback is what handles the
    ```json fence the model emits in practice."""
    stripped = text.strip()
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    m = _BRACE_RE.search(stripped)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    return None


def ask_llm_pi(question: str, *, model: str = "haiku", cwd: str | None = None,
              timeout: int = 300) -> tuple[bool | None, str, float]:
    """Pi-backed counterpart to claude_cli.ask_llm(). Same contract: returns
    (answer, reason, cost) with answer=None on failure after one retry.

    cost is always 0.0 (see module docstring) -- kept in the return shape
    for drop-in parity with the cc backend, not because it is measured.
    """
    launcher = resolve_pi_launcher()
    if launcher is None:
        return None, ("pi CLI not launchable: not on PATH, or only the "
                      "Windows npm .CMD shim is reachable (it truncates a "
                      "multi-line prompt at the first newline) and no "
                      "node + dist/cli.js could be resolved to bypass it"), 0.0

    prompt = ASK_PROMPT.format(question=question) + _JSON_INSTRUCTION
    cmd = [*launcher, "-p", "--mode", "text", "--tools", "read",
           "--no-session", "--model", model, prompt]

    reason = "no structured answer"
    for _ in range(2):
        try:
            # encoding/errors: see the comment at _launch's subprocess.run in
            # claude_cli.py. This carries an LLM answer, so it is one of the
            # sites where an undecodable byte was never hypothetical.
            proc = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=timeout, cwd=cwd)
        except subprocess.TimeoutExpired:
            reason = f"timeout after {timeout}s"
            continue
        except FileNotFoundError:
            return None, "pi CLI not found on PATH", 0.0
        if proc.returncode != 0:
            reason = (proc.stderr or "").strip()[:500] or f"pi exited {proc.returncode}"
            continue
        obj = _extract_json(proc.stdout)
        if obj is not None and isinstance(obj.get("answer"), bool):
            return obj["answer"], str(obj.get("reason", "")), 0.0
        reason = "no structured answer"
    return None, reason, 0.0


# pi's built-in tool set is fixed by type (dist/core/tools/index.d.ts
# ToolName, design phase6-run-pi-design.md §4.2, checked 2026-07-29):
# read | bash | edit | write | grep | find | ls. --tools has no alias
# normalization and silently grants zero tools on an unrecognized name
# (measured) -- names outside this table, or a converted set that comes out
# empty, must be refused by _convert_tools() rather than forwarded.
TOOL_NAME_MAP = {
    "Read": "read",
    "Write": "write",
    "Edit": "edit",
    "Grep": "grep",
    "Bash": "bash",
    "Glob": "find",
}


def _convert_tools(tools: str) -> tuple[str | None, str | None, list[str]]:
    """CC tool names -> pi tool names (TOOL_NAME_MAP, design §4.2).

    Returns (converted, error, warnings). `error` is set (converted is None)
    when any entry's leading name is outside TOOL_NAME_MAP --
    MultiEdit/NotebookEdit/Task/Agent and any unknown name are rejected, not
    silently dropped (design §4.2) -- or the converted set would be empty.

    An entry may carry a CC-style argument specifier ("Bash(git:*)"; model.py's
    tools_can_write() already treats the leading name as authoritative for the
    same reason). pi's --tools has no per-command matching at all, so the
    specifier cannot be honored -- but refusing the whole entry would be
    worse: it was decided (design phase6 review point 3, 2026-07-30) that
    losing access to a tool the workflow genuinely needs (e.g. git, when only
    `Bash(git:*)` was granted) is a bigger problem than the alternative of
    widening to the bare tool with no argument restriction, given pi's other
    layers (expect-file, the ERROR:/[BLOCKED: guardrails, wf.max) do not
    depend on argument-level tool scoping to begin with. Each such entry
    produces a `warnings` entry so the widening is never silent; the caller
    (run_pi discards them per-call since pi_tool_widening_notes() below
    already surfaces them once at run start) is expected to make them
    observable.
    """
    names = [t.strip() for t in tools.split(",") if t.strip()]
    leading = [t.split("(", 1)[0] for t in names]
    unknown = sorted({lead for entry, lead in zip(names, leading)
                      if lead not in TOOL_NAME_MAP})
    if unknown:
        return None, (
            "tools= names not supported by the pi backend: "
            + ", ".join(unknown)
            + " (pi's --tools has no alias normalization and silently "
              "grants zero tools on an unrecognized name rather than "
              "erroring, so an unconvertible name is refused here instead)"
        ), []
    warnings = [
        f"'{entry}' carries an argument specifier pi cannot enforce (no "
        f"per-command tool matching); widening to the whole "
        f"'{TOOL_NAME_MAP[lead]}' tool"
        for entry, lead in zip(names, leading) if "(" in entry
    ]
    converted = []
    for lead in leading:
        mapped = TOOL_NAME_MAP[lead]
        if mapped not in converted:  # a specifier and its bare form (or two
            converted.append(mapped)  # different specifiers) must not repeat
    if not converted:
        return None, "tools= resolved to an empty tool set", []
    return ",".join(converted), None, warnings


def pi_tool_widening_notes(wf: model.Workflow, agents_cache) -> list[str]:
    """Non-fatal advisory for `wfrun run --backend pi` (design phase6 review
    point 3, 2026-07-30): surfaces _convert_tools()'s specifier-widening
    warnings once per step at run start, rather than leaving them
    discoverable only from a step's own result.json after the run. Called
    by cmd_run (pi backend only) right after Executor construction, reusing
    its agents_cache so role-frontmatter tools= are resolved the same way
    dispatch does.
    """
    notes = []
    for node in wf.iter_steps():
        if not isinstance(node, model.Step):
            continue
        _, dispatch_tools = stepio.dispatch_for(node, agents_cache)
        if not dispatch_tools:
            continue
        _, _, warnings = _convert_tools(dispatch_tools)
        for w in warnings:
            notes.append(f"step '{node.id}': {w}")
    return notes


_USAGE_FIELDS = ("input", "output", "cacheRead", "cacheWrite", "totalTokens")
_USAGE_COST_FIELDS = ("input", "output", "cacheRead", "cacheWrite", "total")


def _sum_usage(turn_ends: list[dict]) -> dict:
    """Sum a `usage` dict's numeric fields (incl. the nested `cost` dict)
    across every turn_end (design phase6 review point 5, 2026-07-30).

    `turn_end.message.usage` is the INCREMENTAL usage for that one
    agent-loop iteration, not a running total -- measured against a real
    tool-using step (tooluse.jsonl): iteration 1 had totalTokens=2436,
    iteration 2 had totalTokens=2598 with input=2438 (~= iteration 1's
    total), which is growing context being resent, not totals accumulating.
    A tool-using step has one turn_end per tool round trip plus a final one;
    reading only the terminal turn_end (as this module did before this fix)
    silently dropped every round trip's cost/tokens but the last one's.
    """
    total = {f: 0 for f in _USAGE_FIELDS}
    total["cost"] = {f: 0 for f in _USAGE_COST_FIELDS}
    for te in turn_ends:
        usage = (te.get("message") or {}).get("usage") or {}
        for f in _USAGE_FIELDS:
            total[f] += usage.get(f) or 0
        cost = usage.get("cost") or {}
        for f in _USAGE_COST_FIELDS:
            total["cost"][f] += cost.get(f) or 0
    return total


def classify_result_pi(returncode: int, stdout: str, stderr: str) -> claude_cli.CliResult:
    """Turn a completed `pi -p --mode json` invocation's (returncode,
    stdout, stderr) into a CliResult (design phase6-run-pi-design.md §4.1).

    stdout is a JSONL event stream -- one JSON object per line
    (mode-orchestrator's harness-pi.md, measured: session, agent_start,
    turn_start, then message_start/message_update*/message_end per message,
    then turn_end, agent_end, agent_settled) -- NOT a single result object
    like `claude -p --output-format json`. Errors never surface on exit code
    or stderr (measured: an invalid API key still exits 0 with empty
    stderr); they appear only inside the JSONL, as a turn_end whose
    message.stopReason is "error".

    **There is one turn_end per agent loop iteration, not one per run.** A
    step that calls tools emits an intermediate turn_end with
    `stopReason: "toolUse"` and an empty content list for every tool round
    trip, then a final one carrying the reply. Measured 2026-07-30: a
    write-then-report prompt produced two turn_end events, the first
    `toolUse`/empty and the second `stop` with the path. The design's §4.1
    table was written from a tool-free probe and said "the turn_end line" as
    if unique; taking the first one classified every tool-using step as
    `empty result`. The terminal turn_end is therefore selected by taking
    the LAST one -- the stream ends with it -- and a stream whose last
    turn_end is still `toolUse` means the loop never finished.

    There is no `transient` class here (design §4.1): errorMessage is a
    provider-shaped raw string with no field equivalent to claude's
    api_error_status, so a retryable upstream hiccup cannot be told apart
    from anything else -- everything that is not guardrail/refusal/empty
    falls to `behavioral` (design §4.1.1 accepts the resulting double-retry
    against pi's own internal retry).
    """
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a stray non-JSON line is not fatal; turn_end absence is

    turn_ends = [e for e in events
                 if isinstance(e, dict) and e.get("type") == "turn_end"]
    turn_end = turn_ends[-1] if turn_ends else None
    if turn_end is None:
        # Interrupted mid-stream (external kill, or the process died before
        # finishing) -- measured (§9.4.1): turn_end/agent_end/agent_settled
        # never appear when the process is killed externally. A timeout is
        # caught earlier, at the launch layer (run_pi), so this branch means
        # the process ended on its own without ever reaching turn_end.
        last_type = (events[-1].get("type")
                    if events and isinstance(events[-1], dict) else None)
        return claude_cli.CliResult(
            ok=False, exit_code=returncode, stderr=stderr,
            error_class="behavioral",
            error=("pi JSONL stream ended without a turn_end event "
                  f"(last observed event: {last_type!r})"),
            raw={"last_event_type": last_type})

    message = turn_end.get("message") or {}
    content = message.get("content") or []
    text_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
    text = "".join(b.get("text", "") for b in text_blocks)
    stop_reason = message.get("stopReason")
    # Summed across every turn_end, not read from this (terminal) one alone
    # -- see _sum_usage's docstring. The reply itself (text/content/
    # stop_reason/errorMessage) still comes from the terminal turn_end only.
    usage = _sum_usage(turn_ends)
    cost_usd = float(usage["cost"]["total"])

    # CliResult.raw reduction rule (design §4.1, review point 3): a
    # sub-dict pulled from turn_end.message, NOT the full JSONL -- thinking
    # blocks and the other event types would bloat the result.json audit
    # trail executor.py writes per attempt.
    raw = {
        "content": text_blocks,
        "stopReason": stop_reason,
        "errorMessage": message.get("errorMessage"),
        "usage": usage,
        "model": message.get("model"),
        "provider": message.get("provider"),
    }

    result = claude_cli.CliResult(ok=True, text=text, cost_usd=cost_usd,
                                  exit_code=returncode, stderr=stderr, raw=raw)

    if stop_reason in ("error", "aborted"):
        result.ok = False
        result.error_class = "behavioral"
        error_message = message.get("errorMessage")
        result.error = (f"pi reported stopReason={stop_reason}"
                        + (f": {error_message}" if error_message else ""))
        return result

    if stop_reason == "toolUse":
        # The LAST turn_end is still a tool round trip: the agent loop was cut
        # off between issuing a tool call and reporting on it. Distinguished
        # from "empty result" on purpose -- the body is empty either way, but
        # the causes differ (a model that answered with nothing vs. a run that
        # never got to answer) and reading the second as the first sends a
        # misleading failure to the recovery loop.
        result.ok = False
        result.error_class = "behavioral"
        result.error = ("pi stream ended mid-tool-use (last turn_end has "
                        "stopReason=toolUse); the agent loop did not finish")
        return result

    # _common.md (mode injection) mandates a leading [Mode: x] line; shared
    # with claude_cli.classify_result so the ERROR:/[BLOCKED: protocols are
    # detected identically regardless of backend.
    body = strip_mode_line(text)
    if body.lstrip().startswith("ERROR:"):
        result.ok = False
        result.error_class = "guardrail"
        result.error = body.strip()
        return result

    blocked = blocked_line(text)
    if blocked is not None:
        result.ok = False
        result.error_class = "refusal"
        result.error = blocked[:500]
        return result

    claimed, _preamble = decision_mod.claim_decision_body(body)
    if claimed is not None:
        # Third prefix, classified exactly as on the cc path (this module
        # keeps its classification aligned with claude_cli by design; see the
        # module docstring), including the D9 preamble claim. Reachable under
        # the pi backend because only decider="llm" is refused there, not the
        # channel itself (xml-wf-decision-request.md §3, §4).
        result.ok = False
        result.error_class = "decision"
        result.error = claimed.strip()
        return result

    if body.strip() == "":
        result.ok = False
        result.error_class = "behavioral"
        result.error = "empty result"
        return result

    return result


def run_pi(prompt: str, *, system_prompt: str | None = None,
          model: str | None = None, effort: str | None = None,
          tools: str | None = None, schema: str | None = None,
          timeout: int = 600, cwd: str | None = None,
          permission_mode: str | None = None,
          kill_tree: bool = False) -> claude_cli.CliResult:
    """`pi -p --mode json` counterpart to claude_cli.run_claude() -- same
    signature (design phase6-run-pi-design.md §4), so Executor's
    run_claude= injection point (executor.py) can take either
    interchangeably.

    Differences from run_claude(), all decided/measured in the design doc:
    - schema is rejected outright (error_class="env"): pi has no
      --json-schema equivalent. This is the second line of defense; the
      first is the startup fail-fast in __main__.py (pi_compat_errors
      below) that refuses a schema= workflow before any process launches
      (design §2.2, "案S1").
    - permission_mode is accepted for signature parity but silently
      dropped: pi has no permission-mode concept (design §4 point 2).
    - effort is forwarded verbatim to --thinking, unconverted: claude's
      five values (low/medium/high/xhigh/max) are all accepted by
      --thinking without warning (measured, design §6).
    - tools is translated from CC names to pi names (TOOL_NAME_MAP, design
      §4.2) and rejected (error_class="env") if any name is outside the
      table or the converted set would be empty.
    - --no-session and --no-skills are always passed (design §4 point 5):
      one run launches "steps x attempts" child pi processes, and wfrun
      already persists prompt/result under steps/<id>_NN/ itself, so
      per-child transcripts and skill discovery are both unneeded overhead
      (and skills reopen a recursive-invocation surface). --no-extensions
      is deliberately NOT passed: canonical model names resolve through the
      pi-claude-agent-sdk extension (design §9.3).
    - the prompt travels as a positional argv argument, not stdin (as
      ask_llm_pi already does) -- pi's `@<file>` include syntax attaches a
      file as content to reason about, not as the turn's instruction.

    `kill_tree` is accepted for signature parity with run_claude but is not
    honored as an off switch here: tree-kill on timeout is unconditional.
    For claude, tree-kill is defense in depth -- reliability-spec.md §5.3
    measured ZERO surviving descendants from a plain timeout-kill of a
    Bash-tool-spawned `sleep 120`, so run_claude's own kill_tree=False
    default is already safe. Measured 2026-07-30 (review point 1, this
    design), pi does NOT share that property: the same real E2E (a step
    with `tools="Bash"`, `model="haiku"` to force the pi-claude-agent-sdk
    bridge, task = `sleep 120`, timeout=8) left a live `sleep.exe` behind
    after a plain `subprocess.run(timeout=)` had already reaped node.exe
    (and any bridged claude.exe) -- node.exe/claude.exe apparently clean up
    after themselves, but the bash tool's own child does not ride along.
    Re-running the identical case routed through `claude_cli._run_with_tree_kill`
    (`taskkill /T /F` on Windows) confirmed no survivor. So here, unlike
    run_claude, there is no working "off" state to preserve, and the
    parameter is kept only so callers written against run_claude's
    signature do not need a special case for this backend.
    """
    if schema is not None:
        return claude_cli.CliResult(
            ok=False, exit_code=-1, error_class="env",
            error="schema= is not supported by the pi backend (no "
                  "forced-structured-output equivalent exists); this call "
                  "should have been rejected before launch by the startup "
                  "fail-fast check (pi_compat_errors)")

    launcher = resolve_pi_launcher()
    if launcher is None:
        return claude_cli.CliResult(
            ok=False, exit_code=-1, error_class="env",
            error="pi CLI not launchable: not on PATH, or only the Windows "
                  "npm .CMD shim is reachable (it truncates a multi-line "
                  "prompt at the first newline) and no node + dist/cli.js "
                  "could be resolved to bypass it")

    mapped_tools = None
    if tools:
        mapped_tools, tools_error, _tool_warnings = _convert_tools(tools)
        if tools_error:
            return claude_cli.CliResult(ok=False, exit_code=-1,
                                        error_class="env", error=tools_error)

    cmd = [*launcher, "-p", "--mode", "json", "--no-session", "--no-skills"]

    sys_prompt_file: str | None = None
    try:
        if system_prompt:
            fd, sys_prompt_file = tempfile.mkstemp(
                prefix="wfrun-pi-sysprompt-", suffix=".txt")
            # UTF-8 explicitly: role/mode/rules bodies routinely contain
            # non-ASCII text (claude_cli._launch does the same, for the
            # same reason).
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(system_prompt)
            cmd += ["--append-system-prompt", sys_prompt_file]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--thinking", effort]
        if mapped_tools:
            cmd += ["--tools", mapped_tools]
        cmd += [prompt]  # positional message argument -- never @<file>

        try:
            # Unconditional, regardless of kill_tree (see the docstring):
            # pi's own children do not self-clean the way claude's do. The
            # real prompt already rides on argv (above); pi does not read it
            # from stdin the way claude does, so the "prompt" this shares
            # with claude_cli's stdin-writing helper is "".
            proc = claude_cli._run_with_tree_kill(cmd, "", timeout, cwd)
        except subprocess.TimeoutExpired:
            return claude_cli.CliResult(ok=False, exit_code=-1,
                                        error_class="timeout",
                                        error=f"timeout after {timeout}s")
        except FileNotFoundError:
            return claude_cli.CliResult(ok=False, exit_code=-1,
                                        error_class="env",
                                        error="pi CLI not found on PATH")
    finally:
        if sys_prompt_file:
            try:
                os.remove(sys_prompt_file)
            except OSError:
                pass

    return classify_result_pi(proc.returncode, proc.stdout, proc.stderr)


def diagnose_stub_pi(step, prompt, failure, *, cwd=None):
    """Injected as Executor's diagnose= under the pi backend (design §1,
    §2.3) -- the second line of defense behind the startup fail-fast
    (pi_compat_errors below), which rejects any on-error="debug" workflow
    before a run starts. adp.diagnose hardcodes run_claude + schema=
    DEBUG_SCHEMA, neither of which the pi backend can satisfy, so
    on-error="debug" is a dead feature here; this stub exists so that if
    Executor._diagnose is ever reached anyway (a bug, or a future change
    bypassing the fail-fast), it fails loudly instead of limping into a
    broken diagnosis.
    """
    from .executor import WorkflowFailure  # deferred: avoids a module-level
                                           # pi_cli <-> executor coupling
    raise WorkflowFailure(
        "diagnose was invoked under the pi backend, which has no debug "
        "implementation (mode-orchestrator-runs/phase6-run-pi-design.md "
        "§2.3); this should be unreachable, since on-error=\"debug\" "
        "workflows are rejected at startup -- if you are seeing this, that "
        "fail-fast check (pi_cli.pi_compat_errors) was bypassed")


# Startup fail-fast rejection messages (design §2.2, §2.3) -- verbatim per
# the design doc's own fenced text, `{id}` filled in with the offending
# step's id. Reproduced exactly (including the 7/9-space continuation
# indentation) since these are meant to be read literally by whoever hits
# them, and the design doc gives them as fixed text, not a paraphrase.
_SCHEMA_FAIL_FAST = '''\
error: step '{id}' declares schema=, which the pi backend cannot enforce
       (no forced-structured-output equivalent exists).
       Rebuild this workflow as pi-compatible: run the skill in build mode
       on this XML and ask for a pi-compatible version. The conversion
       rules are in references/run-pi.md, "Replacing schema=".'''

_ON_ERROR_DEBUG_FAIL_FAST = '''\
error: step '{id}' uses on-error="debug", which the pi backend does not
       support (debug diagnosis has no pi implementation).
       Rebuild this workflow as pi-compatible. Replacement hints:
       - on-error="fail" (default) — stop the run and let resume handle it
       - retry=N — for steps that fail transiently, a plain retry often
         covers what a debug-retry cycle did
       - on-error="ignore" + a follow-up verification step — when the run
         should continue and the failure needs recording instead of fixing
       See references/run-pi.md, "Replacing on-error=debug".'''

MODEL_CATALOG_TIMEOUT = 60


def _parse_model_table(stdout: str) -> list[tuple[str, str]]:
    """The `(provider, id)` pairs in pi's `--list-models` table.

    Layout is `provider  model  context  max-out  thinking  images`, header
    first. Neither a provider nor a model id contains whitespace, so splitting
    on it is enough; the header is recognised by its first column.
    """
    rows = []
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] == "provider":
            continue
        rows.append((parts[0], parts[1]))
    return rows


def list_available_models() -> list[tuple[str, str]] | None:
    """`(provider, id)` for every model pi currently reports as available.

    **None means the catalog could not be read** -- pi not launchable, non-zero
    exit, timeout. Callers must treat None as "unverifiable" and never as an
    empty catalog: an empty list would make every model name look invalid, so a
    missing pi install would turn into a wall of false errors.

    Cached per process, like the launcher: lint asks once per model name.
    """
    if "models" in _resolution_cache:
        return _resolution_cache["models"]
    launcher = resolve_pi_launcher()
    rows: list[tuple[str, str]] | None = None
    if launcher is not None:
        try:
            proc = subprocess.run(
                launcher + ["--list-models"], capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=MODEL_CATALOG_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            proc = None
        if proc is not None and proc.returncode == 0:
            rows = _parse_model_table(proc.stdout)
    _resolution_cache["models"] = rows
    return rows


def model_is_resolvable(name: str, catalog: list[tuple[str, str]]) -> bool:
    """Whether pi could resolve `name` against `catalog`.

    Mirrors `tryMatchModel` (pi's `src/core/model-resolver.ts`): an exact hit on
    `id` or `provider/id` wins (case-insensitive); failing that, ANY model whose
    id contains the pattern is a match. pi never fails on ambiguity -- it takes
    the alias, else the newest dated version -- so the single unresolvable case
    is **zero candidates**, and that is the only thing this reports False for.
    A `:thinking` suffix is tried both whole and split on the last colon,
    because pi tries the full string before splitting.

    Two narrowings this cannot see, both recorded in build.md § Model selection:
    pi also matches a model's display `name`, which `--list-models` does not
    print, and it applies an authenticated-only filter the catalog does not
    reflect. Both make this check *narrower* than pi, i.e. it can only miss a
    resolvable name whose sole match is by display name.
    """
    candidates = {name.strip().lower()}
    if ":" in name:
        candidates.add(name.rsplit(":", 1)[0].strip().lower())
    candidates.discard("")
    for pattern in candidates:
        for provider, model_id in catalog:
            if (model_id.lower() == pattern
                    or f"{provider}/{model_id}".lower() == pattern
                    or pattern in model_id.lower()):
                return True
    return False


def pi_compat_errors(wf: model.Workflow) -> list[str]:
    """Startup fail-fast for `wfrun run --backend pi` (design §2.2, §2.3):
    reject, before any pi process launches, a workflow whose steps declare
    schema= (no forced-structured-output equivalent on pi) or
    on-error="debug" (adp.diagnose hardcodes run_claude + schema=
    DEBUG_SCHEMA, neither of which pi can satisfy).

    Only <step> is checked: <replan> cannot carry schema= at all
    (parser.py's _REPLAN_ATTRS excludes it -- a parse-time error already),
    and its on-error="debug" is already inert under every backend today
    (Executor._exec_replan never calls self._diagnose), so it is not a
    pi-specific gap this check needs to close.

    `decider="llm"` is NOT rejected here. It was, between P4 and P6, on the
    reasoning that adjudication is a claude call with a forced schema whichever
    backend runs the steps -- true of `adjudicate` but not a property of the
    feature. `adjudicate_pi` settles the fork on pi itself, with the ruling
    written as §13.3 text instead of a schema object, so nothing claude-shaped
    is started mid-run and there is nothing to refuse (xml-wf-decision-request
    .md §17, D7 = option 3).

    Returns one fully-formatted rejection message per violation, in step
    order (empty list when the workflow is pi-compatible).
    """
    errors = []
    for node in wf.iter_steps():
        if not isinstance(node, model.Step):
            continue
        if node.schema:
            errors.append(_SCHEMA_FAIL_FAST.format(id=node.id))
        if node.on_error == "debug":
            errors.append(_ON_ERROR_DEBUG_FAIL_FAST.format(id=node.id))
    return errors
