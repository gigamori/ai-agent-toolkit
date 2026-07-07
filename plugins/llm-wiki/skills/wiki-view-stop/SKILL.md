---
name: wiki-view-stop
description: Stop the background llm-wiki viewer server started by /wiki-view (frees the default port 17330). Use when a viewer is running and you want to shut it down, or before starting a fresh /wiki-view.
disable-model-invocation: true
allowed-tools: Bash(pkill *), Bash(netstat *), Bash(grep *), Bash(tr *), Bash(sed *), Bash(sort *), Bash(xargs *), Bash(taskkill *)
---

# /wiki-view-stop

Stop the local wiki viewer server started by `/wiki-view`. It runs as a
background `uv`/`python` process bound to `127.0.0.1:17330` (the default). Killing
the port listener frees the port and lets the `uv` wrapper exit.

Arguments: `$ARGUMENTS` (optional `--port <n>` if the viewer was started on a
non-default port; otherwise 17330 is assumed).

## Step 1 — Stop the server

Run the command for THIS machine's OS (you know the platform from context).
Replace `17330` with the `--port` value if one was given.

- POSIX (Linux / macOS):

```bash
pkill -f "llmwiki-view view --serve"
```

- Windows (Git Bash): MSYS `pkill` cannot terminate the native `uv`/`python`
  processes, so kill by the LISTENING port instead. (The pipeline is kept free of
  `$` — no `$(...)`, no `awk '{print $5}'`, no `$var` — because a `$` is stripped
  when a reply is relayed as prose; it uses `sed 's/.* //'` + `xargs -I{}`.)

```bash
netstat -ano | grep ":17330 " | grep LISTENING | tr -d "\r" | sed "s/.* //" | sort -u | xargs -r -I{} taskkill //F //PID {}
```

## Step 2 — Report

Reply with ONE line:

- if a server was stopped: `wiki-view stopped (port 17330 freed)`
- if nothing was listening: `no wiki-view server was running (port 17330 free)`

Do not add further commentary.
