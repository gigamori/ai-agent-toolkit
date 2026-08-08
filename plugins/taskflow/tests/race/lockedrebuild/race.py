#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""lockedrebuild -- write-lock v2 acceptance harness: two concurrent runs of
`rebuild_progress.py` against the SAME `progress.md`.

This is the "locked rebuild vs locked rebuild" pairing that the write-lock v2
pass criterion is actually about, and that no harness covered before. Its
sibling on the Pi side, `test/race/lostupdate/`, races CC's rebuild against an
Edit-tool-shaped writer -- a permanently unfixable R-lock gap. THIS harness
does NOT cover the Edit-tool path at all; do not read a green run here as
closing that gap.

WHAT IS BEING DETECTED (read this before changing the classifier)
----------------------------------------------------------------
A naive "two rebuilds race each other" CANNOT detect a lost update. Both
processes read the same free text and write the same free text back, so the
interleaving is unobservable: whoever writes last writes byte-identical
content. There is no `lost_*` outcome to tally here, and inventing one would
produce a harness that reports 0 losses forever regardless of locking.

The real, detectable corruption is a TORN FILE:

  `Path.write_text` truncates, then writes. If racer A reads while racer B is
  mid-write, A reads truncated content that no longer contains
  `<!-- @table:begin -->`. `replace_or_append_region` then falls into its
  no-marker branch (rebuild_progress.py:180-181, Risk R7) and APPENDS a second
  table region at the end. The result is a progress.md with two
  `@table:begin` markers -- which taskflow explicitly forbids -- and/or free
  text sections that the truncation ate.

So every trial is classified by the STRUCTURAL INTEGRITY of the final file,
not by "whose write won".

  ok                  exactly one @table:begin, all four section headings
                      present, the free-text marker present, `probe task`
                      rows present
  torn_dup_table      two or more @table:begin
  torn_lost_freetext  any section heading, or the marker, missing
  torn_empty          file empty, or no `probe task` rows present

The chain is ordered most-specific-first so a trial that trips several
conditions is counted exactly once: `torn_dup_table` wins over
`torn_lost_freetext` (an appended-second-region file usually also lost free
text, but the duplicate region is the sharper, structurally-forbidden
signature), and `torn_empty` wins over `torn_lost_freetext` (a file with
nothing in it trivially has no headings; reporting that as "lost freetext"
would understate it).

RACER DESIGN -- do not "simplify" this
--------------------------------------
Racer A is the REAL subprocess, invoked exactly as
`hooks/task_rebuild_progress.py` invokes it:

    uv run --no-project <scripts/rebuild_progress.py> <project_dir>

Racer B is the SAME module driven in-process, loaded via importlib the way
`lostupdate/window.py` loads it. A subprocess-vs-subprocess race is NOT usable
here: `uv run` plus interpreter startup costs ~261-295 ms with a 13-183 ms
jitter spread, while the read->write window is only ~0.3-0.6 ms. A start-offset
sweep cannot aim at a sub-millisecond window through two orders of magnitude
more jitter. Driving racer B in-process is what lets a precise spin delay hit
the window at all.

PRE-PORT / POST-PORT FORK (the single most important thing to understand)
-------------------------------------------------------------------------
Racer B must execute the read->write window through the SAME entry point
production code uses, so that when the lock lands, racer B is locked too. It
therefore forks on whether `rebuild_progress.py` exports `write_region`:

  * `write_region` PRESENT  -> post-port. Both racers go through the locked
    entry point. This is the "locked vs locked" arm, and it is the one the
    pass criterion (0 torn) applies to.
  * `write_region` ABSENT   -> pre-port. Racer B runs the inline, UNLOCKED
    read -> replace_or_append_region -> write_text sequence copied from
    `main()`. This is the BASELINE arm: it is expected to be able to tear.

`write_region` landed with the protocol v2 port, so the post-port arm is the one
you should see today. Which arm ran is printed on every run.

IF A FUTURE READER SEES `racer_b=fallback-unlocked` AFTER THE PORT HAS LANDED,
THAT IS A HARNESS-BROKEN SIGNAL, NOT A PASS. It means racer B stopped going
through the locked entry point, so the run measured nothing about the lock.
A 0-torn tally from the fallback arm post-port must be discarded, not reported.

WHAT THIS HARNESS DOES NOT PROVE
--------------------------------
  * Nothing about the Edit tool / agent-authored writes to progress.md (the
    R-lock gap; see the Pi-side `lostupdate` harness).
  * Nothing about `tasks/*.md` log regions (`lograce`) or project notes
    (`noterace`).
  * Not a proof of absence. Detection is probabilistic and depends on
    `--freetext-kb` (see setup.py): a small probe file narrows the truncated
    window and can make an unlocked build look clean. A 0-torn tally is only
    meaningful next to a NON-zero tally from the same delay band and the same
    `--freetext-kb` on the unlocked baseline.

usage: uv run --script race.py <project_dir> <lo_ms> <hi_ms> [step_ms]
  Requires `setup.py <project_dir> ...` to have been run first.
"""

import importlib.util
import subprocess
import sys
import time
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
        "This harness races the REAL script against itself; this file's "
        "location inside plugins/taskflow/ is what pins the relative path. "
        "Refusing to run rather than silently degrading -- with racer A "
        "no-opping and racer B falling back, every trial would come out `ok` "
        "and the run would misreport as a spurious clean pass instead of a "
        "missing dependency."
    )

SEED_NAME = "progress.seed.md"

# Kept in lockstep with setup.py's constants of the same name.
MARKER = "LOCKEDREBUILD_MARKER_PRESENT"
SECTIONS = (
    "## Architecture",
    "## Key Decisions & Policies",
    "## Open Issues",
    "## Reference Materials",
)
TABLE_BEGIN = "<!-- @table:begin -->"
ROW_SIGIL = "probe task"


def load():
    spec = importlib.util.spec_from_file_location("rp", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # @dataclass resolves annotations via sys.modules[cls.__module__]; register
    # before exec so the decorator can find this module. (rebuild_progress.py
    # declares `from __future__ import annotations` + @dataclass TaskRow.)
    sys.modules["rp"] = mod
    spec.loader.exec_module(mod)
    return mod


def racer_b(rp, progress: Path, region: str) -> None:
    """The read->write window, through the same entry point production uses.

    See this file's PRE-PORT / POST-PORT FORK header section: the fallback
    branch IS the unlocked baseline arm, and taking it after the port has
    landed means the harness is broken, not that the lock works.
    """
    write_region = getattr(rp, "write_region", None)
    if write_region is not None:
        write_region(progress, region)  # post-port: locked entry point
    else:
        content = rp.read_text(progress) or ""  # pre-port fallback: the inline
        new_content = rp.replace_or_append_region(content, region)  # unlocked sequence
        if new_content != content:
            progress.write_text(new_content, encoding="utf-8")


def classify(final: str) -> str:
    """Most-specific-first; every trial counts exactly once."""
    if final.count(TABLE_BEGIN) >= 2:
        return "torn_dup_table"
    if not final.strip() or ROW_SIGIL not in final:
        return "torn_empty"
    if MARKER not in final or any(h not in final for h in SECTIONS):
        return "torn_lost_freetext"
    return "ok"


def read_final(progress: Path) -> str:
    try:
        # errors="replace": a torn file can end mid-sequence. The probe content
        # is ASCII so this never fires in practice, but a decode blow-up
        # halfway through a sweep would throw away the whole tally.
        return progress.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def main() -> int:
    if len(sys.argv) < 4:
        sys.exit("usage: uv run --script race.py <project_dir> <lo_ms> <hi_ms> [step_ms]")

    project_dir = Path(sys.argv[1]).resolve()
    lo_ms = float(sys.argv[2])
    hi_ms = float(sys.argv[3])
    step_ms = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0

    seed_path = project_dir / SEED_NAME
    if not seed_path.is_file():
        sys.exit(
            f"FATAL: pristine seed not found at {seed_path}\n"
            "Run `uv run --script setup.py <project_dir> [--tasks N] "
            "[--freetext-kb K]` first. The seed is generated there so the "
            "--freetext-kb detection-probability knob flows through to every "
            "trial. Refusing to invent a seed here: a small, locally-invented "
            "progress.md would narrow the truncated-write window and misreport "
            "as a spurious clean pass."
        )
    seed = seed_path.read_text(encoding="utf-8")
    if MARKER not in seed or any(h not in seed for h in SECTIONS):
        sys.exit(
            f"FATAL: seed at {seed_path} is missing the marker or a section "
            "heading that the classifier keys on. It was built by a different "
            "(older or foreign) setup.py. Rebuild the probe tree. Refusing to "
            "run rather than tallying torn_lost_freetext on every trial."
        )

    progress = project_dir / "progress.md"
    rp = load()
    arm = "write_region (post-port: locked vs locked)" if getattr(rp, "write_region", None) else "fallback-unlocked (pre-port BASELINE arm)"

    tally = {"ok": 0, "torn_dup_table": 0, "torn_lost_freetext": 0, "torn_empty": 0}
    hits = []

    delay = lo_ms
    trial = 0
    while delay <= hi_ms:
        progress.write_text(seed, encoding="utf-8")

        # Racer B's prep (the same calls main() makes, in the same order) is
        # hoisted ahead of racer A's launch on purpose: gather_tasks walks the
        # whole task tree and would otherwise eat an unpredictable slice of the
        # spin delay, defeating the point of aiming at a sub-millisecond
        # window. Nothing mutates tasks/ during a trial, so hoisting the read
        # of it changes no outcome; the read->write window itself still runs
        # unhoisted, at the targeted offset.
        progress_path = rp.ensure_progress_md(project_dir)
        by_status = rp.gather_tasks(project_dir)
        region = rp.render_table_region(by_status)

        proc = subprocess.Popen(
            ["uv", "run", "--no-project", str(SCRIPT), str(project_dir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        t0 = time.perf_counter()
        target = t0 + delay / 1000.0
        while time.perf_counter() < target:
            pass  # spin: sleep() granularity on Windows is far coarser than this window
        racer_b(rp, progress_path, region)
        proc.wait()

        outcome = classify(read_final(progress))
        tally[outcome] += 1
        if outcome != "ok":
            hits.append((round(delay, 1), outcome))
        trial += 1
        delay += step_ms

    print(f"racer_b={arm}")
    print(f"trials={trial} {tally}")
    if hits:
        print("hits (delay_ms, outcome):")
        for d, o in hits[:40]:
            print(f"  {d} {o}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
