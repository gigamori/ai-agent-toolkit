#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
TESTS_DIR = REPO_ROOT / "plugins" / "taskflow" / "tests"
HOOKS_SRC = REPO_ROOT / "plugins" / "taskflow" / "hooks"
PROMPTS_SRC = REPO_ROOT / "plugins" / "taskflow" / "prompts"
REAL_STATE = REPO_ROOT / "_projects" / "_state"

EXCLUDED = {"capture_paths.sh"}

TRAP_RE = re.compile(r"^trap\s+(\w+)\s+EXIT", re.MULTILINE)
EXIT_1_RE = re.compile(r"\bexit\s+1\b")

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


def die(msg: str) -> None:
    print(f"  ABORT: {msg}")
    raise SystemExit(2)


def exit_on_fail_reachable(text: str) -> bool:
    m = TRAP_RE.search(text)
    if m:
        fname = m.group(1)
        fm = re.search(
            r"^" + re.escape(fname) + r"\(\)\s*\{(.*?)^\}",
            text, re.MULTILINE | re.DOTALL,
        )
        body = fm.group(1) if fm else ""
        return bool(EXIT_1_RE.search(body))
    return bool(EXIT_1_RE.search(text))


def class_lint() -> None:
    scripts = sorted(p for p in TESTS_DIR.glob("*.sh") if p.name not in EXCLUDED)
    print(f"scanned {len(scripts)} test suites under "
          f"{TESTS_DIR.relative_to(REPO_ROOT).as_posix()}/")
    check(len(scripts) > 0, "the class lint scanned a non-zero number of suites")
    for p in scripts:
        text = p.read_text(encoding="utf-8")
        check(exit_on_fail_reachable(text),
              f"{p.name}: an exit-on-FAIL path is reachable "
              f"(trap-function trailer or a tail exit-1 guard)")


def ancestors_with_state(start: Path) -> list[str]:
    hits: list[str] = []
    d = start.resolve()
    while True:
        if (d / "_projects" / "_state").is_dir():
            hits.append(str(d))
        if d.parent == d:
            return hits
        d = d.parent


def assert_isolated(root: Path) -> None:
    hits = ancestors_with_state(root)
    if hits:
        die(f"temp tree is not isolated: ancestor(s) hold _projects/_state: {hits}")
    ok(f"no ancestor of {root} holds _projects/_state")
    try:
        root.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        ok(f"temp tree is outside the repo ({REPO_ROOT})")
    else:
        die(f"temp tree is inside the repo ({REPO_ROOT})")


def real_state_snapshot() -> tuple[int, str]:
    names = sorted(os.listdir(REAL_STATE)) if REAL_STATE.is_dir() else []
    digest = hashlib.md5("\n".join(names).encode("utf-8")).hexdigest()
    return len(names), digest


def mirror_plugin(dest: Path) -> Path:
    tf = dest / "plugins" / "taskflow"
    shutil.copytree(HOOKS_SRC, tf / "hooks",
                     ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(PROMPTS_SRC, tf / "prompts")
    (tf / "tests").mkdir(parents=True)
    return tf


def run_suite(path: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        ["bash", str(path)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )


def control_arm() -> None:
    src = TESTS_DIR / "test_guidelines_reminder_mode.sh"
    text = src.read_text(encoding="utf-8")

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        assert_isolated(root)
        tf = mirror_plugin(root)
        suite_path = tf / "tests" / "test_guidelines_reminder_mode.sh"

        before_green = real_state_snapshot()
        suite_path.write_text(text, encoding="utf-8")
        r_green = run_suite(suite_path)
        after_green = real_state_snapshot()
        check(r_green.returncode == 0,
              f"GREEN arm: the unperturbed mirrored suite exits 0 "
              f"(got {r_green.returncode})")
        green_pass_lines = [ln for ln in r_green.stdout.splitlines()
                             if ln.strip().startswith("PASS:")]
        check(len(green_pass_lines) > 0,
              f"GREEN arm reports at least one PASS line "
              f"(positive marker, got {len(green_pass_lines)})")
        check(after_green == before_green,
              f"real _projects/_state/ is unchanged around the GREEN run "
              f"(before={before_green!r}, after={after_green!r})")

        red_text = text.replace("ROUTER:|RESPONSE", "ROUTEQ:|RESPONSE", 1)
        check(red_text != text,
              "RED arm: the one-character perturbation actually changed the "
              "mirrored source (the marker literal exists as expected)")
        before_red = real_state_snapshot()
        suite_path.write_text(red_text, encoding="utf-8")
        r_red = run_suite(suite_path)
        after_red = real_state_snapshot()
        check(r_red.returncode == 1,
              f"RED arm: the perturbed mirrored suite exits 1 "
              f"(got {r_red.returncode}) -- this is the observation T2's own "
              f"Next Steps require before the fix")
        check(after_red == before_red,
              f"real _projects/_state/ is unchanged around the RED run "
              f"(before={before_red!r}, after={after_red!r})")


def main() -> int:
    print("=== T2 FAIL-to-exit propagation: class lint + RED/GREEN control ===")
    class_lint()
    print()
    control_arm()

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
