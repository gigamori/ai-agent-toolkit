#!/usr/bin/env python3
"""
Stop hook: bind this session's task work to each owning task's `@log` block as
a `- <ISO8601 T> [s:<sid8>]: <summary>` line, with an LLM reminder (Round1) and
a deterministic hook backstop (Round2).

Design: project-notes/specs/exec-binding.md (PostToolUse capture + exec-binding
redesign of the W1 sid-binding gate `sid-binding-gate.md`).

Detection (§3.1/§3.2): `touched` is read from the per-session
`<STATE_DIR>/<session_id>.touched` ledger written by the PostToolUse hook
`touched_capture.py` — NOT from a jsonl scan or git diff. PostToolUse fires for
Agent-tool subagent / fork internal writes with the PARENT session_id
(TBD-1 probe 2026-06-28), so subagent writes (P3) are already in `.touched`.
The `.touched` read is tolerant: a torn trailing line is dropped.

Invariants (§2):
  - INV-1 (no-loop): the gate returns `block` ONLY to (a) emit a Round1
    reminder for a not-yet-reminded missing task, or (b) report a successful
    auto-bind. It NEVER blocks on the raw "task is missing" condition (which can
    persist when `@log:end` is absent or a write fails).
  - INV-2 (no-deadlock): `@log` writes use the bounded `log_lock`; one lock at a
    time.
  - INV-3 (idempotent): the ledger is the actual presence of a `[s:<sid8>]` line
    inside a task md's `<!-- @log:begin/end -->` block, recomputed every Stop.

exec-binding (§3.4): the terminal agent may carry owning tasks whose work landed
OUTSIDE `tasks/` via a `[tasks: a.md b.md]` leading line. This hook regex-reads
it from `last_assistant_message`, union-merges into `state['exec_bind']`, and
deterministically binds each owning task (skip+log + `.bind` record on failure,
to stop retrying — INV-1). Under fork it skips (W2 delegation).

Round state lives in a sidecar `{session_id}.bind` (`reminded` rounds +
`exec_tried`), kept separate from the state JSON so concurrent rewrites by other
hooks cannot clobber it. A 7-day cleanup prunes stale `.bind` / `.touched` /
legacy `.captured` sidecars.
"""
import datetime
import json
import os
import re
import sys

# Sibling import: the shared per-task advisory lock helper lives next to this
# hook. Hook scripts run standalone (no package context), so add this file's
# own directory to sys.path before importing it.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from log_lock import log_lock  # noqa: E402

PROGRESS_ROOT = os.path.join(os.getcwd(), '_projects')
STATE_DIR = os.path.join(PROGRESS_ROOT, '_state')

MAX_TOUCHED_IN_INJECTION = 30
# `[tasks:]` exec-binding carry must appear in the leading lines; accept the
# marker only when it starts within this window (LLM-non-exposed code bound;
# exec-binding.md §9, leading-lines-terminology.md). Overflow truncation of a
# very long task list is accepted.
EXEC_PARSE_WINDOW = 500

_MARKER_MAX_AGE_DAYS = 7
# Sidecars swept by the same 7-day mechanism. `.captured` is retained so markers
# left by a pre-Gate-C hook version are still pruned; the current hook writes
# `.bind` (round state) and `touched_capture.py` writes `.touched`.
_CLEANUP_SUFFIXES = ('.captured', '.bind', '.touched')

# task md files live under `tasks/<status>/*.md` within a project.
_TASK_PATH_RE = re.compile(r'(?:^|/)tasks/[^/]+/[^/]+\.md$', re.IGNORECASE)
_LOG_BLOCK_RE = re.compile(
    r'<!--\s*@log:begin\s*-->(.*?)<!--\s*@log:end\s*-->', re.DOTALL
)
_LOG_END_RE = re.compile(r'<!--\s*@log:end\s*-->')
# exec-binding carry: `[tasks: a.md b.md]` (space-separated basenames).
_TASKS_RE = re.compile(r'\[tasks:\s*([^\]]+)\]')


def normalize_path(p: str, cwd: str) -> str:
    if not p:
        return p
    p = p.replace('\\', '/')
    cwd_norm = cwd.replace('\\', '/').rstrip('/')
    # Case-insensitive prefix match (Windows paths are case-insensitive).
    if p.lower().startswith(cwd_norm.lower() + '/'):
        return p[len(cwd_norm) + 1:]
    return p


def read_touched(touched_path: str, cwd: str) -> list[str]:
    """Read the `.touched` ledger (one normalized path per line) → deduped list.

    Tolerant parse (§3.2): a torn trailing line (no terminating newline, which a
    concurrent best-effort append can leave) is dropped; blank lines skipped.
    """
    if not os.path.isfile(touched_path):
        return []
    out: list[str] = []
    seen: set[str] = set()
    try:
        with open(touched_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                if not line.endswith('\n'):
                    continue  # torn/partial trailing line — drop
                p = line.strip()
                if not p:
                    continue
                n = normalize_path(p, cwd)
                if n and n not in seen:
                    seen.add(n)
                    out.append(n)
    except OSError:
        pass
    return out


def _cleanup_stale_markers(state_dir: str) -> None:
    """Remove sidecar marker files older than _MARKER_MAX_AGE_DAYS
    (`.bind` / `.touched`, plus legacy `.captured`)."""
    try:
        cutoff = datetime.datetime.now().timestamp() - _MARKER_MAX_AGE_DAYS * 86400
        for name in os.listdir(state_dir):
            if not name.endswith(_CLEANUP_SUFFIXES):
                continue
            path = os.path.join(state_dir, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


def _is_task_md(rel_path: str) -> bool:
    """True if a (repo-relative, forward-slashed) path is a task md under
    `tasks/<status>/`."""
    return bool(_TASK_PATH_RE.search(rel_path))


def _task_basename_index(project_root: str) -> dict:
    """Build {basename: absolute_current_path} for every task md under
    `<project_root>/tasks/` (P8: a task may move status folders this session;
    basenames are expected unique across status folders)."""
    tasks_root = os.path.join(project_root, 'tasks')
    current: dict = {}
    if os.path.isdir(tasks_root):
        for dirpath, _dirs, files in os.walk(tasks_root):
            for fn in files:
                if fn.lower().endswith('.md'):
                    current[fn] = os.path.join(dirpath, fn)  # last writer wins
    return current


def resolve_touched_tasks(touched, project_root):
    """Resolve each touched task md to its CURRENT on-disk location by basename
    (P8). Returns {basename: absolute_current_path}; a deleted task is dropped."""
    current = _task_basename_index(project_root)
    resolved = {}
    for rel in touched:
        rel_fs = rel.replace('\\', '/')
        if not _is_task_md(rel_fs):
            continue
        base = os.path.basename(rel_fs)
        cur = current.get(base)
        if cur:
            resolved[base] = cur
    return resolved


def resolve_exec_tasks(basenames, project_root):
    """Resolve exec-bind owning-task basenames to CURRENT on-disk paths (P8)."""
    if not basenames:
        return {}
    current = _task_basename_index(project_root)
    resolved = {}
    for b in basenames:
        base = os.path.basename(str(b).replace('\\', '/'))
        cur = current.get(base)
        if cur:
            resolved[base] = cur
    return resolved


def log_block_has_sid(path, sid8):
    """True if the task md at `path` already holds a `[s:<sid8>]` line inside its
    `<!-- @log:begin/end -->` block. This is the ledger (INV-3)."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError:
        return False
    m = _LOG_BLOCK_RE.search(content)
    block = m.group(1) if m else ''
    return f'[s:{sid8}]' in block


def append_auto_binding(path, sid8, iso_ts, note='(auto) touched; summary pending'):
    """Code-append a `- <iso_ts> [s:<sid8>]: <note>` line immediately before the
    `<!-- @log:end -->` marker. Append-only; never edits existing lines.
    Returns True on success.

    The read-modify-write is serialized through the shared bounded advisory lock
    (`log_lock`, INV-2). Residual gap: an LLM Edit-tool append at the tool layer
    cannot take this lock — the known R-lock gap (exec-binding.md §3.5 / R1)."""
    with log_lock(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
        except OSError:
            return False
        m = _LOG_END_RE.search(content)
        if not m:
            return False
        line = f'- {iso_ts} [s:{sid8}]: {note}\n'
        insert_at = m.start()
        prefix = content[:insert_at]
        if prefix and not prefix.endswith('\n'):
            line = '\n' + line
        new_content = prefix + line + content[insert_at:]
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(new_content)
        except OSError:
            return False
        return True


def _load_bind(bind_path):
    try:
        with open(bind_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {'reminded': {}, 'exec_tried': []}
    reminded = data.get('reminded')
    exec_tried = data.get('exec_tried')
    return {
        'reminded': reminded if isinstance(reminded, dict) else {},
        'exec_tried': exec_tried if isinstance(exec_tried, list) else [],
    }


def _save_bind(bind_path, reminded, exec_tried):
    try:
        with open(bind_path, 'w', encoding='utf-8') as f:
            json.dump({'reminded': reminded, 'exec_tried': exec_tried},
                      f, ensure_ascii=False)
    except OSError:
        pass


def merge_exec_bind(state, state_path, data):
    """Parse a `[tasks: a.md b.md]` carry from the leading lines of
    `last_assistant_message` and union-merge the basenames into
    `state['exec_bind']` (append-only; durable within this session). Returns the
    current exec_bind list. Persists state.json on change."""
    existing = state.get('exec_bind')
    existing = list(existing) if isinstance(existing, list) else []
    msg = data.get('last_assistant_message', '')
    if not isinstance(msg, str):
        return existing
    m = _TASKS_RE.search(msg)
    if not m or m.start() >= EXEC_PARSE_WINDOW:
        return existing
    new = [t for t in m.group(1).split() if t.endswith('.md')]
    merged = list(existing)
    for t in new:
        b = os.path.basename(t.replace('\\', '/'))
        if b not in merged:
            merged.append(b)
    if merged != existing:
        state['exec_bind'] = merged
        try:
            with open(state_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False)
        except OSError:
            pass
        return merged
    return existing


def _rel(path, cwd):
    return os.path.relpath(path, cwd).replace('\\', '/')


def main() -> int:
    if not os.path.isdir(PROGRESS_ROOT):
        return 0
    _cleanup_stale_markers(STATE_DIR)
    try:
        data = json.loads(sys.stdin.buffer.read().decode('utf-8'))
    except Exception:
        return 0
    session_id = data.get('session_id', '')
    if not session_id:
        return 0
    state_path = os.path.join(STATE_DIR, f'{session_id}.json')
    if not os.path.exists(state_path):
        return 0
    try:
        with open(state_path, 'r', encoding='utf-8') as f:
            state = json.load(f)
    except Exception:
        return 0

    bind_path = os.path.join(STATE_DIR, f'{session_id}.bind')

    # --- State recovery: pj prefix from assistant response ---
    # If state has no project but the assistant's response contains a
    # [pj:<name>] line in the leading lines, recover the project into state.
    # The [pj:...] line may not be on line 1 — it shares the leading-line region
    # with other plugins' leading lines (e.g. [Mode:]) and the order is
    # unspecified. Self-heals cases where session_init failed to write the
    # project on the first turn or fork inherited an empty parent state.
    if not state.get('project'):
        assistant_msg = data.get('last_assistant_message', '')
        if isinstance(assistant_msg, str):
            # Bound the search to the leading-line region (a few short lines):
            # [pj:...] always lands well within the first 200 chars whatever the
            # line order. Narrowing the window also avoids matching a literal
            # [pj:...] token deeper in the body.
            pj_m = re.search(r'\[pj:([^\]]+)\]', assistant_msg[:200])
            if pj_m:
                val = pj_m.group(1)
                if val and val not in ('(none)', '?', 'none'):
                    if os.path.isdir(os.path.join(PROGRESS_ROOT, val)):
                        state['project'] = val
                        try:
                            with open(state_path, 'w', encoding='utf-8') as f:
                                json.dump(state, f, ensure_ascii=False)
                        except OSError:
                            pass

    project = state.get('project', '')
    if not project:
        return 0
    if not os.path.isdir(os.path.join(PROGRESS_ROOT, project)):
        return 0

    project_root = os.path.join(PROGRESS_ROOT, project)
    cwd = os.getcwd()
    sid8 = session_id[:8]
    # ISO8601 with `T` separator (second resolution).
    iso_ts = datetime.datetime.now().replace(microsecond=0).isoformat()
    date = datetime.date.today().isoformat()

    # --- touched (from the .touched ledger; tolerant + dedup) ---
    touched = read_touched(os.path.join(STATE_DIR, f'{session_id}.touched'), cwd)
    resolved = resolve_touched_tasks(touched, project_root)

    # --- exec-binding carry: merge [tasks:] → state.exec_bind, resolve (P8) ---
    is_fork = bool(state.get('parent_session_id'))
    exec_bind = merge_exec_bind(state, state_path, data)
    exec_resolved = resolve_exec_tasks(exec_bind, project_root)

    bind = _load_bind(bind_path)
    reminded = bind['reminded']
    exec_tried = bind['exec_tried']

    # `auto_bound`: list[str rel] code-appended this Stop (Round2 + exec). Drives
    # the (b) report; INV-1.
    auto_bound: list[str] = []
    # `exec_skipped`: NEW exec-bind skips this Stop (no @log:end / write fail).
    # Surfaced once via the injection (F5 / AC-7) then suppressed by exec_tried,
    # so a given task is reported at most once — bounded (INV-1 c).
    exec_skipped: list[str] = []

    # --- exec-binding bind (deterministic; §3.4) ----------------------------
    # Each resolved owning task missing its [s:sid8] line is bound directly by
    # the hook. Fork → skip (W2 delegation; guard inert per U3 but kept). On
    # bind failure (no @log:end / write fail) → skip+log + record in exec_tried
    # so we do not retry every Stop (打止め). INV-1: never blocks on missing.
    if not is_fork:
        for base, path in exec_resolved.items():
            rel = _rel(path, cwd)
            if log_block_has_sid(path, sid8):
                continue  # idempotent
            if rel in exec_tried:
                continue  # already tried and failed
            if append_auto_binding(
                path, sid8, iso_ts,
                '(auto) executed via [tasks:] carry; summary pending',
            ):
                auto_bound.append(rel)
            else:
                exec_tried.append(rel)
                exec_skipped.append(rel)
                print(f'[progress capture] auto-skip(ambiguous): {rel} — exec-bind '
                      f'failed (no @log block / write failed); left unbound.',
                      file=sys.stderr)

    # --- ledger: which touched task md still lack a [s:sid8] line? ----------
    missing = {
        base: path
        for base, path in resolved.items()
        if not log_block_has_sid(path, sid8)
    }

    # --- Round2 backstop: touched tasks reminded once (round==1) still missing
    for base, path in list(missing.items()):
        rel = _rel(path, cwd)
        if reminded.get(rel) == 1:
            if append_auto_binding(path, sid8, iso_ts):
                reminded[rel] = 2  # stop escalating this task
                auto_bound.append(rel)
                missing.pop(base, None)

    # --- Round1: touched tasks missing and not yet reminded → block-reminder.
    round1_targets = []
    for base, path in missing.items():
        rel = _rel(path, cwd)
        if rel not in reminded:
            round1_targets.append((rel, path))

    # --- Gate (INV-1): block only to (a) Round1-remind, (b) report an auto-bind,
    # or (c) report a NEW exec-bind skip (bounded — exec_tried suppresses re-report).
    if not round1_targets and not auto_bound and not exec_skipped:
        # Nothing to remind, nothing auto-bound, no new skip. Persist bind state
        # (exec_tried may have grown) and exit without injecting. A still-missing
        # task with no @log:end does NOT block (no-loop).
        _save_bind(bind_path, reminded, exec_tried)
        return 0

    for rel, _path in round1_targets:
        reminded[rel] = 1
    _save_bind(bind_path, reminded, exec_tried)

    # --- F5 observability (AC-7): one line per auto-binding and per exec-skip.
    auto_lines = ''.join(
        f'[progress capture] auto-bound: {rel} [s:{sid8}]\n' for rel in auto_bound
    )
    auto_lines += ''.join(
        f'[progress capture] auto-skip(ambiguous): {rel}\n' for rel in exec_skipped
    )

    if round1_targets:
        shown = touched[:MAX_TOUCHED_IN_INJECTION]
        tail = '' if len(touched) <= MAX_TOUCHED_IN_INJECTION else \
            f' ...({len(touched) - MAX_TOUCHED_IN_INJECTION} more)'
        reason = (
            f'{auto_lines}'
            f'[progress capture] session={sid8} date={date}\n'
            f'touched: {" ".join(shown)}{tail}\n\n'
            f'Procedure (do not shortcut):\n'
            f'1. For every `tasks/<status>/*.md` in `touched`: locate the task in '
            f'its CURRENT folder (it may have moved this session). If its '
            f'`<!-- @log:begin/end -->` block does not already contain a line '
            f'tagged `[s:{sid8}]`, APPEND-ONLY a line of exactly this form before '
            f'`<!-- @log:end -->`: `- {iso_ts} [s:{sid8}]: <one-line summary>` '
            f'(ISO8601, `T` separator, do not edit existing lines). Adjust '
            f'`## Next Steps`: write remaining items, or clear (header only) if '
            f'the task is complete. If no task file exists for work you did, '
            f'propose creating one: state the suggested title, status (TODO / In '
            f'Progress / Done), and reason for that status. Do NOT create the '
            f'file yet — wait for user confirmation. If the user ignores the '
            f'proposal, do not create it.\n'
            f'2. For `touched` paths outside `tasks/` (source files, specs, '
            f'configs): map each to the owning task (by scope, any status) and '
            f'update per (1). Bug fixes and verification-driven tweaks ARE task '
            f'progress.\n'
            f'3. Reply `[progress capture] skip — no task work` IF every '
            f'`touched` entry is unrelated to any task in this project (e.g., '
            f'transient scratch files, generated artifacts, project-notes). If a '
            f'mapping is ambiguous, skip rather than force-assign.\n'
            f'4. After completing updates, reply with exactly this format (one '
            f'line per updated task):\n'
            f'   [progress capture] done\n'
            f'   - <task-filename> ← [s:{sid8}] logged\n'
            f'   If you proposed a new task (not yet created), append:\n'
            f'   - (proposed) <suggested-title> — <status> — awaiting '
            f'confirmation\n'
            f'   If no task was updated, the skip message from step 3 serves as '
            f'the output.'
        )
    else:
        # Only auto-binds / exec-skips to report (INV-1 b/c); no touched task
        # needs an LLM summary this turn.
        note = '(reported above. '
        if exec_skipped:
            note += ('auto-skip = the [tasks:] target has no writable '
                     '<!-- @log:begin/end --> block; add one to it if that task '
                     'should carry a session log. ')
        if auto_bound:
            note += 'auto-bound entries need no further action. '
        note = note.rstrip() + ')'
        reason = (
            f'{auto_lines}'
            f'[progress capture] session={sid8} date={date}\n'
            f'{note}'
        )

    result = {'decision': 'block', 'reason': reason}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
    sys.stdout.buffer.write(b'\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
