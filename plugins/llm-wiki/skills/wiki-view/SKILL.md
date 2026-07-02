---
name: wiki-view
description: Generate and open a browsable local view of the active wiki — pages rendered from markdown with traversable [[wikilinks]].
disable-model-invocation: true
allowed-tools: Bash(uv run *)
---

# /wiki-view

Serve a local HTML viewer for the active llm-wiki. Renders wiki/ + wiki/derived/ markdown pages on demand and turns `[[wikilinks]]` into navigable links between pages. The wiki root is resolved multi-scope (prompt>pj>workspace>cwd), so the CWD need not be the wiki root; pass `--root <path>` to target one explicitly.

## Step 1 — Start the wiki viewer server

```bash
nohup uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-view view --serve > /tmp/llm-wiki-view.log 2>&1 &
sleep 2
grep "serving" /tmp/llm-wiki-view.log | tail -1
```

This starts an HTTP server on `http://127.0.0.1:17330/` in the background and returns the server summary line. Pass `--root <path>` after `--serve` to target a specific wiki root.

The wiki root is resolved via `wiki_root_resolver` (prompt>pj>workspace>cwd). If nothing resolves, the script exits 2 with `error: no wiki resolved (prompt>pj>workspace>cwd all empty). Pass --root <path> or run from a wiki root.` — surface that error and stop.

## Step 2 — Report

Extract the page count from the server summary line (`[wiki-view] serving <N> pages at ...`) and reply with a one-line summary:

```
wiki-view: http://127.0.0.1:17330/ — <N> pages

To stop: pkill -f "llmwiki-view view --serve"
```

Do not add further commentary. Always include the stop command.
