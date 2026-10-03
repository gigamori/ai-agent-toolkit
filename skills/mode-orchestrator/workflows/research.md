# Workflow: research

## Target task type

Evidence-led research for answering a bounded question for a stated audience.

## Scope and inputs

The input document supplies the question, audience, scope, time range, authorized source classes, requested deliverables, acceptable uncertainty, and the absolute path the report is written to. This workflow may refine the research plan inside those bounds, but it must not silently change them.

The input todolist must already contain the steps to run and, for each step, the earlier steps whose deliverables it needs (see Artifact flow). Optional steps are not inserted automatically.

## Recommended step sequence

| id | mode | effort | task |
|---|---|---|---|
| method | plan | (infer) | Restate the question, audience, scope, constraints and deliverables from the input document, then define subquestions, acceptance criteria, candidate source classes, search and exclusion rules, evidence checks, and an answer-or-unknown stopping rule. State whether terminology or the entity set is too unclear to plan against. Surface unresolved ambiguity without narrowing the task; return `needs-decision` if continuing requires a requester choice, or `needs-human` if human action is required. |
| scout | survey | (infer) | Run bounded reconnaissance for the terminology, key entities, likely sources, disagreements and edge cases the `method` plan flagged as unclear. Record findings as candidate material without changing the plan. |
| refine-plan | plan | (infer) | Revise the `method` plan using the `scout` findings, inside the bounds of the input document, and write the complete revised plan. |
| gather | survey | (infer) | Find candidate sources for the plan. Record provenance, date/version, coverage and which subquestion or criterion each source may address; do not treat discovery results as verified evidence. |
| validate | survey | (infer) | Check candidate material in the original sources for relevance, provenance, period, scope, independence, methodology and limitations. Write the evidence register: one entry per claim, each with a stable evidence ID and a state of verified, contradictory, insufficient or excluded. |
| reason | survey | (infer) | Organize verified evidence into supported findings, competing interpretations, contradictions and unknowns for each subquestion, citing evidence IDs. Add a section of plan-gap candidates: evidence-backed discoveries the plan does not represent, each with its possible impact. Do not resolve unsupported disagreements or change the plan. |
| replan | plan | (infer) | Make only evidence-justified, in-scope changes required by the plan-gap candidates of the earlier `reason` deliverable: update subquestions, criteria, comparison dimensions, search tasks or report structure. Return `needs-decision` instead of changing the objective, scope, time range, population or authorized sources. Write the complete revised plan, or report no-op if no change is required. |
| gap-fill | survey | (infer) | Gather only the evidence required by the gaps this step's text names and by the `replan` items. Record candidate sources and the exact gap each may resolve; do not promote them to verified evidence. If no gap remains, report no-op. |
| revalidate | survey | (infer) | Validate the `gap-fill` candidates using the same rules as `validate`, and write the complete updated evidence register, keeping existing evidence IDs. If no new evidence was gathered, report no-op. |
| re-reason | survey | (infer) | Update the findings, contradictions and unknowns that newly verified evidence materially affects, without changing the plan, and write the complete updated synthesis. If none is affected, report no-op. |
| draft | execute | (infer) | Write the report to the report path the input document names, strictly from the plan and the evidence register, citing evidence IDs for every material claim and stating uncertainty, exclusions and limitations. Planned check: a command you write and run confirms that every evidence ID cited in the report is in the register with state verified or contradictory, and that every required deliverable section is present; record that exact command in your change-report. |
| challenge | review-doc | (infer) | Review the report against the input document's bounds and audience, the plan, and the evidence register, including the counterevidence already recorded there. Check unsupported claims, citation support, contradictions, scope, consistency and clarity; do not expand scope or search for new evidence. |
| revise | execute | (infer) | Revise the report at the report path for the findings of `challenge`, using only evidence already in the register. Planned check: the command recorded in the `draft` change-report, run again. If `challenge` reported no finding to revise, report no-op; the planned check does not apply. |
| recheck | review-doc | (infer) | Review the claims, citations, contradictions and limitations `revise` changed against the evidence register and the input document. A clean pass ends the current round. If no revision occurred, report no-op. |

`(infer)` does not pin effort.

A first round runs `method`, `gather`, `validate`, `reason`, `draft` and `challenge`. Two pairs are optional in it:

- `scout` with `refine-plan`, only when the `method` plan states that terminology or the entity set is too unclear to plan against;
- `revise` with `recheck`; include both whenever a revision may be needed, since each reports no-op when its trigger did not occur.

`replan`, `gap-fill`, `revalidate` and `re-reason` belong to a second round only: their targets come from the first round's `reason` deliverable, which does not exist while the first-round todolist is written. A second round runs `replan`, `gap-fill`, `revalidate` and `re-reason`, then `draft`, `challenge` and optionally `revise` with `recheck`. It takes the first round's plan, evidence register and synthesis as inputs by absolute path.

The orchestrator warns when a todolist omits a recommended row. A warning naming only rows that the two paragraphs above leave out is expected.

An unresolved question may remain explicitly labeled as unknown rather than trigger unbounded search.

## Artifact flow

Within one run, the orchestrator writes each turn's deliverable under the run directory, so the todolist cannot name that path in advance. Each step instead names the earlier steps whose deliverables it uses; the orchestrator passes those deliverables to the turn by absolute path.

An input that exists before the run starts (the input document, this workflow file, deliverables of a previous round) is named in the todolist by absolute path.

The report is not a run-directory deliverable. The input document names its absolute path, and `draft` and `revise` write the report there; their own deliverables are change-reports, as for every `execute` turn. Every step that reads the report (`challenge`, `revise`, `recheck`) receives the report path and the change-report of the step that last wrote it.

A first round flows:

```text
method       → plan
scout        → reconnaissance findings, if included
refine-plan  → complete revised plan, if included
gather       → candidate source register
validate     → evidence register
reason       → synthesis + contradictions + unknowns + plan-gap candidates
draft        → report at the report path; change-report
challenge    → review findings
revise       → revised report at the report path; change-report, if included
recheck      → final review findings, if included
```

A second round flows:

```text
replan       → complete revised plan, or no-op
gap-fill     → additional candidate sources, or no-op
revalidate   → complete updated evidence register, or no-op
re-reason    → complete updated synthesis, or no-op
draft, challenge, revise, recheck → as in the first round
```

An update step (`refine-plan`, `replan`, `revalidate`, `re-reason`) writes either a complete replacement of the artifact it updates or a single no-op line naming the earlier artifact that stands. A later step receives both the earlier artifact and the update, and uses the update unless it is a no-op. Never merge the two.

Do not rely on a turn remembering an artifact that is not supplied through its inputs.

## Policy delivery

Workflow prose outside the todolist is not injected into autonomous turns. A turn applies the policies below only when it receives them. Pass them to every step, either:

- as this workflow file, by the absolute path `<this skill's dir>/workflows/research.md`, with `<this skill's dir>` replaced by the directory the mode-orchestrator skill is loaded from; or
- as a dedicated policy artifact holding the Turn policy sections listed for that step, by absolute path.

Write an input path as an absolute path, with forward slashes on Windows (`C:/…`), never with `~`. A relative path resolves against the workspace the run was started in, not against `<this skill's dir>`.

A turn does not otherwise know which step of this workflow it is. The text of each todolist step therefore names its step id, for example "As step `validate` of the research workflow: …". A turn applies the Turn policy sections listed for its step id below. Failure policy and Authoring guidance address the orchestrator and the todolist author, not the turn.

| step id | Turn policy sections |
|---|---|
| `method`, `refine-plan` | Research plan; External retrieval; Stopping |
| `replan` | Research plan; Dynamic discovery; External retrieval |
| `scout`, `gather` | Evidence states; Search and sources; External retrieval |
| `gap-fill` | Evidence states; Search and sources; External retrieval; Gap-fill |
| `validate`, `revalidate` | Evidence states; Evidence register; Search and sources; External retrieval; Quantitative research |
| `reason`, `re-reason` | Evidence states; Reasoning; Dynamic discovery; Quantitative research; Stopping |
| `draft`, `revise` | Evidence register; Quantitative research; Drafting and review |
| `challenge`, `recheck` | Evidence register; Drafting and review |

## Turn policy

### Research plan

The plan produced by `method`, or by `refine-plan` or `replan` when used, should make subsequent research executable.

For each material subquestion, record where applicable:

- identifier;
- question;
- acceptance criterion;
- required evidence type;
- candidate source classes;
- search or retrieval tasks;
- inclusion and exclusion rules;
- relevant entities, populations, periods or comparison dimensions;
- known ambiguity or terminology risks;
- target deliverable or report section.

A plan change must preserve prior verified evidence unless the new evidence shows that it is invalid, out of scope or requires reinterpretation.

### Evidence states

Keep discovery separate from validation.

- `candidate`: discovered but not checked in the original source;
- `verified`: checked in the original source and usable for the stated claim within recorded limits;
- `contradictory`: verified evidence that materially conflicts with another relevant claim or source;
- `insufficient`: relevant material that does not establish the intended claim;
- `excluded`: rejected for a recorded reason such as wrong population, period, version, provenance, duplication or scope.

`gather`, `scout` and `gap-fill` produce candidate material, not verified evidence.

Only `validate` and `revalidate` may promote candidate material into the evidence register.

### Evidence register

Every register entry has a stable evidence ID (`E1`, `E2`, …), and a later update keeps existing IDs. The synthesis and the report cite evidence by these IDs.

A material claim is one the answer or a key finding depends on. For each material verified or contradictory claim, record enough to recover:

- original source location;
- checked passage, table, figure, dataset field or method;
- source date or version;
- covered population, geography and period;
- claim supported or contradicted;
- material limitations;
- upstream dependency when multiple sources rely on the same original evidence.

Any other entry records only its ID, source location and state, plus the reason for an excluded entry.

Do not treat multiple reproductions of the same upstream source as independent confirmation.

### Search and sources

Search breadth follows the plan and its acceptance criteria rather than a fixed query count.

Where relevant:

- prefer primary or first-party sources for an organization's own actions, specifications, filings, policies or measurements;
- prefer authoritative statistics or datasets for population-level quantitative claims;
- prefer peer-reviewed or methodologically transparent research for scientific or causal claims;
- use credible independent sources to test first-party framing and capture external context;
- seek material counterevidence for contested claims;
- record source, population and period exclusions.

Search-result snippets and rankings are discovery aids, not verified evidence.

When the original source is available, inspect the original source before accepting a claim.

Failure to find something under a recorded search method means only that it was not found in the searched material. It does not establish that it does not exist.

### External retrieval

Web search and fetching remote pages send requests outside this machine. A step performs them only when its own todolist text explicitly permits external retrieval; authorized source classes in the input document alone do not.

- Permitted, and a web search or fetch tool is available: use it.
- Permitted, and no such tool is available: work from local material and the sources the input supplies, and record the missing external coverage as a limitation. This is not a reason to stop.
- Not permitted: do not retrieve externally.

### Dynamic discovery

`reason` records plan-gap candidates; it does not modify the plan.

A discovery is a plan-gap candidate only when verified evidence indicates that it could materially affect at least one of:

- the answer to the original question;
- a causal or explanatory model;
- a comparison dimension;
- the relevant entity set inside the existing scope;
- an acceptance criterion;
- the evidence needed to answer a planned subquestion.

Do not create a new research branch merely because a source contains an interesting adjacent topic.

`replan` may make a bounded change only when the change remains inside the framed objective.

It may:

- add or split a subquestion;
- revise an acceptance criterion;
- add a comparison dimension;
- add targeted evidence requests;
- reopen a provisional finding;
- adjust report structure needed for the original deliverable.

It must not silently:

- change the research question;
- expand the authorized geography, population or time range;
- change the intended audience;
- replace the requested deliverable;
- authorize a previously excluded source class;
- alter a requester-supplied normative or decision criterion.

If one of those changes is required, return `needs-decision` with the required Decision request fields.

### Gap-fill

`gap-fill` is targeted research, not a general second search pass. Its step text names, for each gap known from the first round:

- the exact unsupported criterion, contradiction or omitted population, and the first-round deliverable it comes from;
- why it matters;
- the evidence type or source class that could resolve it;
- the stopping condition.

It also covers the items `replan` adds in the same round, under the same stopping discipline.

`gap-fill` produces candidate evidence only. Any new material that may affect the report must pass through `revalidate` before `re-reason`, `draft` or `revise` may use it.

If a gap cannot be resolved under the authorized search scope, preserve it as an unknown or limitation rather than widening the task automatically.

### Reasoning

`reason` and `re-reason` synthesize evidence; they do not perform normative choice or change the plan.

They may:

- group verified facts;
- describe evidence-supported relationships;
- compare supported interpretations;
- identify contradictions;
- state what the evidence supports, does not support or leaves unknown;
- preserve uncertainty.

They must not:

- invent a resolution to conflicting evidence;
- assume missing facts;
- expand the research objective;
- authorize additional research;
- make a requester decision that belongs in `needs-decision`.

### Quantitative research

For measurements, estimates or counts, preserve where applicable:

- exact quantity;
- canonical unit;
- population, geography and period;
- source version or dataset vintage;
- numerator and denominator;
- transformations or calculations;
- known exclusions;
- uncertainty or range;
- incompatible source definitions.

When an estimate is synthesized rather than directly reported, record the method and inputs.

When feasible, use an independent cross-check for important synthesized estimates.

Do not mechanically average incompatible estimates or hide disagreement by selecting one without explanation.

### Drafting and review

`draft` and `revise` may use only evidence in the register supplied in their inputs. They must not search for new evidence or promote candidate evidence.

Their planned check is a command the turn writes and runs over the report and the register, so a `## Failure report` can give that command as its reproduction.

`challenge` and `recheck` review existing written artifacts against the supplied evidence register, the plan and the input document. They judge scope and audience against the input document, not against the plan alone.

They must not:

- expand the research scope;
- inspect new external material;
- perform a new search;
- repair the document while reviewing it.

`challenge` and `recheck` put the number of unresolved material findings on the first line of their gist, after the required header lines (`0` for a clean pass).

If `challenge` identifies a claim that needs new evidence, the current review records that finding. New research requires a second round.

### Stopping

Research may stop when:

- the plan has been executed to its stated evidence standard;
- required deliverables can be produced from verified evidence;
- material contradictions and unknowns are explicitly represented;
- unresolved gaps are documented;
- further search is not required by an already-authored step.

The workflow does not require every uncertainty to be eliminated.

An answer of “unknown under the searched evidence and stated bounds” is a valid outcome when supported by the research record.

## Failure policy

- Recovery-cycle cap: **2 per originating `execute` turn** (`draft`, `revise`).
- LLM-decision insertion cap: **2 per originating turn**.
- The planned check of `draft` and `revise` is the command named in their task. Evidence gaps, conflicting sources, unsuccessful searches, inaccessible sources and a missing retrieval tool are not check failures and never trigger recovery: the turn records them as unknowns or limitations and continues.
- A missing fact, unresolved contradiction or insufficient source base is handled by a documented unknown, a decision request, or a second round, never by repeating research inside recovery.

## Authoring guidance

Make each todolist step independently actionable. Its text names its step id (see Policy delivery) and, if the step retrieves externally, explicitly permits it (see External retrieval).

Provide the explicit inputs it needs, including:

- the input document, by absolute path, to every step: it carries the bounds, the audience and the report path;
- this workflow file or the step's policy artifact, by absolute path (see Policy delivery);
- the earlier steps whose deliverables it uses, with both the earlier artifact and the update wherever an update step ran (see Artifact flow);
- for `challenge`, `revise` and `recheck`, the report path and the change-report of the step that last wrote the report;
- the content its deliverable must contain.

Keep mode responsibilities separate:

- `survey`: collect, inspect, validate or organize evidence without changing the plan or making requester decisions;
- `plan`: create or revise the research plan inside authorized bounds;
- `execute`: produce or revise the report from verified evidence;
- `review-doc`: review an existing report against already supplied evidence and constraints.

Do not ask:

- a discovery turn to treat its own findings as verified evidence;
- a reasoning turn to authorize plan changes;
- a planning turn to silently change requester-controlled scope;
- a drafting turn to find new evidence;
- a review turn to search for unread material or repair the artifact being reviewed.

Feedback received after delivery is new input. Identify the affected question, claim, scope or evidence requirement and author a new research or revision round. This workflow does not run a post-delivery feedback loop automatically.
