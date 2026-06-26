# llm-wiki

A Claude Code plugin that maintains an **LLM-curated wiki**: it ingests sources into Markdown pages, answers questions grounded in those pages, and lints the wiki graph. The plugin is the **immutable engine** (D1) — it ships the ingest / query / lint procedures plus the wiki-contract **schema templates** a per-wiki repo is initialized from. It never rewrites a wiki's contract. Each wiki is a separate, per-wiki repo holding its own schema / index / log / raw / pages, which co-evolve.

[日本語版 README はこちら](README_ja.md)

## What it solves

Notes and decisions scatter across chats, command outputs, and session logs. llm-wiki normalizes any of those into immutable `raw/` artifacts, then has the LLM author and update wiki pages from them under hard code-enforced safety boundaries: untrusted source reading is separated from page writing, every write passes an allowlist gate, and the whole ingest runs as a single git transaction so a failure rolls the wiki back to its pre-ingest state.

## Installation (Claude Code)

### Via the plugin marketplace (recommended)

```
/plugin marketplace add gigamori/ai-agent-toolkit
/plugin install llm-wiki@ai-agent-toolkit
```

### Local (development / testing)

```bash
claude --plugin-dir ./plugins/llm-wiki
```

The plugin's deterministic scripts and hook run via `uv run python`. No separate `init` step is required to load the plugin; the `UserPromptSubmit` hook activates as soon as the plugin is enabled.

## Initializing a wiki

Initialize a wiki with **`/wiki-init`**. It creates the wiki as a **nested independent git repo** (its own `git init` + initial commit) at a scope you select interactively:

- **taskflow active and a project is assigned** — choose the **active pj** (`_projects/<project>/wiki/`), the **workspace** (`<workspace-root>/_llm-wiki/`), or **enter a path**.
- **taskflow inactive or no project assigned** — **pick a project** (scanned from `$TASKFLOW_PROJECT_ROOTS`, else `_projects/`), the **workspace**, or **enter a path**.
- To target a project other than the active one, pass `--root <path>` (such projects are not added to the menu).

```
/wiki-init                       # interactive scope selection
/wiki-init --root ./path/to/wiki # explicit target root, skip selection
```

The wiki is its own repo (so the ingest rollback's `git reset --hard` can never reach a parent repo). `/wiki-init` also detects the surrounding parent repo and registers the wiki-root's relative path in the **parent repo's `.git/info/exclude`** — repo-local and not committed, so the parent never tracks the wiki. Note: deleting a wiki later leaves its line in `.git/info/exclude`; trim it manually.

The wiki is initialized from the plugin's `templates/`:

- `.llmwiki` — the wiki **marker** (D8): `{ version, schema: SCHEMA.md }`. Its presence marks the directory as a wiki root; detection-only, holds no config.
- `SCHEMA.md` — the wiki **contract**: regulatory prose plus YAML frontmatter carrying `config` and `doc_type_profiles` (all 8 doc types seeded, plus a mandatory `default`).
- `index.md` — content-oriented catalog seed.
- `log.md` — append-only log seed with the grep-parseable `## [YYYY-MM-DD] <op>|<provenance-or-origin> | <Title>` prefix convention.
- `raw/` — immutable, redacted source artifacts (content-hash id; the LLM only reads them).
- `wiki/` — LLM-authored pages. `wiki/` is source tier; `wiki/derived/` is un-promoted synthesis.

## Resolving the active wiki

Operations resolve the active wiki by **existence**, top-down: **prompt `--root` > pj (`_projects/<project>/wiki/`) > workspace (`_llm-wiki/`) > CWD `.llmwiki`**. The pj scope is read one-way from taskflow (the most recent `_projects/_state/*.json` `project` field, resolved against `$TASKFLOW_PROJECT_ROOTS`); if there is no state file it is skipped cleanly. Because resolution no longer depends on the CWD alone, the operations work in the **VSCode extension without `cd`**. The marker hook shows `active wiki: <root> (scope: pj|workspace|cwd)` each turn so the resolved wiki is always visible.

## Usage

A typical session:

1. **Enter the wiki.** Resolution is automatic (see *Resolving the active wiki* above) — `cd` into a wiki root, or rely on the pj/workspace scope, or pass `--root <path>`. The scope hook injects `wiki-active` and the `active wiki:` line for that turn; when nothing resolves the plugin stays invisible.

2. **Ingest a source.**

   ```
   /wiki-ingest ./docs/rfc-routing.md            # a 3rd-party document (FE-B → source tier)
   /wiki-ingest ./docs/rfc.md external=https://example.com/rfc   # attach a permalink for citation
   /wiki-ingest ./logs/session.jsonl             # a Claude Code session log (FE-B' → derived tier, pinned doc_type=transcript)
   ```

   The argument can also be a **quoted glob** or a **directory** — the driver expands it in Python (not the shell) and ingests **one file per transaction**:

   ```
   /wiki-ingest "./docs/**/*.md"                 # a quoted glob: expanded in Python, wiki-internal paths excluded, one file per transaction
   /wiki-ingest ./docs/                          # a directory: expanded as ./docs/**/* restricted to the text-type allowlist
   ```

   For a directory, only the text-type allowlist is picked up (`.md` / `.markdown` / `.txt` / `.text` / `.json` / `.jsonl`); non-text files (e.g. images) are skipped. On a batch a per-file failure rolls back **just that file** and the run continues, then a summary `N total / M succeeded / K failed / S dedup-skipped` is reported; zero matches is an error.

   Before anything is written you see the one-line resolved-value declaration (`[wiki] write_mode = explicit (default)` …). With the default `write_mode=explicit` you confirm before the Stage 2 pages are applied; the whole ingest is one git transaction (per file), so a failure or a decline rolls the wiki back to its pre-ingest state.

3. **Ask questions.** Just ask in natural language — e.g. *"what did we decide about retry backoff?"* The `wiki-query` skill auto-activates, reads both `wiki/` and `wiki/derived/`, and cites each claim by page path (the path tells you whether it's source or derived tier). This is read-only.

4. **File an answer (optional, explicit).** Querying never writes on its own. To save an answer, ask explicitly — e.g. *"file that as a page"* — and it lands under `wiki/derived/` as derived synthesis.

   For a **deterministic** filing trigger that does not depend on the LLM judging your intent, include the marker `llm-wiki:file` anywhere in an otherwise normal question — a hook detects it and makes filing mandatory (no confirmation: the marker is explicit by definition), and the answer is filed under `wiki/derived/`:

   ```
   what did we decide about retry backoff? llm-wiki:file              # force filing; page name generated from the answer
   what did we decide about retry backoff? llm-wiki:file=retry-policy # fix the page name → wiki/derived/retry-policy.md
   ```

   `llm-wiki:file=<page-slug>` fixes the page name to `wiki/derived/<page-slug>.md`; without a slug the LLM generates the page name from the answer. The marker is effective only inside a wiki (when `.llmwiki` is present). The safety envelope (redaction → write-tool location gate → single transaction) is unchanged — only the confirmation prompt is skipped.

5. **Lint.** `/wiki-lint` runs the graph/index checks plus the transcript decision-floor and returns a prioritized "next questions" list. Read-only.

6. **Browse the wiki.** `/wiki-view` starts a local HTML viewer at `http://127.0.0.1:17330/` that renders the wiki's `wiki/` + `wiki/derived/` pages and lets you click through `[[wikilinks]]`. Read-only; stop it with `pkill -f "generate_wiki_view.py --serve"`.

7. **Promote.** When a `wiki/derived/` page has earned source tier: `/wiki-promote wiki/derived/retry-policy.md`. After an explicit approval and a contamination check it moves to `wiki/retry-policy.md` and rewrites inbound links. This is the only derived→source path.

**Recovery.** If an ingest is interrupted (the process is killed mid-run), a stale `.llmwiki.lock` can remain. Clear it manually — this rolls back to the pre-ingest checkpoint and releases the lock:

```bash
uv run python "${CLAUDE_PLUGIN_ROOT}/scripts/ingest_driver.py" abort <wiki-root>
```

## Operations

Run all operations from the wiki root (the directory holding `.llmwiki`).

### Scope detection

`activation_scope: scoped` is implemented as a `UserPromptSubmit` hook (`hooks/wiki_marker_inject.py`): each turn it checks the CWD for a `.llmwiki` marker and, when present, injects a `wiki-active` context. When no marker is present it exits silently and injects nothing — outside a wiki, the plugin is invisible. The injected context plus the `wiki-query` skill description is what auto-activates query; the write-bearing operations are explicit commands and do not depend on the hook.

### Ingest — `/wiki-ingest <path-or-source-or-glob> [doc_type=...] [external=...]`

Ingests a 3rd-party source (FE-B) or a Claude Code session jsonl (FE-B') through the 2-stage `extract → apply` core inside one git transaction. The argument may be a single file, a **quoted glob** (`"./docs/**/*.md"`), or a **directory** (`./docs/`): the driver expands it in Python (never the shell), force-excludes wiki-internal paths, and — for a directory — restricts to the text-type allowlist (`.md` / `.markdown` / `.txt` / `.text` / `.json` / `.jsonl`). A glob/directory is ingested **one file per transaction**; a per-file failure rolls back only that file and the run continues, then a `N total / M succeeded / K failed / S dedup-skipped` summary is reported (zero matches is an error).

- **Stage 1 (extract)** — the `wiki-ingest-extract` subagent reads the redacted, untrusted raw source with **no write tool by construction** and emits proposed edits only.
- **Stage 2 (apply)** — the `wiki-ingest-apply` subagent authors page updates and stages every write **only** through the allowlist write tool (`scripts/write_tool.py`), which confines writes to `wiki/` and `wiki/derived/`, rejects `SCHEMA.md` / `.llmwiki` / `raw/` / absolute paths / traversal, and gates on budget. On more than `apply_fanout_k` touched pages, Stage 2 fans out one apply worker per cluster; index / log / commit are centralized after the join.

cc-log (FE-B') input is pinned to `doc_type=transcript` with a deterministic decision floor (`scripts/transcript_floor.py`): a claim is recorded as a decision only with an explicit affirmative token; silence is non-affirmation.

### Query — the `wiki-query` skill

Description-driven auto-activation when a wiki is active. It reads **both** `wiki/` and `wiki/derived/` and cites every claim by page path, where the path encodes the trust tier (`wiki/` = source, `wiki/derived/` = derived). Read-only by default; it files an answer back into the wiki only on an explicit filing trigger — either a natural-language ask (LLM-judged) or the deterministic, hook-detected marker `llm-wiki:file[=<page-slug>]` included anywhere in the question. The marker forces filing without confirmation; with a slug the page name is fixed to `wiki/derived/<page-slug>.md`, without one the LLM generates it. Only the confirmation is skipped — the redaction → write-tool gate → single-transaction envelope is unchanged.

### Lint — `/wiki-lint`

Read-only. Runs the deterministic link / index graph checks (`scripts/link_lint.py`, `scripts/wiki_index.py`) plus the transcript-only type-specific lint (v1), and reports a prioritized "next questions" list. Never writes.

### Promote — `/wiki-promote <wiki/derived/X.md>`

Promotes a derived synthesis page to source tier (`wiki/derived/X.md → wiki/X.md`) as a code-driven move plus inbound link-rewrite (`scripts/promote.py`), gated on explicit human approval and a contamination check. The only path from derived to source tier.

### View — `/wiki-view`

Starts a local HTML viewer for the active wiki — a `127.0.0.1`-bound HTTP server on port `17330` (`scripts/generate_wiki_view.py --serve`, never externally reachable) that renders the wiki's Markdown pages to HTML on demand and turns `[[wikilinks]]` into navigable links between page views. Read-only — it does not write to the wiki. The wiki-root is resolved by the multi-scope resolver (`--root` > pj > workspace > CWD), so it no longer needs the CWD to be the wiki root; pass `--root <path>` to target one explicitly.

- Serves `wiki/` + `wiki/derived/` only; `raw/` is **not** exposed.
- Each page shows a tier badge (**source** / **derived**), and pages are **tier-distinct**: a same-name `wiki/X.md` and `wiki/derived/X.md` are separate pages. A `[[X]]` whose basename resolves to both renders **both** as tier-labelled links (`X (source)` / `X (derived)`).
- A `[[link]]` with no target page is shown as a distinct, non-navigable "missing" link.
- On start it prints the URL + page count (`[wiki-view] serving <N> pages at http://127.0.0.1:17330/ ...`). Stop it with `pkill -f "generate_wiki_view.py --serve"`.

## Configuration & defaults (D3–D5)

Config lives in `SCHEMA.md` frontmatter (wiki-local) and is read by the plugin's own scripts, not by the Claude Code settings mechanism. Each axis resolves independently with the precedence **prompt-explicit > wiki-local config > built-in default** (D4); before any write the resolved value and its source are declared in one line (D5). Built-in defaults:

| Key | Default | Meaning |
|---|---|---|
| `activation_scope` | `scoped` | Activate only inside a `.llmwiki` wiki root (D3) |
| `read_grounding` | `implicit` | Query grounds in the wiki without being asked (D3) |
| `write_mode` | `explicit` | Confirm before applying writes (D3); `implicit` skips confirmation with a loud session-start notice |
| `write_autocommit` | `auto` | Forced `true` when `write_mode=implicit` (floor, D5) |
| `override_scope` | `operation` | A prompt override applies to one operation; `session` makes it sticky |
| `apply_fanout_k` | `10` | ≤K touched pages inline; >K fans out per-cluster (D23) |

The ingest git checkpoint is taken on every run regardless of `write_mode` (D14): `write_mode` controls only whether a confirmation precedes applying writes, not whether the wiki is committed.

## File layout

```
plugins/llm-wiki/
  .claude-plugin/plugin.json   # manifest: name / description / version / author.name
  hooks/
    hooks.json                 # UserPromptSubmit -> wiki_marker_inject.py
    wiki_marker_inject.py      # resolve active wiki -> inject "wiki-active" + "active wiki:" line; silent when dormant
  commands/
    wiki-ingest.md             # /wiki-ingest  (ingest orchestrator)
    wiki-lint.md               # /wiki-lint    (read-only lint dispatch)
    wiki-promote.md            # /wiki-promote (derived -> source)
  agents/
    wiki-ingest-extract.md     # Stage1 extract (tools: Read; no write tool)
    wiki-ingest-apply.md       # Stage2 apply (writes only via allowlist tool)
    wiki-lint.md               # read-centric lint subagent
  skills/
    wiki-init/SKILL.md         # /wiki-init (interactive scope select -> wiki_init.py)
    wiki-query/SKILL.md        # query skill (description-driven auto-trigger)
    wiki-view/SKILL.md         # /wiki-view (start the local HTML page viewer)
  scripts/                     # deterministic engine (uv-runnable)
    config_resolver.py marker.py redaction.py content_hash.py frontends.py
    extract_cc_log.py wiki_log.py wiki_index.py link_lint.py write_tool.py
    transaction.py promote.py transcript_floor.py generate_wiki_view.py
    wiki_root_resolver.py wiki_init.py
  templates/                   # what a new wiki instance is initialized from
    .llmwiki SCHEMA.md index.md log.md raw/ wiki/
```

## License

[MIT](../../LICENSE)
