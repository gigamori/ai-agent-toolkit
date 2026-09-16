"""qmd_index.py — incremental qmd index follow-up (imported by session_progress_capture.py).

The project's qmd index falls behind the tree whenever a round writes into
project-notes/. This module is the driver that closes that gap, in the two
halves the index has:

    lexical   `qmd update`          cheap (0.23 s measured), run SYNCHRONOUSLY
    vector    `qmd embed`           expensive, spawned DETACHED. GPU by
                                    default (789 chunks / 82 docs in
                                    93.9 s measured); `--no-gpu` is the
                                    fallback a GPU run that achieved
                                    nothing escalates into (~4-5
                                    chunks/min measured).

Design: `project-notes/specs/notes-retrieval-rollout-plan.md` §4 「増分」.

Level, not edge. The embed fires on the STATE "the index is behind the tree",
read from `qmd status` (0.26 s measured), not on the EVENT "this round wrote a
note". An edge trigger loses every embed the lock declines: nothing re-drives it
until some later round happens to write another note, and an index can sit docs
short for hours that way. The lexical `qmd update` keeps the edge, because the
status number only means anything once the tree's own changes are in the index.

`pending` read off an index this project does not own would make that state
permanently true, so the probe is gated on a LOCAL `.qmd/index.y*ml`: without
one, qmd walks UP and answers for an ancestor's or the global index. No local
config means no probe, and the follow-up falls back to the edge it had before.

Blast cap, and the retry the level trigger needs bounding. Concurrency is
capped at ONE embed per index by `<project-notes>/.qmd/embed.lock`, a JSON file
holding the spawned pid, the pending count that spawn was meant to clear, the
mode that spawn ran in (`gpu` / `cpu`), and the session tag and round that
spawned it. Two gates read it:

    pid ALIVE                     an embed is running, possibly from another
                                  session — LOCKED
    pid dead, pending UNCHANGED   the last embed cleared nothing. ONE
                                  escalation follows: a `gpu` run that
                                  achieved nothing is retried on CPU, and a
                                  `cpu` run that achieved nothing is
                                  withheld — FUTILE

The second gate exists only because the trigger is a level: a doc qmd cannot
embed (unparseable, oversized, model mismatch) leaves `pending` true forever,
and without it every later Stop would spawn the same failing run. Its CPU arm
covers the other way a run achieves nothing — a machine whose GPU backend
aborts the embed outright — without making CPU a standing preference: the
lock records which device ONE run was ASKED for, never what the machine CAN
do, and not necessarily what qmd then ran on (an inherited `QMD_FORCE_CPU` or
`QMD_LLAMA_GPU=false` overrides the flag; the escalation still terminates,
one run later). A pending count
that FELL is progress and retries; one that ROSE is new work and retries; both
retry on GPU whatever mode they inherit, because nothing signals a GPU coming
back and a sticky CPU would hold a repaired machine at ~4-5 chunks/min forever.
The price is one aborted GPU spawn per CPU batch, paid back by the next Stop.
An unknown count (an older lock with no `pending` key, or a Stop that could not
read one) retries, and an unknown mode (a lock written before that key existed)
retries on GPU, so the guard can only ever withhold a run it has evidence is
futile. The sid/round pair is recorded for diagnostics and gates nothing — a
per-round gate would re-open the very hole the edge trigger left. The lock is
never deleted: liveness is decided from the pid, so a leftover file is
self-clearing and no cleanup path can race the spawn.

Cost. A probed project pays one `qmd status` per Stop. Every probe in one Stop
shares a single wall-clock budget (FOLLOW_BUDGET_S), so the project count cannot
multiply the Stop's latency; a project reached after it is spent falls back to
the edge.

Config. The follow-up rides the retrieval layer's own axis: `TASKFLOW_NOTES_PUSH`
must be `auto` (its default) for anything at all to start. `off` and `lexical`
are the two ways a user says "do not run qmd for this tree", and a maintenance
write is no more exempt from that than the query path is. No axis of its own is
added.

`qmd embed -f` regenerates every embedding in the collection. It is never
reachable from here — see EMBED_ARGS.

stdlib only: hooks run under `uv run --no-project`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from notes_push import MODE_ENV, MODES, QMD_DIR, resolve_qmd_bin  # noqa: E402

NOTES_DIR_NAME = 'project-notes'
LOCK_NAME = 'embed.lock'

# Seconds allowed for the synchronous `qmd update`. Deliberately NOT
# `notes_push.resolve_qmd_timeout`: that axis is documented as the ceiling for
# one qmd QUERY and is a shared contract constant with the Pi extension. This
# is the Stop hook's own ceiling and the spec fixes it at 5 s.
UPDATE_TIMEOUT_S = 5.0

# Seconds allowed for one `qmd status` probe, and the wall-clock budget every
# probe in one Stop shares. The probe measured 0.26 s against an 80-doc /
# 8.4 MB index, so the per-probe ceiling is ~11x the measured cost, and the
# worst case this module can add to a Stop is UPDATE_TIMEOUT_S + FOLLOW_BUDGET_S.
STATUS_TIMEOUT_S = 3.0
FOLLOW_BUDGET_S = 4.0

UPDATE_ARGS = ('update',)
STATUS_ARGS = ('status',)
# `EMBED_ARGS` is the GPU form — qmd's own default, and what every embed
# runs in until one proves futile. `NO_GPU_FLAG` is appended only on the
# escalation, so a machine whose GPU backend cannot embed still gets vectors
# instead of silently getting none. `-f` is absent and must stay absent — it
# rebuilds every embedding in the collection.
EMBED_ARGS = ('embed',)
NO_GPU_FLAG = '--no-gpu'

# Which device one embed run used, recorded in the lock under `mode`.
EMBED_GPU = 'gpu'
EMBED_CPU = 'cpu'

# Windows process-creation flags (winbase.h). DETACHED_PROCESS drops the
# console, CREATE_NEW_PROCESS_GROUP keeps a Ctrl-C in the parent's console off
# the child, CREATE_BREAKAWAY_FROM_JOB leaves the parent's job object so a job
# configured to kill on close does not take the embed down with the hook.
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

AUTO_MODE = 'auto'

# `qmd status` parsing, TRANSCRIBED from `scripts/check_progress.py`
# (QMD_LOCAL_CONFIGS / QMD_TOTAL_RE / QMD_PENDING_RE / parse_qmd_status) rather
# than imported: `scripts/` and `hooks/` are standalone, and that file already
# duplicates `resolve_qmd_bin` out of `notes_push.py` on the same rule. A hook
# importing from scripts/ would invert the one dependency direction this plugin
# keeps. The two copies are a propagation pair from here on.
QMD_LOCAL_CONFIGS = ('index.yaml', 'index.yml')
QMD_TOTAL_RE = re.compile(r'^\s*Total:\s+(\d+)\b', re.MULTILINE)
QMD_PENDING_RE = re.compile(r'^\s*(?:\x1b\[[0-9;]*m)?Pending:\s+(\d+)\b',
                            re.MULTILINE)

# Whether THIS round wrote into the project's project-notes/. It decides one
# thing only: whether the lexical `qmd update` runs before the status probe.
NOTES_WRITTEN = 'notes-written'
NOTES_UNCHANGED = 'notes-unchanged'

# follow_index statuses.
NOT_AUTO = 'not-auto'
NO_QMD = 'no-qmd'
NO_LOCAL_INDEX = 'no-local-index'
NO_BIN = 'no-bin'
STATUS_FAILED = 'status-failed'
NO_BUDGET = 'no-budget'
CURRENT = 'current'
LOCKED = 'locked'
FUTILE = 'futile-retry'
EMBEDDING = 'embedding'
EMBEDDING_CPU = 'embedding-cpu'
EMBED_FAILED = 'embed-failed'

# What the Stop hook prints. Started, blocked and failed earn a line; the
# steady-state no-ops do not — `not-auto` / `no-qmd` / `no-local-index` say the
# tree is none of this module's business, and `current` is the ordinary answer
# on an index already caught up, so printing either would put a line on every
# Stop of every session.
LOUD_STATUSES = (NO_BIN, STATUS_FAILED, NO_BUDGET, LOCKED, FUTILE, EMBEDDING,
                 EMBEDDING_CPU, EMBED_FAILED)

# Statuses that end the caller's loop over projects. NOT_AUTO and NO_BIN are
# env-wide answers the next project would only repeat; LOCKED preserves the
# one-embed-at-a-time cap; EMBEDDING / EMBEDDING_CPU / EMBED_FAILED are this
# Stop's one spawn.
# Everything else is a per-project answer and the loop moves on — FUTILE
# included: nothing is running and this Stop's one spawn is still unspent, so
# another project may still use it.
HALT_STATUSES = (NOT_AUTO, NO_BIN, LOCKED, EMBEDDING, EMBEDDING_CPU,
                 EMBED_FAILED)


def resolve_mode(env: dict | None = None) -> str:
    """`TASKFLOW_NOTES_PUSH`, normalized the way `notes_push.build_notes_push`
    normalizes it: stripped, lowercased, and anything outside `MODES` — an unset
    or empty value included — falling back to `auto`.

    Transcribed from that call site rather than re-derived, and it shares that
    site's `MODE_ENV` / `MODES` constants, so the retrieval layer and this index
    follow-up can never disagree about what a mode string means. The rule itself
    is not a function there; extracting one would have edited a file this change
    was not asked to touch.
    """
    env = os.environ if env is None else env
    mode = env.get(MODE_ENV, AUTO_MODE).strip().lower()
    if mode not in MODES:
        mode = AUTO_MODE
    return mode


def notes_dir_of(project_root: str) -> str:
    return os.path.join(project_root, NOTES_DIR_NAME)


def qmd_dir_of(project_root: str) -> str:
    return os.path.join(notes_dir_of(project_root), QMD_DIR)


def has_local_index(project_root: str) -> bool:
    """True when `.qmd/` carries qmd's own local config. Without one, qmd's
    config lookup walks UP and `status` answers for an ancestor's or the global
    `~/.cache` index — another collection's `pending` under this project's name,
    which a level trigger would read as "behind" forever."""
    qmd_dir = qmd_dir_of(project_root)
    return any(os.path.isfile(os.path.join(qmd_dir, name))
               for name in QMD_LOCAL_CONFIGS)


def has_qmd(project_root: str) -> bool:
    """True when the project is onboarded to qmd. The whole follow-up is gated
    on this: a project without `.qmd/` must be left exactly as it is."""
    return os.path.isdir(qmd_dir_of(project_root))


def lock_path_of(project_root: str) -> str:
    return os.path.join(qmd_dir_of(project_root), LOCK_NAME)


def _pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False

    if os.name != 'nt':
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # foreign owner — the process exists
        except OSError:
            return False
        return True

    # Windows has no signal-0 probe: CPython's os.kill maps every signal other
    # than CTRL_C_EVENT / CTRL_BREAK_EVENT to TerminateProcess, so `os.kill(pid,
    # 0)` would KILL the embed it is asked about. Ask Win32 directly instead.
    import ctypes

    synchronize = 0x00100000
    wait_timeout = 0x00000102
    try:
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        k32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        k32.CloseHandle.argtypes = (ctypes.c_void_p,)
    except (OSError, AttributeError):
        return False

    handle = k32.OpenProcess(synchronize, False, pid)
    if not handle:
        return False  # no such process (its last handle is gone with it)

    try:
        return k32.WaitForSingleObject(handle, 0) == wait_timeout
    finally:
        k32.CloseHandle(handle)


def read_lock(path: str) -> dict | None:
    """The lock's recorded {pid, sid, round}, or None when it is absent or
    unreadable. Unparseable content reads as absent — a corrupt lock must not
    wedge the follow-up forever."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def lock_blocks(info: dict | None) -> bool:
    """Whether an existing lock forbids spawning an embed now: the recorded pid
    is ALIVE, i.e. an embed is already running for this index, possibly from
    another session (多重起動).

    The recorded sid/round is deliberately NOT consulted. It was a second gate
    while the trigger was an edge; with the level trigger the index's own
    `pending` retires a finished embed's work, and a round-shaped gate would
    only refuse the retry a declined embed needs. It stays in the file as
    diagnostics.
    """
    return bool(info) and _pid_alive(info.get('pid'))


def retry_is_futile(info: dict | None, pending: int | None) -> bool:
    """Whether spawning now would only repeat a run that already achieved
    nothing: the recorded embed is gone and it left EXACTLY the pending count it
    was started against.

    Every uncertain case retries. No lock, a lock written before this key
    existed, a non-integer value, or a pending count this Stop could not read
    (`None`) all fall through to False, so the guard withholds a run only on
    evidence that the identical run has already failed once.
    """
    if not info or not isinstance(pending, int):
        return False
    was = info.get('pending')
    return isinstance(was, int) and was == pending


def next_embed_mode(info: dict | None, pending: int | None) -> str | None:
    """Which mode the next embed runs in, or None to withhold it.

    ONE step of escalation, driven by the same nothing-achieved signal
    `retry_is_futile` reads: a run that achieved nothing on the GPU is
    retried on the CPU, and a run that achieved nothing on the CPU is the
    futile retry the level trigger needs withheld.

    Everything else starts on the GPU — progress, new work, no lock, an
    unknown count, and a lock written before the mode key existed. CPU is
    never a preference this module carries forward: the lock says which
    device one run was ASKED for, and a machine whose GPU starts working
    raises no signal of its own, so a sticky CPU would never be left.
    """
    if not retry_is_futile(info, pending):
        return EMBED_GPU

    was = (info or {}).get('mode')
    if was == EMBED_CPU:
        return None
    return EMBED_CPU if was == EMBED_GPU else EMBED_GPU


def write_lock(path: str, pid: int, sid_tag: str, round_n: int,
               pending: int | None, mode: str) -> None:
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'pid': pid, 'sid': sid_tag, 'round': round_n,
                       'pending': pending, 'mode': mode}, f)
    except OSError:
        pass  # the follow-up is best-effort; an unwritable lock only costs a cap


def run_update(bin_path: str, notes_dir: str, runner=subprocess.run) -> bool:
    """Synchronous `node <bin/qmd> update`. Never raises: every failure mode
    (missing node, non-zero exit, timeout) is a silent skip."""
    cmd = ['node', bin_path, *UPDATE_ARGS]
    try:
        cp = runner(cmd, cwd=notes_dir, capture_output=True,
                    timeout=UPDATE_TIMEOUT_S, encoding='utf-8',
                    errors='replace')
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return getattr(cp, 'returncode', 1) == 0


def parse_qmd_status(stdout: str) -> tuple[int, int] | None:
    """(docs with usable vectors, docs indexed) from `qmd status`, or None when
    the output is not the shape this parser knows.

    `Total:` is the active document count. `Pending:` counts those docs whose
    vectors are missing or incomplete for the embed model in force, and the line
    is omitted entirely at zero. `Vectors:` is deliberately NOT used - it counts
    chunks (789 for 82 docs on the reference corpus), not documents."""
    total_m = QMD_TOTAL_RE.search(stdout)
    if not total_m:
        return None
    total = int(total_m.group(1))
    pending_m = QMD_PENDING_RE.search(stdout)
    pending = int(pending_m.group(1)) if pending_m else 0
    if pending > total:
        return None
    return total - pending, total


def run_status(bin_path: str, notes_dir: str, timeout_s: float,
               runner=subprocess.run) -> tuple[int, int] | None:
    """Synchronous `node <bin/qmd> status`, parsed. None on every arm in which
    the number would be absent or wrong: spawn failure, non-zero exit, timeout,
    and output the parser does not recognise. Never raises."""
    cmd = ['node', bin_path, *STATUS_ARGS]
    try:
        cp = runner(cmd, cwd=notes_dir, capture_output=True, timeout=timeout_s,
                    encoding='utf-8', errors='replace')
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if getattr(cp, 'returncode', 1) != 0:
        return None
    return parse_qmd_status(getattr(cp, 'stdout', '') or '')


def new_budget() -> float:
    """Deadline for ONE Stop's whole follow-up, shared by every project probed
    in it. The caller hands it to `follow_index`, so N projects cost one budget
    rather than N ceilings."""
    return time.monotonic() + FOLLOW_BUDGET_S


def pending_docs(project_root: str, bin_path: str, notes_dir: str,
                 deadline: float | None, runner=subprocess.run
                 ) -> tuple[int | None, str | None]:
    """(pending docs, None) when the index's lag is known, else (None, status)
    naming why it is not. Exactly one half is ever set."""
    if not has_local_index(project_root):
        return None, NO_LOCAL_INDEX

    budget = STATUS_TIMEOUT_S
    if deadline is not None:
        budget = min(budget, deadline - time.monotonic())
    if budget <= 0:
        return None, NO_BUDGET

    parsed = run_status(bin_path, notes_dir, budget, runner=runner)
    if parsed is None:
        return None, STATUS_FAILED

    covered, total = parsed
    return total - covered, None


def spawn_embed(bin_path: str, notes_dir: str, mode: str,
                popen=subprocess.Popen) -> int:
    """Start `node <bin/qmd> embed` detached and return its pid. `mode`
    picks the device: EMBED_GPU leaves qmd on its own default, EMBED_CPU
    appends `--no-gpu`.

    Detached means: no inherited stdio, its own session/process group, and — on
    Windows — a breakaway attempt from the parent's job object, since a hook
    launched inside a kill-on-close job would otherwise lose the embed the
    moment the Stop hook exits. A job that forbids breakaway fails the call with
    ERROR_ACCESS_DENIED, so the flag is dropped and retried rather than turning
    the whole follow-up off on machines where breakaway is not permitted.
    """
    args = EMBED_ARGS + ((NO_GPU_FLAG,) if mode == EMBED_CPU else ())
    cmd = ['node', bin_path, *args]
    kwargs = dict(cwd=notes_dir, stdin=subprocess.DEVNULL,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  close_fds=True)

    if os.name != 'nt':
        return popen(cmd, start_new_session=True, **kwargs).pid

    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        return popen(cmd, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB,
                     **kwargs).pid
    except OSError:
        return popen(cmd, creationflags=flags, **kwargs).pid


def follow_index(project_root: str, sid_tag: str, round_n: int,
                 notes: str = NOTES_UNCHANGED, env: dict | None = None,
                 runner=subprocess.run, popen=subprocess.Popen,
                 deadline: float | None = None) -> str:
    """Bring one project's qmd index up to date.

    `notes` (NOTES_WRITTEN / NOTES_UNCHANGED) says whether THIS round wrote into
    that project's project-notes/, and decides the lexical half only. The embed
    is decided by how far the index is behind, so a round that wrote nothing
    still spawns one while docs are pending — that is what re-drives an embed a
    previous round's lock declined.

    `sid_tag` / `round_n` are recorded in the lock as diagnostics and gate
    nothing; `round_n` is the round in force when the caller runs this, i.e. the
    one the session state file goes on to carry.

    Returns a status (NOT_AUTO / NO_QMD / NO_LOCAL_INDEX / NO_BIN /
    STATUS_FAILED / NO_BUDGET / CURRENT / LOCKED / FUTILE / EMBEDDING /
    EMBEDDING_CPU / EMBED_FAILED). EMBEDDING_CPU is the escalated spawn, and
    it is loud for the same reason the others are: a mode that changes
    silently is a mode nobody notices. Raises nothing: the Stop path is
    not allowed to fail on index maintenance.

    `update` runs even when the embed lock blocks. The lock caps CPU-bound
    embedding, and holding the cheap lexical refresh behind a possibly
    minutes-long embed would leave the freshest notes out of the `lex:` layer
    for that entire window — the one thing `update` alone repairs.
    """
    if resolve_mode(env) != AUTO_MODE:
        return NOT_AUTO

    if not has_qmd(project_root):
        return NO_QMD

    bin_path = resolve_qmd_bin(env)
    if not bin_path:
        return NO_BIN

    notes_dir = notes_dir_of(project_root)
    if notes == NOTES_WRITTEN:
        run_update(bin_path, notes_dir, runner=runner)

    # No usable number keeps the edge this module had before, so an unreadable
    # status is never worse than the behaviour it replaced and never turns into
    # an embed spawned on every Stop of its own accord.
    pending, unknown = pending_docs(project_root, bin_path, notes_dir,
                                    deadline, runner=runner)
    if unknown is not None:
        if notes != NOTES_WRITTEN:
            return unknown
    elif pending <= 0:
        return CURRENT

    lock = lock_path_of(project_root)
    info = read_lock(lock)
    if lock_blocks(info):
        return LOCKED

    mode = next_embed_mode(info, pending)
    if mode is None:
        return FUTILE

    try:
        pid = spawn_embed(bin_path, notes_dir, mode, popen=popen)
    except (OSError, ValueError):
        return EMBED_FAILED

    write_lock(lock, pid, sid_tag, round_n, pending, mode)
    return EMBEDDING_CPU if mode == EMBED_CPU else EMBEDDING
