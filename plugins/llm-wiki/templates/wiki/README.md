# wiki/ — generated pages (design §1, D7/D15)

LLM-generated pages. Location is the trust boundary (D15).

- `wiki/<page>.md` — provenance:source tier (promoted). Citable as external fact.
- `wiki/derived/<page>.md` — provenance:derived synthesis, un-promoted. Promotion
  to source tier is `wiki/derived/X` -> `wiki/X` via explicit `promote`
  (move + inbound link-rewrite + human approval; copy is not used).

Cross-namespace flow is forbidden (D20): derived-origin edits land only in
`wiki/derived/`. A source page may reference derived pages by link only — never
inline them.

Page frontmatter exposes the tag axes (design §2):

```yaml
---
provenance: source            # source | derived (mirrors location, D15)
doc_type: spec                # transcript|article|paper|spec|runbook|incident|policy|guide | default
derived_origin:               # derived-side origin (D12); present on derived pages
                              #   conversation | cc-log | pi-log
---
```

`source_ref` (the source-side origin, D12 — the relative `raw_path` plus an
optional `external_locator` url/permalink) is **not** a required frontmatter
field. The engine records it in the wiki-root ledger
`.llmwiki.source-ref.jsonl`: append-only JSON Lines, one record per raw
generation event, written inside the ingest transaction (a rollback removes it)
and out of reach of the allowlist write tool. Absolute paths are never recorded.
A page MAY mirror `source_ref` into its own frontmatter as an optional copy; the
ledger is authoritative.

`epistemic-status` is per-claim in the body (optional enrichment, D15), with a
value space defined by the doc_type profile — not a frontmatter field.
