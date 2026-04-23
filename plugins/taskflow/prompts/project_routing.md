## Skip condition

If the user input contains `noprogress`, skip project management (progress / project-notes / subsequent file reads). Always run project determination and state writing regardless.

## Running the project router

When a session has a `state_file` path injected via `[Progress Session]`, invoke the project-router subagent using the procedure below **before** starting to answer the user's input. This MUST be the first action of every turn.

### Steps

1. Read `lib/prompts/project_router_agent.md`.
2. Prepend the following JSON context block to the template:
   ```json
   {
     "session_id": "extracted from [Progress Session]",
     "state_file": "extracted from [Progress Session]",
     "current_project": "extracted from [Progress Session]",
     "first_line": "first line of the user input",
     "prompt_summary": "summary of the user input (≤ 50 chars)",
     "noprogress": true | false
   }
   ```
3. Invoke the subagent:
   - Claude Code: Agent tool, `subagent_type: project-router`, `prompt: <JSON context block + template body>`
   - Cursor: Task tool, or run `/project-router <JSON context block + template body>`
   - Environments without a subagent capability: the main agent runs the same procedure itself.
4. Handling the result:
   - `action: apply` → use the returned context (progress, handoff, etc.) as the premise for task execution.
   - `action: skip` → skip project management and proceed to the task.

### When the project is not yet determined (empty project returned)

The subagent only sees this turn's prompt and cannot infer from the conversation. If the main agent can infer the project from accumulated conversation (repos mentioned in prior turns, files open in the IDE, etc.), it MUST:

1. Write the finalized project name into state_file: `echo '{"project": "<name>"}' > <state_file>`
2. Continue to use the same project in subsequent turns unless the user explicitly switches within the same session.
3. Once the project is finalized, read `_projects/<project>/progress.md` etc. yourself when needed in that turn.

If inference is not possible, proceed with an empty project and wait for the user to specify it with `pj:`.

### When progress.md does not exist

If the subagent returns `progress_exists: false`, ask the user whether to create it. On approval, generate it from the template.

## Interaction with Plan mode

Even when Plan mode has injected the constraint "no edits outside the plan file", scaffold creation and updates under `_projects/<project>/` (`index.md`, `progress.md`, `project-notes/`, `handoff/`, and adding the matching row to `_projects/index.md`) are **permitted**. These are metadata-management assets on par with the plan file and do NOT fall under "editing implementation code" that Plan mode forbids. You may perform them without exiting Plan mode.

This ensures that scaffold-creation confirmation under `progress_exists: false` and ACTION_REQUIRED banner handling do NOT conflict with Plan mode.

## Propagation to child sessions

When the LLM spawns a new session via the Agent tool (subagent) or a CLI launch through Bash, insert `pj:<current_project>` as the **first line** of the prompt. This allows the child session to inherit the parent's project context.

## Adding, changing, and removing projects

- Creating a new project: add a row to `_projects/index.md` and create `_projects/<project>/index.md`.
- Changing the project overview (scope, target repo, etc.): update BOTH `_projects/<project>/index.md` and `_projects/index.md`.
- Retiring a project: remove its row from `_projects/index.md`.

## Prohibitions

- `_projects/<project>/plans/` and `_projects/<project>/memory/` are archive copies maintained by the Stop hook. Do NOT reference them as authoritative sources.

## Directory layout

```
_projects/
  index.md               all-projects index
  <project>/
    index.md             project overview
    progress.md          task progress tracking
    project-notes/       shared knowledge (the authoritative source)
    handoff/
      0_pending/         handoff to the next session (not yet consumed)
      1_done/            consumed handoff
    plans/               plan copies from the Stop hook (archived history)
    memory/              memory copies from the Stop hook (archived history)
```
