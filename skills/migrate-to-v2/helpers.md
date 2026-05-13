# migrate-to-v2 helpers

Detailed rules referenced from SKILL.md. Read this file once at Phase 2 start.

## Slugify rules (Phase 2)

Given a task `title` string, produce a `topic_slug` for the filename:

1. Lowercase ASCII letters and digits.
2. Replace whitespace with `-`.
3. **Strip punctuation regardless of width**:
   - ASCII: `.`, `,`, `:`, `;`, `!`, `?`, `(`, `)`, `[`, `]`, `{`, `}`, `<`, `>`, `"`, `'`, `` ` ``, `/`, `\`, `|`, `*`, `&`, `+`, `=`, `@`, `#`, `$`, `%`, `^`, `~`
   - Full-width (CJK): `（`, `）`, `「`, `」`, `『`, `』`, `、`, `。`, `〜`, `・`, `…`, `：`, `；`, `！`, `？`, `〝`, `〟`, `“`, `”`, `‘`, `’`
4. **Preserve non-ASCII LETTERS**: Japanese kana, CJK ideographs, Hangul, etc. Modern filesystems support Unicode filenames; stripping CJK letters loses signal.
5. Collapse consecutive `-` into a single `-`.
6. Trim leading and trailing `-`.
7. Truncate to 50 characters; trim trailing `-` again.
8. If the result is empty (e.g., title was only stripped punctuation), use `untitled`.

### Examples

| Title | Slug |
|---|---|
| `OverlayTree表示改善 + branch_summary + ナビゲーション修正` | `overlaytree表示改善-branch_summary-ナビゲーション修正` |
| `Tools infra: frontmatter args 移行 PoC` | `tools-infra-frontmatter-args-移行-poc` |
| `Plan Mode Revamp フェーズ A` | `plan-mode-revamp-フェーズ-a` |
| `subagent 拡張 hardening（誤呼出対策 1〜4）` | `subagent-拡張-hardening誤呼出対策-1-4` |
| `--- (only punctuation) ---` | `untitled` |

## Filename collision rule (Phase 4)

Initial: `<date>_<slug>.md`. On collision, append `-N` starting from 2:

- `2026-05-13_setup.md`
- `2026-05-13_setup-2.md`
- `2026-05-13_setup-3.md`

## Handoff–task merge criteria (Phase 2)

Merge a handoff file into a planned task entry when **all** hold:

- Handoff filename starts with a date (`YYYY-MM-DD_…`)
- Date is within ±14 days of the task entry's `date`
- Slug portions share at least 1 meaningful token (≥ 3 chars, ignoring numerics and stopwords like `phase`, `step`, `fix`, `v1`, `v2`)

If multiple candidates match, pick the closest by date. If still ambiguous, do NOT auto-merge — treat the handoff as an orphan and create its own task entry.

## Session-log mapping heuristics (Phase 3)

For each `### YYYY-MM-DD - <title>` header, score each task entry:

| Signal | Weight |
|---|---|
| Title contains explicit `task #N` or `#N` matching task's source row | +5 (assigns directly) |
| Date within ±7 days of task's `date` | +2 |
| Date within ±14 days | +1 |
| Token overlap (Jaccard ≥ 0.3 on tokens ≥ 3 chars) between log title and task title | +3 |
| Token overlap (Jaccard ≥ 0.5) | +5 |

Assignment rule:

- Score ≥ 5 → assign to that task
- Score 3-4 with no competing higher score → tentatively assign
- Score < 3 or tied at low score → `[unassigned]`

Log body chunk: from the `###` header line to the next `###` header (exclusive). Use `Read` with `offset`/`limit` to fetch.

When summarizing the log body into a `<!-- @log -->` entry, condense to one line:

- `- 2026-05-09: <one-sentence summary>`

If body is short (≤ 5 lines), use its first non-empty bullet verbatim. If long, summarize the `Done:` or `Goal:` line.

## Category map (Phase 6)

Match against filename and (if ambiguous) read the file's first 30 lines for content hints.

**Rules are matched in order; first match wins. Specific suffix rules take precedence over generic "contains keyword" rules.**

| Pattern | Target category |
|---|---|
| Path is under `_archive/` | `_archive/` (keep) |
| Path is under `tests/` (legacy v1 subdir) | `checks/` |
| Path is under `debug-handoff/` (legacy v1 subdir) | `procedures/` |
| Filename is bare `spec.md`, `specs.md`, `design.md`, `architecture.md`, `decision.md`, `adr.md` | `specs/` |
| Filename ends in `-design.md`, `-spec.md`, `-decision.md`, `-adr.md` | `specs/` |
| Filename ends in `-impl.md`, `-architecture.md` | `specs/` |
| Filename is bare `runbook.md`, `procedures.md`, `howto.md`, `guide.md` | `procedures/` |
| Filename ends in `-runbook.md`, `-gotchas.md`, `-pitfalls.md`, `-troubleshoot.md`, `-howto.md` | `procedures/` |
| Filename is bare `investigation.md`, `analysis.md`, `survey.md`, `research.md` | `investigations/` |
| Filename ends in `-investigation.md`, `-survey.md`, `-analysis.md`, `-comparison.md` | `investigations/` |
| Filename ends in `-postmortem.md`, `-retro.md`, `-retrospective.md` | `investigations/` |
| Filename ends in `-history.md` | `_archive/` |
| Filename is bare `backlog.md`, `ideas.md`, `roadmap.md`, `todo.md` | `backlog/` |
| Filename ends in `-backlog.md`, `-ideas.md` | `backlog/` |
| Filename matches `feature-status.md`, `issues.md` | `backlog/` |
| Filename contains `test` AND no other rule matched (generic fallback) | `checks/` |
| (no match) | `investigations/` (default; flag as ambiguous in report) |

**Note on `-runbook.md` vs `test`**: A file like `end_to_end_test_runbook.md` matches `-runbook.md` (procedures/) before the generic "contains test" rule fires. Runbooks are procedures regardless of domain.

**Note**: "bare" means the filename equals the listed name with no prefix/suffix (e.g., `spec.md` matches but `api-spec.md` would match the `-spec.md` row instead, both go to `specs/`). When a file's bare name equals a category-related word, it usually represents the project's primary document of that kind.

### Disambiguation when content is mixed

If the heuristic suggests a category but the file content clearly fits another, override:

- File body has many `## TC-` or `### TC-` headers → `checks/`
- File body has `## Decision` or `## Decided` headers → `specs/`
- File body starts with `# <feature> Backlog` or has many `- [ ]` checkbox items at top level → `backlog/`

Override decisions should be reported in the final report as "Phase 6: <file> placed in <category> by content override".

## Section preservation in legacy progress.md (Phase 7)

When editing the legacy progress.md before rebuild:

**Remove entirely** (these become task files or are obsolete):

- `## TODO`
- `## In Progress`
- `## Completed`
- `## Session Log` (entries already migrated to task `<!-- @log -->` blocks)
- `## Last Updated` (no longer used; `updated` is per-task frontmatter)
- The H1 line if it duplicates the project name (rebuild_progress.py creates `# Progress: <project>`)

**Keep** (free-text sections, both LLM and human edit them):

- `## Architecture`
- `## Key Decisions & Policies`
- `## Open Issues`
- `## Reference Materials`

If unusual extra sections exist (e.g., `## Project Overview` left from old template), **drop them**: the project overview now lives in `index.md` per the v2 design.

## Frontmatter prepend for un-frontmattered notes (Phase 6)

For each notes file lacking `---` frontmatter, prepend:

```yaml
---
domain: development
created: <YYYY-MM-DD from stat mtime>
updated: <YYYY-MM-DD from stat mtime>
---

```

(Empty line after closing `---` is required.)

Skip files that already begin with `---\n` followed by another `---\n` within the first 50 lines.

## Index.md rebuild (Phase 6)

Old format (3 columns):
```
| File | Description | Tags |
```

New format (4 columns):
```
| File | Description | Tags | Updated |
```

For each row in old index:

1. The `File` value gains a category prefix (e.g., `api-design.md` → `specs/api-design.md`) based on the file's new location after Phase 6 moves
2. `Description` is truncated to 100 characters (move overflow into the file body if needed, but for migration, just truncate with `…`)
3. `Tags` carried over
4. `Updated` = `YYYY-MM-DD` from the file's new mtime (or its frontmatter `updated` if present)

For files newly moved but not in the old index, add a row with description = first H1 from body (or filename stem if no H1), tags = empty.

For old index rows whose file no longer exists (was deleted at some point), drop the row.
