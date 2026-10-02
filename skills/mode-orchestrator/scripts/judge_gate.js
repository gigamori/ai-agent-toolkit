#!/usr/bin/env node
// judge_gate.js -- the one judge of a stopped attempt: issue it, then check
// what it answered.
//
//   node judge_gate.js issue --stem <S> [--deliv <path>|-]
//                            [--raw <raw.jsonl> --session-dir <dir>]   (pi-cli)
//   node judge_gate.js check --stem <S> --reply "<allow|deny> <record path>"
//
// Why this exists: after an attempt stopped, whether the same work may run
// again from the start turns on what the attempt already did, and only a
// reader of the evidence can say. The orchestrator never reads task content,
// so that reader is a fixed, separate delegation -- the judge -- and this
// script is the part that does not take its word for it. It runs at most once
// per attempt.
//
// <S> is the attempt's file prefix, as for stop_observe.js. This script reads
// `<S>-stop.json` and writes `<S>-evidence.json` (the manifest),
// `<S>-judge-prompt.md` (the judge's whole prompt) and, through the judge,
// `<S>-judge.md` (its record). It never overwrites any of them.
//
// `issue` refuses -- and writes nothing -- unless <S> names an attempt's first
// run (it ends in `-1`: a turn key runs again at most once, so a later attempt
// is never judged), the attempt's stop record says CONFIRMED and no judge was
// issued for it before. It lists the evidence the judge may cite -- the
// deliverable (when --deliv names a file that exists), the stop record, and the
// transcripts -- each with its sha256, byte and line counts, and writes the
// judge's prompt. Transcripts:
//   cc      the ones the stop record lists (the turn's and those of every agent
//           it started)
//   pi-cli  the session file whose name carries the session id on the first
//           line of --raw, found in --session-dir
// It prints one line and nothing else: the prompt's path and the facts to
// delegate the judge with.
//   <prompt> (judge, model=ultra, tools=Read,Grep,Write, record=<path>,
//             timeout=900, description=<d>)
// The judge is an ordinary turn of the harness: give it the model that
// `ultra` resolves to, the tools listed (Pi CLI names: read,grep,write), the
// timeout as its deadline, and -- on Claude Code -- the description verbatim,
// because the turn watchdog finds its transcript by it. The message it gets is
// "Read <prompt> and execute its instructions. Use only these tools: <tools>.
// Reply to me with the single line it asks for." Nothing else.
//
// `check` takes the judge's one-line reply and accepts it only when ALL hold:
//   - the reply is `allow <this attempt's record>`;
//   - the record was written within the judge's deadline and ends with the
//     judge's own end marker as its last line;
//   - it says `verdict: allow` and `method: restart` (running the work again
//     from the start is the only method a restart can carry out) and states
//     its `limits:`;
//   - it cites at least one range `<path>#L<first>-L<last>`, each inside a
//     file of the manifest and inside that file's lines;
//   - every file of the manifest is byte for byte what it was when listed;
//   - after a deadline, it gives `next-timeout: <seconds>` longer than the
//     deadline the attempt hit, and cites a transcript line stamped inside the
//     attempt's own run (from its first transcript line to that deadline).
// It prints `ALLOW <timeout> <record path>` (exit 0): run the attempt again
// with that deadline -- the one it had before, or the longer one the judge
// backed. Anything else prints REFUSED (exit 1), the reason on stderr: the
// attempt is not run again; stop and report. A judge is never issued twice.
//
// Not checked, and not checkable here: who wrote the record, and whether what
// it says about the evidence is true. The record's form, citations, checksums
// and timing are checked; its judgment is the judge's.
//
// Needs node.

"use strict";

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const util = require("./gate_util");

const { Refusal, UsageError, parseArguments, requireFlag } = util;

const SCRIPT = "judge_gate";

const JUDGE_TIMEOUT = 900;
const JUDGE_MODEL = "ultra";
const JUDGE_TOOLS = ["Read", "Grep", "Write"];
const TOKEN_BYTES = 4;
const START_SCAN_LINES = 200;
const NO_DELIVERABLE = "-";
const ATTEMPT_SUFFIX = /-(\d+)$/;
const FIRST_ATTEMPT = "1";

const FACET_CC = "cc";
const FACET_PI_CLI = "pi-cli";
const STOP_CONFIRMED = "CONFIRMED";
const CAUSE_DEADLINE = "deadline";
const CAUSE_ENDED = "ended";

const REPLY_ALLOW = "allow";
const REPLY_DENY = "deny";
const METHOD_RESTART = "restart";

const KIND_DELIVERABLE = "deliverable";
const KIND_STOP = "stop";
const KIND_TRANSCRIPT = "transcript";

const VERDICT_ALLOW = "ALLOW";
const VERDICT_REFUSED = "REFUSED";

const USAGE = [
  "usage: judge_gate.js issue --stem <S> [--deliv <path>|-] [--raw <raw.jsonl> --session-dir <dir>]",
  "       judge_gate.js check --stem <S> --reply \"<allow|deny> <record path>\"",
];

// The judge's whole prompt: one fixed text, worded for no particular skill.
// Paths and rules only -- code assembles it, so no task content passes
// through the orchestrator. The record's fields are what `check` reads, and
// the reply is one line because the reply is the one channel back into an
// orchestrator that never reads task content.
const JUDGE_PROMPT = [
  "You judge one thing: whether running a piece of delegated work again from the start could duplicate an effect of an earlier attempt at it. That attempt stopped because {cause}, and it has been stopped for good. Decide nothing else, and do none of the work yourself.",
  "",
  "## Evidence",
  "You may cite only these files, by line. Read what you need and change none of them: their checksums are recorded in {manifest}, and a changed file voids your record.",
  "{files}",
  "",
  "## How to judge",
  "- Work out from the evidence what the attempt did. Files it wrote or edited are stock. Anything that acted outside files -- a command with side effects, a request, a job, a message, an agent it started -- is flow.",
  "- Stock alone: a run from the start is safe only if it would rewrite those files whole rather than add to them, and nothing else uses them in between.",
  "- Any flow: allow only if a cited line shows that repeating it cannot duplicate its effect (it checks an idempotency key, or it only reads). If you cannot see the state of an operation, deny.",
  "- Only cited lines are evidence. Your own reasoning, the work's name and its mode are not. When the evidence is incomplete or ambiguous, deny.",
  "{deadline_clause}",
  "## Record",
  "Write this record to {record}, and write nothing to any other file:",
  "",
  "verdict: <allow or deny>",
  "method: <restart or none>",
  "{next_timeout_line}evidence:",
  "- <file path>#L<first>-L<last>: <what those lines show>",
  "limits: <what you could not check, or none>",
  "{sentinel}",
  "",
  "Replace each <...> with the value alone. method is restart only with allow, and means running the work again from the start; adopting the attempt's result or resuming it from a saved state are not choices here, so when a run from the start is not safe the verdict is deny. Give one evidence line per cited range. Copy the last line exactly as it stands.",
  "",
  "## Reply",
  "Your reply is exactly one line: `allow {record}` or `deny {record}`. Put nothing else in it.",
].join("\n");

// Added when the attempt was stopped at its deadline: a restart with the same
// deadline would only hit it again.
const JUDGE_DEADLINE_CLAUSE = "- The attempt was stopped at its deadline of {d_prev} seconds, which began at {t0}. A run with the same deadline would only hit it again. Allow only if you also give a longer one, next-timeout greater than {d_prev}, and cite at least one transcript line stamped between {t0} and {deadline} that shows the attempt was still making progress.\n";
const JUDGE_NEXT_TIMEOUT_LINE = "next-timeout: <seconds>\n";

const CITATION = /^-\s*(.+?)#L(\d+)(?:-L?(\d+))?\s*(?::.*)?$/;

function refusal(reason) {
  return new Refusal(`${reason} -- stop and report`);
}

function fill(template, values) {
  return template.replace(/\{(\w+)\}/g, (whole, key) => (key in values ? values[key] : whole));
}

function stemFiles(stemArgument) {
  const stem = util.absolutePath(stemArgument);
  return {
    stem,
    stop: `${stem}-stop.json`,
    evidence: `${stem}-evidence.json`,
    prompt: `${stem}-judge-prompt.md`,
    record: `${stem}-judge.md`,
  };
}

// A judge exists to allow the one restart of a turn key, so only an attempt's
// first run is judged: <S> ends in `-<attempt>`, and that number is 1.
function requireFirstAttempt(files) {
  const name = path.basename(files.stem);
  const match = ATTEMPT_SUFFIX.exec(name);
  if (match === null || match[1] !== FIRST_ATTEMPT) {
    throw refusal(`only an attempt's first run is judged: a turn key runs again at most once, and ${name} does not end in -${FIRST_ATTEMPT}`);
  }
}

function isFile(file) {
  return fs.existsSync(file) && fs.statSync(file).isFile();
}

function evidenceEntry(kind, file) {
  const bytes = fs.readFileSync(file);
  return {
    kind,
    path: path.resolve(file),
    sha256: util.sha256Of(bytes),
    bytes: bytes.length,
    lines: util.lineCount(bytes),
  };
}

// The earliest stamp among the first lines of the attempt's own transcript:
// the start of the run a longer deadline has to be backed from.
function startOfRun(transcript) {
  const stamps = util.jsonLines(transcript)
    .slice(0, START_SCAN_LINES)
    .map(({ entry }) => util.isoToSeconds(entry.timestamp))
    .filter((stamp) => stamp !== null);
  return stamps.length === 0 ? null : Math.min(...stamps);
}

function piSessionFile(rawArgument, sessionDirArgument) {
  const raw = util.absolutePath(rawArgument);
  let header;
  try {
    header = JSON.parse(fs.readFileSync(raw, "utf8").split("\n")[0]);
  } catch {
    throw refusal(`the raw stream ${raw} cannot be read as a Pi event stream, so the attempt's session cannot be found`);
  }
  if (!header || header.type !== "session" || typeof header.id !== "string" || !header.id) {
    throw refusal(`the first line of ${raw} is not a session event with an id, so the attempt's session cannot be found`);
  }
  const directory = util.absolutePath(sessionDirArgument);
  let names = [];
  try {
    names = fs.readdirSync(directory)
      .filter((name) => name.endsWith(".jsonl") && name.includes(header.id));
  } catch {
    names = [];
  }
  if (names.length !== 1) {
    throw refusal(`${names.length} session file(s) in ${directory} carry the session id ${header.id}; a judge needs exactly one transcript to read`);
  }
  return path.join(directory, names[0]);
}

function ccTranscriptFiles(stop) {
  const listed = Array.isArray(stop.transcripts) ? stop.transcripts : [];
  if (listed.length === 0) {
    throw refusal("the stop record lists no transcript a judge could read");
  }
  for (const file of listed) {
    if (!isFile(file)) {
      throw refusal(`a transcript the stop record lists does not exist: ${file}`);
    }
  }
  return listed.map((file) => path.resolve(file));
}

// What `issue` may go on from: the confirmed stop of an attempt that has no
// judge yet. Throws the refusal otherwise.
function confirmedStop(files) {
  const stop = util.readJson(files.stop);
  if (stop === null) {
    throw refusal(`no stop record for this attempt (${files.stop}): observe the stop with stop_observe.js first`);
  }
  if (stop.verdict !== STOP_CONFIRMED) {
    throw refusal("the stop of this attempt is not confirmed; an attempt whose stop is not confirmed is never run again");
  }
  if (![FACET_CC, FACET_PI_CLI].includes(stop.facet)) {
    throw refusal(`the stop record names facet '${stop.facet}', which has no judge evidence here`);
  }
  const deadline = Number(stop.deadline);
  if (!Number.isFinite(deadline) || deadline <= 0) {
    throw refusal("the stop record carries no deadline for the attempt");
  }
  return stop;
}

function evidenceSources(flags, files, transcripts) {
  const sources = [];
  if (flags.deliv && flags.deliv !== NO_DELIVERABLE) {
    const deliverable = util.absolutePath(flags.deliv);
    if (isFile(deliverable)) {
      sources.push([KIND_DELIVERABLE, deliverable]);
    }
  }
  sources.push([KIND_STOP, files.stop]);
  for (const file of transcripts) {
    sources.push([KIND_TRANSCRIPT, file]);
  }
  return sources;
}

function judgePrompt(files, listed, details) {
  const iso = (seconds) => new Date(seconds * util.MS_PER_SECOND).toISOString();
  const atDeadline = details.cause === CAUSE_DEADLINE;
  return fill(JUDGE_PROMPT, {
    cause: atDeadline ? "its deadline passed" : "it ended without a valid result",
    manifest: files.evidence,
    files: listed.map((entry) => `- ${entry.kind}: ${entry.path}`).join("\n"),
    deadline_clause: atDeadline
      ? fill(JUDGE_DEADLINE_CLAUSE, {
        d_prev: details.deadline, t0: iso(details.t0), deadline: iso(details.t0 + details.deadline),
      })
      : "",
    next_timeout_line: atDeadline ? JUDGE_NEXT_TIMEOUT_LINE : "",
    record: files.record,
    sentinel: details.sentinel,
  });
}

function issue(args) {
  const { flags } = parseArguments(args, ["stem", "deliv", "raw", "session-dir"]);
  const files = stemFiles(requireFlag(flags, "stem"));
  requireFirstAttempt(files);
  const stop = confirmedStop(files);
  if (stop.facet === FACET_PI_CLI) {
    requireFlag(flags, "raw");
    requireFlag(flags, "session-dir");
  }
  if ([files.evidence, files.prompt, files.record].some((file) => fs.existsSync(file))) {
    throw refusal("a judge was already issued for this attempt; one attempt gets one judge, and its record is final");
  }

  const transcripts = stop.facet === FACET_CC
    ? ccTranscriptFiles(stop)
    : [piSessionFile(flags.raw, flags["session-dir"])];
  const cause = stop.cause === CAUSE_DEADLINE ? CAUSE_DEADLINE : CAUSE_ENDED;
  const deadline = Number(stop.deadline);
  const t0 = startOfRun(transcripts[0]);
  if (cause === CAUSE_DEADLINE && t0 === null) {
    throw refusal("the attempt hit its deadline, but no stamped line of its transcript shows when it started, so a longer deadline could not be backed");
  }

  const listed = evidenceSources(flags, files, transcripts)
    .map(([kind, file]) => evidenceEntry(kind, file));
  const token = crypto.randomBytes(TOKEN_BYTES).toString("hex");
  const sentinel = `[[MO-JUDGE-END ${token}]]`;
  const description = `mo-judge ${path.basename(files.stem)} ${token}`;
  const prompt = judgePrompt(files, listed, { cause, deadline, t0, sentinel });
  const manifest = {
    stem: files.stem, facet: stop.facet, cause, t0, d_prev: deadline, files: listed,
    judge: {
      issued_at: util.nowSeconds(), timeout: JUDGE_TIMEOUT, model: JUDGE_MODEL, tools: JUDGE_TOOLS,
      record_path: files.record, prompt_path: files.prompt, sentinel, description,
    },
  };

  fs.writeFileSync(files.prompt, prompt, { flag: "wx" });
  if (!util.writeJsonOnce(files.evidence, manifest)) {
    throw refusal("a judge was already issued for this attempt");
  }
  process.stdout.write(`${files.prompt} (judge, model=${JUDGE_MODEL}, tools=${JUDGE_TOOLS.join(",")}, `
    + `record=${files.record}, timeout=${JUDGE_TIMEOUT}, description=${description})\n`);
  return util.EXIT_OK;
}

// Fields and cited ranges of a record body (its end marker already removed).
function parseRecord(lines) {
  const fields = {};
  const refs = [];
  const errors = [];
  let inEvidence = false;
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) {
      continue;
    }
    if (inEvidence && line.startsWith("-")) {
      const match = CITATION.exec(line);
      if (match === null) {
        errors.push(`unreadable evidence line '${line.slice(0, 80)}'`);
        continue;
      }
      const first = Number(match[2]);
      refs.push({ file: match[1].trim(), first, last: match[3] === undefined ? first : Number(match[3]) });
      continue;
    }
    const colon = line.indexOf(":");
    if (colon < 0) {
      continue;
    }
    const key = line.slice(0, colon).trim().toLowerCase();
    inEvidence = key === "evidence";
    if (!inEvidence) {
      fields[key] = line.slice(colon + 1).trim();
    }
  }
  return { fields, refs, errors };
}

function citedStamps(file, first, last) {
  let lines;
  try {
    lines = fs.readFileSync(file).toString("utf8").split("\n");
  } catch {
    return [];
  }
  const stamps = [];
  for (const text of lines.slice(first - 1, last)) {
    try {
      const entry = JSON.parse(text);
      const stamp = entry && typeof entry === "object" ? util.isoToSeconds(entry.timestamp) : null;
      if (stamp !== null) {
        stamps.push(stamp);
      }
    } catch {
      continue;
    }
  }
  return stamps;
}

function checkReply(reply, record) {
  const match = /^(\S+)\s+([\s\S]*\S)$/.exec(reply.trim());
  const notJudgment = refusal(`the judge's reply is not \`${REPLY_ALLOW} ${record}\`; it is not a judgment`);
  if (match === null || !util.samePath(match[2], record)) {
    throw notJudgment;
  }
  const verb = match[1].toLowerCase();
  if (verb === REPLY_DENY) {
    throw refusal("the judge denied running this attempt again");
  }
  if (verb !== REPLY_ALLOW) {
    throw notJudgment;
  }
}

function recordLines(record, judge) {
  let stat;
  let text;
  try {
    stat = fs.statSync(record);
    text = fs.readFileSync(record, "utf8");
  } catch {
    throw refusal(`the judge's record ${record} cannot be read`);
  }
  if (stat.mtimeMs / util.MS_PER_SECOND > judge.issued_at + judge.timeout) {
    throw refusal(`the judge's record ${record} was written after the judge's deadline`);
  }
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  while (lines.length > 0 && lines[lines.length - 1].trim() === "") {
    lines.pop();
  }
  if (lines.length === 0 || lines[lines.length - 1].trim() !== judge.sentinel) {
    throw refusal(`the judge's record ${record} does not end in the judge's end marker, so it is incomplete`);
  }
  lines.pop();
  return lines;
}

function checkRecord(manifest, record) {
  const { fields, refs, errors } = parseRecord(recordLines(record, manifest.judge));
  if (errors.length > 0) {
    throw refusal(`the judge's record is malformed (${errors.join("; ")})`);
  }
  if ((fields.verdict || "").toLowerCase() !== REPLY_ALLOW) {
    throw refusal("the judge's record does not say `verdict: allow`");
  }
  if ((fields.method || "").toLowerCase() !== METHOD_RESTART) {
    throw refusal(`the judge's record proposes method '${fields.method}'; only '${METHOD_RESTART}' can be carried out here`);
  }
  if (!fields.limits) {
    throw refusal("the judge's record does not state its limits (`limits:`)");
  }
  if (refs.length === 0) {
    throw refusal("the judge's record cites no evidence line");
  }

  const listed = new Map(manifest.files.map((entry) => [util.foldPath(entry.path), entry]));
  const cited = refs.map((ref) => {
    const entry = listed.get(util.foldPath(ref.file));
    if (entry === undefined) {
      throw refusal(`the judge's record cites ${ref.file}, which is not in the evidence manifest`);
    }
    if (!(ref.first >= 1 && ref.first <= ref.last && ref.last <= entry.lines)) {
      throw refusal(`the judge's record cites lines ${ref.first}-${ref.last} of ${ref.file}, which has ${entry.lines}`);
    }
    return { ...ref, entry };
  });
  for (const entry of listed.values()) {
    let current = null;
    try {
      current = util.sha256Of(fs.readFileSync(entry.path));
    } catch {
      current = null;
    }
    if (current !== entry.sha256) {
      throw refusal(`evidence file ${entry.path} changed after it was listed`);
    }
  }
  return { fields, cited };
}

function checkLongerDeadline(manifest, fields, cited) {
  const given = fields["next-timeout"] || "";
  if (!/^[0-9]+$/.test(given)) {
    throw refusal("the attempt hit its deadline and the judge's record gives no whole-number `next-timeout`");
  }
  const proposed = Number(given);
  if (proposed <= manifest.d_prev) {
    throw refusal(`the judge's next-timeout ${proposed} is not longer than the deadline the attempt hit (${manifest.d_prev})`);
  }
  if (typeof manifest.t0 !== "number") {
    throw refusal("no stamped line of the attempt's transcript shows when it started");
  }
  const inside = cited
    .filter((ref) => ref.entry.kind === KIND_TRANSCRIPT)
    .flatMap((ref) => citedStamps(ref.entry.path, ref.first, ref.last))
    .some((stamp) => stamp >= manifest.t0 && stamp <= manifest.t0 + manifest.d_prev);
  if (!inside) {
    throw refusal("the judge's record cites no transcript line stamped inside the attempt's own run to support a longer deadline");
  }
  return proposed;
}

function check(args) {
  const { flags } = parseArguments(args, ["stem", "reply"]);
  const files = stemFiles(requireFlag(flags, "stem"));
  if (flags.reply === undefined) {
    throw new UsageError("--reply is required");
  }
  try {
    const manifest = util.readJson(files.evidence);
    if (manifest === null || !manifest.judge || !Array.isArray(manifest.files)) {
      throw refusal(`no judge was issued for this attempt (${files.evidence}) -- run \`judge_gate.js issue\` first`);
    }
    const record = manifest.judge.record_path;
    checkReply(flags.reply, record);
    const { fields, cited } = checkRecord(manifest, record);
    const timeout = manifest.cause === CAUSE_DEADLINE
      ? checkLongerDeadline(manifest, fields, cited)
      : manifest.d_prev;
    process.stdout.write(`${VERDICT_ALLOW} ${timeout} ${record}\n`);
    return util.EXIT_OK;
  } catch (err) {
    if (err instanceof Refusal) {
      process.stdout.write(`${VERDICT_REFUSED}\n`);
    }
    throw err;
  }
}

function main(argv) {
  const [command, ...rest] = argv;
  if (command === "issue") {
    return issue(rest);
  }
  if (command === "check") {
    return check(rest);
  }
  throw new UsageError(command === undefined ? "no subcommand" : `unknown subcommand: ${command}`);
}

util.runCli(SCRIPT, USAGE, main);
