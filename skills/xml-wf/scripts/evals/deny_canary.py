"""Refused-call detection canary (opt-in; calls the claude CLI and costs money).

wfrun learns of a refused tool call only from what Claude Code emits: the
`permission_denials` field of every stream-json result event (run-cc, and the
layer-A `wait` and the wrapper that `dispatch` launches,
`claude_cli.classify_result`) and the `toolDenialKind` field of transcript
lines (run-llm layer B, `ccdeny`). Either can move under a Claude Code upgrade
without any unit test noticing -- their fixtures are fixed. This harness
launches real `claude -p` children and checks that each measured shape still
classifies as it did (docs/dev/test-constraints.md, 2026-09-26).

Usage, after every Claude Code upgrade:
    uv run python evals/deny_canary.py [--model NAME]

One sample per arm, so a miss means re-measure, not a rate. Exit 0 when every
arm matched, 1 otherwise. The `transcript` arm persists one small session under
Claude Code's projects directory; the other arms persist nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun import ccdeny, claude_cli  # noqa: E402

# The measurement layer of references/build.md "Model selection".
DEFAULT_MODEL = "sonnet"
TIMEOUT_S = 300

DESCRIPTION = "deny canary"
REFUSED = ("Use the Bash tool to run exactly this command: "
           'node -e "process.exit(0)" . Then reply with its output, or the '
           "exact error text you received.")
CLEAN = ("Use the Bash tool to run exactly this command: echo canary . "
         "Then reply with its output.")
IN_AGENT = (f"Use the Agent tool with subagent_type general-purpose, description "
            f"'{DESCRIPTION}' and exactly this prompt: '{REFUSED}' Do not run any "
            "command yourself. Then reply with what the agent returned.")
HOOK_DENY = json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse", "permissionDecision": "deny",
    "permissionDecisionReason": "deny canary hook"}})


def _launch(argv: list[str], prompt: str, cwd: Path) -> claude_cli.CliResult:
    proc = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=TIMEOUT_S,
                          cwd=cwd)
    return claude_cli.classify_result(proc.returncode, proc.stdout, proc.stderr)


def _hook_settings(work: Path) -> Path:
    script = work / "hook-deny.sh"
    script.write_text(f"cat > /dev/null\nprintf '%s' '{HOOK_DENY}'\n",
                      encoding="utf-8", newline="\n")
    settings = work / "hook-settings.json"
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [{
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": f"bash {script.as_posix()}"}],
    }]}}), encoding="utf-8")
    return settings


def _transcript_verdict(res: claude_cli.CliResult) -> str:
    session = (res.raw or {}).get("session_id")
    if not session:
        return "no session_id on the result"
    previous = os.environ.get(ccdeny.SESSION_ENV)
    os.environ[ccdeny.SESSION_ENV] = session
    try:
        return ccdeny.check_delegation({"delegation": DESCRIPTION}).label
    finally:
        if previous is None:
            os.environ.pop(ccdeny.SESSION_ENV, None)
        else:
            os.environ[ccdeny.SESSION_ENV] = previous


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()

    claude_bin, _via_shim = claude_cli._resolve_claude_bin()
    if claude_bin is None:
        print("claude CLI not found on PATH", file=sys.stderr)
        return 1
    model = ["--model", args.model]
    persisting = [a for a in claude_cli.PRINT_ARGS if a != "--no-session-persistence"]
    misses = 0
    with tempfile.TemporaryDirectory(prefix="wfrun-deny-canary-") as tmp:
        work = Path(tmp)
        arms = [
            ("direct", "denied", lambda: claude_cli.run_claude(
                REFUSED, model=args.model, tools="Read", timeout=TIMEOUT_S,
                cwd=str(work))),
            ("hook", "denied", lambda: _launch(
                [claude_bin, *claude_cli.PRINT_ARGS, *model,
                 "--allowedTools", "Bash", "--permission-mode", "acceptEdits",
                 "--settings", str(_hook_settings(work))], CLEAN, work)),
            ("in-agent", "denied", lambda: claude_cli.run_claude(
                IN_AGENT, model=args.model, tools="Agent",
                permission_mode="acceptEdits", timeout=TIMEOUT_S, cwd=str(work))),
            ("clean", None, lambda: claude_cli.run_claude(
                CLEAN, model=args.model, tools="Bash",
                permission_mode="acceptEdits", timeout=TIMEOUT_S, cwd=str(work))),
        ]
        for name, expected, run in arms:
            res = run()
            matched = res.error_class == expected
            misses += not matched
            print(f"{name}: expected {expected or 'ok'}, got "
                  f"{res.error_class or 'ok'} -- {'ok' if matched else 'MISS'}"
                  + ("" if matched else f" ({res.error})"))

        res = _launch([claude_bin, *persisting, *model, "--allowedTools", "Agent",
                       "--permission-mode", "acceptEdits"], IN_AGENT, work)
        verdict = _transcript_verdict(res)
        matched = verdict.startswith(ccdeny.DENIED)
        misses += not matched
        print(f"transcript: expected {ccdeny.DENIED}, got {verdict} -- "
              f"{'ok' if matched else 'MISS'}")
    return 1 if misses else 0


if __name__ == "__main__":
    sys.exit(main())
