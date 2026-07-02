---
name: wiki-init
description: Initialize a new LLM wiki at a chosen scope (active project, workspace, or an explicit path). Copies the contract templates into a plain directory (git-independent; the engine invokes no git). Use when the user wants to create/initialize a new wiki.
disable-model-invocation: true
allowed-tools: AskUserQuestion, Bash(uv run *), Bash(mkdir *)
---

# /wiki-init

Create a brand-new llm-wiki. Scope selection is context-driven and interactive
(Q3); the actual generation — the template copy into a plain directory (no git;
the engine invokes none) — is done by the `llmwiki init` verb (W-c: generation
lives only there; do NOT reimplement it here). This skill RESOLVES a target root,
then calls the verb.

## Step 0 — Honor an explicit `--root` (Q4)

If the invocation carries `--root <path>` in `$ARGUMENTS`, SKIP all selection
and use that path verbatim as the target root, with scope label `prompt`. Go
straight to Step 2.

`--root <path>` is also the way to target a project OTHER than the active one
(other projects are NOT added to the selection menu — Q4).

## Step 1 — Select the scope (only when no `--root`)

First read the context for two facts:

- **taskflow active & a project is assigned?** A "wiki-active" / project context
  may be injected, or the most recent `_projects/_state/*.json` carries a
  `project` field (mtime-descending; same lookup as `/progress`). If that field
  resolves to a project, taskflow is active with a pj assigned.
- Resolve the **active pj wiki path** as `<proot>/<project>/wiki/`, where
  `<proot>` is the first existing root from `$TASKFLOW_PROJECT_ROOTS` (`;`-split;
  if unset, `_projects/` in the workspace). Resolve the **workspace wiki path**
  as `<workspace-root>/_llm-wiki/`, where `<workspace-root>` is the parent of
  that project-roots container. (These are the same conventions
  `wiki_root_resolver` uses; you MAY call it to compute the candidate paths, but
  resolver never generates — generation is this skill's call to `wiki_init.py`.)

Then branch with **AskUserQuestion**:

- **taskflow active AND a pj is assigned** — options:
  1. **active pj** — `<proot>/<project>/wiki/` (scope label `pj`)
  2. **workspace** — `<workspace-root>/_llm-wiki/` (scope label `workspace`)
  3. **enter a path** — ask for the path (scope label `prompt`)
- **taskflow inactive OR no pj assigned** — options:
  1. **pick a project** — scan the project-roots (`$TASKFLOW_PROJECT_ROOTS`, else
     `_projects/`) for the projects present and present them; the chosen
     project's wiki is `<proot>/<project>/wiki/` (scope label `pj`)
  2. **workspace** — `<workspace-root>/_llm-wiki/` (scope label `workspace`)
  3. **enter a path** — ask for the path (scope label `prompt`)

Do NOT add non-active projects to the active-pj branch's menu — those go through
`--root <path>` (Q4).

The selection yields a resolved target `<root>` and a `<scope>` label.

## Step 2 — Initialize (the verb does all generation)

Call `llmwiki init` with the resolved root and scope label. The verb refuses
to overwrite an existing wiki (`.llmwiki` present → error) and copies the contract
templates into the target root. It invokes no git — the wiki is a plain directory;
the shipped `<wiki-root>/.gitignore` keeps a surrounding parent repo clean if you
choose to version the wiki yourself.

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki init "<root>" --scope "<scope>"
```

If it exits non-zero (e.g. a wiki already exists at `<root>`), surface its error
verbatim and stop — do NOT attempt to overwrite or repair anything yourself.

## Step 3 — Report

Relay the script's report (created wiki-root, scope) as a short summary, then
point at the next step:

```
wiki initialized at <root> (scope: <scope>)

Next: /wiki-view to browse it, or /wiki-ingest to add content.
```

Do not add further commentary.
