// delegation_plan.js -- the v2 delegation-plan validator: schema, source
// priority, references, digests, approval bindings, launch agreement, audit.
//
//   node delegation_plan.js validate --file <index.md|ledger.jsonl>
//        [--approvals approvals.jsonl] [--resolutions resolutions.jsonl]
//        [--policy policy.jsonl]
//   node delegation_plan.js dispatch-check --file <...> --dispatch <call.json>
//        [same evidence flags]
//   node delegation_plan.js audit --file <...> [same evidence flags]
//
// Structures and rules: references/delegation-contract.md. This script never
// launches a model, a CLI or an agent; it proves the integrity of the data
// handed to it and nothing more.
//
// Exit 0 prints one OK line. Exit 1 prints VIOLATION lines (stdout) or one
// REFUSED line (stderr): refused means the input could not be validated at
// all (not a mo-delegation-v2 block, an unparseable line), a violation means
// the ledger was parsed and found broken. Exit 64 usage, exit 70 internal.
//
// Needs node.

"use strict";

const crypto = require("crypto");
const fs = require("fs");

const util = require("./gate_util");

const { Refusal, UsageError, parseArguments, requireFlag, runCli } = util;

const SCRIPT = "delegation_plan";

const V2 = "mo-delegation-v2";

const CONTRACT_FIRST = "contract";

const KINDS = [
  "planned",
  "recovery-debug",
  "recovery-reexecute",
  "decision-turn",
  "decision-rerun",
];
const DYNAMIC_KINDS = KINDS.filter((k) => k !== "planned");

const EFFORTS = ["basic", "pro", "ultra"];
const EFFORT_SOURCES = [
  "step-model",
  "step-effort",
  "workflow-effort",
  "inferred-effort",
  "policy-effort",
  "inherited",
];
const SOURCES = ["runtime", "environment", "skill"];
const GRANULARITIES = ["provider-model", "harness-ref"];
const THINKING_STATES = ["fixed", "host-inherited", "undisclosed"];
const MATCH_KINDS = ["exact", "alias", "prefix", "series", "nl-interpret"];
const APPROVAL_KINDS = ["user-plan", "auto-invocation", "derived"];
const AMENDMENT_CAUSES = ["todolist", "model-resolution", "policy", "user-revision"];
const OWNER_KINDS = ["turn", "restart-judge"];

const REPORTED_STATUS = ["ok", "failed", "blocked", "needs-human", "needs-decision", "aborted"];
const EFFECTIVE_STATUS = REPORTED_STATUS.concat(["stale-plan"]);
const EFFECTIVE_ONLY = ["stale-plan"];

const ENV_FALL_THROUGH = ["absent", "unset"];
const ENV_CONFIGURED = "configured";

const FIELD_SETS = {
  contract: { required: ["record", "version"], optional: [] },
  plan: {
    required: ["record", "id", "replaces", "after_turn", "digest", "approval_ref", "contingency_policy_ref"],
    optional: [],
  },
  turn: {
    required: ["record", "key", "plan", "kind", "step", "parent", "inherits", "inputs", "mode",
      "effort", "effort_source", "budget_refs", "rebinds", "amendment_ref", "selection", "dispatch_plan"],
    optional: ["role", "amendment_item"],
  },
  delegation: {
    required: ["record", "id", "owner", "owner_kind", "plan", "approval_ref", "resolution_ref",
      "policy_ref", "route", "actual_selection_args", "call_ref", "reported_model", "reported_thinking"],
    optional: [],
  },
  attempt: {
    required: ["record", "turn", "attempt", "delegation_id", "reported_status", "effective_status", "file"],
    optional: [],
  },
  decision: { required: ["record", "key", "parent", "decider", "file"], optional: [] },
  amendment: {
    required: ["record", "key", "plan", "replaces", "after_turn", "parent", "cause", "source_file",
      "source_sha256", "supersedes", "replacements"],
    optional: [],
  },
};

const EVIDENCE_SETS = {
  approvals: {
    approval: { required: ["record", "key", "kind", "target_digest", "event_ref", "parent_approval_ref", "scope"], optional: ["snapshot_keys", "contingency_ref"] },
    contingency: {
      required: ["record", "key", "allowed_kinds", "origin_turn", "mode_policy", "effort_policy",
        "model_binding", "scope", "tool_constraints", "caps"],
      optional: [],
    },
  },
  resolutions: {
    resolution: {
      required: ["record", "key", "turn", "request", "source", "observed_state", "candidates",
        "match", "resolved_model", "granularity", "thinking", "grounds"],
      optional: [],
    },
  },
  policy: {
    permission: { required: ["record", "key", "state", "scope", "revision", "grounds"], optional: [] },
    "route-policy": { required: ["record", "key", "route", "state", "constraints", "grounds"], optional: [] },
    "env-model-setting": { required: ["record", "key", "state", "per_effort", "grounds"], optional: [] },
  },
};

const TURN_KEY = /^[0-9]{2,}[a-z]*$/;
const PLANNED_KEY = /^[0-9]{2,}$/;
const AMENDMENT_KEY = /^amend-[0-9]+$/;
const PLAN_ID = /^r[0-9]+$/;
const JUDGE_OWNER = /^[A-Za-z0-9-]+-[0-9]+$/;

const MAX_PRINTED_VIOLATIONS = 20;

function canonicalJson(value) {
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(",")}]`;
  }
  if (value !== null && typeof value === "object") {
    const keys = Object.keys(value).sort();
    return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(value[k])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function digestOf(snapshot) {
  return crypto.createHash("sha256").update(Buffer.from(canonicalJson(snapshot), "utf8")).digest("hex");
}

function originKey(key) {
  return key.replace(/[a-z]+$/, "");
}

function sameJson(a, b) {
  return canonicalJson(a) === canonicalJson(b);
}

function isObject(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

class Ledger {
  constructor(records, source) {
    this.records = records;
    this.source = source;
    this.byKey = new Map();
    this.turns = [];
    this.plans = [];
    this.amendments = [];
    this.delegations = [];
    this.attempts = [];
    this.decisions = [];
    this.contracts = [];
  }

  index() {
    for (const { line, entry } of this.records) {
      const record = entry.record;
      if (record === "contract") this.contracts.push({ line, entry });
      else if (record === "plan") this.plans.push({ line, entry });
      else if (record === "turn") this.turns.push({ line, entry });
      else if (record === "amendment") this.amendments.push({ line, entry });
      else if (record === "delegation") this.delegations.push({ line, entry });
      else if (record === "attempt") this.attempts.push({ line, entry });
      else if (record === "decision") this.decisions.push({ line, entry });
    }
    for (const list of [this.plans, this.turns, this.amendments, this.delegations, this.decisions]) {
      for (const { line, entry } of list) {
        const keyField = entry.id !== undefined ? "id" : "key";
        const key = entry[keyField];
        if (this.byKey.has(key)) {
          throw new Refusal(`${this.source}: duplicate key at line ${line}: ${key}`);
        }
        this.byKey.set(key, { line, entry });
      }
    }
  }

  lineOf(key) {
    const found = this.byKey.get(key);
    return found ? found.line : null;
  }

  get(key) {
    const found = this.byKey.get(key);
    return found ? found.entry : null;
  }

  recordOf(key) {
    const found = this.byKey.get(key);
    return found ? found.entry.record : null;
  }
}

function extractLedgerBlock(text, source) {
  const lines = text.split("\n");
  const blocks = [];
  let inBlock = false;
  let current = [];
  for (const line of lines) {
    if (!inBlock && /^```/.test(line)) {
      inBlock = true;
      current = [];
      continue;
    }
    if (inBlock && /^```/.test(line)) {
      blocks.push(current);
      inBlock = false;
      continue;
    }
    if (inBlock) current.push(line);
  }
  for (const block of blocks) {
    const first = block.map((l) => l.trim()).find((l) => l !== "");
    if (!first) continue;
    let parsed;
    try {
      parsed = JSON.parse(first);
    } catch {
      continue;
    }
    if (isObject(parsed) && parsed.record === CONTRACT_FIRST) {
      return block.join("\n");
    }
  }
  throw new Refusal(`${source}: no JSONL block whose first record is ${CONTRACT_FIRST}`);
}

function readLedger(file) {
  let text;
  try {
    text = fs.readFileSync(file, "utf8");
  } catch {
    throw new Refusal(`${file}: cannot read`);
  }
  const body = /\.md$/i.test(file) ? extractLedgerBlock(text, file) : text;
  const records = [];
  const lines = body.split("\n");
  for (let index = 0; index < lines.length; index += 1) {
    const trimmed = lines[index].trim();
    if (trimmed === "") continue;
    let entry;
    try {
      entry = JSON.parse(trimmed);
    } catch {
      throw new Refusal(`${file}: unparseable ledger line ${index + 1}`);
    }
    if (!isObject(entry) || typeof entry.record !== "string") {
      throw new Refusal(`${file}: line ${index + 1} is not a record object`);
    }
    records.push({ line: index + 1, entry });
  }
  if (records.length === 0) {
    throw new Refusal(`${file}: empty ledger`);
  }
  const first = records[0].entry;
  if (first.record !== "contract" || first.version !== V2) {
    const version = first.record === "contract" ? first.version : "(no contract record first)";
    throw new Refusal(`${file}: not a ${V2} ledger (first record: ${version})`);
  }
  return new Ledger(records, file);
}

function readEvidenceFile(file, kinds, label) {
  let text;
  try {
    text = fs.readFileSync(file, "utf8");
  } catch {
    throw new Refusal(`${file}: cannot read`);
  }
  const byKey = new Map();
  const entries = [];
  const lines = text.split("\n");
  for (let index = 0; index < lines.length; index += 1) {
    const trimmed = lines[index].trim();
    if (trimmed === "") continue;
    let entry;
    try {
      entry = JSON.parse(trimmed);
    } catch {
      throw new Refusal(`${file}: unparseable ${label} line ${index + 1}`);
    }
    if (!isObject(entry) || !kinds.includes(entry.record)) {
      throw new Refusal(`${file}: line ${index + 1} is not a ${kinds.join("/")} record`);
    }
    const spec = EVIDENCE_SETS[label][entry.record];
    const extra = Object.keys(entry).filter((k) => !spec.required.includes(k) && !spec.optional.includes(k));
    if (extra.length > 0) {
      throw new Refusal(`${file}: line ${index + 1} ${entry.record} has unlisted keys: ${extra.join(", ")}`);
    }
    for (const key of spec.required) {
      if (!(key in entry)) {
        throw new Refusal(`${file}: line ${index + 1} ${entry.record} misses ${key}`);
      }
    }
    if (byKey.has(entry.key)) {
      throw new Refusal(`${file}: duplicate ${entry.record} key at line ${index + 1}: ${entry.key}`);
    }
    byKey.set(entry.key, entry);
    entries.push({ line: index + 1, entry });
  }
  return { byKey, entries, file };
}

class Checker {
  constructor(ledger, evidence) {
    this.ledger = ledger;
    this.evidence = evidence;
    this.violations = [];
    this.checks = 0;
  }

  violation(where, message) {
    this.violations.push(`${where}: ${message}`);
  }

  check(condition, where, message) {
    this.checks += 1;
    if (!condition) this.violation(where, message);
    return condition;
  }

  fieldSet(entry, where) {
    const spec = FIELD_SETS[entry.record];
    if (!spec) return;
    const present = Object.keys(entry);
    for (const key of present) {
      if (!spec.required.includes(key) && !spec.optional.includes(key)) {
        this.violation(where, `unlisted key: ${key}`);
      }
    }
    for (const key of spec.required) {
      if (!(key in entry)) this.violation(where, `missing key: ${key}`);
    }
    if (spec.optional.includes("amendment_item") && entry.amendment_item !== undefined) {
      if (entry.amendment_ref === null || entry.amendment_ref === undefined) {
        this.violation(where, "amendment_item present without amendment_ref");
      }
    }
  }

  checkSelection(selection, where) {
    const spec = { required: ["source", "request", "scope_ref", "resolution_ref", "resolved_model", "granularity", "thinking"], optional: [] };
    for (const key of Object.keys(selection)) {
      if (!spec.required.includes(key)) this.violation(where, `selection unlisted key: ${key}`);
    }
    for (const key of spec.required) {
      if (!(key in selection)) this.violation(where, `selection missing key: ${key}`);
    }
    if (!SOURCES.includes(selection.source)) {
      this.violation(where, `selection.source not in ${SOURCES.join("|")}: ${selection.source}`);
    }
    if (!GRANULARITIES.includes(selection.granularity)) {
      this.violation(where, `selection.granularity not in ${GRANULARITIES.join("|")}`);
    }
    const thinking = selection.thinking;
    if (!isObject(thinking) || !THINKING_STATES.includes(thinking.state)) {
      this.violation(where, `selection.thinking.state not in ${THINKING_STATES.join("|")}`);
    } else if (thinking.state === "fixed" && typeof thinking.value !== "string") {
      this.violation(where, "selection.thinking fixed without value");
    }
    if (typeof selection.request !== "string" || selection.request === "") {
      this.violation(where, "selection.request must be the original wording");
    }
    if (typeof selection.scope_ref !== "string") {
      this.violation(where, "selection.scope_ref must be a reference");
    }
  }

  checkDispatchPlan(plan, where) {
    if (!isObject(plan) || !("route" in plan) || !("args" in plan)) {
      this.violation(where, "dispatch_plan needs route and args");
      return;
    }
    const extra = Object.keys(plan).filter((k) => k !== "route" && k !== "args");
    if (extra.length > 0) this.violation(where, `dispatch_plan unlisted keys: ${extra.join(", ")}`);
    if (typeof plan.route !== "string" || plan.route === "") {
      this.violation(where, "dispatch_plan.route must be a route name");
    }
    this.checkBindingArgs(plan.args, where, "dispatch_plan.args");
  }

  checkBindingArgs(args, where, label) {
    if (!isObject(args)) {
      this.violation(where, `${label} must be an object`);
      return null;
    }
    const allowed = ["model", "effort", "resolution_ref"];
    const present = Object.keys(args).filter((k) => allowed.includes(k));
    const extra = Object.keys(args).filter((k) => !allowed.includes(k));
    if (extra.length > 0) this.violation(where, `${label} unlisted keys: ${extra.join(", ")}`);
    this.check(present.length === 1, where, `${label} carries exactly one of model|effort|resolution_ref (got ${present.join(",") || "none"})`);
    return present;
  }

  checkBudgetRefs(refs, where) {
    const spec = ["recovery_owner", "decision_owner", "restart_owner"];
    if (!isObject(refs)) {
      this.violation(where, "budget_refs must be an object");
      return;
    }
    const extra = Object.keys(refs).filter((k) => !spec.includes(k));
    if (extra.length > 0) this.violation(where, `budget_refs unlisted keys: ${extra.join(", ")}`);
    for (const key of spec) {
      if (typeof refs[key] !== "string" || refs[key] === "") {
        this.violation(where, `budget_refs.${key} must be a turn key`);
      }
    }
  }

  structure() {
    const ledger = this.ledger;
    const first = ledger.records[0];
    this.check(first.entry.record === "contract" && first.entry.version === V2, `line ${first.line}`, "first record must be the v2 contract");
    const seenTurnKeys = new Set();
    for (const { line, entry } of ledger.records) {
      const where = `line ${line} (${entry.record})`;
      if (!FIELD_SETS[entry.record]) {
        this.violation(where, "unknown record kind");
        continue;
      }
      this.fieldSet(entry, where);
      if (entry.record === "turn") {
        if (typeof entry.key !== "string" || !TURN_KEY.test(entry.key)) {
          this.violation(where, `turn key shape: ${entry.key}`);
        } else if (seenTurnKeys.has(entry.key)) {
          this.violation(where, `duplicate turn key: ${entry.key}`);
        }
        seenTurnKeys.add(entry.key);
        if (entry.kind !== undefined && !KINDS.includes(entry.kind)) {
          this.violation(where, `kind not in ${KINDS.join("|")}: ${entry.kind}`);
        }
        if (entry.effort !== null && entry.effort !== undefined && !EFFORTS.includes(entry.effort)) {
          this.violation(where, `effort not in ${EFFORTS.join("|")}|null: ${entry.effort}`);
        }
        if (!EFFORT_SOURCES.includes(entry.effort_source)) {
          this.violation(where, `effort_source not in ${EFFORT_SOURCES.join("|")}`);
        }
        if (!Array.isArray(entry.inputs)) this.violation(where, "inputs must be an array of turn keys");
        if (isObject(entry.selection)) this.checkSelection(entry.selection, where);
        this.checkDispatchPlan(entry.dispatch_plan, where);
        this.checkBudgetRefs(entry.budget_refs, where);
        if (entry.amendment_ref !== null && entry.amendment_ref !== undefined && typeof entry.amendment_item !== "string") {
          this.violation(where, "amendment_item required when amendment_ref is set");
        }
      } else if (entry.record === "plan") {
        if (typeof entry.id !== "string" || !PLAN_ID.test(entry.id)) this.violation(where, `plan id shape: ${entry.id}`);
        if (typeof entry.digest !== "string" || !/^[0-9a-f]{64}$/.test(entry.digest)) this.violation(where, "plan.digest must be sha256 hex");
      } else if (entry.record === "delegation") {
        if (!OWNER_KINDS.includes(entry.owner_kind)) this.violation(where, `owner_kind not in ${OWNER_KINDS.join("|")}`);
        if (entry.owner_kind === "restart-judge" && !JUDGE_OWNER.test(String(entry.owner))) {
          this.violation(where, "restart-judge owner must derive from an attempt stem");
        }
        this.checkBindingArgs(entry.actual_selection_args, where, "actual_selection_args");
        if (typeof entry.route !== "string" || entry.route === "") this.violation(where, "delegation.route must be a route name");
      } else if (entry.record === "attempt") {
        if (typeof entry.attempt !== "number" || !Number.isInteger(entry.attempt) || entry.attempt < 1) {
          this.violation(where, "attempt must be a positive integer");
        }
        if (entry.effective_status !== undefined && !EFFECTIVE_STATUS.includes(entry.effective_status)) {
          this.violation(where, `effective_status not in ${EFFECTIVE_STATUS.join("|")}: ${entry.effective_status}`);
        }
        if (entry.reported_status !== null && entry.reported_status !== undefined && !REPORTED_STATUS.includes(entry.reported_status)) {
          this.violation(where, `reported_status not in ${REPORTED_STATUS.join("|")}|null: ${entry.reported_status}`);
        }
        if (EFFECTIVE_ONLY.includes(entry.effective_status) && entry.reported_status !== null) {
          this.violation(where, `${entry.effective_status} is effective-only; reported_status must be null`);
        }
      } else if (entry.record === "amendment") {
        if (!AMENDMENT_CAUSES.includes(entry.cause)) this.violation(where, `cause not in ${AMENDMENT_CAUSES.join("|")}`);
        if (!Array.isArray(entry.supersedes)) this.violation(where, "supersedes must be an array of turn keys");
        if (!Array.isArray(entry.replacements)) {
          this.violation(where, "replacements must be an array");
        } else {
          for (const replacement of entry.replacements) {
            if (!isObject(replacement)) {
              this.violation(where, "each replacement must be an object");
              continue;
            }
            const keys = Object.keys(replacement);
            if (keys.length !== 2 || !keys.includes("key") || !keys.includes("amendment_item")) {
              this.violation(where, `replacement must be exactly {key, amendment_item}: ${JSON.stringify(replacement)}`);
            }
          }
        }
        if (typeof entry.source_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(entry.source_sha256)) {
          this.violation(where, "source_sha256 must be sha256 hex");
        }
      } else if (entry.record === "decision") {
        if (entry.decider !== "human") this.violation(where, "decision record is human-limited");
      }
      this.checks += 1;
    }
  }

  references() {
    const ledger = this.ledger;
    for (const { line, entry } of ledger.turns) {
      const where = `line ${line} (turn ${entry.key})`;
      if (ledger.get(entry.plan) === null || ledger.get(entry.plan).record !== "plan") {
        this.violation(where, `plan reference unresolved: ${entry.plan}`);
      }
      for (const field of ["parent", "inherits"]) {
        const value = entry[field];
        if (value !== null && (ledger.get(value) === null || ledger.get(value).record !== "turn")) {
          this.violation(where, `${field} reference unresolved: ${value}`);
        }
      }
      if (Array.isArray(entry.inputs)) {
        for (const input of entry.inputs) {
          if (ledger.get(input) === null || ledger.get(input).record !== "turn") {
            this.violation(where, `inputs reference unresolved: ${input}`);
          }
        }
      }
      if (entry.rebinds !== null && entry.rebinds !== undefined) {
        const target = ledger.get(entry.rebinds);
        if (target === null || target.record !== "turn") {
          this.violation(where, `rebinds reference unresolved: ${entry.rebinds}`);
        }
      }
      if (entry.amendment_ref !== null && entry.amendment_ref !== undefined) {
        const amendment = ledger.get(entry.amendment_ref);
        if (amendment === null || amendment.record !== "amendment") {
          this.violation(where, `amendment_ref unresolved: ${entry.amendment_ref}`);
        }
      }
    }
    for (const { line, entry } of ledger.plans) {
      const where = `line ${line} (plan ${entry.id})`;
      if (entry.replaces !== null && (ledger.get(entry.replaces) === null || ledger.get(entry.replaces).record !== "plan")) {
        this.violation(where, `replaces unresolved: ${entry.replaces}`);
      }
      if (entry.after_turn !== null && (ledger.get(entry.after_turn) === null || ledger.get(entry.after_turn).record !== "turn")) {
        this.violation(where, `after_turn unresolved: ${entry.after_turn}`);
      }
    }
    for (const { line, entry } of ledger.amendments) {
      const where = `line ${line} (amendment ${entry.key})`;
      if (ledger.get(entry.plan) === null || ledger.get(entry.plan).record !== "plan") {
        this.violation(where, `plan unresolved: ${entry.plan}`);
      }
      if (ledger.get(entry.replaces) === null || ledger.get(entry.replaces).record !== "plan") {
        this.violation(where, `replaces unresolved: ${entry.replaces}`);
      }
      if (entry.parent !== null && (ledger.get(entry.parent) === null || !["turn", "decision"].includes(ledger.get(entry.parent).record))) {
        this.violation(where, `parent unresolved: ${entry.parent}`);
      }
      if (entry.after_turn !== null && (ledger.get(entry.after_turn) === null || ledger.get(entry.after_turn).record !== "turn")) {
        this.violation(where, `after_turn unresolved: ${entry.after_turn}`);
      }
      for (const key of entry.supersedes || []) {
        const target = ledger.get(key);
        if (target === null || target.record !== "turn") this.violation(where, `supersedes unresolved: ${key}`);
      }
      for (const replacement of entry.replacements || []) {
        if (!isObject(replacement)) continue;
        const turn = ledger.get(replacement.key);
        if (turn === null || turn.record !== "turn") {
          this.violation(where, `replacement key has no turn record: ${replacement.key}`);
        } else if (turn.amendment_ref !== entry.key || turn.amendment_item !== replacement.amendment_item) {
          this.violation(where, `replacement turn does not carry this amendment_ref/amendment_item: ${replacement.key}`);
        }
      }
    }
    for (const { line, entry } of ledger.delegations) {
      const where = `line ${line} (delegation ${entry.id})`;
      if (entry.owner_kind === "turn" && (ledger.get(entry.owner) === null || ledger.get(entry.owner).record !== "turn")) {
        this.violation(where, `owner turn unresolved: ${entry.owner}`);
      }
      if (ledger.get(entry.plan) === null || ledger.get(entry.plan).record !== "plan") {
        this.violation(where, `plan unresolved: ${entry.plan}`);
      }
      if (this.evidence.approvals) {
        this.check(this.evidence.approvals.byKey.has(entry.approval_ref), where, `approval_ref unresolved: ${entry.approval_ref}`);
      }
      if (this.evidence.resolutions) {
        this.check(this.evidence.resolutions.byKey.has(entry.resolution_ref), where, `resolution_ref unresolved: ${entry.resolution_ref}`);
      }
      if (this.evidence.policy) {
        this.check(this.evidence.policy.byKey.has(entry.policy_ref), where, `policy_ref unresolved: ${entry.policy_ref}`);
      }
    }
    for (const { line, entry } of ledger.attempts) {
      const where = `line ${line} (attempt ${entry.turn}-${entry.attempt})`;
      if (ledger.get(entry.turn) === null || ledger.get(entry.turn).record !== "turn") {
        this.violation(where, `turn unresolved: ${entry.turn}`);
      }
      const delegation = ledger.get(entry.delegation_id);
      if (delegation === null || delegation.record !== "delegation") {
        this.violation(where, `delegation_id unresolved: ${entry.delegation_id}`);
      }
    }
    for (const { line, entry } of ledger.decisions) {
      const where = `line ${line} (decision ${entry.key})`;
      if (ledger.get(entry.parent) === null || ledger.get(entry.parent).record !== "turn") {
        this.violation(where, `parent unresolved: ${entry.parent}`);
      }
    }
  }

  lineage() {
    const ledger = this.ledger;
    for (const { line, entry } of ledger.turns) {
      const where = `line ${line} (turn ${entry.key})`;
      const kind = entry.kind;
      const isDynamic = DYNAMIC_KINDS.includes(kind);
      const planned = kind === "planned";
      if (planned && (entry.parent !== null || entry.inherits !== null)) {
        this.violation(where, "a planned turn has null parent/inherits");
      }
      if (isDynamic && entry.parent === null) {
        this.violation(where, "a dynamic turn needs its loop parent");
      }
      if (entry.rebinds !== null && entry.rebinds !== undefined) {
        if (entry.amendment_ref === null || entry.amendment_ref === undefined) {
          this.violation(where, "a rebind goes through an amendment (amendment_ref required)");
        }
        const target = ledger.get(entry.rebinds);
        if (target !== null && target.record === "turn") {
          const old = target;
          this.check(kind === old.kind, where, `rebind keeps kind (${old.kind} -> ${kind})`);
          this.check(entry.mode === old.mode, where, "rebind keeps mode");
          this.check(entry.role === old.role, where, "rebind keeps role");
          this.check(entry.step === old.step, where, "rebind keeps the step's own work text");
          this.check(sameJson(entry.budget_refs, old.budget_refs), where, "rebind keeps budget_refs from the origin turn");
          this.check(entry.plan !== old.plan, where, "a rebind's turn belongs to the new plan");
          this.check(entry.parent === this.mappedParent(old, entry), where, "rebind keeps the loop parent (mapped within the replacement tail)");
        }
      }
    }
    const rebindTargets = new Map();
    for (const { line, entry } of ledger.turns) {
      if (entry.rebinds === null || entry.rebinds === undefined) continue;
      if (rebindTargets.has(entry.rebinds)) {
        this.violation(`line ${line} (turn ${entry.key})`, `branching successors rebind one target: ${entry.rebinds} already rebound at line ${rebindTargets.get(entry.rebinds)}`);
      }
      rebindTargets.set(entry.rebinds, line);
    }
    for (const { line, entry } of ledger.turns) {
      const seen = new Set();
      let cursor = entry;
      while (cursor !== null && cursor.rebinds !== null && cursor.rebinds !== undefined) {
        if (seen.has(cursor.key)) {
          this.violation(`line ${line} (turn ${entry.key})`, `rebinds chain is cyclic at ${cursor.key}`);
          break;
        }
        seen.add(cursor.key);
        const next = ledger.get(cursor.rebinds);
        if (next === null || next.record !== "turn") break;
        cursor = next;
      }
    }
  }

  mappedParent(oldTurn, newTurn) {
    const amendment = newTurn.amendment_ref ? this.ledger.get(newTurn.amendment_ref) : null;
    if (!amendment || amendment.record !== "amendment") return oldTurn.parent;
    const supersedes = amendment.supersedes || [];
    const replacements = amendment.replacements || [];
    const index = supersedes.indexOf(oldTurn.parent);
    if (oldTurn.parent !== null && index >= 0 && isObject(replacements[index])) {
      return replacements[index].key;
    }
    return oldTurn.parent;
  }

  budgets() {
    const ledger = this.ledger;
    for (const { line, entry } of ledger.turns) {
      const where = `line ${line} (turn ${entry.key})`;
      const refs = entry.budget_refs;
      if (!isObject(refs) || typeof refs.recovery_owner !== "string") continue;
      const isDynamic = DYNAMIC_KINDS.includes(entry.kind);
      if (entry.rebinds !== null && entry.rebinds !== undefined) continue;
      if (isDynamic) {
        const origin = originKey(entry.key);
        this.check(refs.recovery_owner === origin && refs.decision_owner === origin, where, "dynamic turn charges recovery/decision to its originating turn");
        this.check(refs.restart_owner === entry.key, where, "dynamic turn owns its restart budget");
      } else {
        this.check(refs.recovery_owner === entry.key && refs.decision_owner === entry.key && refs.restart_owner === entry.key, where, "first-appearance planned turn owns all three budgets");
      }
    }
  }

  snapshotKeysFor(planEntry) {
    const planId = planEntry.id;
    if (this.evidence.approvals) {
      const approval = this.evidence.approvals.byKey.get(planEntry.approval_ref);
      if (approval && approval.record === "approval" && Array.isArray(approval.snapshot_keys)) {
        return { keys: approval.snapshot_keys, source: "approval evidence" };
      }
    }
    const isInitial = planEntry.replaces === null;
    const keys = [];
    for (const { entry } of this.ledger.turns) {
      if (entry.plan !== planId) continue;
      if (isInitial) {
        if (entry.kind === "planned" && (entry.amendment_ref === null || entry.amendment_ref === undefined) && (entry.rebinds === null || entry.rebinds === undefined)) {
          keys.push(entry.key);
        }
      } else if (entry.amendment_ref !== null && entry.amendment_ref !== undefined) {
        keys.push(entry.key);
      }
    }
    const hasContingency = planEntry.contingency_policy_ref !== null && planEntry.contingency_policy_ref !== undefined;
    if (hasContingency && !this.evidence.approvals) {
      return { keys: null, source: "unverifiable" };
    }
    return { keys, source: "ledger structure" };
  }

  snapshotFor(planEntry, keys) {
    const turns = keys
      .map((key) => this.ledger.get(key))
      .filter((t) => t !== null && t.record === "turn");
    return {
      contingency_policy_ref: planEntry.contingency_policy_ref,
      turns: turns.map((t) => ({
        key: t.key,
        kind: t.kind,
        step: t.step,
        parent: t.parent,
        inherits: t.inherits,
        inputs: t.inputs,
        mode: t.mode,
        role: t.role === undefined ? null : t.role,
        effort: t.effort,
        budget_refs: t.budget_refs,
        selection: {
          source: t.selection.source,
          request: t.selection.request,
          scope_ref: t.selection.scope_ref,
          resolved_model: t.selection.resolved_model,
          granularity: t.selection.granularity,
          thinking: t.selection.thinking,
        },
        dispatch_plan: t.dispatch_plan,
      })),
    };
  }

  digests() {
    for (const { line, entry } of this.ledger.plans) {
      const where = `line ${line} (plan ${entry.id})`;
      const { keys, source } = this.snapshotKeysFor(entry);
      if (keys === null) {
        this.violation(where, `digest not verifiable without snapshot_keys in approval evidence (${source})`);
        continue;
      }
      const computed = digestOf(this.snapshotFor(entry, keys));
      this.check(computed === entry.digest, where, `digest mismatch (recorded ${entry.digest}, computed ${computed} over ${source})`);
    }
  }

  approvals() {
    const evidence = this.evidence;
    if (!evidence.approvals) return;
    const ledger = this.ledger;
    for (const { line, entry } of ledger.plans) {
      const where = `line ${line} (plan ${entry.id})`;
      const approval = evidence.approvals.byKey.get(entry.approval_ref);
      if (!approval) {
        this.violation(where, `approval_ref unresolved in approvals evidence: ${entry.approval_ref}`);
        continue;
      }
      if (!APPROVAL_KINDS.includes(approval.kind)) {
        this.violation(where, `approval kind not in ${APPROVAL_KINDS.join("|")}: ${approval.kind}`);
        continue;
      }
      this.check(approval.target_digest === entry.digest, where, "approval.target_digest must equal the plan digest");
      if (approval.kind === "auto-invocation") {
        this.check(typeof approval.event_ref === "string" && approval.event_ref !== "", where, "auto-invocation carries the original invocation event reference");
        if (evidence.resolutions) {
          const { keys } = this.snapshotKeysFor(entry);
          if (keys !== null) {
            for (const key of keys) {
              const turn = ledger.get(key);
              if (!turn || turn.record !== "turn") continue;
              const resolution = evidence.resolutions.byKey.get(turn.selection.resolution_ref);
              this.check(resolution !== undefined && resolution.match === "exact", where, `auto-invocation needs exact resolution for ${key} (got ${resolution ? resolution.match : "unresolved"})`);
            }
          }
        }
      } else if (approval.kind === "user-plan") {
        this.check(typeof approval.event_ref === "string" && approval.event_ref !== "", where, "user-plan carries the original approval event reference");
      } else if (approval.kind === "derived") {
        const parent = evidence.approvals.byKey.get(approval.parent_approval_ref);
        this.check(parent !== undefined, where, `derived parent approval unresolved: ${approval.parent_approval_ref}`);
      }
      if (entry.contingency_policy_ref !== null && entry.contingency_policy_ref !== undefined) {
        const contingency = evidence.approvals.byKey.get(entry.contingency_policy_ref);
        this.check(contingency !== undefined && contingency.record === "contingency", where, `contingency_policy_ref unresolved: ${entry.contingency_policy_ref}`);
      }
    }
  }

  evidenceRequirements() {
    const evidence = this.evidence;
    if (!evidence.approvals && this.ledger.plans.length > 0) {
      this.violation("evidence", "approval evidence required (--approvals): plan approval bindings unverifiable without it");
    }
    if (!evidence.resolutions && this.ledger.turns.length > 0) {
      this.violation("evidence", "resolution evidence required (--resolutions): source and match rules unverifiable without it");
    }
    if (!evidence.policy && this.ledger.delegations.length > 0) {
      this.violation("evidence", "policy evidence required (--policy): permission-state rule unverifiable without it");
    }
  }

  insertions() {
    const evidence = this.evidence;
    if (!evidence.approvals) return;
    const ledger = this.ledger;
    for (const { line, entry } of ledger.turns) {
      if (!DYNAMIC_KINDS.includes(entry.kind)) continue;
      if (entry.amendment_ref !== null && entry.amendment_ref !== undefined) continue;
      const where = `line ${line} (turn ${entry.key})`;
      const plan = ledger.get(entry.plan);
      if (!plan || plan.record !== "plan") continue;
      const contingencyRef = plan.contingency_policy_ref;
      if (contingencyRef === null || contingencyRef === undefined) {
        this.violation(where, `dynamic insertion requires the plan's approved contingency policy (plan ${plan.id} has none)`);
        continue;
      }
      const contingency = evidence.approvals.byKey.get(contingencyRef);
      if (!contingency || contingency.record !== "contingency") {
        this.violation(where, `contingency policy unresolved: ${contingencyRef}`);
        continue;
      }
      this.check(Array.isArray(contingency.allowed_kinds) && contingency.allowed_kinds.includes(entry.kind), where, `insertion kind not allowed by the contingency policy: ${entry.kind}`);
      const concrete = digestOf(this.snapshotFor(plan, [entry.key]));
      const covering = [...evidence.approvals.byKey.values()].find((a) => a.kind === "derived" && a.target_digest === concrete && a.contingency_ref === contingencyRef);
      if (!covering) {
        this.violation(where, `no derived approval covers this insertion (concrete digest ${concrete})`);
        continue;
      }
      this.checkDerivedChain(covering, where);
      for (const { line: delegationLine, entry: delegation } of ledger.delegations) {
        if (delegation.owner_kind === "turn" && delegation.owner === entry.key) {
          this.check(delegation.approval_ref === covering.key, `line ${delegationLine} (delegation ${delegation.id})`, `an insertion's delegation carries the covering derived approval (got ${delegation.approval_ref}, want ${covering.key})`);
        }
      }
    }
  }

  checkDerivedChain(approval, where) {
    const seen = new Set();
    let cursor = approval;
    while (cursor && cursor.kind === "derived") {
      if (seen.has(cursor.key)) {
        this.violation(where, `derived approval chain is cyclic at ${cursor.key}`);
        return;
      }
      seen.add(cursor.key);
      const parent = this.evidence.approvals.byKey.get(cursor.parent_approval_ref);
      if (!parent) {
        this.violation(where, `derived parent approval unresolved: ${cursor.parent_approval_ref}`);
        return;
      }
      if (!APPROVAL_KINDS.includes(parent.kind)) {
        this.violation(where, `derived parent kind invalid: ${parent.kind}`);
        return;
      }
      cursor = parent;
    }
  }

  sources() {
    const evidence = this.evidence;
    if (!evidence.resolutions) return;
    for (const { line, entry } of this.ledger.turns) {
      const where = `line ${line} (turn ${entry.key})`;
      const resolution = evidence.resolutions.byKey.get(entry.selection.resolution_ref);
      if (!resolution) continue;
      this.check(MATCH_KINDS.includes(resolution.match), where, `resolution match kind not in ${MATCH_KINDS.join("|")}: ${resolution.match}`);
      if (entry.selection.source === "skill") {
        this.check(ENV_FALL_THROUGH.includes(resolution.observed_state), where, `skill default requires environment state ${ENV_FALL_THROUGH.join("|")} (got ${resolution.observed_state})`);
      } else if (entry.selection.source === "environment") {
        this.check(resolution.observed_state === ENV_CONFIGURED, where, `environment source requires observed_state ${ENV_CONFIGURED} (got ${resolution.observed_state})`);
      }
      this.check(resolution.turn === entry.key, where, "resolution record names its turn");
      this.check(resolution.source === entry.selection.source, where, "resolution source agrees with the selection");
    }
    if (evidence.policy) {
      for (const { line, entry } of this.ledger.delegations) {
        const where = `line ${line} (delegation ${entry.id})`;
        const permission = evidence.policy.byKey.get(entry.policy_ref);
        if (!permission) continue;
        this.check(permission.state === "allow", where, `delegation only under permission allow (got ${permission.state})`);
      }
    }
  }

  conversion() {
    const ledger = this.ledger;
    const attemptsByDelegation = new Map();
    for (const { entry } of ledger.attempts) {
      const list = attemptsByDelegation.get(entry.delegation_id) || [];
      list.push(entry);
      attemptsByDelegation.set(entry.delegation_id, list);
    }
    for (const { line, entry } of ledger.delegations) {
      const where = `line ${line} (delegation ${entry.id})`;
      if (entry.owner_kind === "restart-judge") {
        this.check(!attemptsByDelegation.has(entry.id), where, "a judge delegation creates no attempt");
        continue;
      }
      this.check(attemptsByDelegation.has(entry.id), where, "a turn delegation has at least one attempt");
    }
    const judgeOwners = new Map();
    for (const { line, entry } of ledger.delegations) {
      if (entry.owner_kind !== "restart-judge") continue;
      if (judgeOwners.has(entry.owner)) {
        this.violation(`line ${line} (delegation ${entry.id})`, `one judge per attempt: ${entry.owner} already judged at line ${judgeOwners.get(entry.owner)}`);
      }
      judgeOwners.set(entry.owner, line);
    }
    for (const { line, entry } of ledger.attempts) {
      const where = `line ${line} (attempt ${entry.turn}-${entry.attempt})`;
      const delegation = ledger.get(entry.delegation_id);
      const turn = ledger.get(entry.turn);
      if (!delegation || delegation.record !== "delegation" || !turn || turn.record !== "turn") continue;
      const started = entry.reported_status !== null;
      this.check(started || ["blocked", "aborted", "stale-plan"].includes(entry.effective_status), where, `reported_status null only on the unstarted paths (got ${entry.effective_status})`);
      if (delegation.owner_kind === "turn") {
        this.check(delegation.owner === entry.turn, where, "delegation owner agrees with the attempt's turn");
      }
      const selection = turn.selection;
      const binding = delegation.actual_selection_args;
      if (binding && binding.model !== undefined) {
        this.check(selection.resolved_model !== null && binding.model === selection.resolved_model, where, "actual model must equal the approved resolution");
      } else if (binding && binding.effort !== undefined) {
        this.check(turn.effort !== null && binding.effort === turn.effort, where, "actual effort must equal the turn's effort");
      } else if (binding && binding.resolution_ref !== undefined) {
        this.check(binding.resolution_ref === selection.resolution_ref, where, "actual resolution_ref must equal the approved one");
      }
      if (turn.dispatch_plan && turn.dispatch_plan.route !== undefined) {
        this.check(delegation.route === turn.dispatch_plan.route, where, "delegation route agrees with dispatch_plan");
      }
    }
    const lastAttemptOfTurn = new Map();
    for (const { entry } of ledger.attempts) {
      lastAttemptOfTurn.set(entry.turn, entry);
    }
    for (const { line, entry } of ledger.attempts) {
      if (entry.effective_status !== "stale-plan") continue;
      const where = `line ${line} (attempt ${entry.turn}-${entry.attempt})`;
      const delegation = ledger.get(entry.delegation_id);
      if (delegation && delegation.record === "delegation") {
        this.check(delegation.reported_model === null, where, "a stale-plan attempt reports no model");
        this.check(delegation.reported_thinking === null, where, "a stale-plan attempt reports no thinking");
      }
      const later = lastAttemptOfTurn.get(entry.turn);
      this.check(later === entry, where, `no attempt after stale-plan on the same key (${entry.turn})`);
      this.staleLineage(entry.turn, line, where);
    }
    for (const { line, entry } of ledger.delegations) {
      if (entry.owner_kind !== "turn") continue;
      const turn = ledger.get(entry.owner);
      if (!turn || turn.record !== "turn") continue;
      this.reportContradictionDelegation(entry, turn, `line ${line} (delegation ${entry.id})`);
    }
  }

  reportContradictionDelegation(delegationEntry, turnEntry, where) {
    const selection = turnEntry.selection;
    if (delegationEntry.reported_model !== null && selection.resolved_model !== null && delegationEntry.reported_model !== selection.resolved_model) {
      const attempts = this.ledger.attempts.filter(({ entry }) => entry.turn === turnEntry.key);
      const latest = attempts[attempts.length - 1];
      this.check(latest !== undefined && latest.effective_status === "blocked", where, `reported model contradicts the approved resolution: turn must be blocked (got ${latest ? latest.effective_status : "no attempt"})`);
      this.noFurtherWork(turnEntry.key, where, "reported model contradiction");
    }
    const reported = delegationEntry.reported_thinking;
    const approved = selection.thinking;
    if (isObject(reported) && reported.state === "fixed" && isObject(approved) && approved.state === "fixed" && reported.value !== approved.value) {
      const attempts = this.ledger.attempts.filter(({ entry }) => entry.turn === turnEntry.key);
      const latest = attempts[attempts.length - 1];
      this.check(latest !== undefined && latest.effective_status === "blocked", where, "reported thinking contradicts the approved fixed thinking: turn must be blocked");
      this.noFurtherWork(turnEntry.key, where, "reported thinking contradiction");
    }
  }

  noFurtherWork(turnKey, where, why) {
    const ledger = this.ledger;
    const anchor = ledger.lineOf(turnKey);
    for (const { line, entry } of ledger.attempts) {
      if (entry.turn === turnKey && line > anchor) {
        this.violation(where, `${why}: later attempt on ${turnKey} at line ${line}`);
      }
    }
    for (const { line, entry } of ledger.turns) {
      if (entry.rebinds === turnKey) {
        this.violation(where, `${why}: ${entry.key} rebinds the started turn ${turnKey}`);
      }
      const touches = entry.parent === turnKey || entry.inherits === turnKey || (Array.isArray(entry.inputs) && entry.inputs.includes(turnKey));
      if (touches && DYNAMIC_KINDS.includes(entry.kind)) {
        const covered = ledger.amendments.some(({ entry: amendment }) => amendment.supersedes.includes(turnKey));
        this.check(covered, where, `${why}: dynamic successor ${entry.key} of ${turnKey} needs an amendment superseding it`);
      }
    }
  }

  staleLineage(turnKey, staleLine, where) {
    const ledger = this.ledger;
    const coveredBy = ledger.amendments.filter(({ entry }) => entry.supersedes.includes(turnKey));
    if (coveredBy.length > 0) return;
    for (const { line, entry } of ledger.turns) {
      if (line <= staleLine) continue;
      const touches = entry.parent === turnKey || entry.inherits === turnKey || (Array.isArray(entry.inputs) && entry.inputs.includes(turnKey));
      if (touches && DYNAMIC_KINDS.includes(entry.kind)) {
        this.violation(where, `stale-plan ${turnKey}: dynamic successor ${entry.key} at line ${line} without an amendment superseding it`);
      }
    }
  }

  amendments() {
    const ledger = this.ledger;
    const supersededEarlier = new Set();
    const STARTED = ["ok", "failed", "needs-human", "needs-decision"];
    const isExecuted = (turnKey) => ledger.attempts.some(({ entry }) => entry.turn === turnKey && STARTED.includes(entry.effective_status));
    for (const { line, entry } of ledger.amendments) {
      const where = `line ${line} (amendment ${entry.key})`;
      const oldPlan = entry.replaces;
      const unexecuted = new Set();
      for (const { line: turnLine, entry: turn } of ledger.turns) {
        if (turn.plan !== oldPlan || turnLine >= line) continue;
        if (supersededEarlier.has(turn.key)) continue;
        if (!isExecuted(turn.key)) unexecuted.add(turn.key);
      }
      const supersedes = new Set(entry.supersedes || []);
      for (const key of supersedes) {
        if (!unexecuted.has(key)) {
          this.violation(where, `supersedes names a key that is not an unexecuted turn of ${oldPlan}: ${key}`);
        }
      }
      for (const key of unexecuted) {
        if (!supersedes.has(key)) {
          this.violation(where, `incomplete tail: unexecuted turn ${key} of ${oldPlan} is not superseded`);
        }
      }
      for (const key of supersedes) supersededEarlier.add(key);
      for (const { line: attemptLine, entry: attempt } of ledger.attempts) {
        if (attemptLine > line && supersedes.has(attempt.turn)) {
          this.violation(`line ${attemptLine} (attempt ${attempt.turn}-${attempt.attempt})`, `attempt for a superseded turn (${attempt.turn})`);
        }
      }
      const replacementKeys = new Set();
      for (const replacement of entry.replacements || []) {
        if (!isObject(replacement)) continue;
        if (replacementKeys.has(replacement.key)) {
          this.violation(where, `duplicate replacement key: ${replacement.key}`);
        }
        replacementKeys.add(replacement.key);
        const turn = ledger.get(replacement.key);
        if (turn && turn.record === "turn" && turn.plan !== entry.plan) {
          this.violation(where, `replacement ${replacement.key} belongs to plan ${turn.plan}, not ${entry.plan}`);
        }
      }
    }
  }

  run() {
    this.evidenceRequirements();
    this.structure();
    this.references();
    this.lineage();
    this.budgets();
    this.digests();
    this.approvals();
    this.insertions();
    this.sources();
  }

  runAudit() {
    this.run();
    this.conversion();
    this.amendments();
  }
}

function printViolations(checker) {
  const shown = checker.violations.slice(0, MAX_PRINTED_VIOLATIONS);
  for (const line of shown) process.stdout.write(`VIOLATION ${line}\n`);
  if (checker.violations.length > shown.length) {
    process.stdout.write(`VIOLATION ... ${checker.violations.length - shown.length} more\n`);
  }
  process.stdout.write(`FAILED ${checker.violations.length} violation(s), ${checker.checks} checks\n`);
}

function evidenceFromFlags(flags) {
  const evidence = {};
  if (flags.approvals) evidence.approvals = readEvidenceFile(flags.approvals, ["approval", "contingency"], "approvals");
  if (flags.resolutions) evidence.resolutions = readEvidenceFile(flags.resolutions, ["resolution"], "resolutions");
  if (flags.policy) evidence.policy = readEvidenceFile(flags.policy, ["permission", "route-policy", "env-model-setting"], "policy");
  return evidence;
}

function opValidate(args) {
  const { flags } = parseArguments(args, ["file", "approvals", "resolutions", "policy"]);
  const ledger = readLedger(requireFlag(flags, "file"));
  ledger.index();
  const evidence = evidenceFromFlags(flags);
  const checker = new Checker(ledger, evidence);
  checker.run();
  if (checker.violations.length > 0) {
    printViolations(checker);
    return 1;
  }
  process.stdout.write(`OK validate records=${ledger.records.length} plans=${ledger.plans.length} turns=${ledger.turns.length} delegations=${ledger.delegations.length} attempts=${ledger.attempts.length} checks=${checker.checks}\n`);
  return 0;
}

function opAudit(args) {
  const { flags } = parseArguments(args, ["file", "approvals", "resolutions", "policy"]);
  const ledger = readLedger(requireFlag(flags, "file"));
  ledger.index();
  const evidence = evidenceFromFlags(flags);
  const checker = new Checker(ledger, evidence);
  checker.runAudit();
  if (checker.violations.length > 0) {
    printViolations(checker);
    return 1;
  }
  process.stdout.write(`OK audit records=${ledger.records.length} plans=${ledger.plans.length} turns=${ledger.turns.length} delegations=${ledger.delegations.length} attempts=${ledger.attempts.length} checks=${checker.checks}\n`);
  return 0;
}

function opDispatchCheck(args) {
  const { flags } = parseArguments(args, ["file", "dispatch", "approvals", "resolutions", "policy"]);
  const ledger = readLedger(requireFlag(flags, "file"));
  ledger.index();
  const evidence = evidenceFromFlags(flags);
  const checker = new Checker(ledger, evidence);
  const dispatchFile = requireFlag(flags, "dispatch");
  const dispatch = util.readJson(dispatchFile);
  if (!dispatch) throw new Refusal(`${dispatchFile}: cannot read`);
  const where = `dispatch ${dispatchFile}`;
  if (typeof dispatch.turn !== "string") throw new UsageError("dispatch needs turn");
  const turn = ledger.get(dispatch.turn);
  if (!turn || turn.record !== "turn") throw new Refusal(`${where}: turn unresolved: ${dispatch.turn}`);
  const turnEntry = turn;
  const superseding = checker.ledger.amendments.find(({ entry }) => (entry.supersedes || []).includes(dispatch.turn));
  if (superseding) {
    const amendment = superseding.entry;
    const index = (amendment.supersedes || []).indexOf(dispatch.turn);
    const replacement = isObject(amendment.replacements[index]) ? amendment.replacements[index].key : "(none)";
    throw new Refusal(`${where}: ${dispatch.turn} is superseded by ${amendment.key} (replacement ${replacement}); dispatch the successor instead`);
  }
  const latest = checker.ledger.attempts.filter(({ entry }) => entry.turn === dispatch.turn);
  if (latest.length > 0) {
    const last = latest[latest.length - 1].entry;
    if (last.effective_status === "stale-plan") {
      throw new Refusal(`${where}: ${dispatch.turn} is stale-plan; replan and approval required before any further dispatch`);
    }
    if (last.effective_status === "blocked") {
      throw new Refusal(`${where}: ${dispatch.turn} is blocked; no re-dispatch`);
    }
  }
  const plan = turnEntry.dispatch_plan || {};
  checker.check(dispatch.route === plan.route, where, `route differs from the approved dispatch_plan (${dispatch.route} vs ${plan.route})`);
  const present = checker.checkBindingArgs(dispatch.args, where, "dispatch.args");
  const selection = turnEntry.selection;
  if (present && present.includes("model")) {
    checker.check(selection.resolved_model !== null && dispatch.args.model === selection.resolved_model, where, `dispatch model differs from the approved resolution (${dispatch.args.model} vs ${selection.resolved_model})`);
  } else if (present && present.includes("effort")) {
    checker.check(turnEntry.effort !== null && dispatch.args.effort === turnEntry.effort, where, "dispatch effort differs from the turn's effort");
  } else if (present && present.includes("resolution_ref")) {
    checker.check(dispatch.args.resolution_ref === selection.resolution_ref, where, "dispatch resolution_ref differs from the approved one");
  }
  const observed = dispatch.observed;
  if (isObject(observed)) {
    const extra = Object.keys(observed).filter((k) => !["policy_state", "model", "thinking", "revision"].includes(k));
    if (extra.length > 0) checker.violation(where, `observed unlisted keys: ${extra.join(", ")}`);
    if (observed.policy_state !== undefined && observed.policy_state !== "allow") {
      checker.violation(where, `launch blocked: policy state ${observed.policy_state}`);
    }
    if (typeof observed.model === "string" && selection.resolved_model !== null && observed.model !== selection.resolved_model) {
      checker.violation(where, `observed model differs from the approved resolution (${observed.model} vs ${selection.resolved_model})`);
    }
    if (isObject(observed.thinking) && observed.thinking.state === "fixed" && isObject(selection.thinking) && selection.thinking.state === "fixed" && observed.thinking.value !== selection.thinking.value) {
      checker.violation(where, `observed thinking differs from the approved fixed thinking (${observed.thinking.value} vs ${selection.thinking.value})`);
    }
  }
  if (checker.violations.length > 0) {
    printViolations(checker);
    return 1;
  }
  process.stdout.write(`OK dispatch-check turn=${dispatch.turn} route=${dispatch.route} checks=${checker.checks}\n`);
  return 0;
}

const USAGE = [
  `usage: node ${SCRIPT}.js validate --file <index.md|ledger.jsonl> [--approvals f] [--resolutions f] [--policy f]`,
  `       node ${SCRIPT}.js audit --file <...> [same evidence flags]`,
  `       node ${SCRIPT}.js dispatch-check --file <...> --dispatch <call.json> [same evidence flags]`,
];

runCli(SCRIPT, USAGE, function main(args) {
  const operation = args[0];
  const rest = args.slice(1);
  if (operation === "validate") return opValidate(rest);
  if (operation === "audit") return opAudit(rest);
  if (operation === "dispatch-check") return opDispatchCheck(rest);
  throw new UsageError(`unknown operation: ${operation}`);
});
