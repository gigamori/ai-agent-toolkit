#!/usr/bin/env node
// stop_observe.js -- what a stop request left behind, for one delegated attempt.
//
//   node stop_observe.js pids   --stem <S> <windows-pid> [<windows-pid> ...]
//   node stop_observe.js cc     --stem <S> --desc <description>
//                               --task-stop stopped|completed|failed
//                               --cause deadline|ended --deadline <seconds>
//                               [--window <1-60>] [--project-root <p>]
//   node stop_observe.js pi-cli --stem <S> --cause deadline|ended
//                               --deadline <seconds> [--window <1-60>]
//
// Why this exists: a deadline notice is not a stop, and a stop request is not
// a confirmed stop. An attempt may be run again only when it and everything
// the script can watch of it have ended and can no longer write; that is
// decided here, the same way every time, and written down once. Whether a
// re-run may be allowed at all is a separate judgment (judge_gate.js).
//
// <S> is the attempt's file prefix, one per attempt, in an existing directory
// (`<run dir>/<turn key>-<attempt>`). This script writes `<S>-pids.json` and
// `<S>-stop.json` and never overwrites either.
//
// `pids` -- Pi CLI only, run from the launch line itself (see harness-pi.md):
// records each given Windows PID as (PID, image, creation time), once, while
// the CLI runs. It is silent on success. It refuses -- and records nothing --
// when the attempt already has a record or a stop, or when a PID is not a
// running process whose image and creation time can be read. A refusal must
// not stop the launch: the attempt simply can never confirm a stop.
//
// `cc` and `pi-cli` -- observe the stop for at most --window seconds (default
// and maximum 60, the ruled bound; `pi-cli` can overrun it by one process
// check, up to 20 s: PROBE_TIMEOUT_MS in proc_identity.js) and print one
// verdict word:
//   CONFIRMED    exit 0  the facet's evidence holds (below)
//   UNCONFIRMED  exit 1  anything else; the reasons are on stderr and in the
//                        record. An unconfirmed attempt is never run again
// An attempt is observed once: asking again prints the first verdict, so a
// second look cannot turn an unconfirmed stop into a confirmed one. No verdict
// word on stdout (a crash, a refusal) is not a confirmation either.
//
// CONFIRMED means, per facet:
//   cc      TaskStop reported the task stopped (or already ended), the
//           attempt's transcript and the transcript of every agent it started
//           were found, none of them changed for the whole window, and none of
//           them holds a call without a result, a call that started background
//           work, or a call refused or interrupted. TaskStop interrupting a
//           running call leaves it recorded as interrupted, so a stop that cut
//           a running call stays unconfirmed. UNVERIFIED on a real host: how
//           soon after TaskStop returns Claude Code stops writing the
//           transcript.
//   pi-cli  every process recorded at launch is gone: no live process carries
//           its PID and creation time. Processes outside the recorded set --
//           descendants included -- are not observed.
// What a record never speaks for is written into it (`not_observed`).
//
// --cause says why the attempt was stopped: `deadline` (its wall-clock budget,
// --deadline seconds, ran out) or `ended` (anything else: a stall, no reply, a
// malformed reply). The judge reads both.
//
// Needs node. Windows PIDs are read through PowerShell (proc_identity.js);
// off Windows nothing can be recorded, so nothing is confirmed.

"use strict";

const fs = require("fs");
const path = require("path");

const ccTranscripts = require("./cc_transcripts");
const procIdentity = require("./proc_identity");
const util = require("./gate_util");

const { Refusal, UsageError, parseArguments, requireFlag, wholeNumber } = util;

const SCRIPT = "stop_observe";

const STOP_OBSERVE_MAX = 60;
const POLL_MS = 500;

const FACET_CC = "cc";
const FACET_PI_CLI = "pi-cli";

const VERDICT_CONFIRMED = "CONFIRMED";
const VERDICT_UNCONFIRMED = "UNCONFIRMED";

const CAUSE_DEADLINE = "deadline";
const CAUSE_ENDED = "ended";
const CAUSES = [CAUSE_DEADLINE, CAUSE_ENDED];

const TASK_STOPPED = "stopped";
const TASK_COMPLETED = "completed";
const TASK_FAILED = "failed";
const TASK_STOP_RESULTS = [TASK_STOPPED, TASK_COMPLETED, TASK_FAILED];

const NOT_OBSERVED = {
  [FACET_CC]: "processes that tool calls started and left running -- detached "
    + "or not -- and every operation outside files",
  [FACET_PI_CLI]: "every process outside the recorded set -- descendants of the "
    + "recorded processes included, detached or not -- and every operation "
    + "outside files",
};

const USAGE = [
  "usage: stop_observe.js pids   --stem <S> <windows-pid> [<windows-pid> ...]",
  "       stop_observe.js cc     --stem <S> --desc <description> --task-stop stopped|completed|failed",
  "                              --cause deadline|ended --deadline <seconds> [--window <1-60>] [--project-root <p>]",
  "       stop_observe.js pi-cli --stem <S> --cause deadline|ended --deadline <seconds> [--window <1-60>]",
];

const FLAGS = {
  pids: ["stem"],
  cc: ["stem", "desc", "task-stop", "cause", "deadline", "window", "project-root"],
  "pi-cli": ["stem", "cause", "deadline", "window"],
};

function stemFiles(stemArgument) {
  const stem = util.absolutePath(stemArgument);
  if (!fs.existsSync(path.dirname(stem))) {
    throw new Refusal(`the directory of --stem does not exist: ${path.dirname(stem)}`);
  }
  return { stem, pids: `${stem}-pids.json`, stop: `${stem}-stop.json` };
}

function recordPids(args) {
  const { flags, positional } = parseArguments(args, FLAGS.pids);
  const files = stemFiles(requireFlag(flags, "stem"));
  if (positional.length === 0) {
    throw new UsageError("pids needs at least one Windows PID");
  }
  const pids = positional.map((text) => wholeNumber(text, "a PID"));
  if (pids.some((pid) => pid <= 0)) {
    throw new UsageError("a PID is a positive integer");
  }
  if (fs.existsSync(files.pids)) {
    throw new Refusal(`this attempt already has its processes recorded (${files.pids}); `
      + "they are recorded once, at launch");
  }
  if (fs.existsSync(files.stop)) {
    throw new Refusal("this attempt was already stopped; its launch is over");
  }

  const probes = procIdentity.probeMany(pids);
  const processes = [];
  for (const pid of new Set(pids)) {
    const found = probes.get(pid);
    if (found.state !== procIdentity.STATE_RUNNING) {
      const why = found.state === procIdentity.STATE_GONE
        ? "it is not running" : found.reason;
      throw new Refusal(`process ${pid}: ${why} -- nothing recorded`);
    }
    processes.push({ pid, image: found.image, created: found.created });
  }
  const written = util.writeJsonOnce(files.pids, {
    stem: files.stem, recorded_at: util.nowSeconds(), processes,
  });
  if (!written) {
    throw new Refusal("this attempt already has its processes recorded");
  }
  return util.EXIT_OK;
}

function observeCc(options) {
  const resolve = () => ccTranscripts.delegationTranscripts(options.desc, options.projectRoot);
  const { transcripts: watched, reason } = resolve();
  const reasons = [];
  if (watched.length === 0) {
    reasons.push(reason);
  }
  if (![TASK_STOPPED, TASK_COMPLETED].includes(options.taskStop)) {
    reasons.push(`TaskStop reported ${options.taskStop}`);
  }
  const atStop = ccTranscripts.openToolCalls(watched);
  reasons.push(...atStop.map((call) => `at the stop: ${call}`));

  // The window only matters while the stop could still be confirmed: once a
  // reason is on the table the verdict is decided, and waiting would only
  // delay the report.
  if (reasons.length === 0) {
    const before = ccTranscripts.fileStamps(watched);
    const end = Date.now() + options.window * util.MS_PER_SECOND;
    let changed = false;
    while (Date.now() < end) {
      util.sleepMs(Math.min(POLL_MS, end - Date.now()));
      const now = ccTranscripts.fileStamps(resolve().transcripts);
      changed = changed || !ccTranscripts.sameStamps(now, before);
    }
    if (changed) {
      reasons.push("a transcript changed during the observation window");
    }
  }

  const final = resolve().transcripts;
  const lateCalls = ccTranscripts.openToolCalls(final).filter((call) => !atStop.includes(call));
  reasons.push(...lateCalls.map((call) => `at the end of the window: ${call}`));
  return { reasons, extra: { transcripts: final.map((file) => util.absolutePath(file)) } };
}

function observePiCli(options) {
  const recorded = util.readJson(`${options.stem}-pids.json`);
  const processes = recorded && Array.isArray(recorded.processes) ? recorded.processes : [];
  if (processes.length === 0) {
    return {
      reasons: ["no process set was recorded when this attempt was launched"],
      extra: { processes: [] },
    };
  }
  const end = Date.now() + options.window * util.MS_PER_SECOND;
  let running = processes;
  for (;;) {
    const probes = procIdentity.probeMany(processes.map((p) => p.pid));
    running = processes.filter((p) => procIdentity.mayStillRun(p, probes.get(p.pid)));
    if (running.length === 0 || Date.now() >= end) {
      break;
    }
    util.sleepMs(Math.min(POLL_MS, end - Date.now()));
  }
  const alive = new Set(running.map((p) => p.pid));
  const reasons = running.length === 0 ? [] : [
    "still running at the end of the window: "
      + running.map((p) => `${p.pid} (${p.image})`).join(", "),
  ];
  return {
    reasons,
    extra: { processes: processes.map((p) => ({ ...p, running: alive.has(p.pid) })) },
  };
}

// Anything but the exact word CONFIRMED in a record reads as UNCONFIRMED.
function printVerdict(record, stopPath, note) {
  const confirmed = record.verdict === VERDICT_CONFIRMED;
  const verdict = confirmed ? VERDICT_CONFIRMED : VERDICT_UNCONFIRMED;
  const reasons = Array.isArray(record.reasons) ? record.reasons : [];
  const detail = reasons.length > 0 ? ` -- ${reasons.join("; ")}` : "";
  process.stdout.write(`${verdict}\n`);
  process.stderr.write(`${SCRIPT}: ${note || verdict}${detail} (record: ${stopPath})\n`);
  return confirmed ? util.EXIT_OK : util.EXIT_REFUSED;
}

function observe(facet, args) {
  const { flags } = parseArguments(args, FLAGS[facet]);
  const files = stemFiles(requireFlag(flags, "stem"));
  const cause = requireFlag(flags, "cause");
  if (!CAUSES.includes(cause)) {
    throw new UsageError(`--cause is ${CAUSES.join(" or ")}`);
  }
  const deadline = wholeNumber(requireFlag(flags, "deadline"), "--deadline");
  if (deadline <= 0) {
    throw new UsageError("--deadline is a positive number of seconds");
  }
  const window = flags.window === undefined
    ? STOP_OBSERVE_MAX : wholeNumber(flags.window, "--window");
  if (window < 1 || window > STOP_OBSERVE_MAX) {
    throw new UsageError(`--window takes 1 to ${STOP_OBSERVE_MAX} seconds`);
  }
  const options = { stem: files.stem, window };
  if (facet === FACET_CC) {
    options.desc = requireFlag(flags, "desc");
    options.taskStop = requireFlag(flags, "task-stop");
    options.projectRoot = flags["project-root"];
    if (!TASK_STOP_RESULTS.includes(options.taskStop)) {
      throw new UsageError(`--task-stop is ${TASK_STOP_RESULTS.join(", ")}`);
    }
  }

  const prior = util.readJson(files.stop);
  if (prior !== null) {
    return printVerdict(prior, files.stop, `already observed: the first verdict stands (${prior.verdict})`);
  }
  const requestedAt = util.nowSeconds();
  const { reasons, extra } = facet === FACET_CC ? observeCc(options) : observePiCli(options);
  const record = {
    stem: files.stem, facet, cause, deadline, window,
    ...(facet === FACET_CC ? { task_stop: options.taskStop, desc: options.desc } : {}),
    requested_at: requestedAt, observed_until: util.nowSeconds(),
    verdict: reasons.length === 0 ? VERDICT_CONFIRMED : VERDICT_UNCONFIRMED,
    reasons, not_observed: NOT_OBSERVED[facet], ...extra,
  };
  if (!util.writeJsonOnce(files.stop, record)) {
    throw new Refusal("this attempt was observed by another call at the same time");
  }
  return printVerdict(record, files.stop);
}

function main(argv) {
  const [command, ...rest] = argv;
  if (command === "pids") {
    return recordPids(rest);
  }
  if (command === FACET_CC || command === FACET_PI_CLI) {
    return observe(command, rest);
  }
  throw new UsageError(command === undefined ? "no subcommand" : `unknown subcommand: ${command}`);
}

util.runCli(SCRIPT, USAGE, main);
