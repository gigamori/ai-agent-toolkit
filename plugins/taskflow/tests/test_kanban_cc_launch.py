#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import sys
import types
from pathlib import Path

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



def _fake_which(mapping: dict[str, str | None]):
    return lambda name: mapping.get(name)


def _fake_run(stdout: str):
    def run(cmd, capture_output=False, text=False, timeout=None, **kwargs):
        return types.SimpleNamespace(stdout=stdout, stderr="", returncode=0)
    return run


def test_launcher_no_cli() -> None:
    gk._reset_cc_launcher_cache()
    orig_which = shutil.which
    calls = {"n": 0}
    fake = _fake_which({"codium": None, "code": None})

    def counting_which(name):
        calls["n"] += 1
        return fake(name)

    shutil.which = counting_which
    try:
        cmd, status = gk._resolve_cc_launcher()
        check((cmd, status) == (None, gk.EXT_MISSING),
              "_resolve_cc_launcher: neither code nor codium on PATH -> (None, EXT_MISSING)")
        resolved_calls = calls["n"]
        second = gk._resolve_cc_launcher()
        check(second == (None, gk.EXT_MISSING) and calls["n"] == resolved_calls,
              "_resolve_cc_launcher: no-CLI is determinate -> cached, a second call "
              f"does not re-run shutil.which (got {calls['n'] - resolved_calls} extra call(s))")
    finally:
        shutil.which = orig_which
        gk._reset_cc_launcher_cache()


def test_launcher_ext_present() -> None:
    gk._reset_cc_launcher_cache()
    orig_which, orig_run = shutil.which, subprocess.run
    shutil.which = _fake_which({"codium": "/x/codium", "code": None})
    subprocess.run = _fake_run("gigamori.run-sql-grid\nanthropic.claude-code\n")
    try:
        cmd, status = gk._resolve_cc_launcher()
        check((cmd, status) == ("/x/codium", gk.EXT_OK),
              "_resolve_cc_launcher: extension present in --list-extensions -> (cmd, EXT_OK)")
    finally:
        shutil.which, subprocess.run = orig_which, orig_run
        gk._reset_cc_launcher_cache()


def test_launcher_ext_absent() -> None:
    gk._reset_cc_launcher_cache()
    orig_which, orig_run = shutil.which, subprocess.run
    shutil.which = _fake_which({"codium": None, "code": "/x/code"})
    subprocess.run = _fake_run("ms-python.python\nesbenp.prettier-vscode\n")
    try:
        cmd, status = gk._resolve_cc_launcher()
        check((cmd, status) == ("/x/code", gk.EXT_MISSING),
              "_resolve_cc_launcher: CLI present but extension absent -> (cmd, EXT_MISSING)")
    finally:
        shutil.which, subprocess.run = orig_which, orig_run
        gk._reset_cc_launcher_cache()


def test_launcher_case_insensitive() -> None:
    gk._reset_cc_launcher_cache()
    orig_which, orig_run = shutil.which, subprocess.run
    shutil.which = _fake_which({"codium": "/x/codium"})
    subprocess.run = _fake_run("  Anthropic.Claude-Code  \n")
    try:
        cmd, status = gk._resolve_cc_launcher()
        check(status == gk.EXT_OK,
              "_resolve_cc_launcher: extension id matched case-insensitively and trimmed")
    finally:
        shutil.which, subprocess.run = orig_which, orig_run
        gk._reset_cc_launcher_cache()


def test_launcher_subprocess_error() -> None:
    gk._reset_cc_launcher_cache()
    orig_which, orig_run = shutil.which, subprocess.run

    def boom(*a, **k):
        raise OSError("cannot exec")

    shutil.which = _fake_which({"codium": "/x/codium"})
    subprocess.run = boom
    try:
        cmd, status = gk._resolve_cc_launcher()
        check((cmd, status) == ("/x/codium", gk.EXT_UNKNOWN),
              "_resolve_cc_launcher: --list-extensions raising -> (cmd, EXT_UNKNOWN), not a crash")
    finally:
        shutil.which, subprocess.run = orig_which, orig_run
        gk._reset_cc_launcher_cache()


def test_launcher_probes_once() -> None:
    gk._reset_cc_launcher_cache()
    orig_which, orig_run = shutil.which, subprocess.run
    calls = {"n": 0}

    def counting_run(cmd, capture_output=False, text=False, timeout=None,
                     **kwargs):
        calls["n"] += 1
        return types.SimpleNamespace(stdout="anthropic.claude-code\n", stderr="", returncode=0)

    shutil.which = _fake_which({"codium": "/x/codium"})
    subprocess.run = counting_run
    try:
        gk._resolve_cc_launcher()
        gk._resolve_cc_launcher()
        gk._resolve_cc_launcher()
        check(calls["n"] == 1,
              "_resolve_cc_launcher: determinate result probes --list-extensions once, "
              f"got {calls['n']}")
    finally:
        shutil.which, subprocess.run = orig_which, orig_run
        gk._reset_cc_launcher_cache()


def test_launcher_unknown_not_cached() -> None:
    """A raising probe must be re-run on the next call, and must say so once."""
    gk._reset_cc_launcher_cache()
    orig_which, orig_run = shutil.which, subprocess.run
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise OSError("cannot exec")

    shutil.which = _fake_which({"codium": "/x/codium"})
    subprocess.run = boom
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            first = gk._resolve_cc_launcher()
            second = gk._resolve_cc_launcher()
        check(calls["n"] == 2 and first == second == ("/x/codium", gk.EXT_UNKNOWN),
              "_resolve_cc_launcher: EXT_UNKNOWN is not cached, a second call re-probes, "
              f"got {calls['n']} probe(s)")
        lines = [ln for ln in err.getvalue().splitlines() if ln.strip()]
        check(len(lines) == 2
              and all("/x/codium" in ln and "OSError" in ln for ln in lines),
              "_resolve_cc_launcher: every EXT_UNKNOWN logs one stderr line naming cmd and cause")
    finally:
        shutil.which, subprocess.run = orig_which, orig_run
        gk._reset_cc_launcher_cache()


def test_launcher_nonzero_returncode() -> None:
    """A non-zero exit is EXT_UNKNOWN, never EXT_MISSING, even with empty stdout."""
    gk._reset_cc_launcher_cache()
    orig_which, orig_run = shutil.which, subprocess.run
    calls = {"n": 0}

    def failing_run(cmd, capture_output=False, text=False, timeout=None,
                    **kwargs):
        calls["n"] += 1
        return types.SimpleNamespace(stdout="", stderr="boom", returncode=3)

    shutil.which = _fake_which({"codium": "/x/codium"})
    subprocess.run = failing_run
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            cmd, status = gk._resolve_cc_launcher()
            gk._resolve_cc_launcher()
        check((cmd, status) == ("/x/codium", gk.EXT_UNKNOWN),
              "_resolve_cc_launcher: returncode != 0 with empty stdout -> EXT_UNKNOWN, "
              "not EXT_MISSING")
        check(calls["n"] == 2,
              "_resolve_cc_launcher: a non-zero exit is not cached either, "
              f"got {calls['n']} probe(s)")
        check("/x/codium" in err.getvalue() and "3" in err.getvalue(),
              "_resolve_cc_launcher: non-zero exit logged to stderr with cmd and exit code")
    finally:
        shutil.which, subprocess.run = orig_which, orig_run
        gk._reset_cc_launcher_cache()


class _StrictAsciiStderr(io.StringIO):
    """Stand-in for a console whose codec cannot encode the message: it raises at the
    write, so an unsanitized log line fails here instead of passing silently."""

    encoding = "ascii"

    def write(self, s):  # type: ignore[override]
        s.encode(self.encoding)
        return super().write(s)


def test_probe_failure_log_is_sanitized() -> None:
    """`str(OSError)` carries an OS-localized strerror and `cmd` is a filesystem path, so
    printing them raw would raise inside the probe's except block and escape into /open."""
    gk._reset_cc_launcher_cache()
    orig_which, orig_run = shutil.which, subprocess.run

    def boom(*a, **k):
        raise OSError("caf\u00e9 could not exec\nsecond line of strerror")

    shutil.which = _fake_which({"codium": "/x/codium"})
    subprocess.run = boom
    err = _StrictAsciiStderr()
    cmd = status = None
    try:
        with contextlib.redirect_stderr(err):
            cmd, status = gk._resolve_cc_launcher()
        raised = None
    except UnicodeEncodeError as e:
        raised = e
    finally:
        shutil.which, subprocess.run = orig_which, orig_run
        gk._reset_cc_launcher_cache()
    check(raised is None,
          f"_resolve_cc_launcher: an unencodable strerror does not escape the probe, got {raised!r}")
    lines = [ln for ln in err.getvalue().splitlines() if ln.strip()]
    check(raised is None and (cmd, status) == ("/x/codium", gk.EXT_UNKNOWN) and len(lines) == 1,
          f"_resolve_cc_launcher: a multi-line strerror still logs exactly one line, got {len(lines)}")
    check(bool(lines) and "/x/codium" in lines[0] and "OSError" in lines[0]
          and "second line of strerror" in lines[0],
          "_resolve_cc_launcher: the line is sanitized, not dropped -- cmd, cause and detail survive")



def test_error_html_carries_hint_and_escapes() -> None:
    hint = gk.ManualHint("Resume it <l>:", "claude --resume abc-123 <y>")
    html = gk._launch_error_html("boom <x>", hint).decode("utf-8")
    check("boom" in html and "Resume it" in html
          and "<pre>claude --resume abc-123 &lt;y&gt;</pre>" in html,
          "_launch_error_html: message, hint lead, and payload in a standalone <pre>")
    check("<x>" not in html and "<y>" not in html and "<l>" not in html
          and "&lt;x&gt;" in html,
          "_launch_error_html: HTML-escapes the message, the hint lead and the payload")



def test_session_hint_with_claude_cli() -> None:
    orig_which = shutil.which
    shutil.which = _fake_which({"claude": "/x/claude"})
    try:
        hint = gk._session_manual_hint(SESSION)
        check(hint.payload == f"claude --resume {SESSION}" and hint.lead.strip() != "",
              "_session_manual_hint: claude on PATH -> payload is `claude --resume <uuid>`")
    finally:
        shutil.which = orig_which


def test_session_hint_without_claude_cli() -> None:
    orig_which = shutil.which
    shutil.which = _fake_which({"claude": None})
    try:
        hint = gk._session_manual_hint(SESSION)
        check(hint.payload == SESSION and "--resume" not in hint.payload
              and SESSION in hint.payload,
              "_session_manual_hint: claude absent -> payload is the bare UUID, no command")
    finally:
        shutil.which = orig_which


def test_prompt_hint_carries_prompt_body() -> None:
    prompt = "pj:harness-taskflow @project-notes/specs/x.md <b>"
    hint = gk._prompt_manual_hint(prompt)
    check(hint.payload == prompt and hint.lead.strip() != "",
          "_prompt_manual_hint: payload is the prompt body itself")
    html = gk._launch_unverified_html(hint).decode("utf-8")
    check("<pre>pj:harness-taskflow @project-notes/specs/x.md &lt;b&gt;</pre>" in html
          and "<b>" not in html,
          "_launch_unverified_html: prompt payload escaped in a standalone <pre>")



SESSION = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"


def _make_open_handler(launcher, popen_recorder):
    cls = gk.make_handler(lambda: b"", "vscode", [Path(".")],
                          open_token="TOK", key="k", port=12345)
    h = cls.__new__(cls)
    h.headers = {"Host": "localhost"}
    captured: list[tuple] = []
    h._respond = lambda code, body, content_type="text/plain": captured.append(
        (code, body, content_type))

    gk._resolve_cc_launcher = launcher
    orig_sub = gk.subprocess
    gk.subprocess = types.SimpleNamespace(
        Popen=popen_recorder, DEVNULL=subprocess.DEVNULL,
        SubprocessError=subprocess.SubprocessError,
    )
    return h, captured, orig_sub


def test_open_no_cli_returns_error_page() -> None:
    orig_launcher = gk._resolve_cc_launcher
    orig_which = shutil.which
    popen_calls = []
    h, captured, orig_sub = _make_open_handler(
        lambda: (None, gk.EXT_MISSING), lambda *a, **k: popen_calls.append(a))
    shutil.which = _fake_which({"claude": "/x/claude"})
    try:
        h.path = f"/open?session={SESSION}&t=TOK"
        h.do_GET()
        code, body, ctype = captured[-1]
        text = body.decode("utf-8")
        check(code == 200 and "text/html" in ctype, "open/no-cli: responds 200 HTML")
        check(SESSION in text and "CLI" in text, "open/no-cli: error page names the session and the missing CLI")
        check(not popen_calls, "open/no-cli: does not attempt to launch an editor")
    finally:
        shutil.which = orig_which
        gk._resolve_cc_launcher = orig_launcher
        gk.subprocess = orig_sub


def test_open_ext_absent_returns_error_page() -> None:
    orig_launcher = gk._resolve_cc_launcher
    orig_which = shutil.which
    popen_calls = []
    h, captured, orig_sub = _make_open_handler(
        lambda: ("/x/codium", gk.EXT_MISSING), lambda *a, **k: popen_calls.append(a))
    shutil.which = _fake_which({"claude": "/x/claude"})
    try:
        h.path = f"/open?session={SESSION}&t=TOK"
        h.do_GET()
        code, body, ctype = captured[-1]
        text = body.decode("utf-8")
        check(code == 200 and gk.CLAUDE_CODE_EXT_ID in text,
              "open/ext-absent: error page names the missing extension")
        check(not popen_calls, "open/ext-absent: does not launch an editor")
    finally:
        shutil.which = orig_which
        gk._resolve_cc_launcher = orig_launcher
        gk.subprocess = orig_sub


def test_open_success_launches_editor() -> None:
    orig_launcher = gk._resolve_cc_launcher
    orig_which = shutil.which
    popen_calls = []
    h, captured, orig_sub = _make_open_handler(
        lambda: ("/x/codium", gk.EXT_OK), lambda *a, **k: popen_calls.append(a[0]))
    shutil.which = _fake_which({"claude": "/x/claude"})
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
        shutil.which = orig_which
        gk._resolve_cc_launcher = orig_launcher
        gk.subprocess = orig_sub


def test_open_bad_token_forbidden() -> None:
    orig_launcher = gk._resolve_cc_launcher
    popen_calls = []
    h, captured, orig_sub = _make_open_handler(
        lambda: ("/x/codium", gk.EXT_OK), lambda *a, **k: popen_calls.append(a))
    try:
        h.path = f"/open?session={SESSION}&t=WRONG"
        h.do_GET()
        code, body, ctype = captured[-1]
        check(code == 403, "open/bad-token: rejected with 403 before any probe/launch")
        check(not popen_calls, "open/bad-token: no editor launch")
    finally:
        gk._resolve_cc_launcher = orig_launcher
        gk.subprocess = orig_sub


def test_open_ext_unknown_launches_optimistically() -> None:
    orig_launcher = gk._resolve_cc_launcher
    orig_which = shutil.which
    popen_calls = []
    h, captured, orig_sub = _make_open_handler(
        lambda: ("/x/codium", gk.EXT_UNKNOWN), lambda *a, **k: popen_calls.append(a[0]))
    shutil.which = _fake_which({"claude": "/x/claude"})
    try:
        h.path = f"/open?session={SESSION}&t=TOK"
        h.do_GET()
        check(len(popen_calls) == 1
              and popen_calls[0] == ["/x/codium", "--open-url",
                                     f"vscode://anthropic.claude-code/open?session={SESSION}"],
              f"open/ext-unknown: launches the editor optimistically, got {popen_calls}")
        code, body, ctype = captured[-1]
        text = body.decode("utf-8")
        check(code == 200 and "window.close" not in text,
              "open/ext-unknown: 200 page WITHOUT window.close() so the hint stays on screen")
        check("not installed" not in text
              and f"<pre>claude --resume {SESSION}</pre>" in text,
              "open/ext-unknown: never claims the extension is uninstalled, shows the resume command")
    finally:
        shutil.which = orig_which
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
    test_launcher_unknown_not_cached()
    test_launcher_nonzero_returncode()
    test_probe_failure_log_is_sanitized()
    test_error_html_carries_hint_and_escapes()
    test_session_hint_with_claude_cli()
    test_session_hint_without_claude_cli()
    test_prompt_hint_carries_prompt_body()
    test_open_no_cli_returns_error_page()
    test_open_ext_absent_returns_error_page()
    test_open_success_launches_editor()
    test_open_bad_token_forbidden()
    test_open_ext_unknown_launches_optimistically()

    after = sorted(p.name for p in REPO_STATE_DIR.glob("*")) if REPO_STATE_DIR.is_dir() else []
    check(before == after,
          "real _projects/_state/ is unchanged by this test run")

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} check(s) failed ({PASS} passed).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
