# Delegation contract — `mo-delegation-v2`

The normative definitions for how mode-orchestrator delegates without giving a
child more authority than the parent, adopts a model request, resolves and
approves it, pins it at launch, and records the run. `SKILL.md` and both harness
references point here instead of restating structures; this file is the one
place each structure is defined. Concepts are named here, not any host's API:
where a host does not publish the observation route, the run stops before
delegation (see §1).

Node helper: `scripts/delegation_plan.js` validates the data this file
defines (`validate`, `dispatch-check`, `audit`). It proves the integrity of
the data handed to it and nothing more: not the truth of a host's evidence,
not the authenticity of an approval event, and it cannot stop a shell that
bypasses it. Enforcement lives at the host's launch entry.

## 1. Route availability and the invariant

**A delegation never gives a child more authority than the parent session
has.** This is not a state read before the call: a refusal is decided per
call at the host's call boundary (a `tool_call` handler or tool
replacement on Pi; the permission rules and PreToolUse hooks on Claude Code)
and cannot be read ahead. It holds because of the **route**: the native
subagent route carries the host's mandatory guards into the child, so a
parent doing the same work itself is under the same guards. A permission
state is therefore not computed, and the presence of a handler, a load error
or an extension never decides whether to delegate. A refusal that does occur
is classified afterwards (§6).

The only question asked before the first delegation, and again at the launch
entry, is whether the **native route is available**, with its published
constraints and reason. It is read through the route the harness reference
names — structured startup information or a read-only query the host
publishes. The skill never searches a private settings file for it, and never
launches a real agent to discover it.

| observed | run behavior |
|---|---|
| native `available` | plan and run within the published constraints |
| native `unavailable` (delegation off, profile invalid, tool absent) | `blocked` before delegation, quoting the host's reason string. No CLI detour |
| no observation route (a build before the contract) | the model cannot be fixed: `blocked` before delegation, naming the old build |
| extension load error, presence of `tool_call` handlers | not a delegation input. The parent doing the work itself is in the same state |

What a call reveals — the model-fixing field, recursion suppression, skills
disabled, the evidence lines — is not checked before delegating. Every call
carries the approved binding and recursion off, and a shortfall is classified
from the call's result (§6).

A v2 run never starts a child through the CLI or a free shell, the judge
included. The existence of a shell, a CLI binary or skill text is never
grounds for delegation, and a refused or disabled capability is not
re-created by a CLI that could run it.

Constraint comparisons that may be coded: allowed-tools subsets, non-growth of
recursion caps, launch counts and deadlines, fixed cwd equality, skill
disablement kept. The meaning of an arbitrary hook or policy sentence is not
generally comparable in code.

Recursion and skills: a child never re-delegates. The call must carry the
host's per-call recursion suppression (a `nested: false` equivalent). The
child must not load skills — host-fixed or call-specified. "Don't use them" in
the prompt is not an equivalent guarantee. The orchestrator is never
restarted from a child. `MODE_ORCH_DEPTH` stays as the defense of the CLI
mechanics that remain only for stop observation, not as a delegation ground.
A refused operation is not retried in another shape; a withdrawn permission
stops the run. Only an ordinary transient launch failure feeds the existing
not-started/`aborted` rules.

Taskflow's project-notes scan denial reaches a Pi child of a taskflow-enabled
session from pi-extensions `c647291` on; on older builds it does not, which
leaves the invariant with that one exception there. The denial judges the
`grep` tool only, in the parent and the child alike. Neither case is
something the run works around.

## 2. Model request sources

Priority: **runtime prompt → the environment's child-agent model setting →
skill default**.

- `runtime` — a request made at execution time: whole-run, per-step, or
  per-effort, spoken in the launch prompt or normalized from a `(model:
  VALUE)` in the input document the user designated for this run. The original
  wording and its scope are preserved verbatim into each turn's selection.
  Quotations inside the document, past-run records, and artifact mentions are
  not requests. A direct launch-prompt specification outranks a normalized
  document one on conflict. Two requests from the same source that conflict
  without a readable target are confirmed with the user, never averaged.
- `environment` — the setting by which the environment chooses models for
  child-agent calls, including any per-work-class or per-effort mapping. Its
  observed state is `configured` / `absent` (the host publishes no such
  setting) / `unset` (exists, nothing selected) / `invalid` /
  `unobservable` / `ambiguous` / `unresolved`. Only `absent` and `unset` fall
  through to the skill default; any other state stops — it is never silently
  replaced by a lower source. An agent definition or parent-model inheritance
  counts as this source only when the environment publishes it as an explicit
  selection rule that can be pre-resolved.
- `skill` — `references/execution-profiles.md`, the mandatory default. Its
  shape (basic/pro/ultra, CC/Pi columns, single tokens, no cross-effort
  fallback) does not change. A malformed profile is `blocked`; a higher
  source never hides a broken profile.

An explicit model that makes effort unnecessary records `effort: null`.
"Everything with X" in a runtime request is not overridden by a mode name or
an inferred effort. Mode, role and effort are work classifications; a model
name, a thinking setting and a source are not identified with them.

## 3. Resolution

Resolution starts only after the source is chosen. Existence grounds are the
target host's published candidates, public aliases, or resolver results —
never a model recalled from an LLM's memory. Matching order and ambiguity
handling follow the harness reference's published resolver rules; no
cross-harness fuzzy matcher and no unconditional always-newest /
never-newest rule is added.

- Resolution granularity is recorded explicitly: `provider-model` (a provider
  plus an existing model id) or `harness-ref` (a model reference name the
  environment publishes). A `harness-ref` is never recorded as physically
  resolved.
- Thinking is recorded separately from effort, with state `fixed` (a value) /
  `host-inherited` / `undisclosed`. No value is invented for an undisclosed
  state. A request for thinking whose realization cannot be confirmed is not
  recorded as satisfied.
- On a route change, the model name is not the only thing carried: effective
  thinking, tool constraints, skill disablement and cwd are maintained too. A
  route that inherits parent thinking under an explicit model is not treated
  as equal to a route applying an effort's thinking.
- A model id resolved from natural language is not streamed into an existing
  tool contract that passes the user's words verbatim. A route whose host
  accepts only verbatim user text is not counted as supporting resolved
  values until the host publishes a contract that accepts the adopted
  resolution.

## 4. Approval evidence

Approval ties a plan to a user event. Three kinds:

- `user-plan` — the user approved a presented plan. Carries a reference to
  the original event, and the scope.
- `auto-invocation` — the user's actual `--auto` invocation instruction; its
  `event_ref` quotes that text, `--auto` included.
  Legal only when every resolution the plan uses is `exact`: the
  user-designated model reference matches, with no candidate choice, version
  selection, or semantic interpretation. A mechanical or unique resolution is
  not a user approval; neither is an LLM-written `approved: true`. Under
  `--auto`, a resolution derived from natural language, an abbreviation, an
  alias, a prefix, or a series still requires an explicit approval of scope
  and target; a non-interactive run stops before starting, as `needs-human`,
  showing the resolution and the concrete approval request.
- `derived` — an insertion or re-execution derived from an already-approved
  plan or contingency policy. Carries the parent approval reference. For a dynamic
  insertion — a dynamic turn whose `amendment_ref` is null — the record
  carries `contingency_ref` naming the plan's approved contingency policy, the insertion's kind must be in that
  policy's `allowed_kinds`, the parent chain must reach a plan approval
  without a cycle, and the insertion's delegations carry this derived
  approval as their `approval_ref`. A plan with a null
  `contingency_policy_ref` allows no insertion at all.

Match kinds recorded in resolution evidence: `exact`, `alias`, `prefix`,
`series`, `nl-interpret`. Only `exact` may ride `auto-invocation`. When the
scope's reading or the match kind cannot be confirmed, approval is not
skipped.

## 5. Ledger — run-directory record structures

New runs use `contract.version: ` `` `mo-delegation-v2` ``. The previous
`adaptive-effort-v1` stays readable and read-only: no v2 record is ever
appended to a v1 run, and a new run or plan is made from v1 artifacts rather
than extending them. One JSONL block per run index, one record per line,
fields exactly as tabled here — an unlisted key is rejected. Line order is
the ledger's time order; nothing in a record carries a clock.

Evidence lives in three append-only JSONL files beside `index.md` in the run
directory, referenced by `*_ref` fields — never inlined into the index:

- `approvals.jsonl` — `approval` records (`key`, `kind`, `event_ref`,
  `parent_approval_ref` — each null where it does not apply — `scope`,
  optional `contingency_ref` on a `derived` approval)
  and `contingency` records (`key`, `allowed_kinds`, `origin_turn`,
  `mode_policy`, `effort_policy`, `model_binding`, `scope`, `tool_constraints`,
  `caps`).
- `resolutions.jsonl` — `resolution` records: `key`, `turn`, `request`,
  `source`, `observed_state`, `candidates`, `match`, `resolved_model`,
  `granularity`, `thinking` (`{state, value?}`), `grounds`.
  `observed_state` is the state of the environment's child-agent model
  setting, carried by an `environment` source (`configured`) or a `skill`
  source (`absent` or `unset`); a harness that has no such setting records
  `absent`. For a `runtime` source it is null: the validator requires the
  field but checks its value only for `environment` and `skill` sources.
- `policy.jsonl` — `route-policy` records (`key`, `route`, `state`,
  `constraints`, `grounds`) with `state` `available | unavailable`, and
  `env-model-setting` records (`key`, `state`, `per_effort`, `grounds`). A
  new run writes no `permission` record. A recorded run may still carry
  `permission` records and the route states `permitted | unsupported |
  denied | unknown | allow | deny`: they are read, resolved by `policy_ref`
  and not inspected. The dispatch input's `observed.policy_state` is likewise
  read and not inspected, and so are `plan.digest`, `target_digest` and
  `snapshot_keys` in a recorded run; a new run writes none of them.
- Route names: a new run writes `native-subagent` for the native route in
  `dispatch_plan.route`, `delegation.route`, the dispatch input and its
  `route-policy`. Recorded runs also used `native` and `subagent`; those three
  are the native routes, and any other route — CLI, shell, anything else — is
  rejected on every turn, delegation and dispatch, whatever the policy record
  says. A delegation's `policy_ref` names a `route-policy` (or, in a recorded
  run, a `permission`) record; a current-vocabulary one must be `available`.
  `dispatch-check` takes the last current-vocabulary `route-policy` record for
  the dispatch route in file order and requires it to be `available`; no such
  record refuses the dispatch.

No secrets, credentials or unneeded configuration go into any of these. The
ledger is inspection evidence, not a resumable scheduler; the status
vocabulary children return is unchanged.

| record | fields besides `record` |
|---|---|
| `contract` | `version` = `mo-delegation-v2`; must be the block's first line |
| `plan` | `id`, `replaces`, `after_turn`, `approval_ref`, `contingency_policy_ref` |
| `turn` | `key`, `plan`, `kind`, `step`, `parent`, `inherits`, `inputs`, `mode`, `effort`, `effort_source`, `budget_refs`, `rebinds`, `amendment_ref`, `selection`, `dispatch_plan`; `role` optional; `amendment_item` required when `amendment_ref` is non-null |
| `delegation` | `id`, `owner`, `owner_kind`, `plan`, `approval_ref`, `resolution_ref`, `policy_ref`, `route`, `actual_selection_args`, `call_ref`, `reported_model`, `reported_thinking` |
| `attempt` | `turn`, `attempt`, `delegation_id`, `reported_status`, `effective_status`, `file` |
| `decision` | `key`, `parent`, `decider`, `file` — human adjudication only, as before |
| `amendment` | `key`, `plan`, `replaces`, `after_turn`, `parent`, `cause`, `source_file`, `source_sha256`, `supersedes`, `replacements` |

- `selection`: `source` (`runtime` / `environment` / `skill`), `request`
  (original wording), `scope_ref` (origin and scope reference),
  `resolution_ref` (resolutions.jsonl key), `resolved_model`,
  `granularity`, `thinking`. An `environment` selection's
  `observed_state` lives in its resolution record.
- `dispatch_plan`: `route` and the planned selection arguments (`args`), the
  same one-of shape as `actual_selection_args`.
- `actual_selection_args`: what the call actually carried — `model` only,
  `effort` only, or a published fixed resolution reference (`resolution_ref`
  only: the host's reference, not the resolutions.jsonl key). Never back-filled from a planned value. `model` and `effort` are
  never sent together.
- `effort_source`: `step-model` / `step-effort` / `workflow-effort` /
  `inferred-effort` / `policy-effort` / `inherited`. The v1 `source` field
  becomes `effort_source`; model provenance lives in `selection`. No
  `planned_override` / `actual_override` compatibility hack carries effort or
  a planned model.
- `budget_refs`: `recovery_owner`, `decision_owner`, `restart_owner`. An
  initial turn owns all three itself. A dynamic turn's first two are its
  existing originating turn; its `restart_owner` is the dynamic turn itself.
  A rebind keeps all three from the origin turn. Insertions and cycles are
  counted by this identity at the first appearance along `rebinds`, never by
  replacement-record count; a restart aggregates under the original
  `restart_owner`. Judges create none of these caps.
- `rebinds`: the immediately previous turn key of a one-to-one rebind; null
  otherwise. `amendment_ref` is the amendment key that approved the
  replacement; null on a first appearance. The loop relations `parent` /
  `inherits` never carry amendment lineage.
- `reported_model` / `reported_thinking` are the child's report; undisclosed
  or missing reads `null` (`unknown`). Never a measurement of the physical
  model. `reported_status` never carries `stale-plan` — that value exists
  only in `effective_status`.
- A delegation record is appended from the call's outcome or the host's
  evidence. `owner_kind` is `turn` or `restart-judge`; a judge's `owner` is
  the identifier derived from the existing attempt stem and creates no turn,
  attempt or cap. No call → no delegation, no attempt: a pre-launch refusal
  is prose in the run index. A call that never started still appends its
  delegation record with the actual call evidence; `call_ref` names the start
  state and its classification grounds.

## 6. Host result → run control

At the launch entry, the approved resolution is pinned — the approved resolution
reference, or arguments fixing the same effective model and thinking, are what
the call carries — and the host verifies, before the first model request, that
the current policy still matches the approved result. A settings revision that
changed while the effective result stays semantically the same needs no
re-approval; only the data proves it. A pre-launch re-read alone does not beat
the gap between check and call: pinning to the approved result or an atomic
entry check is what does, and a revision written into JSON is not by itself a
guarantee.

For ordinary turns and their recovery, decision and re-run turns. The
restart-judge has its own rules below — it owns no turn or attempt.

| observed result | records / run behavior | retry budget |
|---|---|---|
| native `unavailable`, invalidity or an absent observation route before any call | no delegation, no attempt; the host's reason in prose. Run `blocked`; a question is presented only when a concrete human choice is required | none consumed. No detour, no retry |
| called, then refused at the host call boundary, never started | delegation + attempt, `effective_status: blocked`. The refusal and not-started evidence saved; run stops | none. Not fed to the existing not-started direct re-run |
| called, resolution changed, never started | delegation + attempt, `reported_status: null`, `effective_status: stale-plan`. Back to replanning / the required approval; the unexecuted tail is replaced | call number recorded; restart / recovery / decision budgets not consumed. Re-sending the same plan without approval is forbidden |
| transient launch failure, not-started confirmed | delegation + attempt, `effective_status: aborted`. Existing not-started direct re-run rules only | the existing once-per-turn-key restart budget. No plan or model change |
| start state unknown, or timeout / drop after start | delegation + attempt, `effective_status: aborted`. Existing stop observation and judge | existing budgets, only where the existing gate allows |
| started; reported model/thinking contradicts the approved result | actual report and child `reported_status` kept, `effective_status: blocked`. Run stops; existing stop means handle the remainder. Side effects are not undone | no automatic re-run, no recovery. Reported as a contract violation for investigation |
| started, consistent with the resolution, or only undisclosed-from-the-start range unobserved | the child's ordinary status classification | existing rules |

`stale-plan` is run-control vocabulary only — never offered to a child, never
counted as a task failure. The run stops at an approval wait that shows the
user the difference; `--auto` never approves a new resolution on its own. A
run that cannot continue unattended ends `needs-human` showing what needs
approval. Evidence of a value declared pinned before launch that comes back
missing is not re-read as "undisclosed from the start" — the contract cannot
be confirmed, so the run stops. When the host cannot prove not-started, the
run does not re-inject on a zero-effect guess.

**Restart-judge rules.** Before `issue`: if native availability, model resolution,
approval or launch preparation cannot be completed, do not issue — stop.
Policy or capability shortfall is `blocked`; a concrete human decision wait is
`needs-human`. After `issue`: the actual call is tracked by its delegation
record (`owner_kind: restart-judge`) and prose in the existing judge record;
no turn or attempt is created. A policy denial, or a violation of the approved
model or constraints, after issue is `blocked`. A resolution gone stale, a
transient not-started, a timeout or an unreadable outcome make the judge
unsuccessful: stop `needs-human` under the existing contract — never a
stale-plan replan, never a not-started direct re-run. The judge is never
re-issued, its record never deleted or reused, no restart budget is grown,
and the origin turn is not re-injected without the judge's ALLOW. A setting
re-read after issue never re-resolves or re-approves. The judge's own reply
still passes the existing deadline, record, citation and end-marker checks;
the 900-second issue deadline and one-judge-per-attempt apply independently
of any host-side stale prevention.

## 7. Rebinding and amendment

An amendment replaces the complete unexecuted tail under a new plan:
`replaces` naming the old plan, `after_turn` the last
executed boundary. `cause` is `todolist` / `model-resolution` / `policy` /
`user-revision`. Only unstarted turns rebind; when a turn has any call at
all, rebinding requires evidence that every call of the turn was unstarted.
Started or start-unknown work is never re-read as unstarted. A successor plan
that is not approved launches nothing. Re-planning never lifts a refusal at the
call boundary — that stops the run.

Amendment lineage and loop lineage are separate fields: `amendment_ref` /
`amendment_item` carry where a replacement came from; `parent` / `inherits`
carry the loop relations of the work itself. A dynamic turn's `parent` is
never overwritten with an amendment key.

A one-to-one model/route rebind's new turn carries `rebinds: <previous key>`
(`rebinds: null` on first appearance). The chain is acyclic, stays within one
kind and one work identity, and rejects branching concurrent successors or
merges into different work. `kind`, mode/role, the step's own work text, the
loop relations (`parent` / `inherits`), and the continuation rules are kept.
The replacement map is fixed for the whole tail first; a `parent` /
`inherits` / `inputs` reference into the replaced tail is rewritten to the
corresponding new key, any reference outside it is untouched, and references
to executed inputs and artifacts never change. A dynamic kind stays dynamic —
replacements are never all flattened to `planned`. A todolist amendment's
new planned turns have `parent` / `inherits` null.

The new `selection` / `dispatch_plan` follow §2 and the change approval; on a
rebind this binding outranks the inherited one, while `inherits` itself stays
as the work-lineage ground. The exception never widens to an unapproved
ordinary retry.

The charging identity of a loop insertion or a recovery cycle is the
first-appearance turn found by walking `rebinds` back. Budgets are counted
there; restart attempts aggregate under the original `restart_owner`. A
rebind's record never counts as a new loop insertion and never re-issues a
budget over a key change.

Continuation follows the active successor's kind: a `decision-turn` keeps its
adjudication-only prompt and three-value reply contract, and its verdict
parents the `decision-rerun`; recovery and re-run turns keep their original
loop transitions. No successor work is launched from an unstarted old turn.

Worked example: `05a` was inserted as a `decision-turn` and went stale
unstarted. Its rebind `05b` carries `kind: decision-turn, rebinds: 05a`;
`parent` stays the original raising turn, `inherits` stays null, the
amendment provenance sits in `amendment_ref`, and the insertion's charging
identity remains `05a`. A `decision-rerun` needed after `05b` adjudicates
takes the next free suffix, `parent: 05b`, `inherits: <the original raising
turn>`.

## 8. What the helper checks — and what it cannot

`scripts/delegation_plan.js` reads only the data named above. `validate`
checks block structure, v2-only vocabulary, exact field sets (unlisted keys
rejected), reference integrity, kind/lineage rules, budget ownership, approval
bindings and the insertion-approval rules. `audit`
adds the delegation/attempt conversion rules of §6, the report-contradiction
stop, stale-plan handling and budget non-double-count. `validate` and `audit`
run with the run's three evidence files: a rule whose evidence file is not
provided is a violation naming the missing file, never a silent skip.
`dispatch-check` checks one planned call against the approved resolution —
binding shape, model/effort/resolution agreement, an optionally reported
observed state, and it refuses a key an amendment has superseded or a turn
sitting on a stale-plan attempt.

It proves the integrity of the data it is given. It does not prove: that a
policy record reflects the host's real policy; that an approval event
happened or was made by the user; that a host launched what the records say;
that a shell call did not bypass the helper; that the ledger was not edited
after approval. An `auto-invocation` whose `event_ref` has no `--auto` is a
violation, which catches an honest mislabel only: a string containing it
passes, and so does a self-written `user-plan`. Those grounds live in the host's
launch entry and the user's event, not in this validator. Because the record
version stayed `mo-delegation-v2` across the route-availability change and the
removal of the plan digest, the
helper cannot tell a recorded run from a new run that wrongly writes the old
vocabulary: such a run passes. Writing only the current vocabulary is the
orchestrator's obligation, not something the helper enforces.
