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

sys.path.insert(0, str(HOOKS_SRC))
from progress_digest import u16len, INLINE_BUDGET_U16, IN_PROGRESS_ROWS_MAX  # noqa: E402

BOOTSTRAP_MARKER = "[taskflow bootstrap - read this block before anything else]"
PROJECT_INDEX_MARKER = "[Project Index:"
ACTION_REQUIRED_MARKER = "!!ACTION_REQUIRED"
PREFLIGHT_FULL_MARKER = "(1) ask the user to approve scaffold generation"
PREFLIGHT_PENDING_MARKER = "Do not re-ask for approval on every turn."
PREFLIGHT_BOUNDARY_MARKER = "another project's files"
PREFLIGHT_REBUILD_MARKER = "/scripts/rebuild_progress.py"
PREFLIGHT_REBUILD_POINTER = "rebuild command in the routing rules"
PEP723_RUNNER = "uv run --script"
UNEXPANDED_PLUGIN_ROOT = "${CLAUDE_PLUGIN_ROOT}"
GUIDELINES_REMINDER_MARKER = "[taskflow guidelines reminder]"
PROGRESS_SESSION_MARKER = "[Progress Session]"

PROJECT_INDEX_FORMAT_HEADING = "## Project index format"
SKELETON_FENCE = "```markdown"
PREVIEW_U16_CAP = 2000
PREVIEW_BYTE_CAP = 2048

SID_LARGE = "b1c2d3e4-f5a6-4789-9abc-def012345601"
SID_SMALL = "c2d3e4f5-a6b7-4890-9abc-def012345602"
SID_EVERY_TURN = "d3e4f5a6-b7c8-4901-9abc-def012345603"
SID_DIGEST_A = "e4f5a6b7-c8d9-4012-9abc-def012345604"
SID_DIGEST_B = "f5a6b7c8-d9e0-4123-9abc-def012345605"
SID_DIGEST_C = "a6b7c8d9-e0f1-4234-9abc-def012345606"
SID_DIGEST_D = "b7c8d9e0-f1a2-4345-9abc-def012345607"
SID_PREFLIGHT_SEQ = "c8d9e0f1-a2b3-4456-9abc-def012345608"
SID_PREFLIGHT_LONG = "d9e0f1a2-b3c4-4567-9abc-def012345609"
PROJECT = "e2e-bootstrap-fixture"
PROJECT_LONG_NAME = "e2e-bootstrap-fixture-longer-name"

DIGEST_HEADER_LITERAL = f"[Project Progress: {PROJECT}]"
OMISSION_NOTICE = "[Project Progress] In Progress digest omitted (context budget)"
NOTES_PUSH_HEADER = "project-notes (index rows):"

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


def run_session_init(hooks: Path, cwd: Path, payload: dict,
                      extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["uv", "run", "--no-project", "python", str(hooks / "session_init.py")],
        cwd=str(cwd), input=json.dumps(payload), capture_output=True,
        text=True, encoding="utf-8", errors="replace", env=env)


def canonical_index_sections(prompts: Path) -> list[str]:
    text = (prompts / "project_routing.md").read_text(encoding="utf-8")
    start = text.find(PROJECT_INDEX_FORMAT_HEADING)
    if start == -1:
        return []
    fence = text.find(SKELETON_FENCE, start)
    if fence == -1:
        return []
    body_start = text.find("\n", fence) + 1
    body_end = text.find("```", body_start)
    if body_end == -1:
        return []
    return [ln.strip() for ln in text[body_start:body_end].splitlines()
            if ln.startswith("## ")]


def action_required_span(ctx: str) -> tuple[int, int]:
    start = ctx.find(ACTION_REQUIRED_MARKER)
    if start == -1:
        return (-1, -1)
    end = ctx.find("\n", start)
    return (start, len(ctx) if end == -1 else end)


def action_required_line(ctx: str) -> str:
    start, end = action_required_span(ctx)
    return "" if start == -1 else ctx[start:end]


def preview_fit(ctx: str) -> tuple[int, int]:
    _, end = action_required_span(ctx)
    if end == -1:
        return (-1, -1)
    head = ctx[:end]
    return (u16len(head), len(head.encode("utf-8")))


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


def small_index_no_progress_fixture(root: Path) -> None:
    proj_dir = root / "_projects" / PROJECT
    proj_dir.mkdir(parents=True, exist_ok=True)
    with open(proj_dir / "index.md", "w", encoding="utf-8", newline="\n") as f:
        f.write("| Project | Description | Target |\n|---------|-------------|--------|\n"
                 "| x | small | none |\n")


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


def seed_ordinary_state(root: Path, session_id: str, project: str,
                         project_rules_indexed: str = "") -> None:
    proj_dir = root / "_projects" / project
    proj_dir.mkdir(parents=True, exist_ok=True)
    with open(proj_dir / "index.md", "w", encoding="utf-8", newline="\n") as f:
        f.write("| Project | Description | Target |\n|---------|-------------|--------|\n"
                 "| x | small | none |\n")
    state_dir = root / "_projects" / "_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "project": project,
        "rules_loaded": True,
        "indexed_project": project,
        "guidelines_loaded": True,
        "project_rules_indexed": project_rules_indexed,
        "origin": "cc",
    }
    with open(state_dir / f"{session_id}.json", "w", encoding="utf-8") as f:
        json.dump(state, f)


def write_project_rules(root: Path, project: str, headings: list[str]) -> None:
    proj_dir = root / "_projects" / project
    proj_dir.mkdir(parents=True, exist_ok=True)
    body = "\n\n".join(f"## {h}\n\nRule text for {h}." for h in headings) + "\n"
    with open(proj_dir / "rules.md", "w", encoding="utf-8", newline="\n") as f:
        f.write(body)


def make_in_progress_row(n: int, task_text: str) -> str:
    return f"| {n} | MID | {task_text} | 2026-09-01 | @tasks/1_in_progress/task{n}.md |"


def padded_task_text(n: int, target_u16: int) -> str:
    overhead = u16len(make_in_progress_row(n, ""))
    fill = max(target_u16 - overhead, 1)
    return "T" * fill


def build_progress_md(row_lines: list[str]) -> str:
    lines = [
        "<!-- @table:begin -->",
        "## TODO",
        "| # | Priority | Task | Created | Link |",
        "|---|----------|------|---------|------|",
        "",
        "## In Progress",
        "| # | Priority | Task | Updated | Link |",
        "|---|----------|------|---------|------|",
    ]
    lines.extend(row_lines)
    lines.extend([
        "",
        "## Completed",
        "| # | Priority | Task | Completed | Link |",
        "|---|----------|------|-----------|------|",
        "<!-- @table:end -->",
    ])
    return "\n".join(lines) + "\n"


def write_progress_md(root: Path, project: str, row_lines: list[str]) -> None:
    proj_dir = root / "_projects" / project
    proj_dir.mkdir(parents=True, exist_ok=True)
    with open(proj_dir / "progress.md", "w", encoding="utf-8", newline="\n") as f:
        f.write(build_progress_md(row_lines))


def seed_notes_l1_rows(root: Path, project: str, session_id: str,
                        n_rows: int, desc_len: int) -> None:
    proj_dir = root / "_projects" / project
    notes_dir = proj_dir / "project-notes"
    specs_dir = notes_dir / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    state_dir = root / "_projects" / "_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    index_lines = ["| File | Description | Tags | Updated |",
                   "|------|-------------|------|---------|"]
    touched_lines = []
    for i in range(1, n_rows + 1):
        rel = f"specs/note{i}.md"
        with open(specs_dir / f"note{i}.md", "w", encoding="utf-8", newline="\n") as f:
            f.write(f"---\nanchors: [{rel}]\n---\n\n# note {i}\n")
        desc = (f"note{i} " + "d" * desc_len)[:desc_len]
        index_lines.append(f"| {rel} | {desc} | | 2026-09-01 |")
        touched_lines.append(rel)
    with open(notes_dir / "index.md", "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(index_lines) + "\n")
    with open(state_dir / f"{session_id}.touched", "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(touched_lines) + "\n")


def digest_block_slice(ctx: str) -> str:
    start = ctx.find(DIGEST_HEADER_LITERAL)
    if start == -1:
        return ""
    stop = ctx.find(GUIDELINES_REMINDER_MARKER, start)
    return ctx[start: stop if stop != -1 else len(ctx)]


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
        source = (hooks / "session_init.py").read_text(encoding="utf-8")
        i_action = source.find("{action_required}")
        i_fork = source.find("{fork_context}")
        check(i_action != -1 and i_fork != -1 and i_action < i_fork,
              f"the assembled payload places action_required ahead of fork_context "
              f"(offsets {i_action} / {i_fork}); fork_context's inherited-task list has "
              f"no length bound, and anything ahead of the banner pushes its line toward "
              f"a cut that drops whole lines, so the preview fit measured below would not "
              f"hold on a forked session's first turn")

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
        check(PREFLIGHT_FULL_MARKER in ctx,
              "a heavy turn carries the full scaffold procedure, which is the only "
              "variant that tells the LLM to ask for approval")
        check(PREFLIGHT_PENDING_MARKER not in ctx,
              "a heavy turn does not also carry the standing notice variant")

        fit_u16, fit_bytes = preview_fit(ctx)
        check(0 < fit_u16 <= PREVIEW_U16_CAP and 0 < fit_bytes <= PREVIEW_BYTE_CAP,
              f"the ACTION_REQUIRED line ends at u16 offset {fit_u16} of "
              f"{PREVIEW_U16_CAP} and byte offset {fit_bytes} of {PREVIEW_BYTE_CAP}, "
              f"inside the preview a heavy turn collapses to on either counting basis; "
              f"the cut walks back to the preceding line break, so one unit over drops "
              f"the whole banner rather than truncating it, and a non-ASCII state_file "
              f"path separates the two counts (both cap figures and their provenance: "
              f"plugins/taskflow/docs/test-constraints.md)")
        check(PREFLIGHT_REBUILD_POINTER in ctx,
              "a heavy turn sends the LLM to the rebuild command instead of ordering a "
              "hand-written progress.md; the command itself is not inlined here because "
              "an absolute plugin path would make the preview fit machine-dependent")
        check(f"`_projects/{PROJECT}/index.md`" in ctx,
              "a heavy turn names the project index by its full path, so it cannot be "
              "read as the master _projects/index.md named later in the same sentence")
        check(UNEXPANDED_PLUGIN_ROOT not in ctx,
              "no injected block reaches the agent with an unexpanded "
              "${CLAUDE_PLUGIN_ROOT}; the agent's shell has no such variable, so a "
              "command printed with one is unrunnable")
        check(PREFLIGHT_REBUILD_MARKER in ctx and PEP723_RUNNER in ctx,
              "the routing rules deliver the rebuild invocation with a resolved path and "
              "the --script runner the script's inline dependencies require")

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
        check(ACTION_REQUIRED_MARKER in ctx,
              "the preflight banner still fires on a non-heavy turn while "
              "progress.md is missing")
        check(PREFLIGHT_PENDING_MARKER in ctx,
              "a non-heavy turn carries the standing notice variant")
        check(PREFLIGHT_FULL_MARKER not in ctx,
              "a non-heavy turn does not repeat the ask-for-approval procedure; "
              "repeating it is what makes the LLM re-ask every turn")

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

    check(INLINE_BUDGET_U16 == 10000,
          "INLINE_BUDGET_U16 == 10000, the harness anchor (CC 2.1.270 NEr); "
          "a NEr change or a same-pass edit of the constant plus a fixture "
          "must fail this")

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        assert_isolated(root)
        hooks = mirror_hooks_and_prompts(root)
        seed_ordinary_state(root, SID_DIGEST_A, PROJECT, project_rules_indexed=PROJECT)
        write_project_rules(root, PROJECT, [
            "Design Decisions, Rationale and Long-Term Architecture Notes",
            "Testing, Verification and Continuous Integration Requirements",
            "Commit Discipline and Code Review Workflow Constraints Overview",
        ])
        seed_notes_l1_rows(root, PROJECT, SID_DIGEST_A, n_rows=8, desc_len=252)
        (root / "_projects" / PROJECT / "project-notes" / ".qmd").mkdir(
            parents=True, exist_ok=True)
        write_progress_md(root, PROJECT,
                           [make_in_progress_row(i, padded_task_text(i, 190))
                            for i in range(1, 10)])

        r = run_session_init(hooks, root, {
            "session_id": SID_DIGEST_A,
            "prompt": "continue please, no project token here",
            "transcript_path": "",
        }, extra_env={"TASKFLOW_QMD_BIN": str(root / "no-such-qmd")})
        ctx = additional_context(r.stdout)
        check(bool(ctx), f"arm A additionalContext is non-empty (rc={r.returncode})")
        check(DIGEST_HEADER_LITERAL in ctx,
              "arm A: digest header present on an ordinary turn (representative worst case)")
        block = digest_block_slice(ctx)
        row_count = block.count("@tasks/1_in_progress/task")
        check(row_count == IN_PROGRESS_ROWS_MAX,
              f"arm A: exactly IN_PROGRESS_ROWS_MAX={IN_PROGRESS_ROWS_MAX} data rows "
              f"present in the digest (got {row_count})")
        check("in-progress: 5 of 9 rows shown" in ctx,
              "arm A: footer literal 'in-progress: 5 of 9 rows shown' present "
              "(written out, never interpolated from IN_PROGRESS_ROWS_MAX)")
        check(f"[Project Rules reminder: {PROJECT}]" in ctx,
              "arm A: project-rules manifest block present (part of the worst-arm composition)")
        check(NOTES_PUSH_HEADER in ctx,
              "arm A: project-notes push block present (part of the worst-arm composition)")
        check("[project-notes] vector layer unavailable: qmd binary not found "
              "(set TASKFLOW_QMD_BIN)" in ctx,
              "arm A: notes_notice degrade block present (qmd unresolved against a "
              "present project-notes/.qmd/, the worst known degrade arm)")
        total_u16 = u16len(ctx)
        check(total_u16 <= INLINE_BUDGET_U16,
              f"arm A: u16len(ctx)={total_u16} <= INLINE_BUDGET_U16={INLINE_BUDGET_U16} "
              f"(headroom={INLINE_BUDGET_U16 - total_u16})")

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        assert_isolated(root)
        hooks = mirror_hooks_and_prompts(root)
        seed_ordinary_state(root, SID_DIGEST_B, PROJECT)
        seed_notes_l1_rows(root, PROJECT, SID_DIGEST_B, n_rows=1, desc_len=20)
        write_progress_md(root, PROJECT,
                           [make_in_progress_row(i, padded_task_text(i, 1800))
                            for i in range(1, 6)])

        r = run_session_init(hooks, root, {
            "session_id": SID_DIGEST_B,
            "prompt": "continue please, no project token here",
            "transcript_path": "",
        })
        ctx = additional_context(r.stdout)
        check(bool(ctx), f"arm B additionalContext is non-empty (rc={r.returncode})")
        check(DIGEST_HEADER_LITERAL not in ctx,
              "arm B: digest header ABSENT (the inline-budget guard fired on "
              "row-length overflow)")
        check(OMISSION_NOTICE in ctx,
              "arm B: omission notice present in place of the digest")
        check(PROGRESS_SESSION_MARKER in ctx,
              "arm B positive marker: the hook ran ([Progress Session] header present)")
        check(NOTES_PUSH_HEADER in ctx,
              "arm B positive marker: the project-notes push block ran "
              "('project-notes (index rows):' present)")
        total_u16 = u16len(ctx)
        check(total_u16 <= INLINE_BUDGET_U16,
              f"arm B: post-guard u16len(ctx)={total_u16} <= INLINE_BUDGET_U16={INLINE_BUDGET_U16}")

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        assert_isolated(root)
        hooks = mirror_hooks_and_prompts(root)
        seed_ordinary_state(root, SID_DIGEST_C, PROJECT)
        write_progress_md(root, PROJECT,
                           [make_in_progress_row(i, "small task") for i in range(1, 4)])

        r = run_session_init(hooks, root, {
            "session_id": SID_DIGEST_C,
            "prompt": "continue please, no project token here",
            "transcript_path": "",
        })
        ctx = additional_context(r.stdout)
        check(bool(ctx), f"arm C additionalContext is non-empty (rc={r.returncode})")
        check(DIGEST_HEADER_LITERAL in ctx,
              "arm C: digest header present when total rows are under the cap")
        block = digest_block_slice(ctx)
        row_count = block.count("@tasks/1_in_progress/task")
        check(row_count == 3,
              f"arm C: exactly 3 data rows present in the digest (got {row_count})")
        check("in-progress: 3 of 3 rows shown" in ctx,
              "arm C: footer literal 'in-progress: 3 of 3 rows shown' present")

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        assert_isolated(root)
        hooks = mirror_hooks_and_prompts(root)
        proj_dir = root / "_projects" / PROJECT
        proj_dir.mkdir(parents=True, exist_ok=True)
        with open(proj_dir / "index.md", "w", encoding="utf-8", newline="\n") as f:
            f.write("| Project | Description | Target |\n|---------|-------------|--------|\n"
                     "| x | small | none |\n")
        write_progress_md(root, PROJECT,
                           [make_in_progress_row(i, "small task") for i in range(1, 3)])

        r = run_session_init(hooks, root, {
            "session_id": SID_DIGEST_D,
            "prompt": f"pj:{PROJECT} start work",
            "transcript_path": "",
        })
        ctx = additional_context(r.stdout)
        check(bool(ctx), f"arm D additionalContext is non-empty (rc={r.returncode})")
        check(BOOTSTRAP_MARKER in ctx,
              "arm D: BOOTSTRAP_BLOCK present on a heavy/fresh-session turn")
        check(DIGEST_HEADER_LITERAL not in ctx,
              "arm D: digest ABSENT on the same turn as BOOTSTRAP_BLOCK (never both)")

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        assert_isolated(root)
        hooks = mirror_hooks_and_prompts(root)
        small_index_no_progress_fixture(root)

        r1 = run_session_init(hooks, root, {
            "session_id": SID_PREFLIGHT_SEQ,
            "prompt": f"pj:{PROJECT} start here",
            "transcript_path": "",
        })
        ctx1 = additional_context(r1.stdout)
        state_json = root / "_projects" / "_state" / f"{SID_PREFLIGHT_SEQ}.json"
        check(bool(ctx1), f"live turn 1 additionalContext is non-empty (rc={r1.returncode})")
        check(state_json.is_file(),
              "live turn 1 wrote the state json the next turn reads its gate flags from")
        check(PREFLIGHT_FULL_MARKER in ctx1,
              "live turn 1 carries the full scaffold procedure")

        r2 = run_session_init(hooks, root, {
            "session_id": SID_PREFLIGHT_SEQ,
            "prompt": "continue please, no project token here",
            "transcript_path": "",
        })
        ctx2 = additional_context(r2.stdout)
        check(bool(ctx2), f"live turn 2 additionalContext is non-empty (rc={r2.returncode})")
        check(PREFLIGHT_PENDING_MARKER in ctx2,
              "live turn 2 degrades to the standing notice from the state turn 1 wrote, "
              "not from a pre-seeded fixture")
        check(PREFLIGHT_FULL_MARKER not in ctx2,
              "live turn 2 does not repeat the ask-for-approval procedure")

        sections = canonical_index_sections(root / "prompts")
        check(len(sections) == 2,
              f"project_routing.md's first {SKELETON_FENCE} block after "
              f"\"{PROJECT_INDEX_FORMAT_HEADING}\" carries exactly the 2 required "
              f"sections, got {sections!r}")
        notice = action_required_line(ctx2)
        missing = [s for s in sections if f"`{s}`" not in notice]
        check(sections and not missing,
              f"every section of the canonical skeleton reaches the standing notice, "
              f"which is the variant the approval-then-write turn gets; missing "
              f"{missing!r}")
        check(PREFLIGHT_REBUILD_MARKER in notice and PEP723_RUNNER in notice,
              "the standing notice carries the rebuild invocation itself, resolved path "
              "and --script runner included; this is the variant the write turn gets, and "
              "it is not preview-bound")
        check(UNEXPANDED_PLUGIN_ROOT not in ctx2,
              "the standing notice's command reaches the agent with the path resolved")
        check(PREFLIGHT_BOUNDARY_MARKER in ctx2,
              "live turn 2 forbids reading another project's files")

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        assert_isolated(root)
        hooks = mirror_hooks_and_prompts(root)

        r = run_session_init(hooks, root, {
            "session_id": SID_PREFLIGHT_LONG,
            "prompt": f"pj:{PROJECT_LONG_NAME} begin",
            "transcript_path": "",
        })
        ctx = additional_context(r.stdout)
        check(bool(ctx), f"long-name additionalContext is non-empty (rc={r.returncode})")
        check(PREFLIGHT_FULL_MARKER in ctx,
              "long-name turn 1 is a heavy turn carrying the full scaffold procedure")
        long_u16, long_bytes = preview_fit(ctx)
        check(0 < long_u16 <= PREVIEW_U16_CAP and 0 < long_bytes <= PREVIEW_BYTE_CAP,
              f"a {len(PROJECT_LONG_NAME)}-character project name still leaves the "
              f"ACTION_REQUIRED line inside the preview (u16 {long_u16} of "
              f"{PREVIEW_U16_CAP}, bytes {long_bytes} of {PREVIEW_BYTE_CAP}); the name is "
              f"interpolated more than once, so the line grows with it")

    after = real_state_snapshot()
    check(after == before,
          f"real _projects/_state/ is unchanged (before={before!r}, after={after!r})")
    check(not (REAL_STATE / f"{SID_LARGE}.json").exists(),
          "synthetic large-fixture session id absent from real _projects/_state/")
    check(not (REAL_STATE / f"{SID_SMALL}.json").exists(),
          "synthetic turn-2 session id absent from real _projects/_state/")
    check(not (REAL_STATE / f"{SID_EVERY_TURN}.json").exists(),
          "synthetic inject_rules_full session id absent from real _projects/_state/")
    check(not (REAL_STATE / f"{SID_DIGEST_A}.json").exists(),
          "synthetic digest arm A session id absent from real _projects/_state/")
    check(not (REAL_STATE / f"{SID_DIGEST_B}.json").exists(),
          "synthetic digest arm B session id absent from real _projects/_state/")
    check(not (REAL_STATE / f"{SID_DIGEST_C}.json").exists(),
          "synthetic digest arm C session id absent from real _projects/_state/")
    check(not (REAL_STATE / f"{SID_DIGEST_D}.json").exists(),
          "synthetic digest arm D session id absent from real _projects/_state/")
    check(not (REAL_STATE / f"{SID_PREFLIGHT_SEQ}.json").exists(),
          "synthetic preflight-sequence session id absent from real _projects/_state/")
    check(not (REAL_STATE / f"{SID_PREFLIGHT_LONG}.json").exists(),
          "synthetic long-project-name session id absent from real _projects/_state/")

    print()
    if FAIL == 0:
        print(f"All {PASS} checks passed.")
        return 0
    print(f"{FAIL} failed, {PASS} passed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
