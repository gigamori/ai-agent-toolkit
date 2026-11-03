#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
script="$root/scripts/delegation_plan.js"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

pass=0

cat > "$tmp/mk.js" <<'EOF'
"use strict";
const crypto = require("crypto");
const fs = require("fs");

function canon(v) {
  if (Array.isArray(v)) return "[" + v.map(canon).join(",") + "]";
  if (v !== null && typeof v === "object") {
    const keys = Object.keys(v).sort();
    return "{" + keys.map((k) => JSON.stringify(k) + ":" + canon(v[k])).join(",") + "}";
  }
  return JSON.stringify(v);
}
function digest(snapshot) {
  return crypto.createHash("sha256").update(Buffer.from(canon(snapshot), "utf8")).digest("hex");
}
function snapshotOf(turns, contingency) {
  return {
    contingency_policy_ref: contingency === undefined ? null : contingency,
    turns: turns.map((t) => ({
      key: t.key, kind: t.kind, step: t.step, parent: t.parent, inherits: t.inherits,
      inputs: t.inputs, mode: t.mode, role: t.role === undefined ? null : t.role,
      effort: t.effort, budget_refs: t.budget_refs,
      selection: {
        source: t.selection.source, request: t.selection.request, scope_ref: t.selection.scope_ref,
        resolved_model: t.selection.resolved_model, granularity: t.selection.granularity,
        thinking: t.selection.thinking,
      },
      dispatch_plan: t.dispatch_plan,
    })),
  };
}

const ZERO = "0".repeat(64);

function baseTurns() {
  return [
    {
      record: "turn", plan: "r0", key: "01", kind: "planned",
      step: "survey the module layout", parent: null, inherits: null, inputs: [],
      mode: "survey", role: null, effort: "basic", effort_source: "step-model",
      budget_refs: { recovery_owner: "01", decision_owner: "01", restart_owner: "01" },
      rebinds: null, amendment_ref: null,
      selection: {
        source: "runtime", request: "run every step with sonnet",
        scope_ref: "launch prompt: whole run", resolution_ref: "res-01",
        resolved_model: "sonnet", granularity: "harness-ref",
        thinking: { state: "host-inherited" },
      },
      dispatch_plan: { route: "native-subagent", args: { model: "sonnet" } },
    },
    {
      record: "turn", plan: "r0", key: "02", kind: "planned",
      step: "implement the fix", parent: null, inherits: null, inputs: ["01"],
      mode: "execute", role: null, effort: null, effort_source: "step-model",
      budget_refs: { recovery_owner: "02", decision_owner: "02", restart_owner: "02" },
      rebinds: null, amendment_ref: null,
      selection: {
        source: "skill", request: "(default profile)", scope_ref: "input doc step 2",
        resolution_ref: "res-02", resolved_model: "sonnet", granularity: "harness-ref",
        thinking: { state: "undisclosed" },
      },
      dispatch_plan: { route: "native-subagent", args: { model: "sonnet" } },
    },
    {
      record: "turn", plan: "r0", key: "03", kind: "planned",
      step: "review the change", parent: null, inherits: null, inputs: ["02"],
      mode: "review-dev", role: null, effort: "pro", effort_source: "inferred-effort",
      budget_refs: { recovery_owner: "03", decision_owner: "03", restart_owner: "03" },
      rebinds: null, amendment_ref: null,
      selection: {
        source: "skill", request: "(default profile)", scope_ref: "input doc step 3",
        resolution_ref: "res-03", resolved_model: null, granularity: "harness-ref",
        thinking: { state: "undisclosed" },
      },
      dispatch_plan: { route: "native-subagent", args: { effort: "pro" } },
    },
  ];
}

function baseEvidence() {
  return {
    approvals: [
      {
        record: "approval", key: "ap1", kind: "user-plan", target_digest: "",
        event_ref: "user message: plan approved", parent_approval_ref: null,
        scope: "all three steps", snapshot_keys: ["01", "02", "03"],
      },
    ],
    resolutions: [
      {
        record: "resolution", key: "res-01", turn: "01", request: "run every step with sonnet",
        source: "runtime", observed_state: null, candidates: [], match: "exact",
        resolved_model: "sonnet", granularity: "harness-ref",
        thinking: { state: "host-inherited" }, grounds: "published model parameter value",
      },
      {
        record: "resolution", key: "res-02", turn: "02", request: "(default profile)",
        source: "skill", observed_state: "absent", candidates: [], match: "exact",
        resolved_model: "sonnet", granularity: "harness-ref",
        thinking: { state: "undisclosed" }, grounds: "profile pro row",
      },
      {
        record: "resolution", key: "res-03", turn: "03", request: "(default profile)",
        source: "skill", observed_state: "unset", candidates: [], match: "exact",
        resolved_model: null, granularity: "harness-ref",
        thinking: { state: "undisclosed" }, grounds: "profile pro row, effort-bound",
      },
    ],
    policy: [
      {
        record: "permission", key: "perm1", state: "allow", scope: "subagent delegation",
        revision: "rev-1", grounds: "published policy information",
      },
    ],
    dispatch: { turn: "03", route: "native-subagent", args: { effort: "pro" }, observed: { policy_state: "allow" } },
  };
}

function baseDelegations() {
  return [
    {
      record: "delegation", id: "d1", owner: "01", owner_kind: "turn", plan: "r0",
      approval_ref: "ap1", resolution_ref: "res-01", policy_ref: "perm1",
      route: "native-subagent", actual_selection_args: { model: "sonnet" },
      call_ref: "task-01", reported_model: "sonnet", reported_thinking: null,
    },
    {
      record: "attempt", turn: "01", attempt: 1, delegation_id: "d1",
      reported_status: "ok", effective_status: "ok", file: "01-survey.md",
    },
    {
      record: "delegation", id: "d2", owner: "02", owner_kind: "turn", plan: "r0",
      approval_ref: "ap1", resolution_ref: "res-02", policy_ref: "perm1",
      route: "native-subagent", actual_selection_args: { model: "sonnet" },
      call_ref: "task-02", reported_model: "sonnet", reported_thinking: null,
    },
    {
      record: "attempt", turn: "02", attempt: 1, delegation_id: "d2",
      reported_status: "ok", effective_status: "ok", file: "02-execute.md",
    },
  ];
}

function buildBase() {
  const turns = baseTurns();
  const evidence = baseEvidence();
  evidence.approvals[0].target_digest = digest(snapshotOf(turns));
  const ledger = [
    { record: "contract", version: "mo-delegation-v2" },
    { record: "plan", id: "r0", replaces: null, after_turn: null, digest: evidence.approvals[0].target_digest, approval_ref: "ap1", contingency_policy_ref: null },
    ...turns,
    ...baseDelegations(),
  ];
  return { ledger, evidence };
}

function buildRebind() {
  const base = buildBase();
  const ledger = [...base.ledger];
  const evidence = base.evidence;
  const raising = {
    record: "turn", plan: "r0", key: "04", kind: "planned",
    step: "run the planned check", parent: null, inherits: null, inputs: ["02"],
    mode: "execute", role: null, effort: "pro", effort_source: "inferred-effort",
    budget_refs: { recovery_owner: "04", decision_owner: "04", restart_owner: "04" },
    rebinds: null, amendment_ref: null,
    selection: {
      source: "skill", request: "(default profile)", scope_ref: "input doc step 4",
      resolution_ref: "res-04", resolved_model: "sonnet", granularity: "harness-ref",
      thinking: { state: "undisclosed" },
    },
    dispatch_plan: { route: "native-subagent", args: { model: "sonnet" } },
  };
  ledger.push(raising);
  ledger.push({
    record: "delegation", id: "d4", owner: "04", owner_kind: "turn", plan: "r0",
    approval_ref: "ap1", resolution_ref: "res-04", policy_ref: "perm1",
    route: "native-subagent", actual_selection_args: { model: "sonnet" },
    call_ref: "task-04", reported_model: "sonnet", reported_thinking: null,
  });
  ledger.push({
    record: "attempt", turn: "04", attempt: 1, delegation_id: "d4",
    reported_status: "ok", effective_status: "ok", file: "04-execute.md",
  });
  const staleTurn = {
    record: "turn", plan: "r0", key: "04a", kind: "decision-turn",
    step: "adjudicate the fork raised by step 4", parent: "04", inherits: null, inputs: ["04"],
    mode: "review-dev", role: null, effort: "ultra", effort_source: "policy-effort",
    budget_refs: { recovery_owner: "04", decision_owner: "04", restart_owner: "04a" },
    rebinds: null, amendment_ref: null,
    selection: {
      source: "skill", request: "(default profile)", scope_ref: "decision insertion policy",
      resolution_ref: "res-04a", resolved_model: "sonnet", granularity: "harness-ref",
      thinking: { state: "undisclosed" },
    },
    dispatch_plan: { route: "native-subagent", args: { model: "sonnet" } },
  };
  ledger.push(staleTurn);
  ledger.push({
    record: "delegation", id: "d5", owner: "04a", owner_kind: "turn", plan: "r0",
    approval_ref: "da1", resolution_ref: "res-04a", policy_ref: "perm1",
    route: "native-subagent", actual_selection_args: { model: "sonnet" },
    call_ref: "task-04a", reported_model: null, reported_thinking: null,
  });
  ledger.push({
    record: "attempt", turn: "04a", attempt: 1, delegation_id: "d5",
    reported_status: null, effective_status: "stale-plan", file: "-",
  });
  evidence.resolutions.push({
    record: "resolution", key: "res-04", turn: "04", request: "(default profile)",
    source: "skill", observed_state: "unset", candidates: [], match: "exact",
    resolved_model: "sonnet", granularity: "harness-ref",
    thinking: { state: "undisclosed" }, grounds: "profile pro row",
  });
  evidence.resolutions.push({
    record: "resolution", key: "res-04a", turn: "04a", request: "(default profile)",
    source: "skill", observed_state: "unset", candidates: [], match: "exact",
    resolved_model: "sonnet", granularity: "harness-ref",
    thinking: { state: "undisclosed" }, grounds: "profile ultra row",
  });
  evidence.resolutions.push({
    record: "resolution", key: "res-03b", turn: "03b", request: "(default profile)",
    source: "skill", observed_state: "unset", candidates: [], match: "exact",
    resolved_model: null, granularity: "harness-ref",
    thinking: { state: "undisclosed" }, grounds: "profile pro row, effort-bound",
  });
  const tailPlannedSelection = deepCopy(findTurn(ledger, "03").selection);
  tailPlannedSelection.resolution_ref = "res-03b";
  const rebindTurn = {
    record: "turn", plan: "r1", key: "04b", kind: "decision-turn",
    step: "adjudicate the fork raised by step 4", parent: "04", inherits: null, inputs: ["04"],
    mode: "review-dev", role: null, effort: "ultra", effort_source: "policy-effort",
    budget_refs: { recovery_owner: "04", decision_owner: "04", restart_owner: "04a" },
    rebinds: "04a", amendment_ref: "amend-1", amendment_item: "2",
    selection: {
      source: "runtime", request: "use opus for the decision",
      scope_ref: "user approval of the rebind", resolution_ref: "res-04b",
      resolved_model: "opus", granularity: "harness-ref",
      thinking: { state: "undisclosed" },
    },
    dispatch_plan: { route: "native-subagent", args: { model: "opus" } },
  };
  const stale = findTurn(ledger, "04a");
  const tailPlanned = {
    record: "turn", plan: "r1", key: "03b", kind: "planned",
    step: findTurn(ledger, "03").step, parent: null, inherits: null, inputs: [],
    mode: "review-dev", role: null, effort: "pro", effort_source: "inferred-effort",
    budget_refs: deepCopy(findTurn(ledger, "03").budget_refs),
    rebinds: "03", amendment_ref: "amend-1", amendment_item: "1",
    selection: tailPlannedSelection,
    dispatch_plan: deepCopy(findTurn(ledger, "03").dispatch_plan),
  };
  const newPlanTurns = [tailPlanned, rebindTurn];
  const r1Digest = digest(snapshotOf(newPlanTurns));
  ledger.push({ record: "amendment", key: "amend-1", plan: "r1", replaces: "r0", after_turn: "04", parent: null, cause: "model-resolution", source_file: "amend-tail.md", source_sha256: "a".repeat(64), supersedes: ["03", "04a"], replacements: [{ key: "03b", amendment_item: "1" }, { key: "04b", amendment_item: "2" }] });
  ledger.push({ record: "plan", id: "r1", replaces: "r0", after_turn: "04", digest: r1Digest, approval_ref: "ap2", contingency_policy_ref: null });
  ledger.push(tailPlanned);
  ledger.push(rebindTurn);
  ledger.push({
    record: "delegation", id: "d6", owner: "04b", owner_kind: "turn", plan: "r1",
    approval_ref: "ap2", resolution_ref: "res-04b", policy_ref: "perm1",
    route: "native-subagent", actual_selection_args: { model: "opus" },
    call_ref: "task-04b", reported_model: "opus", reported_thinking: null,
  });
  ledger.push({
    record: "attempt", turn: "04b", attempt: 1, delegation_id: "d6",
    reported_status: "ok", effective_status: "ok", file: "04b-decision.md",
  });
  evidence.resolutions.push({
    record: "resolution", key: "res-04b", turn: "04b", request: "use opus for the decision",
    source: "runtime", observed_state: null, candidates: [], match: "exact",
    resolved_model: "opus", granularity: "harness-ref",
    thinking: { state: "undisclosed" }, grounds: "user-designated model reference",
  });
  evidence.approvals.push({
    record: "approval", key: "ap2", kind: "user-plan", target_digest: r1Digest,
    event_ref: "user message: rebind approved", parent_approval_ref: null,
    scope: "replaced tail", snapshot_keys: ["03b", "04b"],
  });
  ledger[1].after_turn = null;
  ledger[1].contingency_policy_ref = "cp1";
  evidence.dispatch = { turn: "04b", route: "native-subagent", args: { model: "opus" }, observed: { policy_state: "allow" } };
  ledger[1].digest = digest(snapshotOf([ledger[2], ledger[3], ledger[4], raising], "cp1"));
  evidence.approvals[0].target_digest = ledger[1].digest;
  evidence.approvals[0].snapshot_keys = ["01", "02", "03", "04"];
  evidence.approvals.push({
    record: "contingency", key: "cp1",
    allowed_kinds: ["recovery-debug", "recovery-reexecute", "decision-turn", "decision-rerun"],
    origin_turn: "04",
    mode_policy: "inserted review-dev for forks; debug for failures",
    effort_policy: "ultra for debug and decision; inherits otherwise",
    model_binding: "inherits unless the insertion's own request resolves otherwise",
    scope: "the letter sequence of turn 04",
    tool_constraints: "unchanged from the plan",
    caps: "the existing per-origin caps",
  });
  evidence.approvals.push({
    record: "approval", key: "da1", kind: "derived",
    target_digest: digest(snapshotOf([staleTurn], "cp1")),
    event_ref: null, parent_approval_ref: "ap1",
    scope: "the 04a insertion", contingency_ref: "cp1",
  });
  return { ledger, evidence, r1Digest };
}

function findTurn(ledger, key) {
  return ledger.find((r) => r.record === "turn" && r.key === key);
}

function writeJsonl(name, records) {
  fs.writeFileSync(name, records.map((r) => JSON.stringify(r)).join("\n") + "\n");
}

function emit(built, dispatch) {
  writeJsonl("ledger.jsonl", built.ledger);
  writeJsonl("approvals.jsonl", built.evidence.approvals);
  writeJsonl("resolutions.jsonl", built.evidence.resolutions);
  writeJsonl("policy.jsonl", built.evidence.policy);
  fs.writeFileSync("dispatch.json", JSON.stringify(dispatch || built.evidence.dispatch));
}

function deepCopy(v) {
  return JSON.parse(JSON.stringify(v));
}

function findRecord(built, record, keyField, keyValue) {
  return built.ledger.find((r) => r.record === record && r[keyField] === keyValue);
}

const scenarios = {
  base() {
    emit(buildBase());
  },
  rebind() {
    emit(buildRebind());
  },
  "v1-ledger"() {
    const built = buildBase();
    built.ledger[0] = { record: "contract", version: "adaptive-effort-v1" };
    emit(built);
  },
  corrupt() {
    const built = buildBase();
    const lines = built.ledger.map((r) => JSON.stringify(r));
    lines.splice(3, 0, "{not json");
    fs.writeFileSync("ledger.jsonl", lines.join("\n") + "\n");
    writeJsonl("approvals.jsonl", built.evidence.approvals);
    writeJsonl("resolutions.jsonl", built.evidence.resolutions);
    writeJsonl("policy.jsonl", built.evidence.policy);
    fs.writeFileSync("dispatch.json", JSON.stringify(built.evidence.dispatch));
  },
  "extra-key"() {
    const built = buildBase();
    findRecord(built, "turn", "key", "01").model = "sonnet";
    emit(built);
  },
  "missing-key"() {
    const built = buildBase();
    delete findRecord(built, "turn", "key", "01").effort_source;
    emit(built);
  },
  "rebind-kind"() {
    const built = buildRebind();
    findRecord(built, "turn", "key", "04b").kind = "recovery-debug";
    emit(built);
  },
  "rebind-budget"() {
    const built = buildRebind();
    findRecord(built, "turn", "key", "04b").budget_refs.restart_owner = "04b";
    emit(built);
  },
  "rebind-parent"() {
    const built = buildRebind();
    findRecord(built, "turn", "key", "04b").parent = "04a";
    emit(built);
  },
  "rebind-cycle"() {
    const built = buildRebind();
    const staleStep = findRecord(built, "turn", "key", "04a").step;
    const staleBudget = deepCopy(findRecord(built, "turn", "key", "04a").budget_refs);
    const cycleA = {
      record: "turn", plan: "r1", key: "90a", kind: "decision-turn",
      step: staleStep, parent: "04", inherits: null, inputs: [],
      mode: "review-dev", role: null, effort: "ultra", effort_source: "policy-effort",
      budget_refs: deepCopy(staleBudget), rebinds: "90b", amendment_ref: "amend-1", amendment_item: "9a",
      selection: deepCopy(findRecord(built, "turn", "key", "04a").selection),
      dispatch_plan: { route: "native-subagent", args: { model: "sonnet" } },
    };
    const cycleB = deepCopy(cycleA);
    cycleB.key = "90b";
    cycleB.rebinds = "90a";
    cycleB.amendment_item = "9b";
    cycleB.plan = "r0";
    built.ledger.push(cycleA, cycleB);
    emit(built);
  },
  "rebind-branch"() {
    const built = buildRebind();
    const staleTurn = findRecord(built, "turn", "key", "04a");
    for (const key of ["91a", "91b"]) {
      const branch = deepCopy(staleTurn);
      branch.key = key;
      branch.plan = "r1";
      branch.rebinds = "04a";
      branch.amendment_ref = "amend-1";
      branch.amendment_item = key;
      built.ledger.push(branch);
    }
    emit(built);
  },
  "planned-parent"() {
    const built = buildBase();
    findRecord(built, "turn", "key", "01").parent = "02";
    emit(built);
  },
  "dynamic-orphan"() {
    const built = buildRebind();
    findRecord(built, "turn", "key", "04a").parent = null;
    emit(built);
  },
  "digest-mismatch"() {
    const built = buildBase();
    findRecord(built, "plan", "id", "r0").digest = ZERO;
    emit(built);
  },
  "approval-digest"() {
    const built = buildBase();
    built.evidence.approvals[0].target_digest = ZERO;
    emit(built);
  },
  "auto-alias"() {
    const built = buildBase();
    built.evidence.approvals[0].kind = "auto-invocation";
    built.evidence.resolutions[0].match = "alias";
    emit(built);
  },
  "auto-exact-ok"() {
    const built = buildBase();
    built.evidence.approvals[0].kind = "auto-invocation";
    emit(built);
  },
  "env-invalid"() {
    const built = buildBase();
    built.evidence.resolutions[1].observed_state = "invalid";
    emit(built);
  },
  "stale-reported"() {
    const built = buildRebind();
    findRecord(built, "attempt", "turn", "04a").reported_status = "aborted";
    emit(built);
  },
  "stale-retry"() {
    const built = buildRebind();
    built.ledger = built.ledger.filter((r) => !(r.record === "amendment" || r.record === "plan" && r.id === "r1" || r.record === "turn" && (r.key === "04b" || r.key === "03b") || r.record === "delegation" && r.id === "d6" || r.record === "attempt" && r.turn === "04b"));
    built.evidence.approvals = built.evidence.approvals.filter((a) => a.key === "ap1");
    built.ledger[1].digest = digest(snapshotOf(built.ledger.filter((r) => r.record === "turn" && r.kind === "planned" && r.rebinds === null && r.amendment_ref === null), "cp1"));
    built.evidence.approvals[0].target_digest = built.ledger[1].digest;
    built.ledger.push({
      record: "delegation", id: "d7", owner: "04a", owner_kind: "turn", plan: "r0",
      approval_ref: "ap1", resolution_ref: "res-04a", policy_ref: "perm1",
      route: "native-subagent", actual_selection_args: { model: "sonnet" },
      call_ref: "task-04a-2", reported_model: "sonnet", reported_thinking: null,
    });
    built.ledger.push({
      record: "attempt", turn: "04a", attempt: 2, delegation_id: "d7",
      reported_status: "ok", effective_status: "ok", file: "04b-decision.md",
    });
    emit(built);
  },
  "stale-successor"() {
    const built = buildRebind();
    built.ledger = built.ledger.filter((r) => !(r.record === "amendment" || r.record === "plan" && r.id === "r1" || r.record === "turn" && (r.key === "04b" || r.key === "03b") || r.record === "delegation" && r.id === "d6" || r.record === "attempt" && r.turn === "04b"));
    built.evidence.approvals = built.evidence.approvals.filter((a) => a.key === "ap1");
    built.ledger[1].digest = digest(snapshotOf(built.ledger.filter((r) => r.record === "turn" && r.kind === "planned" && r.rebinds === null && r.amendment_ref === null), "cp1"));
    built.evidence.approvals[0].target_digest = built.ledger[1].digest;
    built.ledger.push({
      record: "turn", plan: "r0", key: "04b", kind: "decision-rerun",
      step: "re-run the forked step", parent: "04a", inherits: "04a", inputs: ["04a"],
      mode: "execute", role: null, effort: "pro", effort_source: "inherited",
      budget_refs: { recovery_owner: "04", decision_owner: "04", restart_owner: "04b" },
      rebinds: null, amendment_ref: null,
      selection: deepCopy(findRecord(built, "turn", "key", "04a").selection),
      dispatch_plan: { route: "native-subagent", args: { model: "sonnet" } },
    });
    emit(built);
  },
  "contradiction-ok"() {
    const built = buildBase();
    findRecord(built, "delegation", "id", "d2").reported_model = "opus";
    emit(built);
  },
  "delegation-orphan"() {
    const built = buildBase();
    built.ledger = built.ledger.filter((r) => r.record !== "attempt");
    emit(built);
  },
  "judge-attempt"() {
    const built = buildBase();
    built.ledger.push({
      record: "delegation", id: "d9", owner: "01-1", owner_kind: "restart-judge", plan: "r0",
      approval_ref: "ap1", resolution_ref: "res-01", policy_ref: "perm1",
      route: "native-subagent", actual_selection_args: { model: "sonnet" },
      call_ref: "task-judge", reported_model: "sonnet", reported_thinking: null,
    });
    built.ledger.push({
      record: "attempt", turn: "01", attempt: 2, delegation_id: "d9",
      reported_status: "ok", effective_status: "ok", file: "-",
    });
    emit(built);
  },
  "null-ok"() {
    const built = buildBase();
    findRecord(built, "attempt", "turn", "01").reported_status = null;
    emit(built);
  },
  "both-args"() {
    const built = buildBase();
    findRecord(built, "delegation", "id", "d1").actual_selection_args = { model: "sonnet", effort: "basic" };
    emit(built);
  },
  "route-mismatch"() {
    const built = buildBase();
    findRecord(built, "delegation", "id", "d1").route = "cli";
    emit(built);
  },
  "superseded-attempt"() {
    const built = buildRebind();
    built.ledger.push({
      record: "delegation", id: "d7", owner: "04a", owner_kind: "turn", plan: "r0",
      approval_ref: "ap1", resolution_ref: "res-04a", policy_ref: "perm1",
      route: "native-subagent", actual_selection_args: { model: "sonnet" },
      call_ref: "task-04a-2", reported_model: null, reported_thinking: null,
    });
    built.ledger.push({
      record: "attempt", turn: "04a", attempt: 2, delegation_id: "d7",
      reported_status: null, effective_status: "stale-plan", file: "-",
    });
    emit(built);
  },
  "incomplete-supersedes"() {
    const built = buildRebind();
    findRecord(built, "amendment", "key", "amend-1").supersedes = [];
    emit(built);
  },
  "todolist-parent"() {
    const built = buildBase();
    const replacement = {
      record: "turn", plan: "r1", key: "04", kind: "planned",
      step: "the revised step text", parent: "03", inherits: null, inputs: [],
      mode: "execute", role: null, effort: "pro", effort_source: "inferred-effort",
      budget_refs: { recovery_owner: "04", decision_owner: "04", restart_owner: "04" },
      rebinds: null, amendment_ref: "amend-1", amendment_item: "1",
      selection: deepCopy(findRecord(built, "turn", "key", "03").selection),
      dispatch_plan: { route: "native-subagent", args: { model: "sonnet" } },
    };
    const r1Digest = digest(snapshotOf([replacement]));
    built.ledger.push({ record: "amendment", key: "amend-1", plan: "r1", replaces: "r0", after_turn: "03", parent: null, cause: "todolist", source_file: "amend-tail.md", source_sha256: "b".repeat(64), supersedes: ["03"], replacements: [{ key: "04", amendment_item: "1" }] });
    built.ledger.push({ record: "plan", id: "r1", replaces: "r0", after_turn: "03", digest: r1Digest, approval_ref: "ap2", contingency_policy_ref: null });
    built.ledger.push(replacement);
    built.evidence.approvals.push({
      record: "approval", key: "ap2", kind: "user-plan", target_digest: r1Digest,
      event_ref: "user message: revised tail approved", parent_approval_ref: null,
      scope: "replaced tail", snapshot_keys: ["04"],
    });
    built.evidence.resolutions.push({
      record: "resolution", key: "res-04", turn: "04", request: "(default profile)",
      source: "skill", observed_state: "unset", candidates: [], match: "exact",
      resolved_model: "sonnet", granularity: "harness-ref",
      thinking: { state: "undisclosed" }, grounds: "profile pro row",
    });
    emit(built);
  },
  "dispatch-model"() {
    const built = buildBase();
    emit(built, { turn: "03", route: "native-subagent", args: { model: "sonnet" }, observed: { policy_state: "allow" } });
  },
  "dispatch-deny"() {
    const built = buildBase();
    emit(built, { turn: "03", route: "native-subagent", args: { effort: "pro" }, observed: { policy_state: "deny" } });
  },
  "dispatch-stale-old-key"() {
    const built = buildRebind();
    emit(built);
    fs.writeFileSync("dispatch-stale.json", JSON.stringify({ turn: "04a", route: "native-subagent", args: { model: "sonnet" } }));
  },
  "dispatch-rebind-ok"() {
    const built = buildRebind();
    emit(built, { turn: "04b", route: "native-subagent", args: { model: "opus" }, observed: { policy_state: "allow" } });
  },
  "dispatch-stale-no-amendment"() {
    const built = buildRebind();
    built.ledger = built.ledger.filter((r) => !(r.record === "amendment" || r.record === "plan" && r.id === "r1" || r.record === "turn" && (r.key === "04b" || r.key === "03b") || r.record === "delegation" && r.id === "d6" || r.record === "attempt" && r.turn === "04b"));
    built.evidence.approvals = built.evidence.approvals.filter((a) => a.key === "ap1");
    built.ledger[1].digest = digest(snapshotOf(built.ledger.filter((r) => r.record === "turn" && r.kind === "planned" && r.rebinds === null && r.amendment_ref === null), "cp1"));
    built.evidence.approvals[0].target_digest = built.ledger[1].digest;
    emit(built);
    fs.writeFileSync("dispatch-stale2.json", JSON.stringify({ turn: "04a", route: "native-subagent", args: { model: "sonnet" } }));
  },
  "dispatch-superseded"() {
    const built = buildRebind();
    emit(built);
    fs.writeFileSync("dispatch-super.json", JSON.stringify({ turn: "03", route: "native-subagent", args: { effort: "pro" } }));
  },
  "insertion-unapproved"() {
    const built = buildRebind();
    built.ledger[1].contingency_policy_ref = null;
    built.ledger[1].digest = digest(snapshotOf([findRecord(built, "turn", "key", "01"), findRecord(built, "turn", "key", "02"), findRecord(built, "turn", "key", "03"), findRecord(built, "turn", "key", "04")]));
    built.evidence.approvals[0].target_digest = built.ledger[1].digest;
    built.evidence.approvals = built.evidence.approvals.filter((a) => a.record !== "contingency" && a.key !== "da1");
    findRecord(built, "delegation", "id", "d5").approval_ref = "ap1";
    emit(built);
  },
  "insertion-kind"() {
    const built = buildRebind();
    built.evidence.approvals.find((a) => a.record === "contingency").allowed_kinds = ["recovery-debug"];
    emit(built);
  },
  "derived-digest"() {
    const built = buildRebind();
    built.evidence.approvals.find((a) => a.key === "da1").target_digest = ZERO;
    emit(built);
  },
  "insertion-delegation"() {
    const built = buildRebind();
    findRecord(built, "delegation", "id", "d5").approval_ref = "ap1";
    emit(built);
  },
  "derived-cycle"() {
    const built = buildRebind();
    built.evidence.approvals.find((a) => a.key === "da1").parent_approval_ref = "da1";
    emit(built);
  },
  "dispatch-both-args"() {
    const built = buildBase();
    emit(built, { turn: "03", route: "native-subagent", args: { model: "opus", effort: "pro" } });
  },
};

const scenario = process.argv[2];
if (!scenario || !scenarios[scenario]) {
  process.stderr.write(`unknown scenario: ${scenario}\n`);
  process.exit(64);
}
scenarios[scenario]();
EOF

EV=(--approvals "$tmp/approvals.jsonl" --resolutions "$tmp/resolutions.jsonl" --policy "$tmp/policy.jsonl")

build() {
  (cd "$tmp" && node mk.js "$1")
}

expect_ok() {
  local label=$1; shift
  local out status
  set +e
    out=$(cd "$tmp" && node "$script" "$@" 2>&1)
    status=$?
  set -e
  if [[ $status -ne 0 ]]; then
    printf 'FAIL %s: expected OK, got exit %s: %s\n' "$label" "$status" "$out" >&2
    exit 1
  fi
  if [[ "$out" != OK* ]]; then
    printf 'FAIL %s: expected an OK line: %s\n' "$label" "$out" >&2
    exit 1
  fi
  pass=$((pass + 1))
}

expect_refused() {
  local label=$1 expected=$2; shift 2
  local out status
  set +e
    out=$(cd "$tmp" && node "$script" "$@" 2>&1)
    status=$?
  set -e
  if [[ $status -eq 0 ]]; then
    printf 'FAIL %s: mutation control accepted\n' "$label" >&2
    exit 1
  fi
  if [[ $status -ne 1 || "$out" == *VIOLATION* || "$out" != *"$expected"* ]]; then
    printf 'FAIL %s: wrong refusal (exit %s): %s\n' "$label" "$status" "$out" >&2
    exit 1
  fi
  pass=$((pass + 1))
}

expect_violation() {
  local label=$1 expected=$2; shift 2
  local out status
  set +e
    out=$(cd "$tmp" && node "$script" "$@" 2>&1)
    status=$?
  set -e
  if [[ $status -eq 0 ]]; then
    printf 'FAIL %s: mutation control accepted\n' "$label" >&2
    exit 1
  fi
  if [[ $status -ne 1 || "$out" != *VIOLATION* || "$out" != *"$expected"* ]]; then
    printf 'FAIL %s: mutation hit the wrong predicate (exit %s): %s\n' "$label" "$status" "$out" >&2
    exit 1
  fi
  pass=$((pass + 1))
}

build base
expect_ok "validate positive with evidence" validate --file ledger.jsonl "${EV[@]}"
expect_ok "audit positive with evidence" audit --file ledger.jsonl "${EV[@]}"
expect_ok "dispatch-check positive" dispatch-check --file ledger.jsonl --dispatch dispatch.json "${EV[@]}"
expect_violation "validate without evidence files fails closed" "approval evidence required" validate --file ledger.jsonl

build rebind
expect_ok "rebind positive passes audit" audit --file ledger.jsonl "${EV[@]}"
expect_ok "dispatch-check on the rebind's approved binding" dispatch-check --file ledger.jsonl --dispatch dispatch.json "${EV[@]}"
build "dispatch-stale-old-key"
expect_refused "dispatch-check refuses the stale key" "is superseded by amend-1" dispatch-check --file ledger.jsonl --dispatch dispatch-stale.json "${EV[@]}"

build "dispatch-stale-no-amendment"
expect_refused "dispatch-check refuses a stale key with no amendment yet" "stale-plan; replan and approval required" dispatch-check --file ledger.jsonl --dispatch dispatch-stale2.json "${EV[@]}"

build "v1-ledger"
expect_refused "v1 ledger is refused" "not a mo-delegation-v2 ledger" validate --file ledger.jsonl "${EV[@]}"

build corrupt
expect_refused "unparseable ledger line is refused" "unparseable ledger line" validate --file ledger.jsonl "${EV[@]}"

build "extra-key"
expect_violation "unlisted turn key is rejected" "unlisted key: model" validate --file ledger.jsonl "${EV[@]}"

build "missing-key"
expect_violation "missing required key is rejected" "missing key: effort_source" validate --file ledger.jsonl "${EV[@]}"

build "rebind-kind"
expect_violation "rebind kind change is rejected" "rebind keeps kind" audit --file ledger.jsonl "${EV[@]}"

build "rebind-budget"
expect_violation "rebind budget_refs change is rejected" "rebind keeps budget_refs" audit --file ledger.jsonl "${EV[@]}"

build "rebind-parent"
expect_violation "rebind loop parent change is rejected" "rebind keeps the loop parent" audit --file ledger.jsonl "${EV[@]}"

build "rebind-cycle"
expect_violation "rebind cycle is rejected" "rebinds chain is cyclic" audit --file ledger.jsonl "${EV[@]}"

build "rebind-branch"
expect_violation "branching rebinds are rejected" "branching successors rebind one target" audit --file ledger.jsonl "${EV[@]}"

build "planned-parent"
expect_violation "planned turn with a parent is rejected" "planned turn has null parent" validate --file ledger.jsonl "${EV[@]}"

build "dynamic-orphan"
expect_violation "dynamic turn without a parent is rejected" "dynamic turn needs its loop parent" validate --file ledger.jsonl "${EV[@]}"

build "digest-mismatch"
expect_violation "digest mismatch is rejected" "digest mismatch" validate --file ledger.jsonl "${EV[@]}"

build "approval-digest"
expect_violation "approval digest mismatch is rejected" "approval.target_digest must equal the plan digest" validate --file ledger.jsonl "${EV[@]}"

build "auto-alias"
expect_violation "auto-invocation over a non-exact match is rejected" "auto-invocation needs exact resolution" validate --file ledger.jsonl "${EV[@]}"

build "auto-exact-ok"
expect_ok "auto-invocation over exact matches passes" validate --file ledger.jsonl "${EV[@]}"

build "env-invalid"
expect_violation "skill default over an invalid environment state is rejected" "skill default requires environment state" validate --file ledger.jsonl "${EV[@]}"

build "stale-reported"
expect_violation "stale-plan with a reported status is rejected" "effective-only" audit --file ledger.jsonl "${EV[@]}"

build "stale-retry"
expect_violation "attempt after stale-plan on the same key is rejected" "no attempt after stale-plan" audit --file ledger.jsonl "${EV[@]}"

build "stale-successor"
expect_violation "dynamic successor of a stale turn without an amendment is rejected" "without an amendment superseding it" audit --file ledger.jsonl "${EV[@]}"

build "contradiction-ok"
expect_violation "reported model contradiction without blocked is rejected" "turn must be blocked" audit --file ledger.jsonl "${EV[@]}"

build "delegation-orphan"
expect_violation "delegation without an attempt is rejected" "at least one attempt" audit --file ledger.jsonl "${EV[@]}"

build "judge-attempt"
expect_violation "judge delegation with an attempt is rejected" "a judge delegation creates no attempt" audit --file ledger.jsonl "${EV[@]}"

build "null-ok"
expect_violation "reported_status null on a classified turn is rejected" "unstarted paths" audit --file ledger.jsonl "${EV[@]}"

build "both-args"
expect_violation "model and effort together are rejected" "exactly one of model|effort|resolution_ref" audit --file ledger.jsonl "${EV[@]}"

build "route-mismatch"
expect_violation "delegation route disagreement is rejected" "delegation route agrees with dispatch_plan" audit --file ledger.jsonl "${EV[@]}"

build "superseded-attempt"
expect_violation "attempt for a superseded turn is rejected" "attempt for a superseded turn" audit --file ledger.jsonl "${EV[@]}"

build "incomplete-supersedes"
expect_violation "incomplete tail supersedes is rejected" "is not superseded" audit --file ledger.jsonl "${EV[@]}"

build "todolist-parent"
expect_violation "amendment-derived planned turn with a parent is rejected" "planned turn has null parent" audit --file ledger.jsonl "${EV[@]}"

build "dispatch-model"
expect_violation "dispatch model off the approved resolution is rejected" "differs from the approved resolution" dispatch-check --file ledger.jsonl --dispatch dispatch.json "${EV[@]}"

build "dispatch-deny"
expect_violation "dispatch under a denied policy is rejected" "launch blocked: policy state deny" dispatch-check --file ledger.jsonl --dispatch dispatch.json "${EV[@]}"

build "dispatch-both-args"
expect_violation "dispatch with model and effort together is rejected" "exactly one of model|effort|resolution_ref" dispatch-check --file ledger.jsonl --dispatch dispatch.json "${EV[@]}"

build "dispatch-superseded"
expect_refused "dispatch-check refuses a superseded key" "is superseded by amend-1" dispatch-check --file ledger.jsonl --dispatch dispatch-super.json "${EV[@]}"

build "insertion-unapproved"
expect_violation "unapproved dynamic insertion is rejected" "dynamic insertion requires the plan's approved contingency policy" validate --file ledger.jsonl "${EV[@]}"

build "insertion-kind"
expect_violation "insertion kind outside the contingency policy is rejected" "insertion kind not allowed" validate --file ledger.jsonl "${EV[@]}"

build "derived-digest"
expect_violation "derived approval with a wrong concrete digest is rejected" "no derived approval covers this insertion" audit --file ledger.jsonl "${EV[@]}"

build "insertion-delegation"
expect_violation "insertion delegation without the covering approval is rejected" "carries the covering derived approval" audit --file ledger.jsonl "${EV[@]}"

build "derived-cycle"
expect_violation "cyclic derived approval chain is rejected" "derived approval chain is cyclic" validate --file ledger.jsonl "${EV[@]}"

printf 'delegation_plan_test: %s cases passed\n' "$pass"
