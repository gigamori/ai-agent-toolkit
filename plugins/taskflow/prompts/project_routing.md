## Response frontmatter

When a project is assigned, include `[pj:<project>]` as a response frontmatter line (before the main body).
When no project is assigned, omit the `[pj:...]` frontmatter entirely.
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
     "first_line": "first line of the user input",
     "prompt_summary": "summary of the user input (≤ 50 chars)"
   }
   ```
2. Invoke the subagent via the Agent tool: `subagent_type: project-router`, `prompt: <JSON context block>`. If the runtime lacks a subagent mechanism, the main agent runs the same procedure itself by reading the spec in the body of `agents/project-router.md`.
4. Handling the result:
   - `action: apply` → use the returned context (progress, tasks, project-notes, etc.) as the premise for task execution.
   - `action: skip` → skip project management and proceed to the task.
   - Treat the routed context as a routing aid, not a primary source. `--- index ---`, `--- progress ---`, and task contents are verbatim file dumps; `--- project_notes_list ---` gives only paths + the index's Description — the router does NOT return note bodies. Before you assert or act on any claim of the form "<file/section> says X" that originates from the routed context (or any subagent digest of a primary artifact), open the cited primary file and confirm it. This generalizes the history_lookup rule (verify against the source, never trust a secondary digest) beyond chat history to all subagent-returned summaries.
   - When `--- project_notes_list ---` marks notes `[relevant]` to the task, proactively open those primary files before acting, rather than working without them.

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

When the LLM spawns a new session via the Agent tool (subagent) or a CLI launch through Bash, insert `pj:<current_project>` as the **first line** of the prompt. This allows the child session to inherit the parent's project context.

## Adding, changing, and removing projects

- Creating a new project: add a row to `_projects/index.md` and create `_projects/<project>/index.md`.
- Changing the project overview (scope, target repo, etc.): update BOTH `_projects/<project>/index.md` and `_projects/index.md`.
- Retiring a project: remove its row from `_projects/index.md`.

## Prohibitions

- `_projects/<project>/plans/` and `_projects/<project>/memory/` are archive copies maintained by the Stop hook. Do NOT reference them as authoritative sources.

## Directory layout (v2)

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
    _archive/            project-level archive (e.g., pre-v2 legacy files)
    plans/               plan copies from the Stop hook (archived history)
    memory/              memory copies from the Stop hook (archived history)
```
