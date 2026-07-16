#!/usr/bin/env python3
"""
Stop hook: bind this session's task work to each owning task's `@log` block as
a `- <ISO8601 T> [s:<sid8>]: <summary>` line. Owner judgment (touched-task
summaries, note→task links) is delegated to an async capture subagent and
applied deterministically here; a deterministic hook backstop guarantees a
binding when the subagent is absent.

Design: project-notes/specs/exec-binding.md (PostToolUse capture + exec-binding)
and project-notes/specs/note-task-link.md §10 (the shared async apply-path that
supersedes the inline Round1 reminder — option-a / §11 R-round1-relationship).

Detection (§3.1/§3.2): `touched` is read from the per-session
`<STATE_DIR>/<session_id>.touched` ledger written by the PostToolUse hook
`touched_capture.py` — NOT from a jsonl scan or git diff. PostToolUse fires for
Agent-tool subagent / fork internal writes with the PARENT session_id
(TBD-1 probe 2026-06-28), so subagent writes (P3) are already in `.touched`.
The `.touched` read is tolerant: a torn trailing line is dropped.

Async apply-path (note-task-link.md §10): when a touched task still needs a
summary or a freshly-written project-notes deliverable has no owning task yet,
the gate (E) commits `capture.status=requested` and blocks with an instruction
to spawn the `taskflow:progress-capture` subagent. That subagent writes a
`<sid>.capture` JSON sidecar; a later Stop (A) applies it — `confirmed` →
`@log` summaries (`append_auto_binding`), `note_links` → task `@notes`
(`append_note_link`, Phase A) — then consumes it. If no sidecar appears within
`_CAPTURE_EXPIRY_S` (§10.4) the request expires and the deterministic G backstop
(D) takes over: placeholder-bind every still-missing touched task and
`referenced` over-bind note-write owners known via the reverse index.

Invariants (§2 / §10):
  - INV-1 (no-loop): the gate returns `block` ONLY to (b) report a deterministic
    bind, (c) report a NEW exec-bind skip, or (d) spawn capture / surface
    proposals. `requested` is committed before the spawn-block, so the next Stop
    re-enters via the requested/pending branch and never re-blocks; an in-flight
    `pending` with nothing to report does not block (AC-9). It NEVER blocks on
    the raw "task is missing" condition.
  - INV-2 (no-deadlock): `@log` / `@notes` writes use the bounded `log_lock`.
  - INV-3 (idempotent): the ledger is the actual presence of a `[s:<sid8>]` line
    inside a task md's `<!-- @log:begin/end -->` block, recomputed every Stop;
    apply / backstop are idempotent and eventual (AC-11).

exec-binding (§3.4): the terminal agent may carry owning tasks whose work landed
OUTSIDE `tasks/` via a `[tasks: a.md b.md]` leading line. This hook regex-reads
it from `last_assistant_message`, union-merges into `state['exec_bind']`, and
deterministically binds each owning task (skip+log + `.bind` record on failure,
to stop retrying — INV-1). Under fork it skips (W2 delegation).

Round / lifecycle state lives in a sidecar `{session_id}.bind` (`reminded`,
`exec_tried`, and the `capture` lifecycle `{status, items, requested_ts,
tried_notes}` — writer = this hook only, §10.1), kept separate from the state
JSON so concurrent rewrites by other hooks cannot clobber it. A 7-day cleanup
prunes stale `.bind` / `.touched` / `.capture` / legacy `.captured` sidecars.
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
from note_links import (  # noqa: E402
    _AUTO_COMMENT,
    NOTES_BEGIN,
    NOTES_END,
    append_note_link,
    build_reverse_index,
    is_note_deliverable,
    normalize_note_rel,
    resolve_note_owner,
)
from tstamp import now_iso  # noqa: E402

PROGRESS_ROOT = os.path.join(os.getcwd(), '_projects')
STATE_DIR = os.path.join(PROGRESS_ROOT, '_state')

# Async capture subagent (spec §10.5). The Stop gate emits a block instruction
# to spawn this agent type; the subagent writes a `<sid>.capture` sidecar that a
# later Stop applies deterministically.
CAPTURE_AGENT_TYPE = 'taskflow:progress-capture'
# Capture expiry (§10.4): a requested capture whose sidecar has not appeared
# within this many seconds (measured at Stop firing) is declared expired and the
# deterministic G backstop takes over. 15s fixed by design; env-overridable so
# tests can force immediate expiry (mirrors log_lock's TASKFLOW_LOCK_TIMEOUT).
try:
    _CAPTURE_EXPIRY_S = float(os.environ.get('TASKFLOW_CAPTURE_EXPIRY_S', '15.0'))
except ValueError:
    _CAPTURE_EXPIRY_S = 15.0

MAX_TOUCHED_IN_INJECTION = 30
# `[tasks:]` exec-binding carry must appear in the leading lines; accept the
# marker only when it starts within this window (LLM-non-exposed code bound;
# exec-binding.md §9, leading-lines-terminology.md). Overflow truncation of a
# very long task list is accepted.
EXEC_PARSE_WINDOW = 500

_MARKER_MAX_AGE_DAYS = 7
# Sidecars swept by the same 7-day mechanism. `.captured` is retained so markers
# left by a pre-Gate-C hook version are still pruned; the current hook writes
# `.bind` (round state) and `touched_capture.py` writes `.touched`. `.capture`
# is the async capture sidecar (§10.1): an orphan (expired, never consumed) is
# pruned here. `'.captured'.endswith('.capture')` is False, so the two suffixes
# do not collide.
_CLEANUP_SUFFIXES = ('.captured', '.bind', '.touched', '.capture')

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
    (P8). Returns {basename: absolute_current_path}; a deleted task or one whose
    resolved path falls outside this project's tasks/ directory is dropped."""
    current = _task_basename_index(project_root)
    tasks_prefix = os.path.normpath(os.path.join(project_root, 'tasks')) + os.sep
    resolved = {}
    for rel in touched:
        rel_fs = rel.replace('\\', '/')
        if not _is_task_md(rel_fs):
            continue
        base = os.path.basename(rel_fs)
        cur = current.get(base)
        if cur:
            # Boundary guard (F-L3): reject tasks not under this project's tasks/.
            if not os.path.normpath(cur).startswith(tasks_prefix):
                continue
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


def repair_log_markers(content):
    """Conservatively restore region markers destroyed by a hand edit inside
    `@log` (observed damage shape: an LLM Edit appends a log line by replacing
    the `<!-- @log:end -->` / `<!-- @notes:begin -->` boundary and drops the
    markers). Returns the repaired content, or None when the damage shape is
    ambiguous (repair only when there is exactly one `@log:begin` and no
    `@log:end`; anything else is left untouched).

    - `<!-- @log:end -->` is re-inserted before the `@notes` block (its begin
      marker, the auto-managed comment, or its end marker — whichever comes
      first after `@log:begin`), or at EOF when no `@notes` block follows.
    - `<!-- @notes:begin -->` is re-inserted before the auto-managed comment
      when it is missing while the comment and `@notes:end` survive.
    """
    if content.count('<!-- @log:begin -->') != 1 or _LOG_END_RE.search(content):
        return None
    begin_at = content.index('<!-- @log:begin -->') + len('<!-- @log:begin -->')
    if (NOTES_BEGIN not in content and _AUTO_COMMENT in content
            and NOTES_END in content):
        content = content.replace(_AUTO_COMMENT, f'{NOTES_BEGIN}\n{_AUTO_COMMENT}', 1)
    anchor = len(content)
    for marker in (NOTES_BEGIN, _AUTO_COMMENT, NOTES_END):
        at = content.find(marker, begin_at)
        if at != -1 and at < anchor:
            anchor = at
    tail = content[anchor:]
    head = content[:anchor]
    if head and not head.endswith('\n'):
        head += '\n'
    sep = '\n' if tail else ''
    return head + '<!-- @log:end -->\n' + sep + tail


def append_auto_binding(path, sid8, iso_ts, note='(auto) touched; summary pending'):
    """Code-append a `- <iso_ts> [s:<sid8>]: <note>` line immediately before the
    `<!-- @log:end -->` marker. Append-only; never edits existing lines.
    Returns True on success.

    When `@log:end` is missing (marker destroyed by a hand edit), a
    conservative `repair_log_markers` pass runs first; the repaired markers are
    persisted together with the appended line.

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
            repaired = repair_log_markers(content)
            if repaired is None:
                return False
            content = repaired
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
    """Round-trip the `.bind` sidecar. CLOSED whitelist — a key not re-emitted
    by `_save_bind` is silently dropped every Stop, so `capture` MUST be carried
    here or the §10 lifecycle never advances (spec §10.1 trap)."""
    try:
        with open(bind_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {'reminded': {}, 'exec_tried': [], 'capture': {}}
    reminded = data.get('reminded')
    exec_tried = data.get('exec_tried')
    capture = data.get('capture')
    return {
        'reminded': reminded if isinstance(reminded, dict) else {},
        'exec_tried': exec_tried if isinstance(exec_tried, list) else [],
        'capture': capture if isinstance(capture, dict) else {},
    }


def _save_bind(bind_path, reminded, exec_tried, capture):
    try:
        with open(bind_path, 'w', encoding='utf-8') as f:
            json.dump({'reminded': reminded, 'exec_tried': exec_tried,
                       'capture': capture}, f, ensure_ascii=False)
    except OSError:
        pass


# --- §10 async capture apply-path helpers ---------------------------------

def _to_project_rel(rel_repo: str, project: str) -> str:
    """Convert a repo-relative path to project-relative by stripping a leading
    `_projects/<project>/` (path convention, note_links.py module docstring).
    A path already without that prefix is returned forward-slashed."""
    r = str(rel_repo).replace('\\', '/')
    prefix = f'_projects/{project}/'
    if r.lower().startswith(prefix.lower()):
        return r[len(prefix):]
    return r


def _scan_note_writes(touched, project, project_root, reverse_index):
    """From `touched` (repo-relative write paths) return (note_writes, unlinked):
    project-relative deliverable notes written this session, and the subset whose
    reverse-index resolution is empty (no owning task yet → needs judgment, §3.2).
    """
    note_writes: list[str] = []
    unlinked: list[str] = []
    seen: set[str] = set()
    for rel in touched:
        prel = normalize_note_rel(_to_project_rel(rel, project))
        if not prel or prel in seen:
            continue
        # Boundary guard (F-L3): reject paths from other projects (not under
        # project-notes/ after project-relative conversion).
        if not prel.startswith('project-notes/'):
            continue
        if not is_note_deliverable(prel):
            continue
        seen.add(prel)
        note_writes.append(prel)
        if not resolve_note_owner(prel, project_root, reverse_index):
            unlinked.append(prel)
    return note_writes, unlinked


def _load_capture_sidecar(path):
    """Return the capture sidecar as a dict, or None if absent / torn / not a
    JSON object (§10.1: partial-apply forbidden — a torn write fails json.loads
    and is treated as absent, never partially applied)."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _apply_capture(sidecar, current_index, project, project_root, sid8, iso_ts):
    """Apply a validated capture sidecar deterministically (§10.3). All writes
    are idempotent (`log_block_has_sid` / `append_note_link` union). Returns
    (summaries, links, proposals, link_skipped) for observability:
      - summaries:    list[task-basename] @log-bound with a real one-line summary
      - links:        list[(note_rel, task-basename)] established in a task @notes
      - proposals:    list[str] surfaced (display-only; never auto-created)
      - link_skipped: list[(note_rel, task-basename)] where append_note_link returned False"""
    summaries: list[str] = []
    links: list[tuple] = []
    proposals: list[str] = []
    link_skipped: list[tuple] = []

    confirmed = sidecar.get('confirmed')
    if isinstance(confirmed, list):
        for item in confirmed:
            if not isinstance(item, dict):
                continue
            base = item.get('task')
            summ = item.get('summary')
            if not isinstance(base, str) or not isinstance(summ, str):
                continue
            base = os.path.basename(base.replace('\\', '/'))
            path = current_index.get(base)
            if not path or log_block_has_sid(path, sid8):
                continue  # missing/already bound — idempotent
            note = ' '.join(summ.split())[:200] or '(captured) summary pending'
            if append_auto_binding(path, sid8, iso_ts, note):
                summaries.append(base)

    note_links = sidecar.get('note_links')
    if isinstance(note_links, list):
        for item in note_links:
            if not isinstance(item, dict):
                continue
            note = item.get('note')
            base = item.get('task')
            if not isinstance(note, str) or not isinstance(base, str):
                continue
            if not base or base.strip().lower() == 'none':
                continue  # task==none → no-op (§10.3)
            base = os.path.basename(base.replace('\\', '/'))
            path = current_index.get(base)
            if not path:
                continue
            note_rel = normalize_note_rel(_to_project_rel(note, project))
            if not is_note_deliverable(note_rel):
                continue
            if append_note_link(path, note_rel):
                links.append((note_rel, base))
            else:
                link_skipped.append((note_rel, base))

    props = sidecar.get('proposals')
    if isinstance(props, list):
        for p in props:
            if isinstance(p, str) and p.strip():
                proposals.append(p.strip())

    return summaries, links, proposals, link_skipped


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
    # Offset-aware ISO8601 with `T` separator (second resolution). Shared
    # generation point with session_init.py's `iso_ts=` header field (tstamp.py)
    # so channel A and channel B never diverge on timezone again.
    iso_ts = now_iso()
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
    capture = bind['capture']

    # `auto_bound`: list[str rel] code-appended this Stop (exec + placeholder +
    # referenced). Drives the (b) report; INV-1.
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

    # === §10 async capture apply-path =======================================
    # Replaces the inline Round1 reminder (option-a, §10.2 / §11
    # R-round1-relationship): the touched-task summary and note→owner judgment
    # are delegated to an async `taskflow:progress-capture` subagent; the Stop
    # hook deterministically applies the sidecar it writes and keeps a
    # deterministic G backstop (placeholder + referenced over-bind) as fallback.
    capture_path = os.path.join(STATE_DIR, f'{session_id}.capture')
    current_index = _task_basename_index(project_root)
    status = capture.get('status') or ''
    tried_notes = capture.get('tried_notes')
    tried_notes = list(tried_notes) if isinstance(tried_notes, list) else []
    # tried_tasks: touched task basenames that went through a capture cycle and
    # still cannot be placeholder-bound (no `@log:end` anchor). Bounding novelty
    # by this is the no-loop 打止め for un-bindable touched tasks (INV-1), the
    # analogue of `exec_tried` / `tried_notes`.
    tried_tasks = capture.get('tried_tasks')
    tried_tasks = list(tried_tasks) if isinstance(tried_tasks, list) else []
    requested_ts = capture.get('requested_ts')

    applied_summaries: list[str] = []
    applied_links: list[tuple] = []
    applied_link_skipped: list[tuple] = []
    proposals: list[str] = []

    def _fold_tried(items):
        for n in (items.get('notes') if isinstance(items, dict) else []) or []:
            if n not in tried_notes:
                tried_notes.append(n)

    # --- (A) apply a delivered sidecar (only when one was requested) --------
    applied_this_stop = False
    if status in ('requested', 'pending', 'expired'):
        sidecar = _load_capture_sidecar(capture_path)
        if sidecar is not None:
            applied_this_stop = True
            applied_summaries, applied_links, proposals, applied_link_skipped = _apply_capture(
                sidecar, current_index, project, project_root, sid8, iso_ts)
            # Consume: unlink so a later request cannot re-match a stale sidecar.
            # On unlink failure, do NOT mark done — the next Stop re-applies
            # (idempotent), keeping the apply eventual (§10.2 / AC-11).
            try:
                os.remove(capture_path)
                _fold_tried(capture.get('items'))
                status = 'done'
            except OSError:
                pass  # leave status; re-apply next Stop

    # --- (B) lifecycle transition for an un-delivered request --------------
    if status in ('requested', 'pending') and not applied_this_stop:
        age = datetime.datetime.now().timestamp() - float(requested_ts or 0)
        if age >= _CAPTURE_EXPIRY_S:
            _fold_tried(capture.get('items'))
            status = 'expired'  # §10.4 — G backstop takes over below
        else:
            status = 'pending'  # in-flight: do NOT block (AC-9, no double-spawn)

    # --- (C) recompute missing AFTER apply, and the novelty set ------------
    missing = {
        base: path
        for base, path in resolved.items()
        if not log_block_has_sid(path, sid8)
    }
    reverse_index = build_reverse_index(project_root)
    note_writes, unlinked = _scan_note_writes(
        touched, project, project_root, reverse_index)
    novel_notes = [n for n in unlinked if n not in tried_notes]

    # --- (D) deterministic G backstop once capture has resolved ------------
    # §10.4: G (touched → placeholder) is guaranteed every Stop once capture is
    # done/expired. Apply runs BEFORE this (§10.2 ordering) so a real summary is
    # never pre-empted by a placeholder. On expiry, note writes whose owner is
    # known via the reverse index get a `referenced` over-bind (AC-6/AC-10);
    # unlinked notes are NOT established under judgment-absent expiry (§10.4).
    if status in ('done', 'expired'):
        for base, path in list(missing.items()):
            if append_auto_binding(path, sid8, iso_ts):
                auto_bound.append(_rel(path, cwd))
                missing.pop(base, None)
            elif base not in tried_tasks:
                # Cannot bind (no @log:end) after a full capture cycle — stop
                # requesting it (打止め / no-loop, INV-1).
                tried_tasks.append(base)
    if status == 'expired':
        for prel in note_writes:
            for owner_path in resolve_note_owner(
                    prel, project_root, reverse_index):
                if log_block_has_sid(owner_path, sid8):
                    continue
                if append_auto_binding(
                        owner_path, sid8, iso_ts,
                        f'(referenced) owner of {prel} via reverse-index; '
                        f'capture expired'):
                    auto_bound.append(_rel(owner_path, cwd))

    # --- (E) request capture when novelty remains in a re-requestable state -
    # Novelty is bounded by the 打止め sets: a touched task already tried (no
    # bindable anchor) and a note already attempted no longer re-trigger a spawn.
    missing_novel = {b: p for b, p in missing.items() if b not in tried_tasks}
    spawn = False
    if status in ('', 'done', 'expired') and (missing_novel or novel_notes):
        requested_ts = datetime.datetime.now().timestamp()
        capture = {
            'status': 'requested',
            'items': {'tasks': sorted(missing_novel.keys()), 'notes': novel_notes},
            'requested_ts': requested_ts,
            'tried_notes': tried_notes,
            'tried_tasks': tried_tasks,
        }
        spawn = True
    else:
        capture = {
            'status': status,
            'items': capture.get('items'),
            'requested_ts': requested_ts,
            'tried_notes': tried_notes,
            'tried_tasks': tried_tasks,
        }

    # --- Gate (INV-1): block only to (b) report binds, (c) report exec-skip,
    # (d) spawn capture, or to surface proposals. `requested` is committed before
    # the block, so the next Stop re-enters via the requested/pending branch
    # (no re-block loop). An in-flight `pending` with nothing to report → no
    # block (AC-9).
    report_binds = auto_bound or applied_summaries or applied_links or applied_link_skipped
    if not spawn and not report_binds and not exec_skipped and not proposals:
        _save_bind(bind_path, reminded, exec_tried, capture)
        return 0
    _save_bind(bind_path, reminded, exec_tried, capture)

    # --- F5 observability: one line per deterministic action this Stop. -----
    auto_lines = ''.join(
        f'[progress capture] auto-bound: {rel} [s:{sid8}]\n' for rel in auto_bound
    )
    auto_lines += ''.join(
        f'[progress capture] applied summary: {b} [s:{sid8}]\n'
        for b in applied_summaries
    )
    auto_lines += ''.join(
        f'[progress capture] linked note: {note} -> {b}\n'
        for note, b in applied_links
    )
    auto_lines += ''.join(
        f'[progress capture] auto-skip(ambiguous): {rel}\n' for rel in exec_skipped
    )
    auto_lines += ''.join(
        f'[progress capture] link-skip: {note} -> {b}\n'
        for note, b in applied_link_skipped
    )

    if spawn:
        shown = touched[:MAX_TOUCHED_IN_INJECTION]
        tail = '' if len(touched) <= MAX_TOUCHED_IN_INJECTION else \
            f' ...({len(touched) - MAX_TOUCHED_IN_INJECTION} more)'
        task_list = ' '.join(f'"{b}"' for b in sorted(missing_novel.keys()))
        note_list = ' '.join(f'"{n}"' for n in novel_notes)
        context = (
            '{'
            f'"sid8":"{sid8}","iso_ts":"{iso_ts}",'
            f'"sidecar_path":"_projects/_state/{session_id}.capture",'
            f'"project_root":"_projects/{project}",'
            f'"touched_tasks":[{task_list}],'
            f'"note_writes":[{note_list}]'
            '}'
        )
        reason = (
            f'{auto_lines}'
            f'[progress capture] session={sid8} date={date}\n'
            f'touched: {" ".join(shown)}{tail}\n\n'
            f'Spawn the async capture subagent to summarize this turn\'s task '
            f'work and map note deliverables to owning tasks. Do NOT update '
            f'`@log` / `@notes` yourself — the taskflow Stop hook applies the '
            f'subagent\'s result deterministically on a later Stop.\n\n'
            f'1. Use the Agent tool with subagent_type `{CAPTURE_AGENT_TYPE}`.\n'
            f'2. In its prompt, give this context block verbatim AND add, in '
            f'prose, what you did this turn (which tasks you advanced, which '
            f'project-notes you wrote/read, any task-worthy work with no task '
            f'file yet):\n'
            f'   {context}\n'
            f'3. The subagent MUST write its judgment as JSON to '
            f'`_projects/_state/{session_id}.capture` and write nothing else. '
            f'If you judge there is genuinely no task work, you may instead '
            f'reply `[progress capture] skip — no task work`; the deterministic '
            f'backstop will still bind touched tasks on a later Stop.'
        )
    else:
        # Report-only block (INV-1 b/c) + any proposals surfaced from a sidecar.
        note = '(reported above. '
        if exec_skipped:
            note += ('auto-skip = the [tasks:] target has no writable '
                     '<!-- @log:begin/end --> block; add one to it if that task '
                     'should carry a session log. ')
        if auto_bound:
            note += ('auto-bound = deterministic backstop (placeholder / '
                     'referenced); a richer summary is no longer needed. ')
        if applied_summaries or applied_links:
            note += 'applied entries came from the capture subagent. '
        note = note.rstrip() + ')'
        prop_lines = ''
        if proposals:
            prop_lines = (
                '\nProposed new tasks (capture subagent — NOT created; confirm '
                'with the user before creating any):\n'
                + ''.join(f'   - (proposed) {p}\n' for p in proposals)
            )
        reason = (
            f'{auto_lines}'
            f'[progress capture] session={sid8} date={date}\n'
            f'{note}{prop_lines}'
        )

    result = {'decision': 'block', 'reason': reason}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
    sys.stdout.buffer.write(b'\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
