# llm-wiki

A Claude Code plugin that maintains an **LLM-curated wiki**: it ingests sources into Markdown pages, answers questions grounded in those pages, and lints the wiki graph. The plugin is the **immutable engine** (D1) — it ships the ingest / query / lint procedures plus the wiki-contract **schema templates** a per-wiki repo is initialized from. It never rewrites a wiki's contract. Each wiki is a separate, per-wiki repo holding its own schema / index / log / raw / pages, which co-evolve.

[日本語版 README はこちら](README_ja.md)

> **New to llm-wiki?** Start with the **[User Guide](USER_GUIDE.md)** — a task-oriented walkthrough with diagrams. This README is the full reference (commands, settings, safety model, design rationale).

## What it solves

Notes and decisions scatter across chats, command outputs, and session logs. llm-wiki normalizes any of those into immutable `raw/` artifacts, then has the LLM author and update wiki pages from them under hard code-enforced safety boundaries: untrusted source reading is separated from page writing, every write passes an allowlist gate, and the whole ingest runs as a single file-journal transaction so a failure rolls the wiki back to its pre-ingest state.

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

The plugin's deterministic engine is a path-imported Python package (`llmwiki/`, no install) driven through three CLI entrypoints under `bin/`, launched with `uv run` (each declares its own deps via PEP 723):

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki <verb> ...        # dep-free: resolve-root scan-pages search file declare promote-check promote lint init marker-detect ingest-apply floor-check reindex
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest ... # duckdb:   ingest {begin|plan-fanout|finish|apply-finish|abort|enumerate|session-plan|project-batch|project-batch-cleanup}
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-view view --serve # markdown: local HTML viewer
```

The commands / skills / agents / hook are thin shims that invoke these entrypoints; the dep-free read path (`bin/llmwiki`) never pulls in `duckdb` or `markdown`. No separate `init` step is required to load the plugin; the `UserPromptSubmit` hook activates as soon as the plugin is enabled.

## Initializing a wiki

Initialize a wiki with **`/wiki-init`**. It creates the wiki as a **plain directory** (git-independent; the engine invokes no git) at a scope you select interactively:

- **taskflow active and a project is assigned** — choose the **active pj** (`_projects/<project>/wiki/`), the **workspace** (`<workspace-root>/_llm-wiki/`), or **enter a path**.
- **taskflow inactive or no project assigned** — **pick a project** (scanned from `$TASKFLOW_PROJECT_ROOTS`, else `_projects/`), the **workspace**, or **enter a path**.
- To target a project other than the active one, pass `--root <path>` (such projects are not added to the menu).

```
/wiki-init                       # interactive scope selection
/wiki-init --root ./path/to/wiki # explicit target root, skip selection
```

The wiki is a plain directory; the engine never invokes git. The shipped `<wiki-root>/.gitignore` keeps a surrounding parent repo from tracking the wiki's churn if you choose to version it yourself.

The wiki is initialized from the plugin's `templates/`:

- `.llmwiki` — the wiki **marker** (D8): `{ version, schema: SCHEMA.md }`. Its presence marks the directory as a wiki root; detection-only, holds no config.
- `SCHEMA.md` — the wiki **contract**: regulatory prose plus YAML frontmatter carrying `config` and `doc_type_profiles` (all 8 doc types seeded, plus a mandatory `default`).
- `index.md` — content-oriented catalog seed.
- `log.md` — append-only log seed with the grep-parseable `## [YYYY-MM-DD] <op>|<provenance-or-origin> | <Title>` prefix convention.
- `raw/` — immutable, redacted source artifacts (content-hash id; the LLM only reads them). Redaction masks local-path patterns (Windows drive-letter paths, UNC paths, POSIX system/home roots, `~/...`) and secret-shaped tokens; URLs (`https://...`) and a bare `~` with no path attached are left unmasked.
- `wiki/` — LLM-authored pages. `wiki/` is source tier; `wiki/derived/` is un-promoted synthesis.

## Resolving the active wiki

Operations resolve the active wiki by **existence**, top-down: **prompt `--root` > pj (`_projects/<project>/wiki/`) > workspace (`_llm-wiki/`) > CWD `.llmwiki`**. The pj scope is read one-way from taskflow, resolved against `$TASKFLOW_PROJECT_ROOTS`: the marker hook passes the turn's `session_id`, so the pj scope reads **this session's own** `_projects/_state/<session_id>.json` `project` field first — two sessions on different projects never resolve each other's wiki, even running concurrently. When that file is absent it falls back to the most recent `_projects/_state/*.json` (mtime); with no state file at all the pj scope is skipped cleanly. (The CLI carries no session context, so `resolve-root` keeps the mtime-latest behavior.) Because resolution no longer depends on the CWD alone, the operations work in the **VSCode extension without `cd`**. The marker hook shows `active wiki: <root> (scope: pj|workspace|cwd)` each turn so the resolved wiki is always visible.

## Usage

A typical session:

1. **Enter the wiki.** Resolution is automatic (see *Resolving the active wiki* above) — `cd` into a wiki root, or rely on the pj/workspace scope, or pass `--root <path>`. The scope hook injects `wiki-active`, the `active wiki:` line, and a `[wiki:on]` leading-line directive (mirroring taskflow's `[pj:…]`) for that turn; when the wiki resolved through the **pj** scope it also adds a wiki↔taskflow split guide (durable/cross-cutting knowledge → the wiki; task-execution context/progress → taskflow) plus a filing-proposal norm. When nothing resolves the plugin stays invisible.

   **Toggle it per session — `wiki:on` / `wiki:off`.** The wiki is on by default whenever one resolves. Include `wiki:off` anywhere in a prompt to silence it for the current session: the hook suppresses the `wiki-active` / filing injection and emits only a minimal `[wiki:off]` notice (Claude leads its reply with `[wiki:off]` and leaves the wiki untouched). `wiki:on` restores it. The state is a per-session marker under the resolved wiki root (`.llmwiki.toggle.d/<session_id>.off`, existence = off), so it is **sticky within a session** and a **new session starts on**; for a permanent off use the wiki's `activation_scope` in `SCHEMA.md` instead. When no wiki resolves, `wiki:on|off` is ignored (nothing is emitted — same shape as an unassigned pj).

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

   Before anything is written you see the one-line resolved-value declaration (`[wiki] write_mode = explicit (default)` …). With the default `write_mode=explicit` you confirm before the Stage 2 pages are applied; the whole ingest is one file-journal transaction (per file), so a failure or a decline rolls the wiki back to its pre-ingest state.

3. **Ask questions.** Just ask in natural language — e.g. *"what did we decide about retry backoff?"* The `wiki-query` skill auto-activates, reads both `wiki/` and `wiki/derived/`, and cites each claim by page path (the path tells you whether it's source or derived tier). This is read-only.

4. **File an answer (optional, explicit).** Querying never writes on its own. To save an answer, ask explicitly — e.g. *"file that as a page"* — and it lands under `wiki/derived/` as derived synthesis.

   For a **deterministic** filing trigger that does not depend on the LLM judging your intent, include the marker `llm-wiki:file` anywhere in an otherwise normal question — a hook detects it and makes filing mandatory (no confirmation: the marker is explicit by definition), and the answer is filed under `wiki/derived/`:

   ```
   what did we decide about retry backoff? llm-wiki:file              # force filing; page name generated from the answer
   what did we decide about retry backoff? llm-wiki:file=retry-policy # fix the page name → wiki/derived/retry-policy.md
   ```

   `llm-wiki:file=<page-slug>` fixes the page name to `wiki/derived/<page-slug>.md`; without a slug the LLM generates the page name from the answer. The marker is effective only inside a wiki (when `.llmwiki` is present). The safety envelope (redaction → write-tool location gate → single transaction) is unchanged — only the confirmation prompt is skipped. (Filing is suppressed while the session is `wiki:off`; re-enable with `wiki:on` first.)

   llm-wiki owns two prompt markers: `llm-wiki:file[=<slug>]` (this deterministic filing trigger) and `wiki:on|off` (the per-session toggle in step 1). Both fire only at a string start or after whitespace and are case-insensitive, so they never match mid-token.

5. **Lint.** `/wiki-lint` runs the graph/index checks plus the transcript decision-floor and returns a prioritized "next questions" list. Read-only.

6. **Browse the wiki.** `/wiki-view` starts a local HTML viewer at `http://127.0.0.1:17330/` that renders the wiki's `wiki/` + `wiki/derived/` pages (sanitized, CSP-protected, loopback-Host-only) and lets you click through `[[wikilinks]]`. Read-only; refuses to start while another viewer holds the port. Stop it with **`/wiki-view-stop`** — a dedicated skill that frees port 17330 cross-platform (see the `/wiki-view` section).

7. **Promote.** When a `wiki/derived/` page has earned source tier: `/wiki-promote wiki/derived/retry-policy.md`. After an explicit approval and a contamination check it moves to `wiki/retry-policy.md` and rewrites inbound links. This is the only derived→source path.

**Recovery.** If an ingest is interrupted (the process is killed mid-run), a stale `.llmwiki.lock` and the `.llmwiki.txn.d/` journal dir can remain. A **bare** stale lock — one left by a dead process with NO open transaction (no journal dir, no sidecar) — is reclaimed automatically on the next ingest (the recorded owner pid is liveness-checked; fail-closed on any doubt, so a still-running ingest is never reclaimed). A lock left alongside a journal or sidecar (an interrupted transaction) is **not** auto-reclaimed — recover it explicitly with the `abort` verb, which rolls the wiki back to its pre-ingest state (replaying the journal, removing any orphan `raw/` artifact) and releases the lock. It recovers even when the crash happened before the transaction sidecar was written (it keys on the journal / lock, not only the sidecar):

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest abort <wiki-root>
```

## Operations

Operations resolve the active wiki multi-scope (prompt>pj>workspace>cwd), so they work without `cd` into the wiki; pass `--root <path>` to target a specific wiki explicitly.

All CLI entrypoints pin their stdio to UTF-8 at startup (stdin strict — corrupted input fails fast; stdout/stderr with `errors="replace"` — reporting never crashes), overriding the host locale and `PYTHONIOENCODING`. This matters on Windows, where piped Python stdio defaults to the ANSI codepage (e.g. cp932 on Japanese systems) and would otherwise reject or mangle UTF-8 page content flowing through the write verbs (`file`, `ingest-apply` read page content from STDIN). Contract-tested with a forced-cp932 subprocess round-trip of non-BMP characters.

### Scope detection

`activation_scope: scoped` is implemented as a `UserPromptSubmit` hook (`hooks/wiki_marker_inject.py`): each turn it resolves the active wiki multi-scope (prompt>pj>workspace>cwd, via `wiki_root_resolver`), passing the turn's `session_id` so the pj scope reads this session's own state file first (see *Resolving the active wiki*). When one resolves, it injects a `wiki-active` context (including the resolved root + scope, so the wiki is visible even when the CWD is not it — e.g. in the VSCode extension), the `[wiki:on]` leading-line directive, and — pj scope only — the wiki↔taskflow split/filing guide. When no wiki resolves in any scope it exits silently and injects nothing — outside a wiki, the plugin is invisible. The injected context plus the `wiki-query` skill description is what auto-activates query; the write-bearing operations are explicit commands and do not depend on the hook.

The hook also honors the `wiki:on|off` toggle: a `wiki:on`/`wiki:off` marker in the prompt sets a per-session flag (kept as `.llmwiki.toggle.d/<session_id>.off`, existence = off, under the resolved wiki root, alongside `.llmwiki.lock` / `.cc-turn-ledger.jsonl`; managed by `llmwiki/core/wiki_toggle.py`, best-effort with mtime pruning of abandoned sessions). While off, the whole `wiki-active` / filing block is suppressed and only a minimal `[wiki:off]` notice is injected. The toggle is honored only when a wiki resolves (nothing is emitted otherwise); it is session-sticky and defaults on (a new session starts on). The toggle dir is force-excluded from ingest (`.llmwiki.toggle.d` in the self-ingestion guard), like `.llmwiki.txn.d`.

### Ingest — `/wiki-ingest <path-or-source-or-glob> [doc_type=...] [external=...]`

Ingests a 3rd-party source (FE-B) or a Claude Code session jsonl (FE-B') through the 2-stage `extract → apply` core inside one file-journal transaction. The argument may be a single file, a **quoted glob** (`"./docs/**/*.md"`), or a **directory** (`./docs/`): the driver expands it in Python (never the shell), force-excludes wiki-internal paths, and — for a directory — restricts to the text-type allowlist (`.md` / `.markdown` / `.txt` / `.text` / `.json` / `.jsonl`). A glob/directory is ingested **one file per transaction**; a per-file failure rolls back only that file and the run continues, then a `N total / M succeeded / K failed / S dedup-skipped` summary is reported (zero matches is an error).

`/wiki-ingest` (and `/wiki-ingest-sessions`) is a multi-stage orchestration (`begin` → Stage 1 subagent → Stage 2 subagent → `apply-finish`), so run it on a **capable model**: a lightweight/minimal model tends to drop the Stage 2 apply dispatch or skip the closing `apply-finish`, which leaves the transaction **open** (a stale `.llmwiki.lock` / `.llmwiki.txn` with no pages written — clear it with the `abort` verb; see *Recovery* above).

- **Stage 1 (extract)** — the `wiki-ingest-extract` subagent reads the redacted, untrusted raw source with **no write tool by construction** and emits proposed edits only.
- **Stage 2 (apply)** — the `wiki-ingest-apply` subagent authors page updates with **no write tool by construction** and returns them as a page manifest; the orchestrator pipes those manifests through the allowlist write tool (`llmwiki/write/write_tool.py`) via the compound `apply-finish` verb, which confines writes to `wiki/` and `wiki/derived/`, rejects `SCHEMA.md` / `.llmwiki` / `raw/` / absolute paths / traversal, and gates on budget. On more than `apply_fanout_k` touched pages, Stage 2 fans out one apply worker per cluster; `apply-finish` then applies every cluster's manifest and centralizes index / log / commit after the join. The total proposed touched-page set is first gated against `max_count`: an ingest proposing more pages than that escalates to the human gate instead of fanning out, so the per-worker write budget can't be silently multiplied by the cluster count. On fan-out, each cluster's worker also receives a code-authored absolute manifest path (`manifest_paths`, returned by `plan-fanout`) so it never has to reconstruct a temp file path itself.

`begin` fails closed on a `.jsonl` source: when `--kind` is omitted/`auto` and the source path ends in `.jsonl`, `begin` refuses to ingest it (nothing locked or written) rather than silently treating a session log as plain text — pass `--kind=fe_b_prime` (cc-log transcript), `--kind=fe_pi_log` (pi-log transcript), or an explicit `--kind=fe_b` to ingest a plain-text `.jsonl` DATA file. In a glob/directory batch (the text-type allowlist still keeps `.jsonl`), a source that hits this refusal is simply counted `failed` in the summary and the run continues.

cc-log (FE-B') input is pinned to `doc_type=transcript` with a deterministic decision floor (`llmwiki/ingest/transcript_floor.py`, invoked as `llmwiki floor-check`): a claim is recorded as a decision only with an explicit affirmative token; silence is non-affirmation.

FE-B' extraction is **fork-aware**: instead of a single-file read it projects a session (`session_id`) — including its agent/fork children, which carry the parent's `session_id` — out of the vendored DuckDB views (`llmwiki/ingest/cc_views.sql`, a byte-for-byte vendored copy of the `inspect-cc-log` skill's `views.sql`, kept in sync and guarded by a contract test) via the projector `llmwiki/ingest/cc_log_project.py`. Injected boilerplate is stripped and turns are deduped **exact and length-independent** by a content hash `md5(nfc_normalize(role) ‖ 0x1F ‖ nfc_normalize(text))` (thinking blocks excluded at the SQL level). A wiki-local **turn ledger** (`.cc-turn-ledger.jsonl`, written by `llmwiki/ingest/ledger.py`) records each owned turn's hash on the first ingest, so re-ingesting a session — or the same shared prefix across sessions — files each turn **only once** (first-ingested-owns; cross-path and cross-rerun idempotent). The ledger diff is journaled inside the transaction, and the diff itself runs **inside the transaction lock**: `begin` extracts turns before locking (read-only) but reads the seen-set and drops owned turns only after `.llmwiki.lock` is held, so a concurrent ingest's `finish` cannot append between diff and lock (no duplicate filing, no first-owner races). A failed session leaves nothing owned and the next session re-files the shared prefix (no gap). The dedup/ledger unit is the **CC record** (`record_uuid`), not a conversation turn: a synthesized replay record (one record embedding many `USER:`/`ASSISTANT:` blocks) is treated as one unit; splitting into conversation grain is a non-goal.

### Ingest a session set — `/wiki-ingest-sessions [--workspace | --pj <name>] [--root <wiki>]`

Path B ingests **every cc-log session of a resolved set** in one command. It resolves the session set from, in order: an explicit `--workspace` (every sid across the whole workspace's `_projects/_state/*.json`, no project filter); else an explicit `--pj <name>` (`_projects/_state/*.json` filtered by `project == <name>`); else — no-args — the resolved wiki scope (`WIKI_SCOPE` from `resolve-root`): scope `workspace` follows the same workspace-wide union, scope `pj`/`prompt` resolves this session's own taskflow-applied project (`_projects/_state/<sid>.json`, failing closed with guidance to pass `--pj <name>` if unresolvable), and scope `cwd` (unchanged, legacy/standalone) resolves the CC project directory of the running session (ground-truth: the directory holding the current session's `<sid>.jsonl`). It then orders the session ids by session-start timestamp ascending and loops the existing per-transaction ingest cycle **one session per transaction** (failure-continue, same `N total / M succeeded / K failed / S dedup-skipped` summary as the glob loop). Because dedup is ledger-driven, re-running as the project grows is **incremental**: already-owned turns are skipped, and the summary also reports the resolved session count and the number of **ledger-skipped turns** so an incremental re-run is never silently a no-op. Zero matches / an unresolvable session set is an explicit error (fail-closed).

The `--pj <name>` scope (and the no-args `pj`/`prompt` resolution it also backs) covers **only sessions taskflow registered** (`_projects/_state/*.json` with `project == <name>`) — not the whole CC directory; a session with no `_state` file is not in the `--pj` set. `--workspace` (and no-args on a workspace-scoped wiki) widens this to every registered sid regardless of project — still bounded by what taskflow registered, not the whole CC directory. To ingest every CC session of a standalone/legacy repo regardless of taskflow registration, run on a cwd-scoped wiki with both flags omitted (the driver resolves the CC directory as ground truth). To avoid re-scanning the whole CC session-log corpus (`~/.claude/projects`, or `$CLAUDE_CONFIG_DIR/projects` as well when that variable is set) once per session (N sessions → N scans), a read-only `project-batch` verb extracts all sessions' turns in **one** scan before the loop (writing per-session turn files to a temp dir the loop cleans up); each `begin` then consumes its pre-extracted turns via `--turns` and runs only the cheap per-session dedup + ledger diff, keeping the ledger read-after-write sequential.

### Query — the `wiki-query` skill

Description-driven auto-activation when a wiki is active **and not toggled off** (`wiki:off` suppresses the activating injection for the session). It reads **both** `wiki/` and `wiki/derived/` and cites every claim by page path, where the path encodes the trust tier (`wiki/` = source, `wiki/derived/` = derived). Read-only by default; it files an answer back into the wiki only on an explicit filing trigger — either a natural-language ask (LLM-judged) or the deterministic, hook-detected marker `llm-wiki:file[=<page-slug>]` included anywhere in the question. The marker forces filing without confirmation; with a slug the page name is fixed to `wiki/derived/<page-slug>.md`, without one the LLM generates it. Only the confirmation is skipped — the redaction → write-tool gate → single-transaction envelope is unchanged.

### Search backend (optional external) — qmd

By default the query path is `index`: `wiki-query` enumerates all pages
(`llmwiki search <root> --q …` returning the same set as `scan-pages`) and the LLM
selects what to read — **no external dependency, byte-identical to prior
behavior**. For large wikis you can opt in to **qmd (Quick
Markdown Search)** — an external on-device full-text engine (installed
separately; ~GB models). Set `search_backend: qmd` in the wiki's `SCHEMA.md`
config; the `search` verb then dispatches internally to qmd and returns a ranked
top-k of the most relevant pages instead of the full enumeration.

- **Opt-in and isolated.** qmd is never bundled and adds no Python dependency (the
  `read/` layer shells out to the `qmd` CLI). All qmd state lives under
  `<wiki-root>/.qmd/` (project-local; the `wiki/` subtree only — `raw/` is never
  indexed). The two code gates (write allowlist, ingest transaction) are untouched;
  qmd only reads pages.
- **Correctness boundary.** Every qmd hit is filtered through `scan_pages` (the
  single page-ness authority), so `raw/` and `wiki/README.md` are never cited and
  the tier is still decided by path (D22) — never by qmd.
- **Build it with `/wiki-reindex`.** Run it once (and after large ingests) to build
  / refresh the project-local index (`qmd init` → `collection add wiki/` → `embed`
  → `update`). It writes only under `.qmd/`, is idempotent, and is a **no-op when
  `search_backend` is not `qmd` or qmd is not installed** (announce + exit, no
  crash). If qmd is unavailable at query time, `search` loud-announces one line and
  degrades to the index path — the same one-line degrade also covers a qmd error
  mid-query or an empty result (e.g. an index that was never built).

### Lint — `/wiki-lint`

Read-only. Runs the deterministic link / index graph checks (`llmwiki/lint/link_lint.py`, `llmwiki/core/wiki_index.py`, invoked as `llmwiki lint`) plus the transcript-only type-specific lint (v1), and reports a prioritized "next questions" list. Never writes.

The transcript decision floor's affirmative-token check (`AFFIRMATIVE_TOKENS`, `transcript_floor.py`) is **English-only**. On a Japanese-language transcript, every `decisions` candidate currently fails the check and fires `FLOOR-VIOLATION` — the floor is effectively inert for Japanese content and manual judgment applies instead. This is a known, accepted limitation (Japanese affirmation/negation is predominantly postfix and isn't reliably coverable by the same token approach), not a bug; the fail direction stays conservative (over-flagging, never a false admit).

### Promote — `/wiki-promote <wiki/derived/X.md>`

Promotes a derived synthesis page to source tier (`wiki/derived/X.md → wiki/X.md`) as a code-driven move plus inbound link-rewrite (`llmwiki/write/promote.py`), gated on explicit human approval and a contamination check. The flow is split across read-only and write verbs: `llmwiki declare` (Step 1 resolved-value declaration), read-only `llmwiki promote-check` (Step 2 pre-approval contamination preview, no move), then `llmwiki promote` (Step 3 move, only after approval). The only path from derived to source tier.

### View — `/wiki-view`

Starts a local HTML viewer for the active wiki — a `127.0.0.1`-bound HTTP server on port `17330` (`llmwiki-view view --serve`, wrapping `llmwiki/view/generate_wiki_view.py`, never externally reachable) that renders the wiki's Markdown pages to HTML on demand and turns `[[wikilinks]]` into navigable links between page views. Read-only — it does not write to the wiki. The wiki-root is resolved by the multi-scope resolver (`--root` > pj > workspace > CWD), so it no longer needs the CWD to be the wiki root; pass `--root <path>` to target one explicitly.

- Serves `wiki/` + `wiki/derived/` only; `raw/` is **not** exposed.
- Each page shows a tier badge (**source** / **derived**), and pages are **tier-distinct**: a same-name `wiki/X.md` and `wiki/derived/X.md` are separate pages. A `[[X]]` whose basename resolves to both renders **both** as tier-labelled links (`X (source)` / `X (derived)`).
- A `[[link]]` with no target page is shown as a distinct, non-navigable "missing" link.
- **Hardened against untrusted page content**: page bodies are ingested from untrusted sources, so the rendered HTML is sanitized with `nh3` (scripts / event handlers / `javascript:` URLs stripped, wikilink markup preserved) and every response carries a strict `Content-Security-Policy` (`default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:`) as the second layer. Requests whose `Host` header is not a loopback name (`127.0.0.1` / `localhost` / `::1`) are refused with 403 (DNS-rebinding hardening).
- **Exclusive port bind**: the server refuses to start when the port is already in use (`allow_reuse_address` is disabled) instead of silently co-binding with a stale viewer for a *different* wiki — a stale server would otherwise answer some browser connections and show the wrong wiki. The bind error tells you to stop the old viewer or pass `--port <other>`.
- On start it prints the URL + page count (`[wiki-view] serving <N> pages at http://127.0.0.1:17330/ ...`). Stop it with the dedicated **`/wiki-view-stop`** skill, which kills the port-17330 listener cross-platform: on POSIX it runs `pkill -f "llmwiki-view view --serve"`; on Windows/Git Bash — where MSYS `pkill` cannot terminate the native `uv`/`python` processes — it kills by port with `netstat -ano | grep ":17330 " | grep LISTENING | tr -d "\r" | sed "s/.* //" | sort -u | xargs -r -I{} taskkill //F //PID {}`. Pass `--port <n>` to `/wiki-view-stop` if the viewer was started on a non-default port.

## Configuration & defaults (D3–D5)

Config lives in `SCHEMA.md` frontmatter (wiki-local) and is read by the plugin's own scripts, not by the Claude Code settings mechanism. Each axis resolves independently with the precedence **prompt-explicit > wiki-local config > built-in default** (D4); before any write the resolved value and its source are declared in one line (D5). Built-in defaults:

| Key | Default | Meaning |
|---|---|---|
| `activation_scope` | `scoped` | Activate only inside a `.llmwiki` wiki root (D3) |
| `read_grounding` | `implicit` | Query grounds in the wiki without being asked (D3) |
| `write_mode` | `explicit` | Confirm before applying writes (D3); `implicit` skips confirmation with a loud session-start notice |
| `write_autocommit` | `auto` | INERT — the engine invokes no git; retained for config stability |
| `override_scope` | `operation` | A prompt override applies to one operation; `session` makes it sticky |
| `apply_fanout_k` | `10` | ≤K touched pages inline; >K fans out per-cluster (D23) |
| `max_count` | `100` | Write-count budget: the per-apply-worker page limit, and the ingest-grain gate — a touched set larger than this escalates to the human gate (F2) |
| `max_bytes` | `10485760` | Write-size budget per write session (10 MiB); overflow escalates to the human gate |
| `search_backend` | `index` | Query read path: `index` (default, no external dep) or `qmd` (opt-in external full-text backend) |
| `qmd_bin` | `qmd` | qmd binary resolved via PATH (used only when `search_backend=qmd`) |
| `qmd_page_threshold` | `100` | Use qmd only when the wiki holds more than this many pages (below it, index-direct) |

The ingest journal checkpoint is taken on every run (D14): `write_mode` controls only whether a confirmation precedes applying writes. The engine never commits to git.

`max_bytes` is enforced **cumulatively**, not per write: on a fanned-out ingest (`apply_fanout_k`), the running byte total carries across every cluster in the same transaction, so a combined-over-budget fanout is rejected (`REJECTED budget`, the whole transaction rolled back) even when each individual cluster is under the limit on its own. The same budget also gates a direct page write (the `file` verb, e.g. filing a query answer) using the wiki's configured `max_count`/`max_bytes` rather than a hardcoded default.

## File layout

```
plugins/llm-wiki/
  .claude-plugin/plugin.json   # manifest: name / description / version / author.name
  hooks/
    hooks.json                 # UserPromptSubmit -> wiki_marker_inject.py
    wiki_marker_inject.py      # resolve active wiki -> inject "wiki-active" + "active wiki:" line; silent when dormant
  agents/
    wiki-ingest-extract.md     # Stage1 extract (tools: Read; no write tool)
    wiki-ingest-apply.md       # Stage2 apply (authors a page manifest; no write tool)
    wiki-lint.md               # read-centric lint subagent
  skills/                      # all user-facing entries are skills (bare /wiki-*; no plugin namespace prefix)
    wiki-ingest/SKILL.md          # /wiki-ingest  (ingest orchestrator; single file / glob / directory)
    wiki-ingest-sessions/SKILL.md # /wiki-ingest-sessions (Path B: ingest a resolved cc-log session set)
    wiki-init/SKILL.md            # /wiki-init (interactive scope select -> llmwiki init)
    wiki-lint/SKILL.md            # /wiki-lint (read-only lint dispatch)
    wiki-promote/SKILL.md         # /wiki-promote (derived -> source)
    wiki-query/SKILL.md           # query skill (description-driven auto-trigger)
    wiki-reindex/SKILL.md         # /wiki-reindex (rebuild the optional qmd search index; .qmd/ only)
    wiki-view/SKILL.md            # /wiki-view (start the local HTML page viewer)
    wiki-view-stop/SKILL.md       # /wiki-view-stop (stop the viewer; frees port 17330 cross-platform)
  llmwiki/                     # path-imported package (no install); deterministic engine
    __init__.py                # version + public re-exports
    cli.py                     # verb dispatch (branch-local lazy imports enforce the read-only profile)
    core/                      # single-authority, dep-free
      wiki_index.py marker.py config_resolver.py wiki_root_resolver.py wiki_log.py content_hash.py wiki_toggle.py   # wiki_toggle: per-session wiki:on|off state
    write/                     # allowlist write gates + promote
      write_tool.py transaction.py promote.py
    ingest/                    # duckdb
      ingest_driver.py frontends.py redaction.py transcript_floor.py
      cc_log_project.py ledger.py cc_views.sql   # fork-aware cc-log projector + turn-hash dedup ledger + vendored SQL views
    read/                      # dep-free read paths (qmd is an external CLI shell-out, not a Python dep)
      query.py qmd_search.py
    lint/   link_lint.py       # graph/index lint
    view/   generate_wiki_view.py  # local HTML viewer (markdown)
    init/   wiki_init.py       # wiki initializer
  bin/                         # CLI entrypoints (PEP 723 dep decls; uv run)
    llmwiki                    # dep-free: resolve-root scan-pages search file declare promote-check promote lint init marker-detect ingest-apply floor-check reindex
    llmwiki-ingest             # duckdb:   ingest {begin|plan-fanout|finish|apply-finish|abort|enumerate|session-plan|project-batch|project-batch-cleanup}
    llmwiki-view               # markdown: view --serve
  pyproject.toml               # version / requires-python / extras(doc); runtime is not installed
  templates/                   # what a new wiki instance is initialized from
    .llmwiki SCHEMA.md index.md log.md raw/ wiki/
```

## License

[MIT](../../LICENSE)
