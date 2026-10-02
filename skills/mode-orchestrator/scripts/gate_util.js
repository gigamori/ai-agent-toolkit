// Small shared helpers of the restart-gate scripts (stop_observe.js,
// judge_gate.js). The command line, files, paths, checksums and the clock;
// nothing about what a stop or a judge record means -- that lives in the two
// scripts.
//
// Node's standard library only, like pi_reply.js.

"use strict";

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const EXIT_OK = 0;
const EXIT_REFUSED = 1;
const EXIT_USAGE = 64;
const EXIT_INTERNAL = 70;

const MS_PER_SECOND = 1000;
const NEWLINE_BYTE = 0x0a;
const FLAG_PREFIX = "--";

// A command line that is wrong (exit 64) and a request the script will not
// carry out (exit 1). Anything else that is thrown is a defect (exit 70), so a
// crash can never pass for a refusal.
class UsageError extends Error {}
class Refusal extends Error {}

// { flags, positional } of `--name value` pairs and bare arguments. A flag
// that is not in `allowed`, or that has no value, is a usage error: the
// caller must never loop on an argument it cannot consume.
function parseArguments(args, allowed) {
  const flags = {};
  const positional = [];
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (!arg.startsWith(FLAG_PREFIX)) {
      positional.push(arg);
      continue;
    }
    const name = arg.slice(FLAG_PREFIX.length);
    if (!allowed.includes(name)) {
      throw new UsageError(`unknown argument: ${arg}`);
    }
    if (index + 1 >= args.length) {
      throw new UsageError(`${arg} needs a value`);
    }
    index += 1;
    flags[name] = args[index];
  }
  return { flags, positional };
}

function requireFlag(flags, name) {
  if (flags[name] === undefined || flags[name] === "") {
    throw new UsageError(`--${name} is required`);
  }
  return flags[name];
}

function wholeNumber(text, label) {
  if (!/^[0-9]+$/.test(text)) {
    throw new UsageError(`${label} must be a whole number: ${text}`);
  }
  return Number(text);
}

// Runs `main(args)` and turns what it returns or throws into the exit code.
function runCli(script, usageLines, main) {
  try {
    process.exitCode = main(process.argv.slice(2));
  } catch (err) {
    if (err instanceof UsageError) {
      process.stderr.write(`${script}: ${err.message}\n${usageLines.join("\n")}\n`);
      process.exitCode = EXIT_USAGE;
      return;
    }
    if (err instanceof Refusal) {
      process.stderr.write(`${script}: ${err.message}\n`);
      process.exitCode = EXIT_REFUSED;
      return;
    }
    process.stderr.write(`${script}: internal error: ${err && err.stack ? err.stack : err}\n`);
    process.exitCode = EXIT_INTERNAL;
  }
}

// A path as the caller wrote it, as a native path. Under Git Bash an
// MSYS-form path (`/c/work/x`) is what the shell prints, and node reads it as
// `C:\c\work\x`; this turns it into `C:/work/x`. Every other path is left
// alone.
function nativePath(given) {
  if (process.platform !== "win32") {
    return given;
  }
  const drive = /^\/([A-Za-z])(?:\/(.*))?$/.exec(given);
  if (!drive) {
    return given;
  }
  return `${drive[1].toUpperCase()}:/${drive[2] || ""}`;
}

function absolutePath(given) {
  return path.resolve(nativePath(given));
}

// Paths compare equal when they name the same file on this platform:
// Windows ignores case.
function foldPath(given) {
  const absolute = absolutePath(given);
  return process.platform === "win32" ? absolute.toLowerCase() : absolute;
}

function samePath(a, b) {
  return foldPath(a) === foldPath(b);
}

function readJson(file) {
  try {
    const data = JSON.parse(fs.readFileSync(file, "utf8"));
    return data !== null && typeof data === "object" && !Array.isArray(data)
      ? data
      : null;
  } catch {
    return null;
  }
}

// Writes `data` only when `file` does not exist: the record of an attempt is
// written once. Returns false when it already existed.
function writeJsonOnce(file, data) {
  try {
    fs.writeFileSync(file, JSON.stringify(data, null, 2) + "\n", { flag: "wx" });
    return true;
  } catch (err) {
    if (err.code === "EEXIST") {
      return false;
    }
    throw err;
  }
}

function sha256Of(bytes) {
  return crypto.createHash("sha256").update(bytes).digest("hex");
}

// Lines as `wc -l` plus a final unterminated one, which is what a citation
// `#L<n>` can name.
function lineCount(bytes) {
  let count = 0;
  for (const byte of bytes) {
    if (byte === NEWLINE_BYTE) {
      count += 1;
    }
  }
  const last = bytes.length > 0 ? bytes[bytes.length - 1] : NEWLINE_BYTE;
  return last === NEWLINE_BYTE ? count : count + 1;
}

// Synchronous sleep: these scripts are one-shot commands, not servers.
function sleepMs(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

function nowSeconds() {
  return Date.now() / MS_PER_SECOND;
}

function isoToSeconds(value) {
  if (typeof value !== "string") {
    return null;
  }
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? null : ms / MS_PER_SECOND;
}

// The parsed JSON object lines of a JSONL file, with the 1-based number of
// each. Unparseable lines (a cut-off write, a NUL byte) are skipped.
function jsonLines(file) {
  let bytes;
  try {
    bytes = fs.readFileSync(file);
  } catch {
    return [];
  }
  const entries = [];
  const lines = bytes.toString("utf8").split("\n");
  for (let index = 0; index < lines.length; index += 1) {
    const text = lines[index].trim();
    if (!text) {
      continue;
    }
    try {
      const entry = JSON.parse(text);
      if (entry !== null && typeof entry === "object") {
        entries.push({ number: index + 1, entry });
      }
    } catch {
      continue;
    }
  }
  return entries;
}

module.exports = {
  EXIT_OK,
  EXIT_REFUSED,
  MS_PER_SECOND,
  Refusal,
  UsageError,
  absolutePath,
  foldPath,
  isoToSeconds,
  jsonLines,
  lineCount,
  nowSeconds,
  parseArguments,
  readJson,
  requireFlag,
  runCli,
  samePath,
  sha256Of,
  sleepMs,
  wholeNumber,
  writeJsonOnce,
};
