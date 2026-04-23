# taskflow internal architecture

Internal design document for developers — read this when you need to understand or modify how the plugin works.

## Context-management types

### Four roles

| Type | Role | Lifetime | Audience | Context injection |
|---|---|---|---|---|
| progress.md | Progress state: TODO / In Progress / Completed + Session Log | Project lifetime | Human + AI | On apply, subagent reads the full file and returns it to the main agent |
| handoff/ | Concrete context attached to a task | Task lifetime (3-state transition) | AI | Subagent consumes `0_pending/` and selectively reads `1_in_progress/`, returning both to the main agent |
| project-notes/ | Project-specific persistent knowledge | Project lifetime | AI | Subagent uses `index.md` to select, reads only the relevant files, and returns them |
| plans/ memory/ | Auto-archived copies of `~/.claude/` | Archive | Human | Never injected. Not to be referenced. |

### Role boundaries

- progress TODO `Prompt` column: a concise instruction that a human can lift and paste into the LLM.
- handoff: detailed context paired with the `Prompt` column. `progress.md` acts as the handoff index.
- progress Session Log: history only — not a communication channel.
- project-notes: a knowledge base referenced selectively by the AI. Project-specific technical content that is too large for progress.md.
- auto-memory: a human-facing artifact. The LLM does not reference it directly.

## handoff 3-state lifecycle

### Folder model

```
handoff/
  0_pending/        not started (created but not yet consumed)
  1_in_progress/    in progress (consumed by AI; work is ongoing)
  2_done/           done (human-approved)
```

### Transition rules

| Transition | Trigger | Actor | Counterpart in progress.md |
|---|---|---|---|
| 0_pending → 1_in_progress | Subagent consumes at session start | AI (automatic) | TODO → In Progress |
| 1_in_progress → 2_done | Human approves completion | Human instruction → LLM executes | In Progress → Completed |
| 1_in_progress → 0_pending | Human sends the task back | Human instruction → LLM executes | In Progress → TODO |

### Sync mechanism with progress.md

`progress.md` and `handoff` are two sides of the same task (state management vs. context). When the LLM changes a status in `progress.md`, it uses the `@handoff/<filename>` reference in the `Prompt` column to move the corresponding handoff file in lockstep.

The `Prompt` column omits the folder and keeps only the filename (e.g. `@handoff/build-fix.md`) so that a folder move does not break the reference.

### Approval gate

Transitioning into `2_done` requires explicit human approval. At session start, the subagent checks `1_in_progress/`; if files are present, it emits a reminder in the `pending_approval` section.

`[HOLD]` marker: attaching it to an In Progress entry in `progress.md` suppresses the reminder.

### Drift detection

The subagent checks consistency between `progress.md` state and the handoff folders:
- An In Progress entry whose `@handoff/<filename>` is in `0_pending/` → warning.
- A TODO entry whose `@handoff/<filename>` is in `1_in_progress/` → warning.
- Findings are recorded in the `drift_warnings` section of the output.

## project-notes selective read

### Index-file approach

Each project has a `project-notes/index.md` — a three-column table: File / Description / Tags.

The subagent reads `index.md` once to get an overview, then selects files that match `prompt_summary` and reads only those.

### Rationale

- The subagent (haiku) benefits most from the "one read → select → read selected files" flow.
- Avoids per-turn hook overhead.
- Remains human-readable.

### Fallback

If `index.md` is missing, fall back to a Glob of `project-notes/` + filename matching.

### Drift detection

When the Glob and `index.md` disagree (unregistered file, entry without a real file), record it in `drift_warnings`.

## Session lifecycle

### Per-turn flow

```
user prompt
  │
  ▼ [UserPromptSubmit hook] session_init.py
  │  ├─ first turn: create state_file
  │  │   1. parse `pj:xx` from the first line of the prompt
  │  │   2. if absent, infer from the `_projects/<project>/` path
  │  │   3. write {"project": "..."} into state_file
  │  │
  │  ├─ subsequent turns:
  │  │   1. if `pj:xx` is present, update state_file
  │  │   2. otherwise read the current value from state_file
  │  │
  │  └─ every turn: inject additionalContext
  │     "[Progress Session] session_id=... state_file=... current_project=..."
  │     + when `pj:` is specified or the project is already set, additionally inject the body of project_routing.md
  │       (lib/prompts/ paths are dynamically rewritten to the plugin's absolute path)
  │
  ▼ [LLM] detects [Progress Session]
  │  1. read project_router_agent.md
  │  2. prepend a JSON context block
  │  3. invoke the project-router subagent via the Agent tool
  │
  ▼ [project-router subagent] runs on an isolated generation path (haiku)
  │  1. write state_file (always)
  │  2. applicability decision (skip / apply)
  │  3. on apply: read index.md, progress.md, and the 3 guideline files
  │  4. handoff: consume 0_pending/ → 1_in_progress/; selectively read 1_in_progress/ and include reminders
  │  5. project-notes: selective read via index.md (fallback: Glob + filename)
  │  6. drift detection
  │  7. return a structured result
  │
  ▼ [LLM] receives the subagent result
  │  - apply → use as context for executing the task
  │  - skip  → skip project management
  │
  ▼ task execution
```

### Session end

```
session end
  │
  ▼ [Stop hook] session_sync.py
     1. read `project` from state_file
     2. empty project or directory missing → skip
     3. copy files modified in the last 10 minutes:
        ~/.claude/plans/*.md      → _projects/<project>/plans/
        ~/.claude/projects/       → _projects/<project>/memory/
          {encoded_cwd}/memory/*.md
```

## state_file

Path: `_projects/_state/{session_id}.json`

```json
{"project": "pi-studio-dev"}
```

### Writers

| Actor | Timing | Condition |
|---|---|---|
| hook (init) | every turn | `pj:` is specified, or path inference succeeded |
| subagent | when the project is finalized | the routing procedure has decided |

### Readers

| Actor | Timing | Purpose |
|---|---|---|
| hook (init) | 2nd turn onward | fetch the `current_project` value for injection |
| hook (sync) | session end | determine the copy destination project |

## `pj:` syntax

Place `pj:<project_name>` on the first line of the prompt.

| Input | Effect |
|---|---|
| `pj:pi-studio-dev` | Set the project |
| `pj:none` | Declare no matching project |
| omitted | Keep the existing value; the LLM infers from context |

### Rationale

The YAML `key: value` form is a shape the model already recognizes as a metadata declaration. `pj=xx` (clashes with shell variables), `#pj=xx` (clashes with H1), `@pj=xx` (clashes with `@mention`), and `project=xx` (too long) were all considered and rejected in favor of `pj:xx`.

## subagent delegation — design decision

### Problem

When routing and task execution share a single generation path, they compete for attention. On technically dense tasks, routing was repeatedly skipped.

### Resolution

Delegate routing to a dedicated subagent (haiku, isolated generation path). The main agent's system prompt keeps only a short instruction to "invoke the subagent." Attention is now separated.

## Path resolution

### Paths used by hooks

| Path | Resolution |
|---|---|
| _projects/ | `os.getcwd() + '/_projects'` (CWD-based) |
| prompts/ | derived from `__file__` back to the plugin root |
| ~/.claude/plans/ | `os.path.expanduser` |
| ~/.claude/projects/.../memory/ | encode CWD (`lower().replace(':', '-').replace('/', '-')`) |

### When _projects/ is absent

If `_projects/` is not in CWD, the hook immediately `sys.exit(0)`s as a harmless no-op. The plugin can be enabled without affecting projects that have not been initialized.

## Directory layout

```
_projects/
  index.md               all-projects index
  _state/                session state (hook-managed)
    {session_id}.json
  <project>/
    index.md             project overview
    progress.md          task progress tracking
    project-notes/
      index.md           index for project-notes
      *.md               individual project-notes
    handoff/
      0_pending/         not started
      1_in_progress/     in progress
      2_done/            done (human-approved)
    plans/               plan copies (archived)
    memory/              memory copies (archived)
```
