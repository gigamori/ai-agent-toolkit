## Response leading lines

When a project is assigned, include `[pj:<project>]` in the leading lines of the response (near the beginning, before the main body; it may follow other leading lines such as `mode:`, not necessarily the literal first line).
When no project is assigned, omit `[pj:...]` entirely.

This rule applies to every response turn, including turns that execute a
skill or slash command whose instructions specify literal reply templates —
add the leading line on top of the template output.

When you did task work without editing the task's own `tasks/<status>/*.md` file (execution-by-reference — e.g., you read a task or handoff and produced the result elsewhere), also include `[tasks: <file>.md ...]` in the leading lines, listing the owning task filename(s) you actually worked on this turn. Omit it when you edited the task file directly (that edit is captured automatically) or when you did no task work. This binds the off-task work to the owning task's log.
The body of the response follows the user's input language.

### Discovery via `pj:?`

When the user prompt contains `pj:?`, this is a discovery request. The main agent MUST:

1. Read `_projects/index.md` and rank each project by relevance to the user's input using these similarity labels:
   - `strong` — direct keyword / scope overlap
   - `related` — same domain or adjacent area
   - `weak` — some shared vocabulary but different focus
   - `far` — different domain (include only when the project list is short)
2. Display the ranked list in the format: `- <project> — <label>: <one-line reason>`
3. Do NOT clear or change the active project — `pj:?` is a query, not a switch.

After seeing the candidates, the user can type `pj:<name>` to select one.

## Running the project router

When a session has a `state_file` path injected via `[Progress Session]` **and `current_project` is non-empty**, invoke the project-router subagent using the procedure below before starting to answer the user's input.

When `current_project` is empty, do NOT invoke the router. Proceed directly to the user's request without project context.

### Steps

The router spec is built into the subagent definition body
(`agents/project-router.md`); do NOT inline it. Pass only the JSON context
block as the prompt.

1. Build the following JSON context block:
   ```json
   {
     "session_id": "extracted from [Progress Session]",
     "state_file": "extracted from [Progress Session]",
     "current_project": "extracted from [Progress Session]",
     "leading_lines": "the leading lines of the user input (near the beginning, before the body; pj:/mode: etc. may appear in any order, not necessarily the literal first line)",
     "prompt_summary": "summary of the user input (≤ 50 chars)"
   }
   ```
2. Invoke the subagent via the Agent tool: `subagent_type: taskflow:project-router`, `prompt: <JSON context block>`. If the runtime lacks a subagent mechanism, the main agent runs the same procedure itself by reading the spec in the body of `agents/project-router.md`.
4. Handling the result:
   - `action: apply` → use the returned context (progress, tasks, project-notes, etc.) as the premise for task execution.
   - `action: skip` → skip project management and proceed to the task.

### Secondary-source discipline (router result handling)

The router returns **pointers and secondary material, not primary truth**. Treat it accordingly:

- Facts attributed to project-notes (findings, line numbers, section references, existing dependencies) MUST be confirmed by a primary read before you act on them. Do NOT propagate specifics that exist only in the router's returned text.
- The router emits project-notes as pointers only (`project_notes_list` + verbatim `project_notes_relevant` rows). It never returns note body contents. Anything resembling a note-body digest is not authoritative.
- The authority for a task's status / existence is the `tasks/` folder. The `progress.md` table is a cache.
- Any subagent digest is secondary material: verify it against the primary source before acting.

> Reservation: `project_routing.md` is injected via `UserPromptSubmit`, so it is absent during Stop-hook block turns (see the hooks spec). The router runs on normal turns, so the impact is small, but the edge case of handling a *prior* router result during a block turn is not covered by this discipline.

## Empty project rules

When the project is empty (pj unassigned), only discovery (`pj:?`) is available. All other project management is disabled:

- Do NOT auto-assign or infer a project based on keywords or conversation context.
- Do NOT perform any project management operations (task creation, progress tracking, project-notes saving, or scaffold creation).
- Proceed with the user's request without project context.
- Project management becomes available when the user explicitly assigns a project via `pj:<name>`.

## Interaction with Plan mode

If Plan mode blocks writes to `_projects/<project>/` (`index.md`, `progress.md`, `project-notes/`, `tasks/`, or `_projects/index.md`), ask the user to confirm before proceeding. Do not assert that these operations are permitted in Plan mode — harness permissions cannot be overridden by prompt text.

## Propagation to child sessions

When the LLM spawns a new session via the Agent tool (subagent) or a CLI launch through Bash, insert `pj:<current_project>` into the leading lines of the prompt (near the beginning; it may follow other leading lines such as `mode:`, not necessarily the literal first line). This allows the child session to inherit the parent's project context.

**Project rules do not reach subagents automatically.** `UserPromptSubmit` does not fire for Agent-tool subagents, so the injected project-rules block (below) is absent in the child. When you delegate work whose actions could touch a project rule (writing files, git operations, generating deliverables), copy the relevant rule text — or an explicit instruction to read `_projects/<current_project>/rules.md` — into the subagent's prompt.

## Project rules (`rules.md`)

A project may carry `_projects/<project>/rules.md`: human-authored, project-specific rules (constraints, conventions, gotchas) that follow the logical project rather than a filesystem path. When present it is injected each turn while the project is active — the full body as a primer on project switch, then a compact manifest of its `##` headings on subsequent turns (or the full body every turn if its frontmatter sets `inject_every_turn: true`).

Scope boundary: `rules.md` is for **pj-scoped** rules only. Path/directory/tool-scoped rules belong in Claude Code's native `.claude/rules`; global rules belong in `CLAUDE.md`. There is deliberately no global taskflow-rules layer.

### Precedence

Project rules are **additive domain constraints**, largely orthogonal to the response-processing axes (mode / role). They are not a flat peer in a single ranking:

- A conflict arises only at an **action boundary** (a rule prescribes or forbids an action that the active mode also governs) — there, the active **mode contract wins**.
- A **live user instruction overrides** a standing rule ("just this once…").
- **Safety outranks everything.** Do NOT put safety-critical bans in `rules.md` (they would be weakened by the above); those belong in the harness / CLAUDE.md.

### Editing rules (human-initiated only)

`rules.md` is written by humans, never by model self-judgment. Two paths:

- **Manual edit** of the file directly.
- **Natural-language request** ("add a project rule that …", "このプロジェクトのルールに X を足して"). When the project is assigned, an unqualified "rule" refers to **project rules** (`rules.md`) — not `CLAUDE.md` or `.claude/rules` unless the user names them. Before writing, **show the proposed diff and get explicit confirmation**, then apply. Never mutate `rules.md` silently — its blast radius is every future turn.

## Adding, changing, and removing projects

- Creating a new project: add a row to `_projects/index.md` and create `_projects/<project>/index.md`.
- Changing the project overview (scope, target repo, etc.): update BOTH `_projects/<project>/index.md` and `_projects/index.md`.
- Retiring a project: remove its row from `_projects/index.md`.

## Prohibitions

- `_projects/<project>/plans/` and `_projects/<project>/memory/` are archive copies maintained by the Stop hook. Do NOT reference them as authoritative sources.

## Project rules (`rules.md`)

`_projects/<project>/rules.md` is an optional, per-project file holding normative rules for that project (e.g. "edit `src/`, never `dist/` directly"). It is scoped to the taskflow project (`pj:`), not to a filesystem path — for path/glob-scoped rules use `.claude/rules`, and for global rules use `CLAUDE.md`. The file is human-authored and versioned in the repo.

**Injection (handled by `session_init.py`, no action needed from you):** on project switch the full body is injected as a primer; on later turns a compact manifest of its `##` headings recurs as a recall cue. If the file's frontmatter sets `inject_every_turn: true`, the full body is injected every turn instead. When you see a `[Project Rules reminder: <project>]` block, treat each `##` heading as a trigger: before writing / committing / generating deliverables or any non-trivial action, check whether it touches a listed rule and, if so, re-read `_projects/<project>/rules.md` before acting.

**Precedence.** Project rules are additive domain constraints, mostly orthogonal to the response `mode`/`role`. They do NOT form a flat priority ladder with mode. Resolve conflict only where a rule prescribes an action the active mode also governs:

- The active turn's **mode contract wins** at that action boundary.
- A **live user instruction overrides** a standing project rule ("just this once …").
- **Safety outranks everything.** Do NOT place safety-critical constraints in `rules.md` (they would be weakened by the above); keep those in the harness/safety layer.

**Editing `rules.md` (human-initiated only).** There is no model-autonomous write to this file. When the user asks (in natural language or via a command) to add/change a project rule:

1. Propose the change as a diff (or the exact lines to add) and state the target `_projects/<project>/rules.md`.
2. Apply only after the user confirms. Manual hand-editing by the user is also fine.

The blast radius is every future turn of the project, so the confirm step is required even though the request is human-initiated.

**NL disambiguation.** When a project is assigned, an unqualified request about "the rules" (「ルール」) refers to this project's `rules.md` by default. Only treat it as `CLAUDE.md` or `.claude/rules` when the user names those explicitly.

**Subagent propagation.** `UserPromptSubmit` does NOT fire for Agent-tool subagents, so a spawned subagent never receives the rules injection. When you delegate work whose actions could touch a listed rule, include the relevant rule text (or an instruction to read `_projects/<project>/rules.md`) in the subagent's prompt.

## Directory layout (v0.2.2)

```
_projects/
  index.md               all-projects index
  <project>/
    index.md             project overview
    rules.md             optional per-project rules (injected by session_init.py; see "Project rules")
    progress.md          task index (auto-generated table region + hand-edited free-text sections)
    tasks/               task files, one per task (status by folder)
      0_todo/            not started
      1_in_progress/     started; work ongoing
      2_done/            complete (human-approved)
    project-notes/       reusable project knowledge (category by folder)
      index.md           4-column index: File | Description | Tags | Updated
      specs/             designs, decisions, ADRs, proposals
      investigations/    research, analysis, post-mortems, retrospectives
      checks/            verification items, checklists (no judgment)
      procedures/        step-by-step instructions for humans
      backlog/           candidate items, ideas, issue tracker entries
      _archive/          exhausted (no longer authoritative)
    _archive/            project-level archive (e.g., pre-v0.2.2 legacy files)
    plans/               plan copies from the Stop hook (archived history)
    memory/              memory copies from the Stop hook (archived history)
```
