"""qmd_index.py — incremental qmd index follow-up (imported by session_progress_capture.py).

A round that WROTE project-notes leaves the project's qmd index behind the tree.
This module is the driver that closes that gap, in the two halves the index has:

    lexical   `qmd update`          cheap (0.23 s measured), run SYNCHRONOUSLY
    vector    `qmd embed --no-gpu`  expensive (~1.9 min per changed note,
                                    extrapolated), spawned DETACHED

Design: `project-notes/specs/notes-retrieval-rollout-plan.md` §4 「増分」.

Blast cap. The detached half is capped at ONE embed per round by
`<project-notes>/.qmd/embed.lock`, a JSON file holding the spawned pid plus the
session tag and round number that spawned it. The lock blocks when the recorded
pid is still alive (another embed is running, possibly from another session) or
when it names THIS session's THIS round (the round already spawned its one
embed). Anything else is stale and the lock is retaken. The lock is never
deleted: liveness is decided from the pid, so a leftover file is self-clearing
and no cleanup path can race the spawn.

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
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from notes_push import MODE_ENV, MODES, QMD_DIR, resolve_qmd_bin  # noqa: E402

NOTES_DIR_NAME = 'project-notes'
LOCK_NAME = 'embed.lock'

# Seconds allowed for the synchronous `qmd update`. Deliberately NOT
# `notes_push.resolve_qmd_timeout`: that axis is documented as the ceiling for
# one qmd QUERY and is a shared contract constant with the Pi extension. This
# is the Stop hook's own ceiling and the spec fixes it at 5 s.
UPDATE_TIMEOUT_S = 5.0

UPDATE_ARGS = ('update',)
# `--no-gpu` is part of the contract, not a preference: the Stop path must not
# contend for the GPU a foreground session may be using. `-f` is absent and must
# stay absent — it rebuilds every embedding in the collection.
EMBED_ARGS = ('embed', '--no-gpu')

# Windows process-creation flags (winbase.h). DETACHED_PROCESS drops the
# console, CREATE_NEW_PROCESS_GROUP keeps a Ctrl-C in the parent's console off
# the child, CREATE_BREAKAWAY_FROM_JOB leaves the parent's job object so a job
# configured to kill on close does not take the embed down with the hook.
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

AUTO_MODE = 'auto'

# follow_index statuses.
NOT_AUTO = 'not-auto'
NO_QMD = 'no-qmd'
NO_BIN = 'no-bin'
LOCKED = 'locked'
EMBEDDING = 'embedding'
EMBED_FAILED = 'embed-failed'


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


def lock_blocks(info: dict | None, sid_tag: str, round_n: int) -> bool:
    """Whether an existing lock forbids spawning an embed now.

    Two independent reasons, both required by §4:
      - the recorded pid is ALIVE: an embed is already running (多重起動).
      - the lock names this session's current round: that round has already had
        its one embed, alive or not (blast cap 1 round 1 本).
    """
    if not info:
        return False
    if _pid_alive(info.get('pid')):
        return True
    return (info.get('sid') == sid_tag
            and info.get('round') == round_n)


def write_lock(path: str, pid: int, sid_tag: str, round_n: int) -> None:
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'pid': pid, 'sid': sid_tag, 'round': round_n}, f)
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


def spawn_embed(bin_path: str, notes_dir: str, popen=subprocess.Popen) -> int:
    """Start `node <bin/qmd> embed --no-gpu` detached and return its pid.

    Detached means: no inherited stdio, its own session/process group, and — on
    Windows — a breakaway attempt from the parent's job object, since a hook
    launched inside a kill-on-close job would otherwise lose the embed the
    moment the Stop hook exits. A job that forbids breakaway fails the call with
    ERROR_ACCESS_DENIED, so the flag is dropped and retried rather than turning
    the whole follow-up off on machines where breakaway is not permitted.
    """
    cmd = ['node', bin_path, *EMBED_ARGS]
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
                 env: dict | None = None, runner=subprocess.run,
                 popen=subprocess.Popen) -> str:
    """Bring one project's qmd index up to date after a note-writing round.

    Returns a status (NOT_AUTO / NO_QMD / NO_BIN / LOCKED / EMBEDDING /
    EMBED_FAILED). Raises nothing: the Stop path is not allowed to fail on index
    maintenance.

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
    run_update(bin_path, notes_dir, runner=runner)

    lock = lock_path_of(project_root)
    if lock_blocks(read_lock(lock), sid_tag, round_n):
        return LOCKED

    try:
        pid = spawn_embed(bin_path, notes_dir, popen=popen)
    except (OSError, ValueError):
        return EMBED_FAILED

    write_lock(lock, pid, sid_tag, round_n)
    return EMBEDDING
