---
name: kanban
description: Generate and open the taskflow project kanban board in the browser. Shows all projects × tasks × sessions with clickable session links.
disable-model-invocation: true
allowed-tools: Bash(uv run *), Bash(nohup uv run *)
---

# /kanban

Generate a Trello-like HTML kanban board of all taskflow projects and open it in the default browser.

Each workspace gets its own server on a port derived from that workspace's `_projects` roots (base 17329, span 64) — running `/kanban` from several workspaces at once no longer collides; each binds its own port and only its own `--stop` can stop it.

## Step 1 — Start the kanban server

```bash
LOGFILE="/tmp/taskflow-kanban-$(pwd | cksum | cut -d' ' -f1).log"
nohup uv run ${CLAUDE_PLUGIN_ROOT}/scripts/generate_kanban.py --serve > "$LOGFILE" 2>&1 &
sleep 2
grep -a "serving" "$LOGFILE" | tail -1
```

The log filename is derived from the current directory so concurrent `/kanban` runs in different workspaces never race on a shared log. The server prints its actual URL (port varies per workspace) in the summary line. Starting is idempotent: if a server for this workspace is already running it reports `already serving …` and does not launch a second one.

## Step 2 — Report

Extract the URL and task count from the server log line (`[kanban] serving <N> tasks at <url> ...`) and reply with a one-line summary using that URL verbatim — do not hardcode a port number:

```
kanban: <url from the log line> — <N> tasks across <M> projects

To stop: uv run ${CLAUDE_PLUGIN_ROOT}/scripts/generate_kanban.py --stop
```

Do not add further commentary. Always include the stop command. `--stop` only stops this workspace's server; `--stop --all` stops every kanban server across all workspaces.
