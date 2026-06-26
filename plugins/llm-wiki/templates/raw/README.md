# raw/ — immutable sources (design §1)

Ingested source material, written once after redaction (D16). Read-only to the LLM.

- `raw/<hash>.<ext>` — provenance:source. `id` = content-hash (D18); re-ingesting
  the same content is a no-op.
- `raw/derived/<hash>.md` — provenance:derived (conversation or cc-log snapshot,
  redacted).
- `raw/assets/` — optional images.

The allowlist write tool REJECTS `raw/` as an LLM write target; only the ingest
front-end (code) writes here.
