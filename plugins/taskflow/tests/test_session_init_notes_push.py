#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK = REPO_ROOT / "plugins" / "taskflow" / "hooks" / "session_init.py"
REAL_STATE = REPO_ROOT / "_projects" / "_state"
SID = "sinotes111-2222-3333-4444-555566667777"
PROJECT = "demo"
HEADER = "project-notes (index rows):"
NOTICE = "[project-notes] vector layer unavailable:"
ROW_N1 = "| specs/n1.md | anchored note | a | 2026-01-02 |"
ROW_N2 = "| specs/n2.md | kanban note | kanban | 2026-01-01 |"

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
    try:
        root.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        ok(f"temp tree {root} is outside the repo and no ancestor holds _projects/_state")
    else:
        die(f"temp tree is inside the repo ({REPO_ROOT})")


def real_state_snapshot() -> tuple[int, str]:
    names = sorted(os.listdir(REAL_STATE)) if REAL_STATE.is_dir() else []
    return len(names), hashlib.md5("\n".join(names).encode("utf-8")).hexdigest()


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def build_ws(root: Path) -> Path:
    ws = root / "ws"
    project = ws / "_projects" / PROJECT
    write(ws / "src" / "a.py", "x = 1\n")
    write(ws / "_projects" / "index.md", "| Project | Description | Target |\n|---|---|---|\n| demo | d | t |\n")
    write(project / "index.md", "# demo\n")
    write(project / "progress.md", "# Progress: demo\n")
    filler = "".join(f"| specs/f{i}.md | filler row {i} | f{i} | 2025-12-0{i} |\n" for i in range(1, 5))
    write(project / "project-notes" / "index.md",
          "| File | Description | Tags | Updated |\n|---|---|---|---|\n" + ROW_N1 + "\n" + ROW_N2 + "\n" + filler)
    write(project / "project-notes" / "specs" / "n1.md", "---\nanchors: [src/a.py]\n---\n\n# n1\n")
    write(project / "project-notes" / "specs" / "n2.md", "# n2\n")
    write(ws / "_projects" / "_state" / f"{SID}.json", json.dumps({
        "project": PROJECT, "rules_loaded": True, "indexed_project": PROJECT,
        "guidelines_loaded": True, "project_rules_indexed": PROJECT, "origin": "cc",
    }))
    write(ws / "_projects" / "_state" / f"{SID}.reads", "src/a.py\n")
    return ws


def run_hook(ws: Path, prompt: str, env_extra: dict | None = None) -> str:
    env = dict(os.environ)
    env.pop("TASKFLOW_NOTES_PUSH", None)
    env.pop("TASKFLOW_QMD_BIN", None)
    env.update(env_extra or {})
    payload = {"session_id": SID, "prompt": prompt, "transcript_path": ""}
    cp = subprocess.run(
        ["uv", "run", "--no-project", "python", str(HOOK)],
        cwd=str(ws), input=json.dumps(payload), capture_output=True,
        text=True, encoding="utf-8", errors="replace", env=env,
    )
    if cp.returncode != 0:
        die(f"session_init exited {cp.returncode}: {cp.stderr}")
    return json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"]


def state_of(ws: Path) -> dict:
    return json.loads((ws / "_projects" / "_state" / f"{SID}.json").read_text(encoding="utf-8"))


def test_push_block(root: Path) -> None:
    print("--- push block from the .reads ledger and the prompt ---")
    ws = build_ws(root)
    ctx = run_hook(ws, "続き")
    check(HEADER in ctx, "the push block header is injected")
    block = ctx[ctx.index(HEADER):]
    check(block.splitlines()[1] == ROW_N1, f"the note anchored on the read file is the first verbatim row (got {block.splitlines()[1]!r})")
    check("project-notes matched: 1 of 6 rows (1 shown, 1 strong)" in block, f"footer counts M of N, K shown and S strong (got {block.splitlines()[-1]!r})")
    check(NOTICE not in ctx, "no degrade notice when project-notes/.qmd/ does not exist")

    ctx2 = run_hook(ws, "kanban の続き")
    block2 = ctx2[ctx2.index(HEADER):]
    check(ROW_N2 in block2 and block2.splitlines()[1] == ROW_N1,
          "a lexical hit joins after the location row; location stays first")

    ctx3 = run_hook(ws, "kanban", {"TASKFLOW_NOTES_PUSH": "off"})
    check(ROW_N1 in ctx3 and ROW_N2 not in ctx3, "mode off keeps location rows only")


def test_notice_repeats(root: Path) -> None:
    print("--- degrade notice repeats while the degrade persists ---")
    ws = build_ws(root)
    (ws / "_projects" / PROJECT / "project-notes" / ".qmd").mkdir()
    env = {"TASKFLOW_QMD_BIN": str(ws / "no-such-qmd")}
    ctx = run_hook(ws, "kanban", env)
    check(NOTICE in ctx and "not found" in ctx, "an unresolvable qmd binary is announced")
    check(ROW_N2 in ctx, "the lexical layer still delivers rows on degrade")
    ctx2 = run_hook(ws, "kanban", env)
    check(NOTICE in ctx2 and HEADER in ctx2,
          "the notice repeats on a later turn; announcing once per session hid a four-day outage because the cause does not clear on its own")
    check("notes_push_notice" not in state_of(ws),
          "no once-per-session flag is persisted; a flag would re-introduce the suppression this check exists to prevent")
    ctx3 = run_hook(ws, "kanban", {})
    check(NOTICE not in ctx3 and HEADER in ctx3,
          "a turn that does not degrade carries no notice, so the repeat is driven by the degrade and not by the block")


def build_overflow_ws(root: Path, anchor: str) -> Path:
    ws = build_ws(root)
    project = ws / "_projects" / PROJECT
    write(ws / anchor, "x\n")
    rows = ["| File | Description | Tags | Updated |", "|---|---|---|---|"]
    for i in range(1, 13):
        rows.append(f"| specs/m{i}.md | overflow row {i} | m{i} | 2025-11-01 |")
        write(project / "project-notes" / "specs" / f"m{i}.md",
              f"---\nanchors: [{anchor}]\n---\n\n# m{i}\n")
    write(project / "project-notes" / "index.md", "\n".join(rows) + "\n")
    write(ws / "_projects" / "_state" / f"{SID}.reads", anchor + "\n")
    return ws


def test_symbol_route_probe(root: Path) -> None:
    print("--- symbol-route probe lands in the state file, never in the context ---")
    ws = build_overflow_ws(root, "src/b.py")
    ctx = run_hook(ws, "続き")
    probes = state_of(ws).get("symbol_route_probe")

    check(isinstance(probes, list) and len(probes) == 1,
          f"one turn appends one probe entry (got {probes!r})")
    entry = probes[0]
    check(entry["due"] is True and entry["m"] > entry["k"] and entry["code"] >= 1,
          f"a code turn whose block overflowed is recorded as due (got {entry!r})")
    check("symbol_route_probe" not in ctx,
          "the probe is never injected; a model that could read the flag would stop being what it measures")

    run_hook(ws, "続き")
    check(len(state_of(ws)["symbol_route_probe"]) == 2, "a second turn appends rather than replaces")


def test_symbol_route_probe_negative(root: Path) -> None:
    print("--- the same overflow on a prose turn is not due ---")
    ws = build_overflow_ws(root, "docs/b.md")
    run_hook(ws, "続き")
    entry = state_of(ws)["symbol_route_probe"][0]
    check(entry["m"] > entry["k"] and entry["code"] == 0 and entry["due"] is False,
          f"overflow alone does not make a turn due; the code clause is load-bearing (got {entry!r})")


def main() -> int:
    print("=== session_init.py project-notes push tests ===")
    before = real_state_snapshot()
    for t in (test_push_block, test_notice_repeats, test_symbol_route_probe,
              test_symbol_route_probe_negative):
        with tempfile.TemporaryDirectory() as d:
            assert_isolated(Path(d))
            t(Path(d))
    after = real_state_snapshot()
    check(before == after, f"the real _projects/_state listing is unchanged (before={before}, after={after})")
    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
