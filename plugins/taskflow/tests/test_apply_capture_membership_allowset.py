#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
HOOK = REPO_ROOT / "plugins" / "taskflow" / "hooks" / "session_progress_capture.py"
REAL_STATE_DIR = REPO_ROOT / "_projects" / "_state"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
import session_progress_capture as spc  # noqa: E402
import note_links as nl  # noqa: E402

PASS = 0
FAIL = 0

PROJECT = "harness-taskflow"
SID8 = "abcd1234"
TS = "2026-08-19T12:00:00+09:00"


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


TASK_TEMPLATE = """\
---
priority: MID
---

# Test task

## Next Steps

<!-- @log:begin -->
- 2026-08-01T00:00:00 [s:zzzzzzzz]: created
<!-- @log:end -->

<!-- @notes:begin -->
<!-- auto-managed by taskflow note-link; do not hand-edit -->
<!-- @notes:end -->
"""


def make_task(root: Path, name: str) -> Path:
    task_dir = root / "tasks" / "1_in_progress"
    task_dir.mkdir(parents=True, exist_ok=True)
    task_path = task_dir / name
    task_path.write_text(TASK_TEMPLATE, encoding="utf-8")
    return task_path


def apply_capture(root: Path, index: dict, sidecar: dict, items):
    return spc._apply_capture(
        sidecar, index, PROJECT, {PROJECT: str(root)}, SID8, TS, items=items)


def confirmed_sidecar(ref: str, summary: str) -> dict:
    return {"confirmed": [{"task": ref, "summary": summary}],
            "note_links": [], "proposals": []}


def sid_lines(path: Path) -> int:
    return path.read_text(encoding="utf-8").count(f"[s:{SID8}]")


def test_allowset_admits_selflogged_task(root: Path) -> None:
    print("--- a task in allow_tasks but NOT in items['tasks'] applies ---")
    task_path = make_task(root, "ac1.md")
    key = f"{PROJECT}/ac1.md"
    items = {"tasks": [], "notes": ["project-notes/specs/demo-note.md"],
             "allow_tasks": [key]}
    summaries, links, proposals, link_skipped, membership_skipped = apply_capture(
        root, {key: str(task_path)}, confirmed_sidecar("ac1.md", "did the thing"), items)
    check(summaries == [key], f"summary applied for the self-logged task (got {summaries})")
    check(membership_skipped == [], f"nothing membership-skipped (got {membership_skipped})")
    check("did the thing" in task_path.read_text(encoding="utf-8"),
          "the summary text reached the task's @log block")


def test_union_does_not_narrow(root: Path) -> None:
    print("--- union — a task in items['tasks'] but not in allow_tasks still applies ---")
    task_path = make_task(root, "ac1b.md")
    key = f"{PROJECT}/ac1b.md"
    items = {"tasks": [key], "notes": [], "allow_tasks": []}
    summaries, links, proposals, link_skipped, membership_skipped = apply_capture(
        root, {key: str(task_path)}, confirmed_sidecar("ac1b.md", "still applies"), items)
    check(summaries == [key], f"summary applied from items['tasks'] alone (got {summaries})")
    check(membership_skipped == [], f"nothing membership-skipped (got {membership_skipped})")


def test_task_in_neither_set_is_skipped(root: Path) -> None:
    print("--- a task in NEITHER set is still membership-skipped ---")
    in_set = make_task(root, "ac3-in.md")
    out_set = make_task(root, "ac3-out.md")
    in_key = f"{PROJECT}/ac3-in.md"
    out_key = f"{PROJECT}/ac3-out.md"
    index = {in_key: str(in_set), out_key: str(out_set)}
    items = {"tasks": [], "notes": [], "allow_tasks": [in_key]}
    summaries, links, proposals, link_skipped, membership_skipped = apply_capture(
        root, index, confirmed_sidecar("ac3-out.md", "out of the round"), items)
    check(summaries == [], f"no summary applied for an out-of-round task (got {summaries})")
    check(membership_skipped == [out_key],
          f"reported as membership-skip (got {membership_skipped})")
    check(sid_lines(out_set) == 0, "the out-of-round task file was not written")


def test_allowset_does_not_widen_note_gate(root: Path) -> None:
    print("--- allow_tasks widens the confirmed gate only, not the note gate ---")
    task_path = make_task(root, "ac3b.md")
    key = f"{PROJECT}/ac3b.md"
    note_rel = "project-notes/specs/not-in-round.md"
    items = {"tasks": [], "notes": [], "allow_tasks": [key]}
    sidecar = {"confirmed": [], "proposals": [],
               "note_links": [{"note": note_rel, "task": "ac3b.md"}]}
    summaries, links, proposals, link_skipped, membership_skipped = apply_capture(
        root, {key: str(task_path)}, sidecar, items)
    check(links == [], f"no note link applied (got {links})")
    check(membership_skipped == [note_rel],
          f"the note is membership-skipped by items['notes'] (got {membership_skipped})")
    check(nl.NOTES_BEGIN not in task_path.read_text(encoding="utf-8")
          or note_rel not in task_path.read_text(encoding="utf-8"),
          "the note rel never reached the task's @notes block")


def test_legacy_items_without_allowset(root: Path) -> None:
    print("--- an items dict with NO allow_tasks behaves exactly as before ---")
    in_set = make_task(root, "ac4-in.md")
    out_set = make_task(root, "ac4-out.md")
    in_key = f"{PROJECT}/ac4-in.md"
    out_key = f"{PROJECT}/ac4-out.md"
    index = {in_key: str(in_set), out_key: str(out_set)}
    legacy_items = {"tasks": [in_key], "notes": []}

    s1, _l, _p, _ls, m1 = apply_capture(
        root, index, confirmed_sidecar("ac4-in.md", "legacy in-set"), legacy_items)
    check(s1 == [in_key], f"in-set confirmed still applies (got {s1})")
    check(m1 == [], f"nothing skipped for the in-set entry (got {m1})")

    s2, _l, _p, _ls, m2 = apply_capture(
        root, index, confirmed_sidecar("ac4-out.md", "legacy out-of-set"), legacy_items)
    check(s2 == [], f"out-of-set confirmed still skipped (got {s2})")
    check(m2 == [out_key], f"reported as membership-skip (got {m2})")


def test_items_none_still_bypasses(root: Path) -> None:
    print("--- items=None still bypasses membership entirely (unchanged) ---")
    task_path = make_task(root, "ac4b.md")
    key = f"{PROJECT}/ac4b.md"
    summaries, links, proposals, link_skipped, membership_skipped = apply_capture(
        root, {key: str(task_path)}, confirmed_sidecar("ac4b.md", "legacy sidecar"), None)
    check(summaries == [key], f"applied with no membership gate at all (got {summaries})")
    check(membership_skipped == [], f"nothing membership-skipped (got {membership_skipped})")


def test_malformed_allowset_is_ignored(root: Path) -> None:
    print("--- a non-list allow_tasks is ignored, gate falls back to items['tasks'] ---")
    task_path = make_task(root, "ac4c.md")
    key = f"{PROJECT}/ac4c.md"
    items = {"tasks": [], "notes": [], "allow_tasks": "not-a-list"}
    summaries, links, proposals, link_skipped, membership_skipped = apply_capture(
        root, {key: str(task_path)}, confirmed_sidecar("ac4c.md", "junk allow-set"), items)
    check(summaries == [], f"no summary applied (got {summaries})")
    check(membership_skipped == [key], f"skipped by the surviving gate (got {membership_skipped})")


def test_allowset_includes_tried_tasks(root: Path) -> None:
    print("--- a 打止め (tried_tasks) task is in the allow-set, and its confirmed applies ---")
    task_path = make_task(root, "m1-tried.md")
    key = f"{PROJECT}/m1-tried.md"
    slice_lines = [f"_projects/{PROJECT}/tasks/1_in_progress/m1-tried.md"]
    pre: dict = {}
    active = spc.compute_round_active(
        slice_lines, {PROJECT: str(root)}, {PROJECT: {}}, SID8,
        {}, {key}, pre_selflog_out=pre)
    check(key not in active,
          "the tried task is filtered out of the round's items['tasks']")
    check(key in pre,
          "the snapshot precedes the 打止め filter: the tried task IS in the allow-set")
    items = {"tasks": sorted(active.keys()), "notes": [],
             "allow_tasks": sorted(pre.keys())}
    summaries, links, proposals, link_skipped, membership_skipped = apply_capture(
        root, {key: str(task_path)},
        confirmed_sidecar("m1-tried.md", "tried but summarized"), items)
    check(summaries == [key],
          f"the union gate admits the tried task's confirmed entry (got {summaries})")
    check(membership_skipped == [],
          f"nothing membership-skipped (got {membership_skipped})")


STOP_PAYLOAD = '{{"session_id":"{sid}"}}'


def run_stop(workspace: Path, sid: str, expiry: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TASKFLOW_CAPTURE_EXPIRY_S"] = expiry
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        ["uv", "run", "--no-project", "python", str(HOOK)],
        input=STOP_PAYLOAD.format(sid=sid),
        cwd=str(workspace), env=env, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )


def read_capture(state_dir: Path, sid: str) -> dict:
    raw = json.loads((state_dir / f"{sid}.bind").read_text(encoding="utf-8"))
    return raw.get("capture", {})


def test_allowset_survives_stops() -> None:
    print("--- allow_tasks survives every Stop ---")
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        proj = "_test-allowset"
        sid = "a115e700-0000-0000-0000-00000000ac05"
        sid_tag = sid.replace('-', '')[-12:]
        projects = ws / "_projects"
        state_dir = projects / "_state"
        pdir = projects / proj
        (pdir / "tasks" / "1_in_progress").mkdir(parents=True)
        (pdir / "project-notes").mkdir(parents=True)
        state_dir.mkdir(parents=True)
        (state_dir / f"{sid}.json").write_text(
            json.dumps({"session_id": sid, "project": proj}), encoding="utf-8")
        (pdir / "project-notes" / "index.md").write_text("# index\n", encoding="utf-8")

        def task(name: str) -> Path:
            p = pdir / "tasks" / "1_in_progress" / name
            p.write_text(TASK_TEMPLATE, encoding="utf-8")
            return p

        def touch(name: str) -> None:
            with open(state_dir / f"{sid}.touched", "a", encoding="utf-8") as f:
                f.write(f"_projects/{proj}/tasks/1_in_progress/{name}\n")

        def agent_log(path: Path) -> None:
            c = path.read_text(encoding="utf-8")
            at = c.index("<!-- @log:end -->")
            path.write_text(
                c[:at] + f"- 2026-08-19T10:00:00+09:00 [s:{sid_tag}]: agent's own line\n" + c[at:],
                encoding="utf-8")

        a = task("2026-08-19_a.md")
        touch("2026-08-19_a.md")
        r1 = run_stop(ws, sid, "30.0")
        cap1 = read_capture(state_dir, sid)
        check(r1.returncode == 0, f"Stop 1 exited 0 (rc={r1.returncode}, err={r1.stderr[:200]!r})")
        check(cap1.get("round") == 1, f"Stop 1 opened round 1 (got {cap1.get('round')})")
        key_a = f"{proj}/2026-08-19_a.md"
        check(cap1.get("items", {}).get("allow_tasks") == [key_a],
              f"round 1 froze allow_tasks (got {cap1.get('items', {}).get('allow_tasks')})")
        check(cap1.get("history", {}).get("1", {}).get("allow_tasks") == [key_a],
              "the round-1 history entry carries allow_tasks too")

        r2 = run_stop(ws, sid, "0")
        cap2 = read_capture(state_dir, sid)
        check(r2.returncode == 0, f"Stop 2 exited 0 (rc={r2.returncode}, err={r2.stderr[:200]!r})")
        check(cap2.get("round") == 1, f"Stop 2 opened no new round (got {cap2.get('round')})")
        check(cap2.get("items", {}).get("allow_tasks") == [key_a],
              f"allow_tasks survived the non-requesting Stop "
              f"(got {cap2.get('items', {}).get('allow_tasks')})")

        b = task("2026-08-19_b.md")
        c_task = task("2026-08-19_c.md")
        touch("2026-08-19_b.md")
        touch("2026-08-19_c.md")
        agent_log(b)
        r3 = run_stop(ws, sid, "30.0")
        cap3 = read_capture(state_dir, sid)
        key_b = f"{proj}/2026-08-19_b.md"
        key_c = f"{proj}/2026-08-19_c.md"
        check(r3.returncode == 0, f"Stop 3 exited 0 (rc={r3.returncode}, err={r3.stderr[:200]!r})")
        check(cap3.get("round") == 2, f"Stop 3 opened round 2 (got {cap3.get('round')})")
        check(cap3.get("items", {}).get("tasks") == [key_c],
              f"items['tasks'] is still the post-subtraction set "
              f"(got {cap3.get('items', {}).get('tasks')})")
        check(cap3.get("items", {}).get("allow_tasks") == sorted([key_b, key_c]),
              f"allow_tasks is the pre-subtraction set, re-frozen for round 2 "
              f"(got {cap3.get('items', {}).get('allow_tasks')})")
        check(cap3.get("history", {}).get("2", {}).get("allow_tasks") == sorted([key_b, key_c]),
              "the round-2 history entry carries the same allow-set")
        check(c_task.read_text(encoding="utf-8").count(f"[s:{sid_tag}]") == 0,
              "no line written to the round-2 task yet (its round is still open)")


def main() -> int:
    print("=== membership allow-set (04-plan /) ===")
    real_before = len(os.listdir(REAL_STATE_DIR)) if REAL_STATE_DIR.is_dir() else -1
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        test_allowset_admits_selflogged_task(root)
        test_union_does_not_narrow(root)
        test_task_in_neither_set_is_skipped(root)
        test_allowset_does_not_widen_note_gate(root)
        test_legacy_items_without_allowset(root)
        test_items_none_still_bypasses(root)
        test_malformed_allowset_is_ignored(root)
        test_allowset_includes_tried_tasks(root)
    test_allowset_survives_stops()

    print("--- sandbox: the real _projects/_state/ is untouched ---")
    real_after = len(os.listdir(REAL_STATE_DIR)) if REAL_STATE_DIR.is_dir() else -1
    check(real_before == real_after,
          f"real _projects/_state/ file count unchanged ({real_before} -> {real_after})")

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
