# llm-wiki test constraints (v0.1.4)

Facts about the world outside this repository that `plugins/llm-wiki/tests/` depends on.
A test file cannot state these: it is read by agents that have only this checkout, and a
claim about a host platform, a database engine, a third-party binary, another harness, or
the live machine cannot be checked from here. They are collected once, with the date they
were observed and how.

This is the single constraints document for the `plugins/llm-wiki/tests/` gate. Entries are
allowed to go stale — whoever next finds one dead deletes it. Nothing links back to it and
no check fails when it drifts.

Dates below are the date each fact first entered the test suite, read from git history
rather than from the comment that carried it.

## Host platform — Windows

- Piped Python stdio defaults to the host ANSI codepage, which is cp932 on a
  Japanese-locale machine. A verb that reads page content from stdin therefore dies with
  `UnicodeDecodeError`, or silently mojibakes, unless the entrypoint reconfigures its
  streams to UTF-8. `PYTHONIOENCODING=cp932` plus `PYTHONUTF8=0` in a child process
  reproduces that hostile locale on any host OS, which is how the contract is tested
  deterministically off Windows. Observed 2026-07-07.
- U+20BB7 (𠮷) is not representable in cp932, so any non-UTF-8 hop drops or corrupts it.
  That is what makes it a discriminating canary rather than a decorative non-ASCII sample.
  Observed 2026-07-07.
- `subprocess`'s `encoding=` governs stdin as well as stdout, so a call that pins strict
  UTF-8 for its captured output also pins how the input text reaches the child. Observed
  2026-08-18.
- Both DuckDB and `os.path.expanduser` resolve `~` from `USERPROFILE`, not `HOME`, so a
  fixture that redirects only `HOME` silently leaves `~` pointing at the real profile.
  Probed 2026-07-29.
- `[` and `]` are legal characters in a directory name, while DuckDB reads them as glob
  metacharacters. An unescaped path with brackets passes a `pathlib` existence check and
  then matches nothing in DuckDB. Observed 2026-07-29.
- A temp path reconstructed from parts (`AppData\Local\Temp\...`) is resolved against the
  current working directory rather than treated as absolute, which is why the driver hands
  back absolute paths instead of letting a caller rebuild them. Observed 2026-07-13.

## Toolchain — DuckDB

- A glob that matches nothing aborts `CREATE VIEW` outright, so an empty universe must
  never be injected into the view SQL. Probed 2026-07-29 against duckdb 1.5.5.
- A native DuckDB runtime keeps its own copy of the environment block and does not observe
  an in-process `os.environ` change. With `USERPROFILE` monkeypatched in the parent, a
  `~/...` glob still resolved to the real profile and read the live corpus, so a hermetic
  test of the default universe has to spawn a child process. Measured 2026-07-29.
- Version 1.5.4 eagerly evaluates the ARRAY-cast subquery of a `CASE` expression whatever
  branch is taken, so a plain-string value raised `ConversionException` from the branch
  that was not selected. `try_cast` returns NULL instead and is what the view SQL uses.
  Observed 2026-07-03.

## Third-party — qmd

- The `--json` output's `file` field is `qmd://` followed by an absolute native path
  (observed against qmd 2.5.3), which is why the wrapper reconstructs a wiki-relative path
  rather than reading one out of the payload. Observed 2026-07-03.
- The live hybrid query path downloads and loads multi-gigabyte models. Unit tests point
  the wrapper at a binary name that cannot resolve, which exercises the availability
  predicate and the loud-announce fallback without installing anything. Observed
  2026-07-03.
- The npm distribution installs a `qmd.CMD` shim on Windows, so invoking a bare `qmd`
  fails with WinError 2 and the binary has to be resolved through `shutil.which`. The shim
  also locates its project index from `PWD`, which `subprocess`'s `cwd=` does not set, so
  the child environment carries `PWD` explicitly. Observed 2026-07-03.

## Harness — Claude Code

- The running session's id is published as `CLAUDE_CODE_SESSION_ID`; `CLAUDE_SESSION_ID`
  is the prompt-template substitution name and is unset in the process environment. Probed
  2026-07-10: the first was set with length 36, the second absent.
- `CLAUDE_CONFIG_DIR` is taken literally: a value of `~/cfgtest` produced
  `<cwd>/~/cfgtest`, a directory actually named `~`. A reader that expands the tilde looks
  somewhere the harness never writes. Observed 2026-07-29.
- Session-log records carry `isMeta` on more than the harness's own noise: an expanded
  skill body is a user-role text record with `isMeta` true, and so is human steering typed
  mid-turn. A locally handled slash command emits three records of which only the caveat
  one carries the flag. That is why a drop verdict is the conjunction of the flag and a
  content denylist, and never the flag alone. Measured 2026-08-12.
- The session store is the live machine's own log corpus. It is not hermetic and it grows
  while a suite runs, so the projector tests stub the extraction seam rather than reading
  it. Observed 2026-07-03.

## Harness — Pi

- A user message's content is typed `string | (TextContent | ImageContent)[]`, and both
  shapes occur in real session data, so the reader has to handle a plain string as well as
  the block array. Observed 2026-07-03.
- The prompt template of a slash command is expanded BEFORE the turn is persisted, so an
  invocation turn's stored text is the command's whole prompt body. Left in place, the next
  run of the same command files those lines as conversation content. Measured 2026-08-12.

## Sibling implementations

- The Python core is vendored into the Pi extensions repository as `packages/llm-wiki`,
  which is not part of this checkout. Test fixtures therefore resolve package assets
  relative to the package root rather than to this repository's root, so the same file
  works in both trees. Observed 2026-07-03.
- `hooks/` and the canonical `skills/inspect-cc-log/scripts/views.sql` are Claude Code-only
  assets that a Pi-style harness does not carry. The tests that need them skip rather than
  fail when they are absent. Observed 2026-07-03.
- `file --content-file` exists because pi-studio's bundled-tooling review exemption cannot
  admit a stdin-fed call; Claude Code has no such review. The flag is carried here as
  shared-core parity, not because this harness needs it. Observed 2026-08-08.
