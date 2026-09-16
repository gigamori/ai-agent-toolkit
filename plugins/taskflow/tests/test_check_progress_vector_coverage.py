#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_progress as cp  # noqa: E402

PASS = 0
FAIL = 0

STATUS_PENDING = (
    "QMD Status\n"
    "\n"
    "Index: C:\\ws\\_projects\\proj\\project-notes\\.qmd\\index.sqlite\n"
    "Size:  4.1 MB\n"
    "\n"
    "Documents\n"
    "  Total:    82 files indexed\n"
    "  Vectors:  789 embedded\n"
    "  Pending:  7 need embedding (run 'qmd embed')\n"
    "  Updated:  2 hours ago\n"
    "\n"
    "AST Chunking\n"
    "  Status:   active\n"
)

STATUS_COMPLETE = (
    "QMD Status\n"
    "\n"
    "Index: C:\\ws\\_projects\\proj\\project-notes\\.qmd\\index.sqlite\n"
    "Size:  4.1 MB\n"
    "\n"
    "Documents\n"
    "  Total:    82 files indexed\n"
    "  Vectors:  789 embedded\n"
    "  Updated:  2 hours ago\n"
)

STATUS_INVERTED = (
    "Documents\n"
    "  Total:    3 files indexed\n"
    "  Pending:  9 need embedding (run 'qmd embed')\n"
)

STATUS_GARBAGE = "bash: qmd: command not found\n"

NOTE_CATEGORIES = ("specs", "investigations", "checks", "procedures", "backlog", "_archive")


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


class FakeCompleted:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def runner_returning(stdout: str, returncode: int = 0, calls: list | None = None):
    def run(cmd, **kwargs):
        if calls is not None:
            calls.append((list(cmd), kwargs))
        return FakeCompleted(returncode, stdout)
    return run


def runner_raising(exc: BaseException):
    def run(cmd, **kwargs):
        raise exc
    return run


def runner_forbidden(calls: list):
    def run(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        raise AssertionError("qmd was spawned")
    return run


def make_project(root: Path, qmd_dir: bool = True, qmd_config: str | None = "index.yaml") -> Path:
    project_dir = root / "_projects" / "proj"
    notes = project_dir / "project-notes"
    for cat in NOTE_CATEGORIES:
        (notes / cat).mkdir(parents=True)
    (notes / "index.md").write_text(
        "| File | Description | Tags | Updated |\n|---|---|---|---|\n", encoding="utf-8"
    )
    (project_dir / "progress.md").write_text("# P\n", encoding="utf-8")
    if qmd_dir:
        (notes / cp.QMD_DIR).mkdir()
        if qmd_config:
            (notes / cp.QMD_DIR / qmd_config).write_text("collections: []\n", encoding="utf-8")
    return project_dir


def fake_bin(root: Path) -> Path:
    p = root / "node_modules" / "@tobilu" / "qmd" / "bin" / "qmd"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    return p


def env_with(bin_path: Path | str) -> dict:
    return {cp.QMD_BIN_ENV: str(bin_path), "PATH": ""}


def run_main(project_dir: Path, coverage_env: dict | None = None, runner=None) -> tuple[int, str]:
    real = cp.qmd_vector_coverage

    def patched(pd, env=None, runner_=None):
        if runner is None:
            return real(pd, env=coverage_env)
        return real(pd, env=coverage_env, runner=runner)

    if runner is not None or coverage_env is not None:
        cp.qmd_vector_coverage = patched
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = cp.main([str(project_dir)])
    finally:
        cp.qmd_vector_coverage = real
    return rc, buf.getvalue()


def test_no_local_index(root: Path) -> None:
    print("--- no local qmd index: no footer line, no spawn ---")
    calls: list = []
    bin_path = fake_bin(root)

    no_dir = make_project(root / "a", qmd_dir=False)
    got = cp.qmd_vector_coverage(no_dir, env=env_with(bin_path), runner=runner_forbidden(calls))
    check(got is None, f"a project without project-notes/.qmd/ yields no value (got {got!r})")

    bare_dir = make_project(root / "b", qmd_dir=True, qmd_config=None)
    got = cp.qmd_vector_coverage(bare_dir, env=env_with(bin_path), runner=runner_forbidden(calls))
    check(got is None,
          f"a .qmd/ directory holding no index.yaml yields no value, because qmd would walk up "
          f"and report another index (got {got!r})")

    yml_dir = make_project(root / "c", qmd_dir=True, qmd_config="index.yml")
    got = cp.qmd_vector_coverage(yml_dir, env=env_with(bin_path),
                                 runner=runner_returning(STATUS_COMPLETE))
    check(got == (82, 82), f"the .yml spelling of the local config is accepted (got {got!r})")

    check(calls == [], f"qmd is never spawned when no local index is present (got {calls})")

    rc, out = run_main(no_dir)
    check("vector coverage" not in out, f"main() prints no coverage line for that project (got {out!r})")
    check(rc == 0, f"the check still passes (got {rc})")


def test_unresolvable_binary(root: Path) -> None:
    print("--- qmd binary unresolvable: no footer line ---")
    project_dir = make_project(root / "a")
    calls: list = []

    got = cp.qmd_vector_coverage(project_dir, env={"PATH": ""}, runner=runner_forbidden(calls))
    check(got is None, f"no override and nothing on PATH yields no value (got {got!r})")

    missing = root / "nowhere" / "qmd"
    got = cp.qmd_vector_coverage(project_dir, env=env_with(missing), runner=runner_forbidden(calls))
    check(got is None, f"an override naming a nonexistent file yields no value (got {got!r})")

    check(calls == [], f"qmd is never spawned when the binary does not resolve (got {calls})")

    rc, out = run_main(project_dir, coverage_env={"PATH": ""}, runner=runner_forbidden(calls))
    check("vector coverage" not in out, f"main() prints no coverage line (got {out!r})")
    check(rc == 0, f"the check still passes (got {rc})")


def test_value_and_format(root: Path) -> None:
    print("--- qmd answers: vector coverage: n of N docs ---")
    project_dir = make_project(root / "a")
    bin_path = fake_bin(root)
    calls: list = []

    got = cp.qmd_vector_coverage(project_dir, env=env_with(bin_path),
                                 runner=runner_returning(STATUS_PENDING, calls=calls))
    check(got == (75, 82),
          f"n is Total minus Pending, never the Vectors line, which counts chunks (got {got!r})")
    check(len(calls) == 1, f"exactly one spawn (got {len(calls)})")
    if calls:
        cmd, kwargs = calls[0]
        check(cmd == ["node", str(bin_path), "status"],
              f"qmd is spawned as node <bin/qmd> status, never through a shim (got {cmd})")
        check(Path(kwargs.get("cwd", "")) == project_dir / "project-notes",
              f"cwd is the notes directory, so qmd finds the local index (got {kwargs.get('cwd')})")
        check(kwargs.get("timeout") == cp.QMD_STATUS_TIMEOUT_S,
              f"the spawn carries a timeout (got {kwargs.get('timeout')!r})")
        check(kwargs.get("encoding") == "utf-8" and kwargs.get("errors") == "replace",
              f"child text is decoded as utf-8 with replacement (got {kwargs.get('encoding')!r}, "
              f"{kwargs.get('errors')!r})")

    got = cp.qmd_vector_coverage(project_dir, env=env_with(bin_path),
                                 runner=runner_returning(STATUS_COMPLETE))
    check(got == (82, 82),
          f"qmd omits the Pending line at zero, which is full coverage (got {got!r})")

    rc, out = run_main(project_dir, coverage_env=env_with(bin_path),
                       runner=runner_returning(STATUS_PENDING))
    check("vector coverage: 75 of 82 docs" in out, f"main() prints the footer line (got {out!r})")
    check(rc == 0, f"the footer never drives a non-zero exit (got {rc})")

    lines = [l for l in out.splitlines() if l.strip()]
    check(lines and lines[-1] == "vector coverage: 75 of 82 docs",
          f"the line prints as a footer, after the other footers (got {lines})")


def test_degrade_arms(root: Path) -> None:
    print("--- non-zero exit, timeout, unparsable output: no line, check survives ---")
    project_dir = make_project(root / "a")
    bin_path = fake_bin(root)
    env = env_with(bin_path)

    arms = {
        "non-zero exit": runner_returning(STATUS_PENDING, returncode=1),
        "timeout": runner_raising(subprocess.TimeoutExpired(cmd="qmd", timeout=10.0)),
        "spawn failure": runner_raising(OSError("node not found")),
        "output with no Documents block": runner_returning(STATUS_GARBAGE),
        "empty output": runner_returning(""),
        "Pending above Total": runner_returning(STATUS_INVERTED),
    }
    for name, runner in arms.items():
        got = cp.qmd_vector_coverage(project_dir, env=env, runner=runner)
        check(got is None, f"{name} yields no value (got {got!r})")

    baseline_rc, baseline_out = run_main(project_dir, coverage_env={"PATH": ""},
                                         runner=runner_forbidden([]))
    for name, runner in arms.items():
        rc, out = run_main(project_dir, coverage_env=env, runner=runner)
        check("vector coverage" not in out, f"{name} prints no coverage line")
        check(rc == baseline_rc, f"{name} leaves the exit code alone (got {rc} vs {baseline_rc})")
        check(out == baseline_out, f"{name} leaves the rest of the report untouched")


def test_parse_status(root: Path) -> None:
    print("--- parse_qmd_status ---")
    check(cp.parse_qmd_status(STATUS_PENDING) == (75, 82),
          "Total and Pending are read from the Documents block")
    check(cp.parse_qmd_status(STATUS_COMPLETE) == (82, 82), "an absent Pending line reads as zero")
    check(cp.parse_qmd_status("  Pending:  7 need embedding\n") is None,
          "Pending without Total is not enough")
    check(cp.parse_qmd_status(
        "Documents\n  Total:    5 files indexed\n  \x1b[33mPending:  2 need embedding\x1b[0m\n"
    ) == (3, 5), "a colored Pending line still parses")


def main() -> int:
    print("=== check_progress.py vector coverage footer ===")
    with tempfile.TemporaryDirectory() as tmp:
        for t in (test_no_local_index, test_unresolvable_binary, test_value_and_format,
                  test_degrade_arms, test_parse_status):
            root = Path(tmp) / t.__name__
            root.mkdir(parents=True)
            t(root)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
