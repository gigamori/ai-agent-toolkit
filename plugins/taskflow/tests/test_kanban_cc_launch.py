#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Unit tests for generate_kanban.py's serve-mode CC-launch gating.

Regression guard for
(_projects/harness-taskflow/tasks/1_in_progress/2026-07-24_kanban-serve-cc-link-ext-check.md):
the serve-mode ``/open`` handler must probe the VS Code / VSCodium CLI and the
``anthropic.claude-code`` extension ONCE per process, and — when the CLI is
missing or the extension is not installed — return an informative error page
(carrying the session UUID / prompt) instead of an uncaught FileNotFoundError
or a silent editor no-op.

stdlib only. No sockets are bound and no `_projects/_state/` is touched: the
handler is driven directly with fake IO and every subprocess/`shutil.which`
call is monkeypatched. Run with:
  uv run --script plugins/taskflow/tests/test_kanban_cc_launch.py
Exits 0 when all checks pass, 1 otherwise.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import types
from pathlib import Path

# Import the module under test from scripts/ (sibling of tests/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import generate_kanban as gk  # noqa: E402

REPO_STATE_DIR = Path(__file__).resolve().parents[3] / "_projects" / "_state"

PASS = 0
FAIL = 0


def ok(msg: str) -> None:
    global PASS
    PASS += 1
    print(f"  PASS: {msg}")


def bad(msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  FAIL: {msg}")


def check(cond: bool, msg: str) -> None:
    ok(msg) if cond else bad(msg)


# --- _resolve_cc_launcher ---------------------------------------------------

def _fake_which(mapping: dict[str, str | None]):
    return lambda name: mapping.get(name)


def _fake_run(stdout: str):
    def run(cmd, capture_output=False, text=False, timeout=None):
        return types.SimpleNamespace(stdout=stdout, stderr="", returncode=0)
    return run


def test_launcher_no_cli() -> None:
    gk._resolve_cc_launcher.cache_clear()
    orig_which = shutil.which
    shutil.which = _fake_which({"codium": None, "code": None})
    try:
        cmd, has_ext = gk._resolve_cc_launcher()
        check((cmd, has_ext) == (None, False),
              "_resolve_cc_launcher: neither code nor codium on PATH -> (None, False)")
    finally:
        shutil.which = orig_which
        gk._resolve_cc_launcher.cache_clear()


def test_launcher_ext_present() -> None:
    gk._resolve_cc_launcher.cache_clear()
    orig_which, orig_run = shutil.which, subprocess.run
    shutil.which = _fake_which({"codium": "/x/codium", "code": None})
    subprocess.run = _fake_run("gigamori.run-sql-grid\nanthropic.claude-code\n")
    try:
        cmd, has_ext = gk._resolve_cc_launcher()
        check((cmd, has_ext) == ("/x/codium", True),
              "_resolve_cc_launcher: extension present in --list-extensions -> (cmd, True)")
    finally:
        shutil.which, subprocess.run = orig_which, orig_run
        gk._resolve_cc_launcher.cache_clear()


def test_launcher_ext_absent() -> None:
    gk._resolve_cc_launcher.cache_clear()
    orig_which, orig_run = shutil.which, subprocess.run
    shutil.which = _fake_which({"codium": None, "code": "/x/code"})
    subprocess.run = _fake_run("ms-python.python\nesbenp.prettier-vscode\n")
    try:
        cmd, has_ext = gk._resolve_cc_launcher()
        check((cmd, has_ext) == ("/x/code", False),
              "_resolve_cc_launcher: CLI present but extension absent -> (cmd, False)")
    finally:
        shutil.which, subprocess.run = orig_which, orig_run
        gk._resolve_cc_launcher.cache_clear()


def test_launcher_case_insensitive() -> None:
    gk._resolve_cc_launcher.cache_clear()
    orig_which, orig_run = shutil.which, subprocess.run
    shutil.which = _fake_which({"codium": "/x/codium"})
    subprocess.run = _fake_run("  Anthropic.Claude-Code  \n")
    try:
        cmd, has_ext = gk._resolve_cc_launcher()
        check(has_ext is True,
              "_resolve_cc_launcher: extension id matched case-insensitively and trimmed")
    finally:
        shutil.which, subprocess.run = orig_which, orig_run
        gk._resolve_cc_launcher.cache_clear()


def test_launcher_subprocess_error() -> None:
    gk._resolve_cc_launcher.cache_clear()
    orig_which, orig_run = shutil.which, subprocess.run

    def boom(*a, **k):
        raise OSError("cannot exec")

    shutil.which = _fake_which({"codium": "/x/codium"})
    subprocess.run = boom
    try:
        cmd, has_ext = gk._resolve_cc_launcher()
        check((cmd, has_ext) == ("/x/codium", False),
              "_resolve_cc_launcher: --list-extensions raising -> (cmd, False), not a crash")
    finally:
        shutil.which, subprocess.run = orig_which, orig_run
        gk._resolve_cc_launcher.cache_clear()


def test_launcher_probes_once() -> None:
    gk._resolve_cc_launcher.cache_clear()
    orig_which, orig_run = shutil.which, subprocess.run
    calls = {"n": 0}

    def counting_run(cmd, capture_output=False, text=False, timeout=None):
        calls["n"] += 1
        return types.SimpleNamespace(stdout="anthropic.claude-code\n", stderr="", returncode=0)

    shutil.which = _fake_which({"codium": "/x/codium"})
    subprocess.run = counting_run
    try:
        gk._resolve_cc_launcher()
        gk._resolve_cc_launcher()
        gk._resolve_cc_launcher()
        check(calls["n"] == 1,
              f"_resolve_cc_launcher: lru_cache probes --list-extensions once, got {calls['n']}")
    finally:
        shutil.which, subprocess.run = orig_which, orig_run
        gk._resolve_cc_launcher.cache_clear()


# --- _launch_error_html -----------------------------------------------------

def test_error_html_carries_hint_and_escapes() -> None:
    html = gk._launch_error_html("boom <x>", "session abc-123 <y>").decode("utf-8")
    check("session abc-123" in html and "boom" in html,
          "_launch_error_html: includes the message and the manual hint")
    check("<x>" not in html and "<y>" not in html and "&lt;x&gt;" in html,
          "_launch_error_html: HTML-escapes both message and hint")


# --- /open handler branching -------------------------------------------------

SESSION = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"


def _make_open_handler(launcher, popen_recorder):
    """Build a KanbanHandler instance wired for a single /open GET, with the
    launcher probe and subprocess.Popen replaced. Returns (handler, captured)
    where captured collects (code, body, content_type) from _respond."""
    cls = gk.make_handler(lambda: b"", "vscode", [Path(".")],
                          open_token="TOK", key="k", port=12345)
    h = cls.__new__(cls)
    h.headers = {"Host": "localhost"}
    captured: list[tuple] = []
    h._respond = lambda code, body, content_type="text/plain": captured.append(
        (code, body, content_type))

    gk._resolve_cc_launcher = launcher  # replace name in module globals
    orig_sub = gk.subprocess
    gk.subprocess = types.SimpleNamespace(
        Popen=popen_recorder, DEVNULL=subprocess.DEVNULL,
        SubprocessError=subprocess.SubprocessError,
    )
    return h, captured, orig_sub


def test_open_no_cli_returns_error_page() -> None:
    orig_launcher = gk._resolve_cc_launcher
    popen_calls = []
    h, captured, orig_sub = _make_open_handler(
        lambda: (None, False), lambda *a, **k: popen_calls.append(a))
    try:
        h.path = f"/open?session={SESSION}&t=TOK"
        h.do_GET()
        code, body, ctype = captured[-1]
        text = body.decode("utf-8")
        check(code == 200 and "text/html" in ctype, "open/no-cli: responds 200 HTML")
        check(SESSION in text and "CLI" in text, "open/no-cli: error page names the session and the missing CLI")
        check(not popen_calls, "open/no-cli: does not attempt to launch an editor")
    finally:
        gk._resolve_cc_launcher = orig_launcher
        gk.subprocess = orig_sub


def test_open_ext_absent_returns_error_page() -> None:
    orig_launcher = gk._resolve_cc_launcher
    popen_calls = []
    h, captured, orig_sub = _make_open_handler(
        lambda: ("/x/codium", False), lambda *a, **k: popen_calls.append(a))
    try:
        h.path = f"/open?session={SESSION}&t=TOK"
        h.do_GET()
        code, body, ctype = captured[-1]
        text = body.decode("utf-8")
        check(code == 200 and gk.CLAUDE_CODE_EXT_ID in text,
              "open/ext-absent: error page names the missing extension")
        check(not popen_calls, "open/ext-absent: does not launch an editor")
    finally:
        gk._resolve_cc_launcher = orig_launcher
        gk.subprocess = orig_sub


def test_open_success_launches_editor() -> None:
    orig_launcher = gk._resolve_cc_launcher
    popen_calls = []
    h, captured, orig_sub = _make_open_handler(
        lambda: ("/x/codium", True), lambda *a, **k: popen_calls.append(a[0]))
    try:
        h.path = f"/open?session={SESSION}&t=TOK"
        h.do_GET()
        check(len(popen_calls) == 1, "open/success: launches the editor exactly once")
        argv = popen_calls[0]
        check(argv[:2] == ["/x/codium", "--open-url"]
              and argv[2] == f"vscode://anthropic.claude-code/open?session={SESSION}",
              f"open/success: invokes <cmd> --open-url <uri>, got {argv}")
        code, body, ctype = captured[-1]
        check(code == 200 and b"opening" in body, "open/success: responds with the opening page")
    finally:
        gk._resolve_cc_launcher = orig_launcher
        gk.subprocess = orig_sub


def test_open_bad_token_forbidden() -> None:
    orig_launcher = gk._resolve_cc_launcher
    popen_calls = []
    h, captured, orig_sub = _make_open_handler(
        lambda: ("/x/codium", True), lambda *a, **k: popen_calls.append(a))
    try:
        h.path = f"/open?session={SESSION}&t=WRONG"
        h.do_GET()
        code, body, ctype = captured[-1]
        check(code == 403, "open/bad-token: rejected with 403 before any probe/launch")
        check(not popen_calls, "open/bad-token: no editor launch")
    finally:
        gk._resolve_cc_launcher = orig_launcher
        gk.subprocess = orig_sub


def main() -> int:
    before = sorted(p.name for p in REPO_STATE_DIR.glob("*")) if REPO_STATE_DIR.is_dir() else []
    test_launcher_no_cli()
    test_launcher_ext_present()
    test_launcher_ext_absent()
    test_launcher_case_insensitive()
    test_launcher_subprocess_error()
    test_launcher_probes_once()
    test_error_html_carries_hint_and_escapes()
    test_open_no_cli_returns_error_page()
    test_open_ext_absent_returns_error_page()
    test_open_success_launches_editor()
    test_open_bad_token_forbidden()

    after = sorted(p.name for p in REPO_STATE_DIR.glob("*")) if REPO_STATE_DIR.is_dir() else []
    check(before == after,
          "AC-6: real _projects/_state/ is unchanged by this test run")

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} check(s) failed ({PASS} passed).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
