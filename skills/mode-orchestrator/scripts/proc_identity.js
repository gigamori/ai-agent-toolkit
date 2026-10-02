// Identify a process by its PID and creation time, and tell whether that same
// process still runs. The only place the restart-gate scripts talk to the OS
// about processes.
//
// A PID names a process only while it runs: once it exits, Windows may give
// the number to another process. So a process is recorded as (PID, creation
// time), and it counts as running only while a live process carries both.
// Every doubt counts as running: a process that cannot be read for any reason
// other than "no such process", or a probe that could not run at all, may
// still be the recorded one.
//
// Windows: one PowerShell call reads each PID through .NET's Process class
// (existence, HasExited, StartTime as a FILETIME). powershell.exe is started
// directly, never through cmd.exe. Every other platform: not implemented --
// each probe answers `unknown`, so nothing can be recorded there and nothing
// recorded ever counts as gone.
//
// A creation time is a FILETIME (100 ns units since 1601): about 1.3e17, past
// what a JS number holds exactly, so it is kept as a decimal string.
//
// Node's standard library only.

"use strict";

const { execFileSync } = require("child_process");

const STATE_RUNNING = "running";
const STATE_GONE = "gone";
const STATE_UNKNOWN = "unknown";

const POWERSHELL = "powershell.exe";
const PROBE_TIMEOUT_MS = 20000;
const FIELD_SEPARATOR = "\t";
const IMAGE_SUFFIX = ".exe";
const FILETIME_DIGITS = /^[0-9]+$/;

const NOT_WINDOWS_REASON = "process identity is read on Windows only";
const NO_PROBE_REASON = "the process probe could not run";
const UNREADABLE_REASON = "the process cannot be read";

// One line per PID: `<pid> TAB gone`, `<pid> TAB unknown`, or
// `<pid> TAB running TAB <name> TAB <FILETIME>`. A missing process makes
// GetProcessById throw ArgumentException; a process that exited but is still
// held open by some handle reports HasExited.
function probeScript(pids) {
  return [
    "$ErrorActionPreference = 'Stop'",
    "$t = [char]9",
    `foreach ($id in @(${pids.join(",")})) {`,
    "  $p = $null",
    "  try { $p = [System.Diagnostics.Process]::GetProcessById([int]$id) }",
    "  catch [System.ArgumentException] { \"$id\" + $t + 'gone'; continue }",
    "  catch { \"$id\" + $t + 'unknown'; continue }",
    "  try {",
    "    if ($p.HasExited) { \"$id\" + $t + 'gone' }",
    "    else { \"$id\" + $t + 'running' + $t + $p.ProcessName + $t + $p.StartTime.ToFileTimeUtc() }",
    "  } catch { \"$id\" + $t + 'unknown' }",
    "  finally { $p.Dispose() }",
    "}",
  ].join("\n");
}

function unknownFor(pids, reason) {
  const found = new Map();
  for (const pid of pids) {
    found.set(pid, { state: STATE_UNKNOWN, reason });
  }
  return found;
}

function parseProbe(output, pids) {
  const found = unknownFor(pids, UNREADABLE_REASON);
  for (const line of output.split(/\r?\n/)) {
    const [idText, state, name, created] = line.split(FIELD_SEPARATOR);
    const pid = Number(idText);
    if (!found.has(pid)) {
      continue;
    }
    if (state === STATE_GONE) {
      found.set(pid, { state: STATE_GONE });
    } else if (state === STATE_RUNNING && name && FILETIME_DIGITS.test(created || "")) {
      found.set(pid, { state: STATE_RUNNING, image: name + IMAGE_SUFFIX, created });
    }
  }
  return found;
}

// pid -> { state, image?, created?, reason? } for every PID asked about.
function probeMany(pids) {
  const wanted = [...new Set(pids)].filter((pid) => Number.isInteger(pid) && pid > 0);
  if (process.platform !== "win32") {
    return unknownFor(wanted, NOT_WINDOWS_REASON);
  }
  if (wanted.length === 0) {
    return new Map();
  }
  const encoded = Buffer.from(probeScript(wanted), "utf16le").toString("base64");
  try {
    const output = execFileSync(
      POWERSHELL,
      ["-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
      { encoding: "utf8", windowsHide: true, timeout: PROBE_TIMEOUT_MS, stdio: ["ignore", "pipe", "ignore"] },
    );
    return parseProbe(output, wanted);
  } catch {
    return unknownFor(wanted, NO_PROBE_REASON);
  }
}

// Whether the process recorded as { pid, created } may still run: false only
// when no live process carries the PID, or the one that does was created at
// another time (the PID went to a new process).
function mayStillRun(recorded, current) {
  if (current === undefined || current.state === STATE_UNKNOWN) {
    return true;
  }
  if (current.state === STATE_GONE) {
    return false;
  }
  return current.created === String(recorded.created);
}

module.exports = {
  STATE_GONE,
  STATE_RUNNING,
  mayStillRun,
  probeMany,
};
