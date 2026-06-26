---
name: wiki-view
description: Generate and open a browsable local view of the active wiki — pages rendered from markdown with traversable [[wikilinks]].
disable-model-invocation: true
allowed-tools: Bash(uv run *)
---

# /wiki-view

Serve a local HTML viewer for the active llm-wiki (CWD must be a wiki root — `.llmwiki` present). Renders wiki/ + wiki/derived/ markdown pages on demand and turns `[[wikilinks]]` into navigable links between pages.

## Step 1 — Start the wiki viewer server

```bash
nohup uv run ${CLAUDE_PLUGIN_ROOT}/scripts/generate_wiki_view.py --serve > /tmp/llm-wiki-view.log 2>&1 &
sleep 2
grep "serving" /tmp/llm-wiki-view.log | tail -1
```

This starts an HTTP server on `http://127.0.0.1:17330/` in the background and returns the server summary line.

The wiki root is the current working directory; `.llmwiki` must be present. If absent, the script exits with `error: no .llmwiki marker ...` — surface that error and stop (run from a wiki root).

## Step 2 — Report

Extract the page count from the server summary line (`[wiki-view] serving <N> pages at ...`) and reply with a one-line summary:

```
wiki-view: http://127.0.0.1:17330/ — <N> pages

To stop: pkill -f "generate_wiki_view.py --serve"
```

Do not add further commentary. Always include the stop command.
