// What a delegated Claude Code turn left in its session transcripts: which
// files they are, whether any of them still changes, and whether any call in
// them may still have work running. The one place the restart-gate scripts
// find a turn's transcripts and read what may still be running in them.
// judge_gate.js reads the files the stop record lists, for checksums, sizes,
// line counts and time stamps only.
//
// A turn is found the way watchdog.sh and deny_scan.sh find it: by the
// description given on its delegation call, matched verbatim against the
// agent-*.meta.json files of the calling session only (the session that
// CLAUDE_CODE_SESSION_ID names). A turn's transcript, plus the transcript of
// every agent it started at any depth, is what a stop has to leave quiet.
//
// Node's standard library only.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");

const { jsonLines, readJson } = require("./gate_util");

const SESSION_ENV = "CLAUDE_CODE_SESSION_ID";
const CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR";

const META_SUFFIX = ".meta.json";
const TRANSCRIPT_SUFFIX = ".jsonl";
const AGENT_PREFIX = "agent-";
const SUBAGENTS_DIR = "subagents";
const GONE = "gone";

// Written at the top level of a refused or interrupted call's own transcript
// line, whatever refused it. Read as a parsed key, never matched as text: the
// same words inside tool output are escaped string content and must not count.
const DENIAL_FIELD = "toolDenialKind";

// Tools whose work can outlive their own call: an agent (in the background
// unless the call says otherwise), a monitor, and a skill, which may fork an
// agent. Whether such work has ended is not read from the transcript, so a
// call that started any of it keeps a stop unconfirmed.
const BACKGROUND_TOOLS = ["Agent", "Task", "Monitor", "Skill"];
const BACKGROUND_FLAG = "run_in_background";

function currentSession() {
  return (process.env[SESSION_ENV] || "").trim();
}

function projectsRoot(given) {
  if (given) {
    return given;
  }
  const configDir = (process.env[CONFIG_DIR_ENV] || "").trim()
    || path.join(os.homedir(), ".claude");
  return path.join(configDir, "projects");
}

function listDir(dir) {
  try {
    return fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return [];
  }
}

function metaFiles(directory) {
  return listDir(directory)
    .filter((entry) => entry.isFile() && entry.name.startsWith(AGENT_PREFIX)
      && entry.name.endsWith(META_SUFFIX))
    .map((entry) => path.join(directory, entry.name));
}

function transcriptOf(metaFile) {
  return metaFile.slice(0, -META_SUFFIX.length) + TRANSCRIPT_SUFFIX;
}

function agentIdOf(transcript) {
  return path.basename(transcript).slice(AGENT_PREFIX.length, -TRANSCRIPT_SUFFIX.length);
}

// The transcript of this session's agent delegated under `description`. When
// the description repeats, the most recently written meta file wins, as in
// deny_scan.sh.
function findTranscript(root, session, description) {
  let best = null;
  let bestMtime = -1;
  for (const project of listDir(root)) {
    if (!project.isDirectory()) {
      continue;
    }
    const agents = path.join(root, project.name, session, SUBAGENTS_DIR);
    for (const meta of metaFiles(agents)) {
      const info = readJson(meta);
      if (!info || info.description !== description) {
        continue;
      }
      const mtime = fs.statSync(meta).mtimeMs;
      if (mtime >= bestMtime) {
        best = meta;
        bestMtime = mtime;
      }
    }
  }
  return best === null ? null : transcriptOf(best);
}

function toolUseIds(transcript) {
  const ids = new Set();
  for (const { entry } of jsonLines(transcript)) {
    const content = entry.message && entry.message.content;
    if (!Array.isArray(content)) {
      continue;
    }
    for (const block of content) {
      if (block && block.type === "tool_use" && typeof block.id === "string") {
        ids.add(block.id);
      }
    }
  }
  return ids;
}

// Every agent that `transcript`'s agent started, at any depth. A child names
// its parent in its meta file by `toolUseId` (the Agent call that started it)
// or `parentAgentId` (all an agent a Skill forks carries).
function descendantsOf(transcript) {
  const directory = path.dirname(transcript);
  const metas = metaFiles(directory).map((meta) => ({ meta, info: readJson(meta) || {} }));
  const found = [];
  const frontier = [transcript];
  while (frontier.length > 0) {
    const parent = frontier.pop();
    const parentId = agentIdOf(parent);
    const started = toolUseIds(parent);
    for (const { meta, info } of metas) {
      const child = transcriptOf(meta);
      if (child === transcript || found.includes(child)) {
        continue;
      }
      if (info.parentAgentId === parentId || started.has(info.toolUseId)) {
        found.push(child);
        frontier.push(child);
      }
    }
  }
  return found;
}

// { transcripts, reason }: the turn's transcript followed by those of every
// agent it started -- or why there are none.
function delegationTranscripts(description, rootArgument) {
  const session = currentSession();
  if (!session) {
    return { transcripts: [], reason: "not running under Claude Code (no session id)" };
  }
  if (!description) {
    return { transcripts: [], reason: "no description was given" };
  }
  const transcript = findTranscript(projectsRoot(rootArgument), session, description);
  const readable = transcript !== null && fs.existsSync(transcript)
    && fs.statSync(transcript).isFile();
  if (!readable) {
    return {
      transcripts: [],
      reason: `no transcript of an agent delegated as '${description}'`,
    };
  }
  return { transcripts: [transcript, ...descendantsOf(transcript)], reason: "" };
}

function startsBackground(block) {
  const input = block.input;
  const flag = input && typeof input === "object" ? input[BACKGROUND_FLAG] : undefined;
  if (flag === true) {
    return true;
  }
  return BACKGROUND_TOOLS.includes(block.name) && flag !== false;
}

// The calls and the answered call ids of one transcript, plus every call that
// was refused or interrupted.
function readCalls(file) {
  const calls = new Map();
  const answered = new Set();
  const interrupted = [];
  for (const { entry } of jsonLines(file)) {
    const kind = entry[DENIAL_FIELD];
    if (typeof kind === "string" && kind) {
      interrupted.push(kind);
    }
    const content = entry.message && entry.message.content;
    if (!Array.isArray(content)) {
      continue;
    }
    for (const block of content) {
      if (!block || typeof block !== "object") {
        continue;
      }
      if (block.type === "tool_use" && typeof block.id === "string") {
        calls.set(block.id, block);
      } else if (block.type === "tool_result" && typeof block.tool_use_id === "string") {
        answered.add(block.tool_use_id);
      }
    }
  }
  return { calls, answered, interrupted };
}

// Why work recorded in these transcripts may still be running, one line per
// call: a tool call with no result, a call that started work in the
// background, and a call refused or interrupted -- a stop interrupts a
// running call that way, so its result says nothing about the work.
function openToolCalls(paths) {
  const found = [];
  for (const file of paths) {
    const name = path.basename(file);
    const { calls, answered, interrupted } = readCalls(file);
    for (const kind of interrupted) {
      found.push(`a call was refused or interrupted (${kind}) in ${name}`);
    }
    for (const [id, block] of calls) {
      const tool = block.name || "?";
      if (!answered.has(id)) {
        found.push(`${tool} ${id} has no result in ${name}`);
      } else if (startsBackground(block)) {
        found.push(`${tool} ${id} started background work in ${name}`);
      }
    }
  }
  return found;
}

// path -> "size:mtime" for each file; a file that cannot be read is "gone".
// Two snapshots that differ mean a transcript was written, or came or went.
function fileStamps(paths) {
  const stamps = new Map();
  for (const file of paths) {
    try {
      const stat = fs.statSync(file);
      stamps.set(file, `${stat.size}:${stat.mtimeMs}`);
    } catch {
      stamps.set(file, GONE);
    }
  }
  return stamps;
}

function sameStamps(a, b) {
  if (a.size !== b.size) {
    return false;
  }
  for (const [file, stamp] of a) {
    if (b.get(file) !== stamp) {
      return false;
    }
  }
  return true;
}

module.exports = {
  delegationTranscripts,
  fileStamps,
  openToolCalls,
  sameStamps,
};
