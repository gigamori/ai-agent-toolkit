#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
script="$root/scripts/delegation_plan.js"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

pass=0

cat > "$tmp/mk.js" <<'EOF'
"use strict";
const fs = require("fs");

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
        record: "approval", key: "ap1", kind: "user-plan",
        event_ref: "user message: plan approved", parent_approval_ref: null,
        scope: "all three steps",
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
        record: "route-policy", key: "rp1", route: "native-subagent", state: "available",
        constraints: { nested: false, child_skills: "disabled" }, grounds: "published delegation contract",
      },
    ],
    dispatch: { turn: "03", route: "native-subagent", args: { effort: "pro" } },
  };
}

function baseDelegations() {
  return [
    {
      record: "delegation", id: "d1", owner: "01", owner_kind: "turn", plan: "r0",
      approval_ref: "ap1", resolution_ref: "res-01", policy_ref: "rp1",
      route: "native-subagent", actual_selection_args: { model: "sonnet" },
      call_ref: "task-01", reported_model: "sonnet", reported_thinking: null,
    },
    {
      record: "attempt", turn: "01", attempt: 1, delegation_id: "d1",
      reported_status: "ok", effective_status: "ok", file: "01-survey.md",
    },
    {
      record: "delegation", id: "d2", owner: "02", owner_kind: "turn", plan: "r0",
      approval_ref: "ap1", resolution_ref: "res-02", policy_ref: "rp1",
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
  const ledger = [
    { record: "contract", version: "mo-delegation-v2" },
    { record: "plan", id: "r0", replaces: null, after_turn: null, approval_ref: "ap1", contingency_policy_ref: null },
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
    approval_ref: "ap1", resolution_ref: "res-04", policy_ref: "rp1",
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
    approval_ref: "da1", resolution_ref: "res-04a", policy_ref: "rp1",
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
  ledger.push({ record: "amendment", key: "amend-1", plan: "r1", replaces: "r0", after_turn: "04", parent: null, cause: "model-resolution", source_file: "amend-tail.md", source_sha256: "a".repeat(64), supersedes: ["03", "04a"], replacements: [{ key: "03b", amendment_item: "1" }, { key: "04b", amendment_item: "2" }] });
  ledger.push({ record: "plan", id: "r1", replaces: "r0", after_turn: "04", approval_ref: "ap2", contingency_policy_ref: null });
  ledger.push(tailPlanned);
  ledger.push(rebindTurn);
  ledger.push({
    record: "delegation", id: "d6", owner: "04b", owner_kind: "turn", plan: "r1",
    approval_ref: "ap2", resolution_ref: "res-04b", policy_ref: "rp1",
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
    record: "approval", key: "ap2", kind: "user-plan",
    event_ref: "user message: rebind approved", parent_approval_ref: null,
    scope: "replaced tail",
  });
  ledger[1].after_turn = null;
  ledger[1].contingency_policy_ref = "cp1";
  evidence.dispatch = { turn: "04b", route: "native-subagent", args: { model: "opus" } };
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
    event_ref: null, parent_approval_ref: "ap1",
    scope: "the 04a insertion", contingency_ref: "cp1",
  });
  return { ledger, evidence };
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
  "recorded-digest-fields"() {
    const built = buildBase();
    findRecord(built, "plan", "id", "r0").digest = "PENDING";
    built.evidence.approvals[0].target_digest = ZERO;
    built.evidence.approvals[0].snapshot_keys = ["01"];
    emit(built);
  },
  "auto-alias"() {
    const built = buildBase();
    built.evidence.approvals[0].kind = "auto-invocation";
    built.evidence.approvals[0].event_ref = "user invocation: /mode-orchestrator --auto plan.md";
    built.evidence.resolutions[0].match = "alias";
    emit(built);
  },
  "auto-exact-ok"() {
    const built = buildBase();
    built.evidence.approvals[0].kind = "auto-invocation";
    built.evidence.approvals[0].event_ref = "user invocation: /mode-orchestrator --auto plan.md";
    emit(built);
  },
  "auto-no-flag"() {
    const built = buildBase();
    built.evidence.approvals[0].kind = "auto-invocation";
    built.evidence.approvals[0].event_ref = "user invocation: /mode-orchestrator plan.md";
    emit(built);
  },
  "auto-other-flag"() {
    const built = buildBase();
    built.evidence.approvals[0].kind = "auto-invocation";
    built.evidence.approvals[0].event_ref = "user invocation: /mode-orchestrator --auto-resume plan.md";
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
    built.ledger.push({
      record: "delegation", id: "d7", owner: "04a", owner_kind: "turn", plan: "r0",
      approval_ref: "ap1", resolution_ref: "res-04a", policy_ref: "rp1",
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
      approval_ref: "ap1", resolution_ref: "res-01", policy_ref: "rp1",
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
      approval_ref: "ap1", resolution_ref: "res-04a", policy_ref: "rp1",
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
    built.ledger.push({ record: "amendment", key: "amend-1", plan: "r1", replaces: "r0", after_turn: "03", parent: null, cause: "todolist", source_file: "amend-tail.md", source_sha256: "b".repeat(64), supersedes: ["03"], replacements: [{ key: "04", amendment_item: "1" }] });
    built.ledger.push({ record: "plan", id: "r1", replaces: "r0", after_turn: "03", approval_ref: "ap2", contingency_policy_ref: null });
    built.ledger.push(replacement);
    built.evidence.approvals.push({
      record: "approval", key: "ap2", kind: "user-plan",
      event_ref: "user message: revised tail approved", parent_approval_ref: null,
      scope: "replaced tail",
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
    emit(built, { turn: "03", route: "native-subagent", args: { model: "sonnet" } });
  },
  "dispatch-legacy-state"() {
    const built = buildBase();
    emit(built, { turn: "03", route: "native-subagent", args: { effort: "pro" }, observed: { policy_state: "deny" } });
  },
  "dispatch-unavailable"() {
    const built = buildBase();
    findRecord({ ledger: built.evidence.policy }, "route-policy", "key", "rp1").state = "unavailable";
    emit(built);
  },
  "dispatch-cli"() {
    const built = buildBase();
    findTurn(built.ledger, "03").dispatch_plan.route = "pi-cli";
    emit(built, { turn: "03", route: "pi-cli", args: { effort: "pro" } });
  },
  "delegation-cli-route"() {
    const built = buildBase();
    findTurn(built.ledger, "01").dispatch_plan.route = "pi-cli";
    findRecord(built, "delegation", "id", "d1").route = "pi-cli";
    emit(built);
  },
  "policy-cli-route"() {
    const built = buildBase();
    findRecord({ ledger: built.evidence.policy }, "route-policy", "key", "rp1").route = "pi-cli";
    emit(built);
  },
  "route-state-unknown-word"() {
    const built = buildBase();
    findRecord({ ledger: built.evidence.policy }, "route-policy", "key", "rp1").state = "maybe";
    emit(built);
  },
  "legacy-ref-cli-route"() {
    const built = buildBase();
    built.evidence.policy.push({ record: "permission", key: "perm-x", state: "deny", scope: "subagent delegation", revision: "rev-0", grounds: "recorded by an earlier build" });
    findTurn(built.ledger, "01").dispatch_plan.route = "pi-cli";
    const d1 = findRecord(built, "delegation", "id", "d1");
    d1.route = "pi-cli";
    d1.policy_ref = "perm-x";
    emit(built);
  },
  "shell-route"() {
    const built = buildBase();
    findTurn(built.ledger, "01").dispatch_plan.route = "bash-shell";
    findRecord(built, "delegation", "id", "d1").route = "bash-shell";
    emit(built);
  },
  "plan-cli-route"() {
    const built = buildBase();
    findTurn(built.ledger, "03").dispatch_plan.route = "pi-cli";
    emit(built);
  },
  "policy-ref-env"() {
    const built = buildBase();
    built.evidence.policy.push({ record: "env-model-setting", key: "env1", state: "unset", per_effort: {}, grounds: "published setting state" });
    findRecord(built, "delegation", "id", "d1").policy_ref = "env1";
    emit(built);
  },
  "dispatch-no-route-policy"() {
    const built = buildBase();
    built.evidence.policy = [{ record: "route-policy", key: "rpz", route: "native", state: "available", constraints: {}, grounds: "published delegation contract" }];
    emit(built);
  },
  "dispatch-newer-available"() {
    const built = buildBase();
    built.evidence.policy.unshift({ record: "route-policy", key: "rp0", route: "native-subagent", state: "unavailable", constraints: {}, grounds: "earlier revision" });
    emit(built);
  },
  "recorded-native-allow"() {
    const built = buildBase();
    for (const r of built.ledger) {
      if (r.record === "turn") r.dispatch_plan.route = "native";
      if (r.record === "delegation") { r.route = "native"; r.policy_ref = "rp-rec"; }
    }
    built.evidence.policy = [
      { record: "permission", key: "p-rec", state: "allow", scope: "host-managed delegation", revision: "rev-rec", grounds: "recorded by an earlier build" },
      { record: "route-policy", key: "rp-rec", route: "native", state: "allow", constraints: { nested: false, skills: "disabled" }, grounds: "recorded by an earlier build" },
      { record: "env-model-setting", key: "env-rec", state: "configured", per_effort: {}, grounds: "recorded by an earlier build" },
    ];
    emit(built);
  },
  "legacy-run"() {
    const built = buildBase();
    built.evidence.policy.push(
      { record: "permission", key: "perm-old", state: "deny", scope: "subagent delegation", revision: "rev-0", grounds: "recorded by an earlier build" },
      { record: "route-policy", key: "rp-old", route: "pi-cli", state: "unknown", constraints: {}, grounds: "recorded by an earlier build" },
      { record: "route-policy", key: "rp-old2", route: "native-subagent", state: "permitted", constraints: {}, grounds: "recorded by an earlier build" },
    );
    findRecord(built, "delegation", "id", "d1").policy_ref = "perm-old";
    findRecord(built, "delegation", "id", "d2").policy_ref = "rp-old";
    emit(built, { turn: "03", route: "native-subagent", args: { effort: "pro" }, observed: { policy_state: "deny" } });
  },
  "dispatch-stale-old-key"() {
    const built = buildRebind();
    emit(built);
    fs.writeFileSync("dispatch-stale.json", JSON.stringify({ turn: "04a", route: "native-subagent", args: { model: "sonnet" } }));
  },
  "dispatch-rebind-ok"() {
    const built = buildRebind();
    emit(built, { turn: "04b", route: "native-subagent", args: { model: "opus" } });
  },
  "dispatch-stale-no-amendment"() {
    const built = buildRebind();
    built.ledger = built.ledger.filter((r) => !(r.record === "amendment" || r.record === "plan" && r.id === "r1" || r.record === "turn" && (r.key === "04b" || r.key === "03b") || r.record === "delegation" && r.id === "d6" || r.record === "attempt" && r.turn === "04b"));
    built.evidence.approvals = built.evidence.approvals.filter((a) => a.key === "ap1");
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
    built.evidence.approvals = built.evidence.approvals.filter((a) => a.record !== "contingency" && a.key !== "da1");
    findRecord(built, "delegation", "id", "d5").approval_ref = "ap1";
    emit(built);
  },
  "insertion-kind"() {
    const built = buildRebind();
    built.evidence.approvals.find((a) => a.record === "contingency").allowed_kinds = ["recovery-debug"];
    emit(built);
  },
  "derived-other-policy"() {
    const built = buildRebind();
    built.evidence.approvals.find((a) => a.key === "da1").contingency_ref = "cp9";
    emit(built);
  },
  "host-ref-ok"() {
    const built = buildBase();
    findTurn(built.ledger, "01").dispatch_plan.args = { resolution_ref: "host-ref-1" };
    findRecord(built, "delegation", "id", "d1").actual_selection_args = { resolution_ref: "host-ref-1" };
    findTurn(built.ledger, "03").dispatch_plan.args = { resolution_ref: "host-ref-3" };
    emit(built, { turn: "03", route: "native-subagent", args: { resolution_ref: "host-ref-3" } });
  },
  "host-ref-other"() {
    const built = buildBase();
    findTurn(built.ledger, "01").dispatch_plan.args = { resolution_ref: "host-ref-1" };
    findRecord(built, "delegation", "id", "d1").actual_selection_args = { resolution_ref: "host-ref-9" };
    findTurn(built.ledger, "03").dispatch_plan.args = { resolution_ref: "host-ref-3" };
    emit(built, { turn: "03", route: "native-subagent", args: { resolution_ref: "host-ref-9" } });
  },
  "host-ref-unplanned"() {
    const built = buildBase();
    findRecord(built, "delegation", "id", "d1").actual_selection_args = { resolution_ref: "host-ref-1" };
    emit(built, { turn: "03", route: "native-subagent", args: { resolution_ref: "host-ref-3" } });
  },
  "insertion-no-delegation"() {
    const built = buildRebind();
    built.ledger = built.ledger.filter((r) => !(r.record === "delegation" && r.id === "d5") && !(r.record === "attempt" && r.delegation_id === "d5"));
    built.evidence.approvals = built.evidence.approvals.filter((a) => a.key !== "da1");
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
  "md-v1-index"() {
    const records = [
      { record: "contract", version: "adaptive-effort-v1" },
      { record: "plan", id: "r0", replaces: null, after_turn: null },
    ];
    fs.writeFileSync("index.md", "# Run index\n\n## Turn plan\n\n```json\n" + records.map((r) => JSON.stringify(r)).join("\n") + "\n```\n");
    fs.writeFileSync("approvals.jsonl", "");
    fs.writeFileSync("resolutions.jsonl", "");
    fs.writeFileSync("policy.jsonl", "");
  },
  "role-absent"() {
    const built = buildBase();
    for (const r of built.ledger) if (r.record === "turn") delete r.role;
    emit(built);
  },
  "md-v2-index"() {
    const built = buildBase();
    emit(built);
    fs.writeFileSync("index.md", "# Run index\n\nprose above the block; the fenced block below is the ledger\n\n```json\n" + built.ledger.map((r) => JSON.stringify(r)).join("\n") + "\n```\n");
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

build "recorded-digest-fields"
expect_ok "digest fields of a recorded run are read and not inspected" validate --file ledger.jsonl "${EV[@]}"

build "auto-alias"
expect_violation "auto-invocation over a non-exact match is rejected" "auto-invocation needs exact resolution" validate --file ledger.jsonl "${EV[@]}"

build "auto-exact-ok"
expect_ok "auto-invocation over exact matches passes" validate --file ledger.jsonl "${EV[@]}"

build "auto-no-flag"
expect_violation "auto-invocation whose event_ref has no --auto is rejected" "must contain the invocation's --auto" validate --file ledger.jsonl "${EV[@]}"

build "auto-other-flag"
expect_violation "a longer flag does not count as --auto" "must contain the invocation's --auto" validate --file ledger.jsonl "${EV[@]}"

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

build "dispatch-legacy-state"
expect_ok "a recorded policy_state is read and not inspected" dispatch-check --file ledger.jsonl --dispatch dispatch.json "${EV[@]}"

build "dispatch-unavailable"
expect_violation "dispatch on an unavailable native route is rejected" "launch blocked: native route native-subagent unavailable" dispatch-check --file ledger.jsonl --dispatch dispatch.json "${EV[@]}"
expect_violation "delegation on an unavailable native route is rejected" "delegation only on an available native route" audit --file ledger.jsonl "${EV[@]}"

build "dispatch-cli"
expect_violation "dispatch through the CLI route is rejected" "a v2 dispatch goes only through a native route" dispatch-check --file ledger.jsonl --dispatch dispatch.json "${EV[@]}"

build "delegation-cli-route"
expect_violation "a delegation on the CLI route is rejected" "a v2 delegation goes only through a native route" audit --file ledger.jsonl "${EV[@]}"

build "policy-cli-route"
expect_violation "a current-vocabulary route-policy naming the CLI is rejected" "route-policy names a non-native route" audit --file ledger.jsonl "${EV[@]}"

build "route-state-unknown-word"
expect_violation "an unrecognized route-policy state is rejected" "route-policy state not recognized: maybe" audit --file ledger.jsonl "${EV[@]}"

build "legacy-ref-cli-route"
expect_violation "a CLI delegation pointing at an old permission record is rejected" "a v2 delegation goes only through a native route" audit --file ledger.jsonl "${EV[@]}"

build "shell-route"
expect_violation "a delegation on a shell route without cli in its name is rejected" "a v2 delegation goes only through a native route" audit --file ledger.jsonl "${EV[@]}"

build "plan-cli-route"
expect_violation "a CLI dispatch_plan is rejected at validate" "a v2 dispatch_plan goes only through a native route" validate --file ledger.jsonl "${EV[@]}"

build "policy-ref-env"
expect_violation "a policy_ref naming an env-model-setting record is rejected" "policy_ref must name a route-policy record" audit --file ledger.jsonl "${EV[@]}"

build "dispatch-no-route-policy"
expect_violation "dispatch with no route-policy record for its route is rejected" "launch blocked: no available route-policy record for native-subagent" dispatch-check --file ledger.jsonl --dispatch dispatch.json "${EV[@]}"

build "dispatch-newer-available"
expect_ok "dispatch passes when the latest route-policy record is available" dispatch-check --file ledger.jsonl --dispatch dispatch.json "${EV[@]}"

build "recorded-native-allow"
expect_ok "a run recorded with route native and state allow is read" audit --file ledger.jsonl "${EV[@]}"
expect_ok "a run recorded with route native and state allow validates" validate --file ledger.jsonl "${EV[@]}"

build "legacy-run"
expect_ok "a run recorded with permission records and old route states is read" audit --file ledger.jsonl "${EV[@]}"
expect_ok "a recorded old-vocabulary run still validates" validate --file ledger.jsonl "${EV[@]}"
expect_ok "a recorded policy_state of deny is not inspected" dispatch-check --file ledger.jsonl --dispatch dispatch.json "${EV[@]}"

build "dispatch-both-args"
expect_violation "dispatch with model and effort together is rejected" "exactly one of model|effort|resolution_ref" dispatch-check --file ledger.jsonl --dispatch dispatch.json "${EV[@]}"

build "dispatch-superseded"
expect_refused "dispatch-check refuses a superseded key" "is superseded by amend-1" dispatch-check --file ledger.jsonl --dispatch dispatch-super.json "${EV[@]}"

build "insertion-unapproved"
expect_violation "unapproved dynamic insertion is rejected" "dynamic insertion requires the plan's approved contingency policy" validate --file ledger.jsonl "${EV[@]}"

build "insertion-kind"
expect_violation "insertion kind outside the contingency policy is rejected" "insertion kind not allowed" validate --file ledger.jsonl "${EV[@]}"

build "derived-other-policy"
expect_violation "derived approval bound to another policy is rejected" "carries the covering derived approval" audit --file ledger.jsonl "${EV[@]}"

build "host-ref-ok"
expect_ok "a host reference that differs from the resolutions key passes audit" audit --file ledger.jsonl "${EV[@]}"
expect_ok "a host reference that differs from the resolutions key passes dispatch-check" dispatch-check --file ledger.jsonl --dispatch dispatch.json "${EV[@]}"

build "host-ref-other"
expect_violation "an actual reference other than the planned one is rejected" "actual resolution_ref must equal the planned one" audit --file ledger.jsonl "${EV[@]}"
expect_violation "a dispatch reference other than the planned one is rejected" "dispatch resolution_ref differs from the planned one" dispatch-check --file ledger.jsonl --dispatch dispatch.json "${EV[@]}"

build "host-ref-unplanned"
expect_ok "a reference is not compared when the plan carried none: audit" audit --file ledger.jsonl "${EV[@]}"
expect_ok "a reference is not compared when the plan carried none: dispatch-check" dispatch-check --file ledger.jsonl --dispatch dispatch.json "${EV[@]}"

build "insertion-no-delegation"
expect_ok "an insertion that was never launched needs no derived approval" validate --file ledger.jsonl "${EV[@]}"

build "insertion-delegation"
expect_violation "insertion delegation without the covering approval is rejected" "carries the covering derived approval" audit --file ledger.jsonl "${EV[@]}"

build "derived-cycle"
expect_violation "cyclic derived approval chain is rejected" "derived approval chain is cyclic" validate --file ledger.jsonl "${EV[@]}"

build "md-v1-index"
expect_refused "v1 contract inside an .md index is refused" "not a mo-delegation-v2 ledger" validate --file index.md "${EV[@]}"

build "role-absent"
expect_ok "a turn without role validates" validate --file ledger.jsonl "${EV[@]}"

build "md-v2-index"
expect_ok "v2 ledger inside an .md index validates" validate --file index.md "${EV[@]}"

printf 'delegation_plan_test: %s cases passed\n' "$pass"
