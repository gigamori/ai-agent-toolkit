# Test constraints — the standalone skills

Facts about the world outside this repository that the test suites under `skills/` depend
on. A test file cannot state these: it is read by agents that hold only this checkout, and
a claim about a host platform, a third-party CLI, or a measured model behaviour cannot be
checked from here. They are collected once, with the date each was observed and how.

This is the single constraints document for the `skills/` test-knowledge gate
([`test-gate.md`](test-gate.md)). Entries are allowed to go stale — whoever next finds one
dead deletes it. Nothing links back to it and no check fails when it drifts.

## Host platform — win32 with a Japanese locale (cp932)

- The byte `0x8f` is a valid cp932 lead byte and is not valid UTF-8 on its own. Decoded
  through the host locale on such a machine it produced garbage; decoded with
  `errors="strict"` it kills `subprocess`'s reader thread outright, so the caller gets
  `stdout is None` and hands that to `json.loads`. Observed 2026-08-18 while fixing the
  child-CLI decode path.
- cp932 has no representation for characters such as an emoji, so encoding a prompt
  through the locale codec raises `UnicodeEncodeError` before the child process ever sees
  it. `encoding=` governs stdin as well as stdout. Observed 2026-08-18.
- A host whose locale codec is already UTF-8 cannot observe either failure — the same code
  is correct there. That is why the launcher call sites are pinned by assertions over the
  source text and not by behaviour alone. Observed 2026-08-18.
- Role and rules bodies in this repository are routinely Japanese, so a system-prompt file
  written with the platform default encoding is corrupted silently on such a host.
  Observed 2026-07-28.

## The claude CLI (`claude -p`)

- A non-zero exit still carries a fully formed error JSON on stdout. Discarding stdout on
  a non-zero exit throws away the only classification signal there is. Observed
  2026-07-28.
- `subtype: "success"` is emitted together with `is_error: true`, so subtype is not a
  success signal. Observed 2026-07-28.
- `permission_denials` was observed with `returncode` 0 and `is_error: false`: a denial is
  invisible to both. Observed 2026-07-28.
- A denial entry's `tool_input.command` carries the command that was refused. `tool_name`
  alone cannot separate "reached for a tool it was never granted" from "sent a command no
  allow-prefix could match" — a distinction an eval spent three runs failing to make.
  Observed 2026-08-12.
- An `is_error` reply whose `terminal_reason` is `api_error` carries `api_error_status`;
  529 (overloaded) is retryable, a 4xx such as 404 is not. Observed 2026-07-28.
- An npm install on Windows puts a `claude.cmd` shim on PATH. The real executable sits
  beside it at `node_modules/@anthropic-ai/claude-code/bin/claude.exe`, and a file smaller
  than 4096 bytes at that path is a stub rather than the real binary. A prompt containing
  shell metacharacters cannot be passed safely through the shim. Observed 2026-07-28.
- `--append-system-prompt-file` is absent from older installs. The probe separates
  "unknown option" (unsupported) from "file not found" (supported); anything else is
  indeterminate. Observed 2026-07-28.

## The pi CLI (`pi -p`)

- On Windows the `pi.CMD` npm shim silently truncates a multi-line prompt at the first
  newline. The launcher therefore resolves node plus the package entry point instead, and
  refuses to run at all when it cannot. Observed 2026-07-29.
- An open stdin pipe makes `pi -p` block forever before dispatch; it has to be launched
  with stdin closed. Observed 2026-07-29.
- `@file` on the command line attaches the file as content to reason about rather than as
  the turn's instruction — a probe that used it drew a prompt-injection refusal. The
  prompt has to ride on argv. Observed 2026-07-29.
- A reply to a JSON-shaped question comes back wrapped in a ```json code fence often
  enough that the brace-extraction pass is load bearing. Observed 2026-07-29.
- A tool-using step emits one `turn_end` per agent-loop iteration. The intermediate ones
  carry `stopReason: "toolUse"` and an empty content list; only the last carries the
  reply. Reading the first classified every tool-using step as an empty result. Observed
  2026-07-30.
- `turn_end` usage is per-iteration, not a running total, so it has to be summed. Real
  values from one captured tool-using step: input 2247 then 2438, output 189 then 160.
  Observed 2026-07-30.
- pi's model matcher falls back to a substring match on the model id, so the canonical
  name `opus` reaches `opus[1m]`. An exact-equality check would reject the default
  adjudicator on every pi workflow. Observed 2026-07-30.
- pi has no per-command tool matching, so a Claude-Code-style `Bash(git:*)` specifier can
  only be widened to the whole tool. Observed 2026-07-30.
- pi leaves a tool-spawned child alive when the parent is reaped by a plain
  `subprocess.run(timeout=)`: a Bash-tool `sleep 120` survived after node.exe was gone.
  The launcher has to kill the process tree. Observed 2026-07-30.
- The `pi -p --mode json` stream fixtures under
  `skills/mode-orchestrator/scripts/fixtures/` are carved from a real stream captured
  against pi 0.84.1 on 2026-08-13. Only long text and thinking bodies were trimmed and
  toolResults emptied; every key and nesting level is as measured.

## Claude Code transcripts — how a permission denial is serialized

- The current template family puts the phrase in the transcript line's `toolUseResult`
  field, not in the `tool_result` content: `Error: Permission to use Bash has been
  denied. …` and `Error: Permission to use Bash with command <cmd> has been denied.` Both
  were real Bash denials that the two older phrasings missed completely. Measured
  2026-08-12 from two real transcripts.
- The two older phrasings still occur: `Permission for this action was denied by the
  Claude Code auto mode classifier.` and `The user doesn't want to proceed with this tool
  use.` Observed 2026-08-12.
- An ordinary tool failure uses the same `is_error: true` line shape, so a detector keyed
  on `is_error` alone reports every failed command as a denial. Observed 2026-08-12.

## Measured model behaviour — prompt protocols

- A prose rule asking a step to report its value and nothing else produced 0 bare values
  in 28 samples across three substrates; every sample was prose carrying the number.
  Labeled lines are the only steering this prompt has actually achieved. Measured
  2026-08-18.
- Steps reporting `work-state: complete` omitted the `output:` field in 3 of 6 samples
  while posing the fork correctly every time. Measured 2026-08-13.
- Of 28 value-typed decision payloads, 0 carried a value the ruling could have chosen; 23
  were self-invalidating prose. Measured 2026-08-13.
- 7 of 45 unambiguous steps wrapped a completion report in the decision channel instead of
  reporting completion. Measured 2026-08-13.

Every count above is a sample from one harness on one date, not a rate that holds for
another model or another prompt. Re-measure before relying on one; the harnesses that
produced them live in `skills/xml-wf/scripts/evals/`.
