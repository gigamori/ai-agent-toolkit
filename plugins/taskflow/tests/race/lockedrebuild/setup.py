#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""Build an isolated `_projects/probe-proj` tree for the lockedrebuild
torn-file repro.

Companion to race.py -- see race.py's header for the harness's baseline /
pass criterion / what it deliberately does NOT prove.

Two knobs, and they widen two different things:

  --tasks N        widens the COMPUTE half of racer A's run: rebuild_progress.py
                   walks every task file (gather_tasks) before it reads
                   progress.md, so a big task tree pushes A's write later in
                   wall-clock time and makes the delay sweep easier to aim.
  --freetext-kb K  THIS IS THE DETECTION-PROBABILITY KNOB. `Path.write_text`
                   truncates first and then writes; the bigger progress.md is,
                   the longer the file spends in a truncated/partial state, and
                   the wider the window a torn read has to land inside. A small
                   probe file can make a real, unlocked build look clean simply
                   because nothing ever observed the truncation. Turn this up
                   (256 KB default, MBs are legitimate) before concluding
                   "0 torn".

progress.md is seeded with an H1, all four free-text section headings, a unique
free-text marker OUTSIDE the @table region, K kilobytes of padding, and exactly
ONE `<!-- @table:begin -->` / `<!-- @table:end -->` pair. race.py classifies a
trial by whether that structure survived, so the pristine seed is also written
to `progress.seed.md` in the probe tree for race.py to reseed from -- the seed
is generated here, not duplicated there, so the --freetext-kb knob flows
through to every trial.

usage: uv run --script setup.py [project_dir] [--tasks N] [--freetext-kb K]
  project_dir:    where to build the probe tree (default: OS temp dir, under
                  cc-taskflow-race/lockedrebuild/ -- never inside this repo,
                  never under any real _projects/ tree).
  --tasks:        task count, spread round-robin across 0_todo /
                  1_in_progress / 2_done (default 2000)
  --freetext-kb:  approximate free-text size of the seeded progress.md in KiB
                  (default 256)
"""

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

# IN-REPO constant. Unlike the Pi-side harnesses (which reach across repos via
# TASKFLOW_CC_PLUGIN_DIR), this harness lives in the same checkout as the code
# under test, so the script is located by a relative path from __file__ and
# there is deliberately NO environment variable to introduce or mis-set.
#   tests/race/lockedrebuild/ -> ../../../scripts/rebuild_progress.py
SCRIPT = (Path(__file__).resolve().parent / ".." / ".." / ".." / "scripts" / "rebuild_progress.py").resolve()

if not SCRIPT.is_file():
    sys.exit(
        f"FATAL: rebuild_progress.py not found at {SCRIPT}\n"
        "This probe tree exists only to be chewed on by that script, and this "
        "file's location inside plugins/taskflow/ is what pins the relative "
        "path -- a miss means the harness was copied out of the plugin or the "
        "script moved. Refusing to build rather than silently producing a tree "
        "no racer will ever touch, which would let race.py report a spurious "
        "clean pass instead of a missing dependency."
    )

DEFAULT_PROJ = (
    Path(tempfile.gettempdir())
    / "cc-taskflow-race"
    / "lockedrebuild"
    / "_projects"
    / "probe-proj"
)

# Stamp file. setup.py rmtree()s its target, so it refuses any pre-existing
# directory that this harness did not build itself -- a mistyped project_dir
# must never be able to delete a real _projects/<project>/.
STAMP = ".lockedrebuild-probe"

SEED_NAME = "progress.seed.md"

# Kept in lockstep with race.py's constants of the same name.
MARKER = "LOCKEDREBUILD_MARKER_PRESENT"
SECTIONS = (
    "## Architecture",
    "## Key Decisions & Policies",
    "## Open Issues",
    "## Reference Materials",
)

TABLE_REGION = """<!-- @table:begin -->
## TODO

| # | Priority | Task | Created | Link |
|---|----------|------|---------|------|
<!-- @table:end -->
"""

TASK = """---
priority: {priority}
created: {created}
updated: {updated}
---

# {title}

## Next Steps

<!-- @log:begin -->
<!-- @log:end -->
"""

PRIORITIES = ("HIGH", "MID", "LOW")
STATUSES = ("0_todo", "1_in_progress", "2_done")
DATE = "2026-08-08"


def pad_block(tag: str, nbytes: int) -> str:
    """Deterministic free-text filler, ~nbytes of it, as whole lines."""
    if nbytes <= 0:
        return ""
    lines = []
    written = 0
    i = 0
    while written < nbytes:
        line = f"pad-{tag}-{i:06d} " + "." * 44 + "\n"
        lines.append(line)
        written += len(line)
        i += 1
    return "".join(lines)


def build_seed(project_name: str, freetext_kb: int) -> str:
    """H1 + 4 headings + marker + padding + exactly one @table pair."""
    target = max(freetext_kb, 0) * 1024
    per_section = target // len(SECTIONS)

    parts = [f"# Progress: {project_name}\n"]
    for idx, heading in enumerate(SECTIONS):
        parts.append(f"\n{heading}\n\n")
        if idx == 0:
            # The marker lives in free text, OUTSIDE the @table region: a torn
            # read that loses free text loses this, and that erasure is the
            # observable.
            parts.append(f"{MARKER}\n\n")
        parts.append(pad_block(f"s{idx}", per_section))
    parts.append("\n")
    parts.append(TABLE_REGION)
    return "".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the lockedrebuild probe _projects tree.",
    )
    parser.add_argument(
        "project_dir",
        nargs="?",
        type=Path,
        default=DEFAULT_PROJ,
        help=f"where to build the probe tree (default: {DEFAULT_PROJ})",
    )
    parser.add_argument(
        "--tasks",
        type=int,
        default=2000,
        help="task count, spread across 0_todo/1_in_progress/2_done "
        "(default 2000). Widens rebuild_progress.py's gather phase, which "
        "pushes its write later and makes the delay sweep easier to aim.",
    )
    parser.add_argument(
        "--freetext-kb",
        type=int,
        default=256,
        help="approximate free-text size of the seeded progress.md, in KiB "
        "(default 256). DETECTION-PROBABILITY KNOB: write_text truncates "
        "before it writes, so a bigger file spends longer in a truncated "
        "state and widens the window a torn read must land inside. A small "
        "value can make an unlocked build look clean.",
    )
    args = parser.parse_args()

    proj: Path = args.project_dir.resolve()
    n: int = args.tasks
    freetext_kb: int = args.freetext_kb

    if proj.exists() and not (proj / STAMP).exists():
        sys.exit(
            f"FATAL: {proj} already exists and is not a lockedrebuild probe "
            f"tree (no {STAMP} stamp file).\n"
            "This script rmtree()s its target. Refusing to delete a directory "
            "it did not build -- point project_dir at a fresh path, or remove "
            "that directory by hand if it really is disposable."
        )

    if proj.exists():
        shutil.rmtree(proj)

    for status in STATUSES:
        (proj / "tasks" / status).mkdir(parents=True)

    for i in range(n):
        status = STATUSES[i % len(STATUSES)]
        priority = PRIORITIES[i % len(PRIORITIES)]
        name = f"{DATE}_probe-task-{i:05d}.md"
        # The H1 carries the literal substring "probe task" -- race.py's
        # classifier looks for it in the final file to tell "table rows landed"
        # from "the table region is empty or was never written".
        (proj / "tasks" / status / name).write_text(
            TASK.format(
                priority=priority,
                created=DATE,
                updated=DATE,
                title=f"probe task {i:05d}",
            ),
            encoding="utf-8",
        )

    seed = build_seed(proj.name, freetext_kb)
    (proj / SEED_NAME).write_text(seed, encoding="utf-8")
    (proj / "progress.md").write_text(seed, encoding="utf-8")
    (proj / STAMP).write_text("lockedrebuild probe tree\n", encoding="utf-8")

    per_status = {s: sum(1 for i in range(n) if STATUSES[i % 3] == s) for s in STATUSES}
    print(f"built {proj}")
    print(f"  tasks={n} " + ", ".join(f"{s}={per_status[s]}" for s in STATUSES))
    print(f"  progress.md={len(seed.encode('utf-8'))} bytes (--freetext-kb {freetext_kb})")
    print(f"  seed={proj / SEED_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
