"""Bind a launched LLM CLI's lifetime to the wfrun process that launched it.

Invariant I2 (xml-wf-kill-safe-cleanup.md §1, §3.4): however this process ends
-- a hard kill (`taskkill /F` without `/T`, TerminateProcess, SIGKILL), a crash
-- the LLM CLI child it was waiting on must not keep working. A child left
behind is not an idle orphan: measured 2026-09-29 on win32 (claude v2.1.270,
haiku), a `claude -p` whose parent was killed with `taskkill /F` finished its
in-flight Bash call, then started and completed the next one and exited 0.
run-cc's `resume` and run-llm's single re-dispatch after `aborted` would then
run the same step a second time alongside it.

The OS mechanics live here and nowhere else. The one public entry point is
`popen_bound(cmd, **popen_kwargs) -> subprocess.Popen`; the returned object is
used exactly like the `subprocess.Popen` it replaces.

- **Windows**: a fresh Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE is
  created before the child and the child is assigned to it right after
  creation. Only this process holds the job handle -- it is created without
  an inheritable security descriptor, so no child can keep the job open.
  However this process ends, the OS closes that handle and terminates every
  process still in the job: the child and every descendant it started after
  the assignment.
  - The binding covers a parent that dies while the child runs, nothing more.
    Once `wait()` (also reached through `communicate()` and the context
    manager) has observed the child's own exit, the kill-on-close limit is
    cleared and the handle closed, so whatever the child left running behaves
    exactly as it did before this module existed. A Popen that is never
    waited keeps its job until this process exits.
  - If the limit cannot be cleared, the handle is kept instead of closed:
    closing it would kill those leftovers now rather than at this process's
    exit.
  - Fail closed: if the child cannot be assigned, it is terminated and
    OSError is raised. A launch that goes ahead unbound would silently drop I2.
  - No CREATE_SUSPENDED. A grandchild the child starts before the assignment
    lands would escape the job. The window is this function's own work between
    CreateProcess and AssignProcessToJobObject, while the first long-lived
    descendant of `claude.exe` appeared 0.419 s after its creation at the
    earliest, and of pi's `node.exe` 1.467 s (six measured launches,
    2026-09-29; the observer missed processes living under 0.2 s).
- **Linux**: `prctl(PR_SET_PDEATHSIG, SIGKILL)` in `preexec_fn`, followed by
  a parent-pid check that closes the race of a parent dying before the prctl.
  - Binds the direct child only: its descendants are not signalled. That still
    satisfies I2, whose harm is the CLI itself carrying on past its in-flight
    tool call.
  - The signal fires when the *thread* that forked exits. Every caller blocks
    in that same thread until the child exits, so this does not fire early.
  - Python documents `preexec_fn` as unsafe in a multi-threaded process (the
    executor runs `<parallel>` branches on threads). The function it runs
    here is a single pre-resolved libc call plus `getppid`.
  - A failing prctl raises inside the child, which Popen reports in the parent
    as SubprocessError: fail closed, as on Windows.
  - NOT run on a Linux host yet (2026-09-29: no Linux host was available);
    its test is skipped everywhere else.
- **macOS and every other POSIX**: no equivalent mechanism; the child is
  launched unbound. Accepted residual (2026-09-29 user ruling).
"""
from __future__ import annotations

import ctypes
import functools
import os
import signal
import subprocess
import sys

__all__ = ["popen_bound"]

# JOBOBJECTINFOCLASS value and limit flags (winnt.h).
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_NO_LIMITS = 0

# Process access rights AssignProcessToJobObject requires (winnt.h).
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100

# prctl option (linux/prctl.h).
_PR_SET_PDEATHSIG = 1


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def popen_bound(cmd, **popen_kwargs) -> subprocess.Popen:
    """`subprocess.Popen(cmd, **popen_kwargs)` whose child cannot outlive a
    kill of this process (Windows, Linux); unbound elsewhere."""
    if os.name == "nt":
        return _popen_in_kill_job(cmd, popen_kwargs)
    if sys.platform.startswith("linux"):
        return _popen_with_pdeathsig(cmd, popen_kwargs)
    return subprocess.Popen(cmd, **popen_kwargs)


# --- Windows: Job Object -------------------------------------------------

@functools.lru_cache(maxsize=None)
def _kernel32():
    # A private WinDLL instance, so these prototypes never clash with
    # another module's use of ctypes.windll.kernel32.
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    k32.SetInformationJobObject.argtypes = (
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
    k32.SetInformationJobObject.restype = ctypes.c_int
    k32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    k32.AssignProcessToJobObject.restype = ctypes.c_int
    k32.CloseHandle.argtypes = (ctypes.c_void_p,)
    k32.CloseHandle.restype = ctypes.c_int
    return k32


def _set_job_limits(job, limit_flags: int) -> bool:
    info = _ExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = limit_flags
    return bool(_kernel32().SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info), ctypes.sizeof(info)))


def _create_kill_job():
    k32 = _kernel32()
    # NULL security attributes: the handle is not inheritable.
    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    if not _set_job_limits(job, _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE):
        error = ctypes.WinError(ctypes.get_last_error())
        k32.CloseHandle(job)
        raise error
    return job


def _assign_to_job(job, pid: int) -> None:
    k32 = _kernel32()
    process = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not k32.AssignProcessToJobObject(job, process):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        k32.CloseHandle(process)


class _JobBoundPopen(subprocess.Popen):
    """A Popen whose child stays in a kill-on-close job until `wait()` has
    seen it exit."""

    _job = None

    def wait(self, timeout=None):
        returncode = super().wait(timeout=timeout)
        self._release_job()
        return returncode

    def _release_job(self) -> None:
        job, self._job = self._job, None
        if job is None:
            return
        if _set_job_limits(job, _JOB_OBJECT_NO_LIMITS):
            _kernel32().CloseHandle(job)


def _popen_in_kill_job(cmd, popen_kwargs: dict) -> subprocess.Popen:
    job = _create_kill_job()
    try:
        proc = _JobBoundPopen(cmd, **popen_kwargs)
    except BaseException:
        _kernel32().CloseHandle(job)
        raise

    try:
        _assign_to_job(job, proc.pid)
    except OSError:
        proc.kill()
        with proc:  # closes the pipes and reaps the child
            pass
        _kernel32().CloseHandle(job)
        raise

    proc._job = job
    return proc


# --- Linux: parent-death signal -----------------------------------------

@functools.lru_cache(maxsize=None)
def _prctl():
    prctl = ctypes.CDLL(None, use_errno=True).prctl
    prctl.argtypes = (ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                      ctypes.c_ulong, ctypes.c_ulong)
    prctl.restype = ctypes.c_int
    return prctl


def _popen_with_pdeathsig(cmd, popen_kwargs: dict) -> subprocess.Popen:
    if "preexec_fn" in popen_kwargs:
        raise TypeError("popen_bound sets preexec_fn itself")

    # Resolved here, before the fork, so the child only makes the call.
    prctl = _prctl()
    parent_pid = os.getpid()

    def bind_to_parent():
        if prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")

        # The parent died between fork and prctl: the signal will never come.
        if os.getppid() != parent_pid:
            os.kill(os.getpid(), signal.SIGKILL)

    return subprocess.Popen(cmd, preexec_fn=bind_to_parent, **popen_kwargs)
