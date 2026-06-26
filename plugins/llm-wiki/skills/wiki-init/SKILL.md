---
name: wiki-init
description: Initialize a new LLM wiki at a chosen scope (active project, workspace, or an explicit path). Copies the contract templates, makes the wiki its own nested git repo, and force-ignores it in the parent repo. Use when the user wants to create/initialize a new wiki.
disable-model-invocation: true
allowed-tools: AskUserQuestion, Bash(uv run python *), Bash(mkdir *)
---

# /wiki-init

Create a brand-new llm-wiki. Scope selection is context-driven and interactive
(Q3); the actual generation — template copy, nested `git init`, and parent-repo
force-ignore — is done by `wiki_init.py` (W-c: generation lives only there; do
NOT reimplement it here). This skill RESOLVES a target root, then calls the
script.

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

## Step 2 — Initialize (the script does all generation)

Call `wiki_init.py` with the resolved root and scope label. The script refuses
to overwrite an existing wiki (`.llmwiki` present → error), copies the contract
templates, makes the root its own nested git repo with an initial commit, and
registers the wiki-root's relative path in the parent repo's `.git/info/exclude`
(idempotent).

```bash
uv run python ${CLAUDE_PLUGIN_ROOT}/scripts/wiki_init.py "<root>" --scope "<scope>"
```

If it exits non-zero (e.g. a wiki already exists at `<root>`), surface its error
verbatim and stop — do NOT attempt to overwrite or repair anything yourself.

## Step 3 — Report

Relay the script's report (created wiki-root, scope, whether a parent repo was
found, and the force-ignore entry written) as a short summary, then point at the
next step:

```
wiki initialized at <root> (scope: <scope>)
force-ignored in parent repo: <entry>   # omit this line if no parent repo

Next: /wiki-view to browse it, or /wiki-ingest to add content.
```

Note: deleting a wiki later leaves its `/<rel>/` line in the parent repo's
`.git/info/exclude` (registration only ever appends, there is no
de-registration); if you remove a wiki, trim that stale line manually.

Do not add further commentary.
