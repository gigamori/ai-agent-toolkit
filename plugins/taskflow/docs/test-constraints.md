# taskflow test constraints (v0.2.8)

Facts about the world outside this repository that `plugins/taskflow/tests/` depends on.
A test file cannot state these: it is read by agents that have only this checkout, and a
claim about a host platform, a third-party tool, or the live machine cannot be checked
from here. They are collected once, with the date they were observed and how.

This is the single constraints document for the `plugins/taskflow/tests/` gate. Entries are
allowed to go stale — whoever next finds one dead deletes it. Nothing links back to it and
no check fails when it drifts.

## Host platform — Git-Bash / MSYS on win32

- A `/`-leading pattern handed to `git grep` through a shell is rewritten by MSYS before
  git sees it, silently and with exit 0; a pattern containing backslashes was collapsed by
  a quoting layer and searched for as the wrong string, reporting a real leak as clean.
  Source-scanning checks therefore match in Python against file contents and shell out to
  nothing. Observed 2026-08-23 while authoring the sandbox-guard ratchet.
- `mktemp -d` resolves under `/tmp`, which carries no drive letter. `/c/...` is the
  Git-Bash drive-mount spelling of `C:\...`, and `os.path.isdir("/c/...")` under native
  Windows Python returns False. `cygpath -m` converts between the two spellings and is how
  a fixture derives one from the other instead of hardcoding either. Observed 2026-07-24.
- The em dash (U+2014) is not encodable in the win32 console codepage, so Python's stderr
  `backslashreplace` handler mangles it in transit. A Japanese-locale Windows console is
  cp932 with `errors='strict'`, so anything outside ASCII raises at the write rather than
  degrading — a log line that has not been sanitized fails loudly there (observed
  2026-08-20). An assertion over a hook's stderr
  matches the ASCII halves on either side of it rather than the character itself.
  Observed 2026-08-09.

## Host platform — macOS / BSD

- `/tmp` is a symlink to `/private/tmp`, so a path comparison against a temp directory
  needs `realpath` + `normcase` on both sides or it fails there and nowhere else.
  Observed 2026-08-05.
- `sed -i` requires an explicit empty suffix argument (`sed -i ''`) where GNU `sed` takes
  none, so the empty string is an option argument on this platform and a file operand on
  the other. Observed 2026-08-20.

## Toolchain — uv

- `uv run python <script>` does not parse a script's PEP 723 `# /// script` header; only
  `uv run --script <script>` (equivalently `uv run <script>`) resolves the dependencies
  declared in it. Under the wrong form an inline dependency raises `ModuleNotFoundError`,
  which a redirected or exit-code-ignored call swallows. Observed 2026-07-18.
- `uv run` resolves the enclosing project's environment from any cwd inside it, so a
  script launched from a temp directory that still sits under this repo picks up this
  repo's `pyproject`. `uv run --no-project` is what makes a hook run against the stdlib
  alone. Observed 2026-08-20.

## Real-world command corpus

- `>|` (the noclobber override) had 0 genuine occurrences in 13,824 Bash commands from a
  single installation's history, measured 2026-08-20. That is why the ledger's redirect
  scan does not recognise it. One genuine occurrence in any consumer's corpus reopens it.

## Harness — Claude Code

- A PreCompact hook that writes anything to stdout invalidates Claude Code's
  precomputed-compaction reuse, so the common case (nothing pending) has to emit zero
  bytes, not a short line. Observed 2026-08-09.
- haiku-class models under-judge the `/progress` router's main-verb and word-boundary
  fallback; the router sampling suites need a sonnet-class model to measure the router
  rather than the model. Observed 2026-07-18.

## Sibling implementation — the Pi taskflow extension

- The `@notes` entry line and its auto-managed comment are a literal contract shared with
  the taskflow extension in the Pi repository, which is not part of this checkout: each
  side parses what the other writes. The two once diverged and could not read each other.
  Changing the entry form means changing both implementations and the shared spec, not
  relaxing the byte-exact assertion on either side. The mirror assertion lives in that
  repository's `binding.test.ts`. Verified 2026-08-08.

## The live machine — `_projects/_state/`

- The real state directory holds live sessions' data and is gitignored, so whatever a
  misdirected run deletes there is not recoverable by git.
- A live session writes and consumes its own `<sid>.touched` continuously, so the
  directory's file count churns while a suite runs: measured 471 → 472 → 471 inside one
  run on 2026-08-21. A before/after file count against the real directory is therefore
  second-order evidence, and a red count is re-run before it is read as a leak.
- Attribution needs a lookup by the full 36-character synthetic session id. A 2–4
  character prefix collides with live sessions: a live `f005be44-….json` made an `f0`
  prefix check false-positive on 2026-08-21.
