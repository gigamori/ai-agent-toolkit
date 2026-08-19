#!/usr/bin/env python3
"""Unit tests for the B-m1 membership allow-set (04-plan §2.2 / §2.4).

Background (03-debug §4.3, observation 1): `compute_round_active` subtracts
every task whose `[s:sid8]` count grew this round — the agent logged it itself,
so no placeholder is needed. That subtracted set was ALSO the request-time
closed set `_apply_capture` gates `confirmed` membership on, so a round in
which the agent wrote its own `@log` line froze `items['tasks'] = []` and the
capture subagent's summary for that very task came back as
`membership-skip: <project>/<task>.md` — the judgment layer discarded for the
one reason that has nothing to do with membership.

B-m1 splits the two roles instead of removing the subtraction: the request
commit freezes an ADDITIONAL `items['allow_tasks']` holding the set as it stood
immediately BEFORE the self-log pass (exec carry included), and only the
`confirmed` gate reads it, as the UNION `items['tasks'] ∪ items['allow_tasks']`.
`items['tasks']` itself is untouched, so the expiry backstop, `round_task_set`,
`log_seen` and `round_base` keep driving off exactly the same set they did
before (04-plan §0.2 U-1 / U-2) and a self-logged task still gets no
placeholder.

Pinned here (04-plan §2.4):
  - B-AC1  a `confirmed` entry for a task that is in `allow_tasks` but NOT in
           `items['tasks']` (the OBS1-DEFECT shape) is APPLIED, not
           membership-skipped.
  - B-AC1b the union never narrows: a task in `items['tasks']` but not in
           `allow_tasks` still applies.
  - B-AC3  a `confirmed` entry in NEITHER set is still membership-skipped —
           the gate is widened, not disabled.
  - B-AC3b `allow_tasks` widens the `confirmed` gate ONLY: a note outside
           `items['notes']` is still membership-skipped even when its owning
           task is in `allow_tasks` (B-c1: the note side is unchanged).
  - B-AC4  fail-open on legacy shape: an `items` dict WITHOUT `allow_tasks`
           behaves exactly as before (in-set applies, out-of-set skips), and
           `items=None` still bypasses membership entirely. No legacy branch
           exists in the implementation — the union simply degenerates.
  - m-1    (review 2026-08-20 of d045d45) the allow-set snapshot precedes the
           INV-1 打止め filter too, so a `tried_tasks` member touched this
           round is absent from `items['tasks']` yet present in `allow_tasks`,
           and its `confirmed` applies — pinned as deliberate.
  - B-AC5  the evaporation trap (04-plan §0.2 U-3): the key is rebuilt from
           TWO `capture = {...}` literals every Stop, so this runs the real
           Stop hook three times in an isolated temp cwd and asserts that
           `allow_tasks` is present at the round-1 request, SURVIVES a
           non-requesting Stop, and is re-frozen (with the round's own,
           wider-than-`tasks` content) at the round-2 request.

State-dir sandbox (plugins/taskflow/CLAUDE.md `e2e_state_dir_sandbox`): the
unit tests build fixtures under `tempfile.TemporaryDirectory()` and never call
`main()`; the B-AC5 arm runs the hook as a SUBPROCESS with `cwd=` set to its
own temp workspace (`PROGRESS_ROOT` is module-scope, resolved from
`os.getcwd()`, so only a subprocess can move it) and brackets the run with a
file count of the real `_projects/_state/`.

Run:  uv run --no-project python plugins/taskflow/tests/test_apply_capture_membership_allowset.py
Exits 0 when all checks pass, 1 otherwise.
"""
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


# ---------------------------------------------------------------- B-AC1 ----
def test_allowset_admits_selflogged_task(root: Path) -> None:
    print("--- B-AC1: a task in allow_tasks but NOT in items['tasks'] applies ---")
    # The OBS1-DEFECT shape verbatim (03-debug §4.3): the agent self-logged the
    # task during the round, so the self-log pass emptied `items['tasks']`, the
    # round still opened on a novel note, and the subagent named the task from
    # conversation context. Pre-B-m1 this was `membership-skip`.
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
    print("--- B-AC1b: union — a task in items['tasks'] but not in allow_tasks still applies ---")
    # The two sets are built by different expressions. Gating on the allow-set
    # ALONE would be equivalent only while containment holds; the union is the
    # spec so that a future divergence cannot silently narrow the gate.
    task_path = make_task(root, "ac1b.md")
    key = f"{PROJECT}/ac1b.md"
    items = {"tasks": [key], "notes": [], "allow_tasks": []}
    summaries, links, proposals, link_skipped, membership_skipped = apply_capture(
        root, {key: str(task_path)}, confirmed_sidecar("ac1b.md", "still applies"), items)
    check(summaries == [key], f"summary applied from items['tasks'] alone (got {summaries})")
    check(membership_skipped == [], f"nothing membership-skipped (got {membership_skipped})")


# ---------------------------------------------------------------- B-AC3 ----
def test_task_in_neither_set_is_skipped(root: Path) -> None:
    print("--- B-AC3: a task in NEITHER set is still membership-skipped ---")
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
    print("--- B-AC3b: allow_tasks widens the confirmed gate only, not the note gate ---")
    # B-c1: the note side keeps gating on `items['notes']`. A note outside it
    # must still be skipped even though its owning task is admitted by the
    # allow-set — otherwise B-m1 would have quietly changed the note contract.
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


# ---------------------------------------------------------------- B-AC4 ----
def test_legacy_items_without_allowset(root: Path) -> None:
    print("--- B-AC4: an items dict with NO allow_tasks behaves exactly as before ---")
    in_set = make_task(root, "ac4-in.md")
    out_set = make_task(root, "ac4-out.md")
    in_key = f"{PROJECT}/ac4-in.md"
    out_key = f"{PROJECT}/ac4-out.md"
    index = {in_key: str(in_set), out_key: str(out_set)}
    legacy_items = {"tasks": [in_key], "notes": []}   # written before B-m1

    s1, _l, _p, _ls, m1 = apply_capture(
        root, index, confirmed_sidecar("ac4-in.md", "legacy in-set"), legacy_items)
    check(s1 == [in_key], f"in-set confirmed still applies (got {s1})")
    check(m1 == [], f"nothing skipped for the in-set entry (got {m1})")

    s2, _l, _p, _ls, m2 = apply_capture(
        root, index, confirmed_sidecar("ac4-out.md", "legacy out-of-set"), legacy_items)
    check(s2 == [], f"out-of-set confirmed still skipped (got {s2})")
    check(m2 == [out_key], f"reported as membership-skip (got {m2})")


def test_items_none_still_bypasses(root: Path) -> None:
    print("--- B-AC4b: items=None still bypasses membership entirely (unchanged) ---")
    task_path = make_task(root, "ac4b.md")
    key = f"{PROJECT}/ac4b.md"
    summaries, links, proposals, link_skipped, membership_skipped = apply_capture(
        root, {key: str(task_path)}, confirmed_sidecar("ac4b.md", "legacy sidecar"), None)
    check(summaries == [key], f"applied with no membership gate at all (got {summaries})")
    check(membership_skipped == [], f"nothing membership-skipped (got {membership_skipped})")


def test_malformed_allowset_is_ignored(root: Path) -> None:
    print("--- B-AC4c: a non-list allow_tasks is ignored, gate falls back to items['tasks'] ---")
    task_path = make_task(root, "ac4c.md")
    key = f"{PROJECT}/ac4c.md"
    items = {"tasks": [], "notes": [], "allow_tasks": "not-a-list"}
    summaries, links, proposals, link_skipped, membership_skipped = apply_capture(
        root, {key: str(task_path)}, confirmed_sidecar("ac4c.md", "junk allow-set"), items)
    check(summaries == [], f"no summary applied (got {summaries})")
    check(membership_skipped == [key], f"skipped by the surviving gate (got {membership_skipped})")


# ------------------------------------------------------------ review m-1 ----
def test_allowset_includes_tried_tasks(root: Path) -> None:
    print("--- m-1: a 打止め (tried_tasks) task is in the allow-set, and its confirmed applies ---")
    # Review 2026-08-20 m-1 (of commit d045d45): the `pre_selflog_out` snapshot
    # in `compute_round_active` is taken before BOTH the self-log pass and the
    # INV-1 打止め filter in the return statement. The allow-set therefore also
    # admits a `confirmed` for a task in `tried_tasks` that was touched this
    # round — intended: the backstop gave up on binding it (INV-1), but the
    # judgment layer's summary may still land. This check pins that widening as
    # deliberate; if the snapshot is ever moved after the 打止め filter, the
    # second and fourth checks below fail.
    task_path = make_task(root, "m1-tried.md")
    key = f"{PROJECT}/m1-tried.md"
    slice_lines = [f"_projects/{PROJECT}/tasks/1_in_progress/m1-tried.md"]
    pre: dict = {}
    active = spc.compute_round_active(
        slice_lines, {PROJECT: str(root)}, {PROJECT: {}}, SID8,
        {}, {key}, pre_selflog_out=pre)
    check(key not in active,
          "the tried task is filtered out of the round's items['tasks'] (INV-1)")
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


# ---------------------------------------------------------------- B-AC5 ----
STOP_PAYLOAD = '{{"session_id":"{sid}"}}'


def run_stop(workspace: Path, sid: str, expiry: str) -> subprocess.CompletedProcess:
    """Fire the real Stop hook with `cwd=workspace` (module-scope PROGRESS_ROOT)."""
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
    print("--- B-AC5: allow_tasks survives every Stop (evaporation trap, U-3) ---")
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        proj = "_test-allowset"
        sid = "a115e700-0000-0000-0000-00000000ac05"
        sid8 = sid[:8]
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
                c[:at] + f"- 2026-08-19T10:00:00+09:00 [s:{sid8}]: agent's own line\n" + c[at:],
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

        # Stop 2: nothing new, request expires -> the NON-requesting `capture`
        # literal is the one that rebuilds the dict. Pre-fix, a key re-emitted
        # in only one literal disappears exactly here.
        r2 = run_stop(ws, sid, "0")
        cap2 = read_capture(state_dir, sid)
        check(r2.returncode == 0, f"Stop 2 exited 0 (rc={r2.returncode}, err={r2.stderr[:200]!r})")
        check(cap2.get("round") == 1, f"Stop 2 opened no new round (got {cap2.get('round')})")
        check(cap2.get("items", {}).get("allow_tasks") == [key_a],
              f"allow_tasks survived the non-requesting Stop "
              f"(got {cap2.get('items', {}).get('allow_tasks')})")

        # Stop 3: task B is self-logged by the agent, task C is not. Round 2
        # therefore opens on C alone, while the allow-set holds BOTH — the
        # end-to-end proof that the frozen allow-set is the pre-subtraction set.
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
        check(c_task.read_text(encoding="utf-8").count(f"[s:{sid8}]") == 0,
              "no line written to the round-2 task yet (its round is still open)")


def main() -> int:
    print("=== B-m1 membership allow-set (04-plan §2.2 / §2.4) ===")
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
