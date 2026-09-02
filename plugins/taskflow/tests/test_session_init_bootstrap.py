#!/usr/bin/env python3
from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOKS_SRC = REPO_ROOT / "plugins" / "taskflow" / "hooks"
PROMPTS_SRC = REPO_ROOT / "plugins" / "taskflow" / "prompts"
REAL_STATE = REPO_ROOT / "_projects" / "_state"

BOOTSTRAP_MARKER = "[taskflow bootstrap - read this block before anything else]"
PROJECT_INDEX_MARKER = "[Project Index:"
ACTION_REQUIRED_MARKER = "!!ACTION_REQUIRED"
GUIDELINES_REMINDER_MARKER = "[taskflow guidelines reminder]"

SID_LARGE = "b1c2d3e4-f5a6-4789-9abc-def012345601"
SID_SMALL = "c2d3e4f5-a6b7-4890-9abc-def012345602"
SID_EVERY_TURN = "d3e4f5a6-b7c8-4901-9abc-def012345603"
PROJECT = "e2e-bootstrap-fixture"

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


def mirror_hooks_and_prompts(dest: Path) -> Path:
    hooks = dest / "hooks"
    shutil.copytree(HOOKS_SRC, hooks, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(PROMPTS_SRC, dest / "prompts")
    return hooks


def run_session_init(hooks: Path, cwd: Path, payload: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        ["uv", "run", "--no-project", "python", str(hooks / "session_init.py")],
        cwd=str(cwd), input=json.dumps(payload), capture_output=True,
        text=True, encoding="utf-8", errors="replace", env=env)


def additional_context(stdout: str) -> str:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return ""
    return data.get("hookSpecificOutput", {}).get("additionalContext", "")


def large_index_fixture(root: Path) -> None:
    proj_dir = root / "_projects" / PROJECT
    proj_dir.mkdir(parents=True, exist_ok=True)
    header = "| Project | Description | Target |\n|---------|-------------|--------|\n"
    row = ("| pad-row | padding content so this fixture crosses the T1 delivery "
           "threshold | none |\n")
    body = header
    while len(body) < 12500:
        body += row
    with open(proj_dir / "index.md", "w", encoding="utf-8", newline="\n") as f:
        f.write(body)


def turn2_seeded_fixture(root: Path, session_id: str) -> None:
    proj_dir = root / "_projects" / PROJECT
    proj_dir.mkdir(parents=True, exist_ok=True)
    with open(proj_dir / "index.md", "w", encoding="utf-8", newline="\n") as f:
        f.write("| Project | Description | Target |\n|---------|-------------|--------|\n"
                 "| x | small | none |\n")
    state_dir = root / "_projects" / "_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "project": PROJECT,
        "rules_loaded": True,
        "indexed_project": PROJECT,
        "guidelines_loaded": True,
        "project_rules_indexed": "",
        "origin": "cc",
    }
    with open(state_dir / f"{session_id}.json", "w", encoding="utf-8") as f:
        json.dump(state, f)


def every_turn_rules_fixture(root: Path, session_id: str) -> None:
    proj_dir = root / "_projects" / PROJECT
    proj_dir.mkdir(parents=True, exist_ok=True)
    with open(proj_dir / "index.md", "w", encoding="utf-8", newline="\n") as f:
        f.write("| Project | Description | Target |\n|---------|-------------|--------|\n"
                 "| x | small | none |\n")
    with open(proj_dir / "rules.md", "w", encoding="utf-8", newline="\n") as f:
        f.write("---\ninject_every_turn: true\n---\n\n# per-turn rules\n\nbody.\n")
    state_dir = root / "_projects" / "_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "project": PROJECT,
        "rules_loaded": True,
        "indexed_project": PROJECT,
        "guidelines_loaded": True,
        "project_rules_indexed": PROJECT,
        "origin": "cc",
    }
    with open(state_dir / f"{session_id}.json", "w", encoding="utf-8") as f:
        json.dump(state, f)


def extract_bootstrap_block(hooks: Path) -> tuple[str, int]:
    src = (hooks / "session_init.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    block = None
    max_chars = None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "BOOTSTRAP_BLOCK" in names:
            block = ast.literal_eval(node.value)
        if "BOOTSTRAP_MAX_CHARS" in names:
            max_chars = ast.literal_eval(node.value)
    return block, max_chars


def main() -> int:
    print("=== session_init.py bootstrap block test ===")
    before = real_state_snapshot()

    with tempfile.TemporaryDirectory() as d:
        hooks = mirror_hooks_and_prompts(Path(d))
        block, max_chars = extract_bootstrap_block(hooks)
        check(block is not None, "BOOTSTRAP_BLOCK symbol found in source")
        check(max_chars is not None, "BOOTSTRAP_MAX_CHARS symbol found in source")
        if block is not None:
            check(len(block) <= (max_chars or 1200),
                  f"len(BOOTSTRAP_BLOCK)={len(block)} <= BOOTSTRAP_MAX_CHARS={max_chars}")
            check(block.isascii(), "BOOTSTRAP_BLOCK is ASCII-only")
            check(BOOTSTRAP_MARKER in block,
                  "BOOTSTRAP_BLOCK carries its own heading marker")

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        assert_isolated(root)
        hooks = mirror_hooks_and_prompts(root)
        large_index_fixture(root)

        r = run_session_init(hooks, root, {
            "session_id": SID_LARGE,
            "prompt": f"pj:{PROJECT} status please",
            "transcript_path": "",
        })
        ctx = additional_context(r.stdout)
        check(bool(ctx), f"turn 1 additionalContext is non-empty (rc={r.returncode})")

        idx = ctx.find(BOOTSTRAP_MARKER)
        check(idx != -1, "bootstrap marker present in turn-1 additionalContext")
        budget = max_chars or 1200
        check(idx != -1 and idx < budget,
              f"bootstrap marker offset {idx} is within the first {budget} characters")

        idx_project = ctx.find(PROJECT_INDEX_MARKER)
        idx_action = ctx.find(ACTION_REQUIRED_MARKER)
        check(idx != -1 and idx_project != -1 and idx < idx_project,
              f"bootstrap offset {idx} precedes project index offset {idx_project}")
        check(idx != -1 and idx_action != -1 and idx < idx_action,
              f"bootstrap offset {idx} precedes ACTION_REQUIRED offset {idx_action}")

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        assert_isolated(root)
        hooks = mirror_hooks_and_prompts(root)
        turn2_seeded_fixture(root, SID_SMALL)

        r = run_session_init(hooks, root, {
            "session_id": SID_SMALL,
            "prompt": "continue please, no project token here",
            "transcript_path": "",
        })
        ctx = additional_context(r.stdout)
        check(bool(ctx), f"turn 2 additionalContext is non-empty (rc={r.returncode})")
        check(BOOTSTRAP_MARKER not in ctx,
              "bootstrap marker absent on an unchanged turn 2")
        check(GUIDELINES_REMINDER_MARKER in ctx,
              "guidelines reminder (carrying its own ROUTER line) is present")

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        assert_isolated(root)
        hooks = mirror_hooks_and_prompts(root)
        every_turn_rules_fixture(root, SID_EVERY_TURN)

        r = run_session_init(hooks, root, {
            "session_id": SID_EVERY_TURN,
            "prompt": "continue please, no project token here",
            "transcript_path": "",
        })
        ctx = additional_context(r.stdout)
        check(bool(ctx), f"inject_rules_full-only turn additionalContext is non-empty "
              f"(rc={r.returncode})")
        check(BOOTSTRAP_MARKER in ctx,
              "bootstrap marker present when only inject_rules_full is true "
              "(inject_rules/inject_guidelines_full/inject_index all false)")

    after = real_state_snapshot()
    check(after == before,
          f"real _projects/_state/ is unchanged (before={before!r}, after={after!r})")
    check(not (REAL_STATE / f"{SID_LARGE}.json").exists(),
          "synthetic large-fixture session id absent from real _projects/_state/")
    check(not (REAL_STATE / f"{SID_SMALL}.json").exists(),
          "synthetic turn-2 session id absent from real _projects/_state/")
    check(not (REAL_STATE / f"{SID_EVERY_TURN}.json").exists(),
          "synthetic inject_rules_full session id absent from real _projects/_state/")

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
