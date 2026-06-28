## Response leading lines

When a project is assigned, include `[pj:<project>]` in the leading lines of the response (near the beginning, before the main body; it may follow other leading lines such as `mode:`, not necessarily the literal first line).
When no project is assigned, omit `[pj:...]` entirely.

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
2. Invoke the subagent via the Agent tool: `subagent_type: project-router`, `prompt: <JSON context block>`. If the runtime lacks a subagent mechanism, the main agent runs the same procedure itself by reading the spec in the body of `agents/project-router.md`.
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

Even when Plan mode has injected the constraint "no edits outside the plan file", scaffold creation and updates under `_projects/<project>/` (`index.md`, `progress.md`, `project-notes/`, `tasks/`, and adding the matching row to `_projects/index.md`) are **permitted**. These are metadata-management assets on par with the plan file and do NOT fall under "editing implementation code" that Plan mode forbids. You may perform them without exiting Plan mode.

This ensures that scaffold-creation confirmation under `progress_exists: false` and ACTION_REQUIRED banner handling do NOT conflict with Plan mode.

## Propagation to child sessions

When the LLM spawns a new session via the Agent tool (subagent) or a CLI launch through Bash, insert `pj:<current_project>` into the leading lines of the prompt (near the beginning; it may follow other leading lines such as `mode:`, not necessarily the literal first line). This allows the child session to inherit the parent's project context.

## Adding, changing, and removing projects

- Creating a new project: add a row to `_projects/index.md` and create `_projects/<project>/index.md`.
- Changing the project overview (scope, target repo, etc.): update BOTH `_projects/<project>/index.md` and `_projects/index.md`.
- Retiring a project: remove its row from `_projects/index.md`.

## Prohibitions

- `_projects/<project>/plans/` and `_projects/<project>/memory/` are archive copies maintained by the Stop hook. Do NOT reference them as authoritative sources.

## Directory layout (v0.2.2)

```
_projects/
  index.md               all-projects index
  <project>/
    index.md             project overview
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
