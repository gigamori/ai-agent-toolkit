---
name: kanban
description: Generate and open the taskflow project kanban board in the browser. Shows all projects × tasks × sessions with clickable session links.
disable-model-invocation: true
allowed-tools: Bash(uv run *)
---

# /kanban

Generate a Trello-like HTML kanban board of all taskflow projects and open it in the default browser.

## Step 1 — Start the kanban server

```bash
nohup uv run ${CLAUDE_PLUGIN_ROOT}/scripts/generate_kanban.py --serve > /tmp/taskflow-kanban.log 2>&1 &
sleep 2
grep "serving" /tmp/taskflow-kanban.log | tail -1
```

This starts an HTTP server on `http://localhost:17329/` in the background and returns the server summary line.

## Step 2 — Report

Extract the task count from the server log and reply with a one-line summary:

```
kanban: http://localhost:17329/ — <N> tasks across <M> projects

To stop: pkill -f "generate_kanban.py --serve"
```

Do not add further commentary. Always include the stop command.
