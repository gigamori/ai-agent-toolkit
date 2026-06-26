# Log

Append-only. Newest entries at the bottom. Each entry header starts at line-begin
with `## [` so it is grep-parseable: `grep "^## \[" log.md | tail`.

Header grammar (fixed token order):

```
## [YYYY-MM-DD] <op>|<provenance-or-origin> | <Title>
```

- `<op>` — ingest | file | ...
- `<provenance-or-origin>` — source | derived | cc-log
- `<Title>` — free text

Examples:
- `## [YYYY-MM-DD] ingest|source | <Title>`   (FE-B, 3rd-party source)
- `## [YYYY-MM-DD] file|derived  | <Title>`   (FE-A, conversation/filing)
- `## [YYYY-MM-DD] file|cc-log    | <Title>`   (FE-B', cc-log jsonl)

<!-- append entries below this line -->
