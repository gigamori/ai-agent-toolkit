# lockedrebuild — write-lock v2 acceptance harness

Races **two concurrent runs of `plugins/taskflow/scripts/rebuild_progress.py`
against each other on the same `progress.md`**.

This is the "locked rebuild vs locked rebuild" pairing that the write-lock v2
pass criterion is actually about, and which no harness covered before.

## Why a lost-update framing does not work here

A naive "two rebuilds race each other" **cannot** detect a lost update. Both
processes read the same free text and write the same free text back, so the
interleaving is unobservable: whoever writes last writes byte-identical
content. There is no `lost_*` outcome to tally, and inventing one would produce
a harness that reports 0 losses forever regardless of whether a lock exists.

## The torn-file redefinition

The real, detectable corruption is a **torn file**:

> `Path.write_text` truncates, then writes. If racer A reads while racer B is
> mid-write, A reads truncated content with no `<!-- @table:begin -->` marker.
> `replace_or_append_region` then takes its no-marker branch (Risk R7) and
> **appends a second table region** at the end. The result is a `progress.md`
> with two `@table:begin` markers — which taskflow explicitly forbids — and/or
> free-text sections that the truncation ate. The next rebuild collapses the
> extra region; the eaten free text it cannot restore, which is why both are
> classified.

Each trial is therefore classified by the **structural integrity of the final
file**, not by "whose write won":

| outcome | condition |
|---|---|
| `ok` | exactly one `@table:begin`, all four section headings present, marker present, `probe task` rows present |
| `torn_dup_table` | two or more `@table:begin` |
| `torn_lost_freetext` | any section heading, or the marker, missing |
| `torn_empty` | file empty, or no `probe task` rows present |

The chain is ordered most-specific-first so a trial tripping several conditions
counts exactly once: `torn_dup_table` beats `torn_lost_freetext` (an
appended-second-region file usually also lost free text, but the duplicate
region is the sharper, structurally-forbidden signature), and `torn_empty`
beats `torn_lost_freetext` (a file with nothing in it trivially has no
headings; calling that "lost freetext" would understate it).

## Racers

**Racer A — the REAL subprocess**, invoked exactly as
`hooks/task_rebuild_progress.py` invokes it:

```
uv run --no-project <plugins/taskflow/scripts/rebuild_progress.py> <project_dir>
```

**Racer B — the same module driven in-process**, loaded via
`importlib.util.spec_from_file_location` (the module is registered as
`sys.modules["rp"]` *before* `exec_module`, because `@dataclass TaskRow`
resolves its annotations through `sys.modules[cls.__module__]`).

A subprocess-vs-subprocess race is **not usable** here: `uv run` plus
interpreter startup costs ~261–295 ms with a 13–183 ms jitter spread, while the
read→write window is only ~0.3–0.6 ms. A start-offset sweep cannot aim at a
sub-millisecond window through two orders of magnitude more jitter. Driving
racer B in-process is what lets a precise spin delay (`while
time.perf_counter() < target: pass` — Windows `sleep()` granularity is far
coarser than this window) hit the window at all.

Racer B's prep (`ensure_progress_md` → `gather_tasks` → `render_table_region`)
is hoisted ahead of racer A's launch so the tree walk does not eat an
unpredictable slice of the spin delay. Nothing mutates `tasks/` during a trial,
so hoisting that read changes no outcome; the read→write window itself still
runs unhoisted, at the targeted offset.

## The pre-port / post-port fork in racer B

Racer B must execute the read→write window through the **same entry point
production code uses**, so that when the lock lands, racer B is locked too. It
forks on whether `rebuild_progress.py` exports `write_region`:

```python
write_region = getattr(rp, "write_region", None)
if write_region is not None:
    write_region(progress, region)      # post-port: locked entry point
else:
    content = rp.read_text(progress) or ""                       # pre-port fallback:
    new_content = rp.replace_or_append_region(content, region)   # the inline, unlocked
    if new_content != content:                                   # sequence from main()
        progress.write_text(new_content, encoding="utf-8")
```

* `write_region` **present** → post-port. Both racers go through the locked
  entry point. This is the *locked vs locked* arm, and the one the pass
  criterion (0 torn) applies to.
* `write_region` **absent** → pre-port. Racer B runs the inline, **unlocked**
  sequence copied from `main()`. **This fallback branch IS the baseline arm**,
  and it is expected to be able to tear.

`write_region` landed with the protocol v2 port, so the post-port arm is the
one you should see today. Which arm ran is printed on every run as the first
output line.

> **If you see `racer_b=fallback-unlocked` after the port has landed, that is a
> harness-broken signal, not a pass.** It means racer B stopped going through
> the locked entry point, so the run measured nothing about the lock. A 0-torn
> tally from the fallback arm post-port must be discarded, not reported.

## `--freetext-kb` — the detection-probability knob

`setup.py --freetext-kb K` (default 256) pads the seeded `progress.md`'s
free-text sections to roughly K KiB.

`write_text` truncates before it writes, so **the bigger the file, the longer
it spends in a truncated/partial state, and the wider the window a torn read
must land inside.** A small probe file can make a real, unlocked build look
clean simply because nothing ever observed the truncation. Turn this up (MBs
are legitimate) before concluding "0 torn".

`--tasks N` (default 2000) is a different knob: it widens the *compute* half of
racer A's run (`gather_tasks` walks every task file before reading
`progress.md`), pushing A's write later in wall-clock time and making the delay
sweep easier to aim.

## Commands

```sh
cd plugins/taskflow/tests/race/lockedrebuild

# 1. build the probe tree (defaults: --tasks 2000 --freetext-kb 256)
uv run --script setup.py <project_dir> [--tasks N] [--freetext-kb K]

# 2. sweep the race
uv run --script race.py <project_dir> <lo_ms> <hi_ms> [step_ms]
```

`project_dir` is optional for `setup.py` and defaults to the temp location
below; `race.py` requires it. Pass the same path to both.

Example:

```sh
uv run --script setup.py "$TEMP/cc-taskflow-race/lockedrebuild/_projects/probe-proj" --tasks 2000 --freetext-kb 4096
uv run --script race.py  "$TEMP/cc-taskflow-race/lockedrebuild/_projects/probe-proj" 530 640 1
```

### Aim the sweep — do not copy a band blindly

The delay is measured from racer A's *launch*, and almost all of A's lifetime is
`uv run` plus interpreter startup. **A 0–3 ms sweep never reaches A's write at
all** and will report a meaningless clean run. Measure first, then sweep.

Recorded on one Windows dev machine at `--tasks 2000 --freetext-kb 4096`
(a ~4 MB `progress.md`), as an order-of-magnitude guide only — this band is a
function of that machine's `uv` startup plus the 2000-file tree walk, and it
moved by ~20–40 ms between two runs on the same machine:

| | |
|---|---|
| A truncates `progress.md` to 0 bytes | ~561–662 ms after launch |
| the file stays truncated | ~4–6 ms per trial |
| unlocked baseline (pre-port arm) | 2 torn / 222 in-band trials (0.90%) |
| locked (post-port arm) | 0 torn / 333 in-band trials |

At a ~1% per-trial tear rate, a few dozen trials prove nothing either way:
drawing 0 torn from 333 trials happens ~5% of the time by luck alone. Budget
several hundred **in-band** trials before reading anything into a clean run.

`setup.py` writes the pristine seed to `progress.seed.md` inside the probe
tree; `race.py` reseeds from that file before every trial, so the
`--freetext-kb` knob flows through to the whole sweep instead of being
duplicated in two places.

Both scripts are PEP 723 scripts run via `uv run --script`. `race.py` declares
`dependencies = ["pyyaml"]` because it imports `rebuild_progress.py` in-process;
`setup.py` only writes files and needs none.

Both scripts locate `rebuild_progress.py` by a **relative** path from
`__file__` (`tests/race/lockedrebuild/` → `../../../scripts/rebuild_progress.py`).
Unlike the Pi-side harnesses there is no `TASKFLOW_CC_PLUGIN_DIR`, and none
must be introduced. If the script is not found, both **refuse to run** rather
than degrade: with racer A no-opping and racer B falling back, every trial
would come out `ok` and the run would misreport as a spurious clean pass
instead of a missing dependency.

## Safety

Everything runs in the OS temp dir. The default probe location is
`<tmpdir>/cc-taskflow-race/lockedrebuild/_projects/probe-proj`. The harness
never touches the repo's `_projects/`, `plugins/taskflow/_projects/`, or any
real project tree.

`setup.py` `rmtree()`s its target, so it refuses any pre-existing directory
that does not carry the `.lockedrebuild-probe` stamp file it writes — a
mistyped `project_dir` cannot delete a real `_projects/<project>/`.

## What this harness does NOT cover

* **The Edit-tool path.** Agent-authored writes to `progress.md` via the Edit
  tool take no lock, by design; that is a **permanently unfixable R-lock gap**
  and is covered by the Pi-side `test/race/lostupdate/` harness, not this one.
  A green run here does not close it.
* `tasks/*.md` log regions (`lograce`) and project notes (`noterace`).
* **Absence of the defect.** Detection is probabilistic and depends on
  `--freetext-kb`. A 0-torn tally is only meaningful next to a non-zero tally
  from the same delay band and the same `--freetext-kb` on the unlocked
  baseline arm.
