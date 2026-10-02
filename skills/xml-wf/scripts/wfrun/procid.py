"""Identify a process by its PID and creation time, and tell whether that
same process still runs.

A PID names a process only while it runs: once the process exits, the OS may
give the number to another one. So a process is recorded as (PID, creation
time), and it counts as running only while a live process carries both.
Every doubt counts as running: a process that cannot be opened for any reason
other than "no such process", or whose creation time cannot be read, may
still be the recorded one.

The OS mechanics live here and nowhere else. The public entry points are
`probe(pid) -> Probe` and `is_running(pid, created) -> bool`.

- **Windows**: the Win32 API through ctypes. A process is opened with
  SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION. Liveness is a zero-timeout
  wait on that handle, never "the open succeeded": a process object stays
  openable after its process exits for as long as anyone holds a handle to it.
- **Every other platform**: not implemented. Each probe answers `unknown`, so
  no process can be recorded there and nothing recorded ever counts as gone.
"""
from __future__ import annotations

import ctypes
import functools
import ntpath
import os
from dataclasses import dataclass

__all__ = ["Probe", "STATE_GONE", "STATE_RUNNING", "STATE_UNKNOWN",
           "is_running", "probe"]

STATE_RUNNING = "running"
STATE_GONE = "gone"
STATE_UNKNOWN = "unknown"

# Process access rights (winnt.h).
_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# WaitForSingleObject results (winbase.h).
_WAIT_OBJECT_0 = 0x0
_WAIT_TIMEOUT = 0x102

# What OpenProcess reports for a PID no process carries (winerror.h).
_ERROR_INVALID_PARAMETER = 87

# The longest path QueryFullProcessImageNameW can return, in characters.
_MAX_IMAGE_PATH = 32768


@dataclass(frozen=True)
class Probe:
    state: str
    image: str | None = None
    created: int | None = None
    reason: str = ""


def probe(pid: int) -> Probe:
    """What runs under `pid` now: `gone`; `running`, with its image name and
    creation time (each None when it cannot be read); or `unknown`, with why.
    A creation time is the process's FILETIME: 100 ns units since 1601."""
    if os.name != "nt":
        return Probe(STATE_UNKNOWN, reason="process identity is read on Windows only")
    return _probe_windows(pid)


def is_running(pid: int, created: int) -> bool:
    """Whether the process recorded as (`pid`, `created`) may still run.
    False only when no live process carries `pid`, or when the one that does
    was created at another time (the PID was given to a new process)."""
    found = probe(pid)
    if found.state == STATE_GONE:
        return False
    if found.state == STATE_RUNNING and found.created is not None:
        return found.created == created
    return True


# --- Windows -------------------------------------------------------------

class _FileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32),
                ("dwHighDateTime", ctypes.c_uint32)]


@functools.lru_cache(maxsize=None)
def _kernel32():
    # A private WinDLL instance, so these prototypes never clash with another
    # module's use of ctypes.windll.kernel32.
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    k32.WaitForSingleObject.restype = ctypes.c_uint32
    k32.GetProcessTimes.argtypes = (ctypes.c_void_p,) + (ctypes.POINTER(_FileTime),) * 4
    k32.GetProcessTimes.restype = ctypes.c_int
    k32.QueryFullProcessImageNameW.argtypes = (
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_uint32))
    k32.QueryFullProcessImageNameW.restype = ctypes.c_int
    k32.CloseHandle.argtypes = (ctypes.c_void_p,)
    k32.CloseHandle.restype = ctypes.c_int
    return k32


def _probe_windows(pid: int) -> Probe:
    k32 = _kernel32()
    handle = k32.OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION,
                             False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == _ERROR_INVALID_PARAMETER:
            return Probe(STATE_GONE)
        return Probe(STATE_UNKNOWN, reason=f"OpenProcess failed (error {error})")
    try:
        waited = k32.WaitForSingleObject(handle, 0)
        if waited == _WAIT_OBJECT_0:
            return Probe(STATE_GONE)
        if waited != _WAIT_TIMEOUT:
            return Probe(STATE_UNKNOWN,
                         reason=f"WaitForSingleObject returned {waited:#x}")
        return Probe(STATE_RUNNING, _image(k32, handle), _created(k32, handle))
    finally:
        k32.CloseHandle(handle)


def _created(k32, handle) -> int | None:
    times = [_FileTime() for _ in range(4)]
    if not k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
        return None
    creation = times[0]
    return (creation.dwHighDateTime << 32) | creation.dwLowDateTime


def _image(k32, handle) -> str | None:
    buffer = ctypes.create_unicode_buffer(_MAX_IMAGE_PATH)
    size = ctypes.c_uint32(_MAX_IMAGE_PATH)
    if not k32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
        return None
    return ntpath.basename(buffer.value)
