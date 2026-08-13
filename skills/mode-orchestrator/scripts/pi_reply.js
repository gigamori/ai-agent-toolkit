#!/usr/bin/env node
// Extract a delegated Pi turn's REPLY from its `pi -p --mode json` event
// stream, and print nothing else.
//
//   node pi_reply.js <raw-event-stream.jsonl>
//
// Why this exists (references/harness-pi.md P1): the delegation's stdout is an
// event stream that is orders of magnitude larger than the reply it carries —
// every streaming delta, every tool round, plus the model's `thinking` blocks.
// Handing that to the orchestrator as a tool result overflows the bash tool's
// result budget, which spills it to a temp log the orchestrator then reads to
// find the reply; measured 2026-08-13, that is exactly what happened, and it
// pulls a whole turn's raw transcript into the context the skill's context
// discipline exists to keep clean. This script is the seam: the stream lands
// in a file (kept as evidence), and the tool result becomes the reply text
// alone.
//
// Contract:
//   stdout  the terminal turn_end's `text` blocks, concatenated, and nothing
//           else. `thinking` blocks are dropped — they are not the reply and
//           must not reach the orchestrator.
//   exit 0  a reply was extracted.
//   exit 2  usage / unreadable input.
//   exit 3  no turn_end in the stream.
//   exit 4  the terminal turn_end did not stop cleanly.
//
// On every non-zero exit stdout stays EMPTY and one line goes to stderr. That
// is deliberate: a half-extracted reply read as a real one is worse than no
// reply. A delegation whose extractor failed therefore reaches the
// orchestrator as a tool result with no `status:` line, which SKILL.md
// Execution step 4 already classifies as `aborted` (its missing-status path) —
// no new rule is needed on the orchestrator's side.
//
// Node's standard library only: node is already the launcher for every
// delegation (P1 runs the pi CLI entry through it), so this adds no dependency.

"use strict";

const fs = require("fs");

function die(code, message) {
  process.stderr.write(message + "\n");
  process.exit(code);
}

const file = process.argv[2];
if (!file || process.argv.length > 3) {
  die(2, "usage: pi_reply.js <raw-event-stream.jsonl>");
}

let raw;
try {
  raw = fs.readFileSync(file, "utf8");
} catch (err) {
  die(2, `cannot read ${file}: ${err.message}`);
}

// Last turn_end wins: a stream carries one per tool round (measured
// 2026-08-13 on pi 0.84.1 — 18 of them in a 17-tool-call run), and the reply
// is the terminal one. Parsing line by line and skipping unparseable lines is
// what makes this survive a stream cut off mid-write by a timeout kill.
let terminal = null;
for (const line of raw.split("\n")) {
  const trimmed = line.trim();
  if (!trimmed) continue;
  let event;
  try {
    event = JSON.parse(trimmed);
  } catch {
    continue;
  }
  if (event && event.type === "turn_end") terminal = event;
}

if (!terminal) die(3, `no turn_end in ${file}`);

const message = terminal.message || {};
const stopReason = message.stopReason;
if (stopReason !== "stop") {
  const detail = String(message.errorMessage || "").slice(0, 200);
  die(4, `stopReason=${String(stopReason)}${detail ? ` errorMessage=${detail}` : ""}`);
}

const blocks = Array.isArray(message.content) ? message.content : [];
const reply = blocks
  .filter((b) => b && b.type === "text")
  .map((b) => (typeof b.text === "string" ? b.text : ""))
  .join("");

// Not truncated on purpose. A turn that honours the reply contract answers in
// a gist plus a status line, so there is nothing to trim; one that does not is
// a deviation the orchestrator should see in full. The status line is read by
// position from the end either way.
process.stdout.write(reply);
