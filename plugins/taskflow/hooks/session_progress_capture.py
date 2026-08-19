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

Round binding (capture-detection-gaps.md §1 / D1): the ledger is NOT "does the
task carry a `[s:sid8]` line" (that capped a session at one line per task and
silently dropped every later round's work). `.touched` is append-only and one
line per write event, so its RAW line count is a cursor: `raw[touch_cursor:]`
is exactly the activity since the last committed round. Each Stop computes the
round-active set A_r from that slice (task writes + note-write owners via the
reverse index + this Stop's `[tasks:]` exec carry), drops tasks the agent
already logged itself this round (`count_sid_lines` vs `log_seen`), and
requests a capture for the rest. `touch_cursor` / `round` / `log_seen` /
`round_base` live INSIDE the `capture` dict so the closed
`_load_bind`/`_save_bind` whitelist carries them (§1.7).

Cross-project resolution (capture-detection-gaps.md §3 / D2): a session's
`.touched` ledger may name tasks in a project OTHER than `state['project']`
(2026-08-08 e4a7583c incident — those writes were silently dropped by the F-L3
boundary guard). Each ledger line therefore carries its OWN project, extracted
from the line with `^_projects/([^/]+)/` and validated by probing for a `tasks/`
subdirectory (that probe is what rejects `_projects/_state/...` lines, which
match the regex but name no project). Every project so resolved gets its own
basename index, note reverse index and F-L3 boundary guard, and every internal
task key — `items.tasks`, `tried_tasks`, `log_seen`, `round_base` — is the
QUALIFIED `"<project>/<basename>"` (§3.3). A `.bind` written before this change
holds BARE basenames; they are read as the primary project's qualified key and
written back normalized, so they disappear after one Stop (F-4, §3.4).

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
  - INV-3 (idempotent): the ledger is the actual presence of the text key
    `[s:<sid8>]: <note>` inside a task md's `<!-- @log:begin/end -->` block,
    recomputed every Stop (§1.5 — generalized from bare sid presence once a
    session may bind a task once per round); apply / backstop are idempotent
    and eventual (AC-11).

exec-binding (§3.4): the terminal agent may carry owning tasks whose work landed
OUTSIDE `tasks/` via a `[tasks: a.md b.md]` leading line. This hook regex-reads
it from `last_assistant_message`, union-merges into `state['exec_bind']`, and
deterministically binds each owning task (skip+log + `.bind` record on failure,
to stop retrying — INV-1). Under fork it skips (W2 delegation).
A carry that names NO task md in the primary project
resolves to nothing and reaches no other detector (execution-by-reference leaves
no `.touched` line by construction), so it is reported ONCE as
`exec-skip(unresolved)` and 打止め'd by a bare-basename entry in `exec_tried`.

Round / lifecycle state lives in a sidecar `{session_id}.bind` (`reminded`,
`exec_tried` — the exec-carry 打止め set, holding BOTH `_rel()` repo-relative
paths of resolved-but-unbindable tasks AND bare basenames of carries that
resolved to no task at all; the two shapes are disjoint because a `_rel()` value
always starts `_projects/` —
and the `capture` lifecycle `{status, items, requested_ts,
tried_notes, tried_tasks, touch_cursor, round, log_seen, round_base}` — writer =
this hook only, §10.1), kept separate from the state
JSON so concurrent rewrites by other hooks cannot clobber it. A 7-day cleanup
prunes stale `.bind` / `.touched` / `.capture` / legacy `.captured` sidecars,
and (F5b / D-2) session-state `.json` whose `project` is empty once it is also
older than 7 days; a `.json` with a non-empty `project` is kept indefinitely
(generate_kanban.py resolves past task `@log` session links from it).
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
    is_contained_note_rel,
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
# deterministic G backstop takes over. 30s fixed by design; env-overridable so
# tests can force immediate expiry (mirrors log_lock's TASKFLOW_LOCK_TIMEOUT).
try:
    _CAPTURE_EXPIRY_S = float(os.environ.get('TASKFLOW_CAPTURE_EXPIRY_S', '30.0'))
except ValueError:
    _CAPTURE_EXPIRY_S = 30.0

# Round-history depth (capture-detection-gaps.md §4.4.1 D4). The `.bind`
# `capture['history']` dict retains the CLOSED item set of the last K rounds so
# a sidecar that lands after its own round already closed is still membership-
# checked against ITS round instead of whatever `items` happens to hold now
# (R-1: the whole judgment layer of a round was discarded as `membership-skip`
# whenever the subagent outran the 30s expiry and the next round committed
# first). The observed delay is one round (round N's sidecar arriving during
# N+1), which K=2 already covers; K=3 buys one more round of slack for a few
# hundred bytes — an entry is just two lists of task/note keys. Deliberately a
# fixed CONSTANT, not an env knob: the value has no per-environment meaning.
_ROUND_HISTORY_K = 3

# Sweep blast-cap (project-notes/specs/capture-hook-sweep-sandbox.md): the
# combined (json + sidecar) per-Stop delete budget for _cleanup_stale_markers.
# Default 50 sits between the empirically observed healthy-steady-state count
# (~2 per sweep, measured against a live _state/ directory) and the 2026-07-17
# incident (250 files deleted in one sweep after an empty-project backlog
# accumulated) — a wrong-cwd or mis-pointed-dir sweep is capped instead of
# silently emptying the directory. env-overridable so a deliberate bulk cleanup
# can raise it (mirrors TASKFLOW_CAPTURE_EXPIRY_S).
try:
    _SWEEP_MAX = int(os.environ.get('TASKFLOW_SWEEP_MAX', '50'))
except ValueError:
    _SWEEP_MAX = 50

# PreCompact placeholder note prefix (capture-detection-gaps.md §1.4 F-1 (b) /
# §2.2). SINGLE source of truth: `hooks/precompact_flush.py` (W3) imports this
# name instead of re-declaring the literal, so the writer and the counter can
# never drift apart. PreCompact cannot write `.bind` (writer single-ownership,
# note-task-link.md §10.1), so its placeholder lines must stay invisible to the
# per-round `log_seen` ledger — `count_sid_lines` excludes them, otherwise a
# compaction would make the next Stop read "the agent self-logged" and the
# round would never form.
_PRECOMPACT_NOTE_PREFIX = '(auto) unflushed at compaction'

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
# Begin-marker probe for the D3 both-absent case (§4.2). Mirrors `_LOG_END_RE`'s
# whitespace tolerance so "neither marker present" is decided on the same
# grammar as `_LOG_BLOCK_RE` — a literal-substring probe would mis-classify a
# whitespace-variant `<!--@log:begin-->` as absent and generate a SECOND block.
_LOG_BEGIN_RE = re.compile(r'<!--\s*@log:begin\s*-->')
# exec-binding carry: `[tasks: a.md b.md]` (space-separated basenames).
_TASKS_RE = re.compile(r'\[tasks:\s*([^\]]+)\]')
# D2 (§3.2): the project a `.touched` line belongs to, read from the line ITSELF
# (`_projects/<project>/...`, repo-relative and forward-slashed by
# `normalize_path`) rather than from `state['project']`.
_PROJECT_RE = re.compile(r'^_projects/([^/]+)/')


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


def read_touched_raw(touched_path: str, cwd: str) -> list[str]:
    """Read the `.touched` ledger → order-preserving, NON-deduped list (§1.2).

    Same tolerant parse as `read_touched` (torn trailing line dropped, blank
    lines skipped) but every append EVENT is kept, because the raw line count
    is the round cursor: `.touched` is append-only with one line per write
    event and no timestamps, so "how many lines have I already consumed" is the
    only novelty signal available. `read_touched` stays as the deduped reader
    used for display and the whole-session note scan.
    """
    if not os.path.isfile(touched_path):
        return []
    out: list[str] = []
    try:
        with open(touched_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                if not line.endswith('\n'):
                    continue  # torn/partial trailing line — drop
                p = line.strip()
                if not p:
                    continue
                n = normalize_path(p, cwd)
                if n:
                    out.append(n)
    except OSError:
        pass
    return out


def _cleanup_stale_markers(state_dir: str) -> None:
    """Remove stale files under STATE_DIR older than _MARKER_MAX_AGE_DAYS:
      - sidecar markers (`.bind` / `.touched` / `.capture`, plus legacy
        `.captured`) — unconditional mtime sweep (unchanged predicate).
      - session-state `.json` (36-char-UUID stem) whose `project` is EMPTY
        (F5b / D-2): parse-guarded — a json that fails to parse or is not a
        dict is NEVER removed (conservative); a NON-EMPTY `project` state is
        kept INDEFINITELY (generate_kanban.build_uuid_index resolves each task
        @log `[s:]` link from it). State mtime is refreshed every turn
        (session_init rewrites it each UserPromptSubmit), so the 7-day cutoff
        only catches DEAD sessions — long-lived projectless sessions are
        naturally protected (same property as the sidecar sweep).
    Non-UUID `.json` (e.g. kanban-port-*.json written by generate_kanban) is
    left untouched: the `len(stem) == 36` guard mirrors build_uuid_index's own
    session-state filter.

    Blast-cap (project-notes/specs/capture-hook-sweep-sandbox.md): deletion
    candidates (json + sidecar, combined) are collected first, sorted
    oldest-mtime-first, then only the first `_SWEEP_MAX` are actually removed
    — a wrong-cwd or mis-pointed state_dir sweep is capped instead of silently
    emptying the directory in one Stop. Deferred candidates are picked up on a
    later Stop (oldest-first ordering makes the capped sweep monotonic —
    it always makes progress rather than re-selecting the same fresh-side
    subset). Any deletion, or a cap hit, is reported on stderr (F-OBS-1) since
    this runs before stdin is read / the block-reason channel exists."""
    try:
        cutoff = datetime.datetime.now().timestamp() - _MARKER_MAX_AGE_DAYS * 86400
        candidates = []  # list of (mtime, path, is_json)
        for name in os.listdir(state_dir):
            path = os.path.join(state_dir, name)
            stem, ext = os.path.splitext(name)
            if ext == '.json' and len(stem) == 36:
                try:
                    mtime = os.path.getmtime(path)
                    if mtime >= cutoff:
                        continue  # fresh mtime → live/recent session; keep
                    with open(path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                except (OSError, ValueError):
                    continue  # unreadable / parse-unable → never delete (D-2)
                if isinstance(data, dict) and not data.get('project'):
                    candidates.append((mtime, path, True))
                continue
            if not name.endswith(_CLEANUP_SUFFIXES):
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime < cutoff:
                candidates.append((mtime, path, False))

        if not candidates:
            return
        candidates.sort(key=lambda c: c[0])  # oldest-first
        total = len(candidates)
        to_delete = candidates[:_SWEEP_MAX]
        deferred = total - len(to_delete)

        removed_json = 0
        removed_sidecar = 0
        for _mtime, path, is_json in to_delete:
            try:
                os.remove(path)
            except OSError:
                continue  # best-effort; not counted as removed
            if is_json:
                removed_json += 1
            else:
                removed_sidecar += 1

        removed_total = removed_json + removed_sidecar
        if removed_total:
            print(
                f'[progress capture] cleanup: removed {removed_total} stale '
                f'file(s) under {state_dir} (json={removed_json} '
                f'sidecar={removed_sidecar})',
                file=sys.stderr,
            )
        if deferred:
            print(
                f'[progress capture] WARNING: sweep cap TASKFLOW_SWEEP_MAX='
                f'{_SWEEP_MAX} hit — {total} candidates, removed '
                f'{len(to_delete)}, {deferred} deferred under {state_dir}',
                file=sys.stderr,
            )
    except OSError:
        pass


def _is_task_md(rel_path: str) -> bool:
    """True if a (repo-relative, forward-slashed) path is a task md under
    `tasks/<status>/`."""
    return bool(_TASK_PATH_RE.search(rel_path))


# --- §3.2 D2: project extraction and per-project resolution ----------------

def extract_project(rel_path: str) -> str:
    """Project name of a repo-relative `.touched` line, or '' (§3.2).

    Derived from the PATH, not from `state['project']`: the ledger legitimately
    carries writes into other projects of this repo, and the 2026-08-08 data
    survey found a real session state whose `project` was CORRUPTED
    (`"i-extensions-dev"` — the leading `p` lost), which path derivation is
    immune to.

    A line that does not start with `_projects/` (an absolute path from another
    repository, a cwd-external write, a bash-parse fragment) yields '' and stays
    out of scope: cross-REPO binding belongs to capture-context-abs-path.md, and
    the survey observed zero cross-repo task writes."""
    m = _PROJECT_RE.match(str(rel_path).replace('\\', '/'))
    return m.group(1) if m else ''


def resolve_project_roots(touched, progress_root: str, primary: str = '') -> dict:
    """Ordered `{project_name: absolute_root}` for this session (§3.2).

    `primary` — the session state's project, already validated by the caller —
    is always first and always present, so it remains the compatibility default
    for everything that has no project of its own (a bare sidecar reference, the
    `[tasks:]` exec carry, a legacy `.bind` key).

    Every OTHER name comes from `extract_project` and must carry a `tasks/`
    subdirectory to be accepted. That probe is mandatory, not cosmetic:
    `_projects/_state/...` lines DO match the extraction regex (7 such lines in
    one real repo's ledgers, 6 in the other's) and `_state` is the sidecar
    directory, not a project. The probe rejects it and any other non-project
    directory that may sit under `_projects/`."""
    roots: dict = {}
    if primary:
        roots[primary] = os.path.join(progress_root, primary)
    for rel in touched:
        name = extract_project(rel)
        if not name or name in roots:
            continue
        root = os.path.join(progress_root, name)
        if os.path.isdir(os.path.join(root, 'tasks')):
            roots[name] = root
    return roots


def qualify(project: str, basename: str) -> str:
    """The qualified task key `"<project>/<basename>"` (§3.3)."""
    return f'{project}/{basename}'


def qualify_legacy(key: str, primary: str) -> str:
    """Normalize one possibly-BARE task key to its qualified form (F-4, §3.4).

    A `.bind` written by W2/W3 keys `items.tasks` / `tried_tasks` / `log_seen` /
    `round_base` by bare basename. Reading such a key as the PRIMARY project's
    qualified key (the same fail-open shape as the legacy `items=None` path) and
    writing the normalized value back means bare keys disappear after a single
    Stop, with no round lost and none replayed at the upgrade boundary."""
    k = str(key).replace('\\', '/').strip('/')
    if not k or '/' in k:
        return k
    return qualify(primary, k)


def _task_basename_index(project_root: str) -> dict:
    """Build {basename: absolute_current_path} for every task md under
    `<project_root>/tasks/` (P8: a task may move status folders this session;
    basenames are expected unique across status folders).

    LOCKSTEP (survey / §3.3): this walk range is mirrored by
    `scripts/check_progress.py::check_duplicate_basename`. D2 calls this
    function once per resolved project root instead of once per session; the
    range INSIDE a root is unchanged, and check_progress already runs
    per-project, so the mirror still holds without a change there."""
    tasks_root = os.path.join(project_root, 'tasks')
    current: dict = {}
    if os.path.isdir(tasks_root):
        for dirpath, _dirs, files in os.walk(tasks_root):
            for fn in files:
                if fn.lower().endswith('.md'):
                    current[fn] = os.path.join(dirpath, fn)  # last writer wins
    return current


def qualified_task_index(project_roots: dict) -> dict:
    """`{"<project>/<basename>": absolute_current_path}` over every resolved
    project root (§3.3) — the union index `_apply_capture` and the G backstop
    resolve against."""
    index: dict = {}
    for name, root in project_roots.items():
        for base, path in _task_basename_index(root).items():
            index[qualify(name, base)] = path
    return index


def resolve_touched_tasks(touched, project_roots):
    """Resolve each touched task md to its CURRENT on-disk location by basename
    (P8), IN ITS OWN PROJECT (§3.2). Returns
    `{"<project>/<basename>": absolute_current_path}`.

    The project comes from the touched line, so a write into another project of
    this repo binds under THAT project's root instead of being basename-matched
    into the session's project or dropped by the boundary guard (the e4a7583c
    loss path). Dropped: a line naming no resolvable project (`extract_project`
    returned '' or the name had no `tasks/` dir — this is where
    `_projects/_state/...` lines die), a deleted task, and a resolution landing
    outside its own project's `tasks/` (F-L3, now applied PER project root)."""
    indexes = {name: _task_basename_index(root)
               for name, root in project_roots.items()}
    prefixes = {name: os.path.normpath(os.path.join(root, 'tasks')) + os.sep
                for name, root in project_roots.items()}
    resolved = {}
    for rel in touched:
        rel_fs = str(rel).replace('\\', '/')
        if not _is_task_md(rel_fs):
            continue
        name = extract_project(rel_fs)
        if name not in indexes:
            continue
        base = os.path.basename(rel_fs)
        cur = indexes[name].get(base)
        if cur:
            # Boundary guard (F-L3), per project root.
            if not os.path.normpath(cur).startswith(prefixes[name]):
                continue
            resolved[qualify(name, base)] = cur
    return resolved


def _exec_base(b) -> str:
    """The lookup key of ONE `[tasks:]` carry entry — single source of truth.

    `merge_exec_bind` already stores bare basenames, but a `state.json` written
    by an older version may hold a path-shaped entry, so the normalization is
    re-applied on read. Both the resolver below and the unresolved-report in
    `main()` derive their key from HERE: a second expression for "the same key"
    could drift, and a report keyed differently from the lookup would flag a name
    the resolver actually found."""
    return os.path.basename(str(b).replace('\\', '/'))


def resolve_exec_tasks(basenames, project_root):
    """Resolve exec-bind owning-task basenames to CURRENT on-disk paths (P8).

    PRIMARY-project-only by design (capture-detection-gaps.md §3.6): unlike a
    `.touched` line, whose project is derived from the path it carries, a
    `[tasks:]` carry is a bare NAME with no path evidence, so D2's per-line
    project derivation has nothing to derive from.

    A basename that resolves to nothing is simply ABSENT from the result. The
    CALLER must report that miss once (F5 / INV-1) — only the caller knows the
    fork guard and holds the `exec_tried` 打止め set — and the retry itself is
    deliberately NOT suppressed: a task claimed before it is created binds on a
    later Stop."""
    if not basenames:
        return {}
    current = _task_basename_index(project_root)
    resolved = {}
    for b in basenames:
        base = _exec_base(b)
        cur = current.get(base)
        if cur:
            resolved[base] = cur
    return resolved


def _log_block_of(path):
    """Return the `<!-- @log:begin/end -->` block body of the task md at `path`
    ('' when the file is unreadable or carries no complete block)."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError:
        return ''
    m = _LOG_BLOCK_RE.search(content)
    return m.group(1) if m else ''


def log_block_has_sid(path, sid8):
    """True if the task md at `path` already holds a `[s:<sid8>]` line inside its
    `<!-- @log:begin/end -->` block. Session-scoped presence: still the ledger
    for the once-per-session deterministic exec-bind (§3.4); the per-ROUND
    ledger is `count_sid_lines` (§1.4)."""
    return f'[s:{sid8}]' in _log_block_of(path)


def count_sid_lines(path, sid8):
    """Number of `[s:<sid8>]` lines inside the `@log` block — the per-round
    ledger (§1.4). Compared against `log_seen[task]` (the count at round open)
    it answers "did anything get logged for this task this round".

    PreCompact placeholders are EXCLUDED (F-1 (b)): a line whose note starts
    with `_PRECOMPACT_NOTE_PREFIX` is written by `precompact_flush.py`, which
    cannot update `.bind`, so counting it would raise `n_now` above `log_seen`
    with no way to resync — the very next Stop would read that as "the agent
    self-logged" and silently drop the round. Transition lines carry no
    `[s:...]` tag and so never enter this count."""
    n = 0
    tag = f'[s:{sid8}]'
    for line in _log_block_of(path).splitlines():
        at = line.find(tag)
        if at == -1:
            continue
        note = line[at + len(tag):].lstrip()
        if note.startswith(':'):
            note = note[1:].lstrip()
        if note.startswith(_PRECOMPACT_NOTE_PREFIX):
            continue
        n += 1
    return n


def log_block_has_note(path, sid8, note):
    """True if the `@log` block already holds the exact text key
    `[s:<sid8>]: <note>` (§1.5). INV-3 generalized: once a session may bind a
    task more than once, "a sid line exists" no longer identifies an entry, so
    idempotency keys on the sid + note TEXT. The timestamp is deliberately not
    part of the key — a sidecar whose unlink failed is re-applied on a later
    Stop with a fresh `iso_ts`, and that re-apply must be a no-op."""
    return f'[s:{sid8}]: {note}' in _log_block_of(path)


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

    Idempotency (§1.5): if the block already holds the text key
    `[s:<sid8>]: <note>` this is a no-op that returns True. A session may now
    bind the same task once per round, so "a `[s:sid8]` line exists" cannot be
    the guard; the note text separates rounds (placeholders carry an `(r{N})`
    tag) while a re-applied identical summary collapses to one line.

    When `@log:end` is missing the recovery depends on the damage shape:
      - `@log:begin` still present (half-destroyed by a hand edit) → a
        conservative `repair_log_markers` pass runs first; the repaired markers
        are persisted together with the appended line.
      - NEITHER marker present (a task md that never carried an `@log` region)
        → the block is GENERATED here (capture-detection-gaps.md §4.2 / D3),
        immediately before the `@notes` block when one exists, otherwise at EOF.
      - anything else (ambiguous residue, e.g. two `@log:begin`) → False.

    The read-modify-write is serialized through the shared bounded advisory lock
    (`log_lock`, INV-2). Residual gap: an LLM Edit-tool append at the tool layer
    cannot take this lock — the known R-lock gap (exec-binding.md §3.5 / R1)."""
    with log_lock(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
        except OSError:
            return False
        m_blk = _LOG_BLOCK_RE.search(content)
        if m_blk and f'[s:{sid8}]: {note}' in m_blk.group(1):
            return True  # text-key idempotency (§1.5) — already recorded
        m = _LOG_END_RE.search(content)
        if not m:
            if _LOG_BEGIN_RE.search(content):
                repaired = repair_log_markers(content)
                if repaired is None:
                    return False
                content = repaired
            else:
                # D3 (§4.2): no `@log` region at all — generate an empty one and
                # let the normal insert-before-`@log:end` path below fill it, so
                # there is exactly one write site and one line-format source.
                at = content.find(NOTES_BEGIN)
                if at == -1:
                    at = len(content)
                head, tail = content[:at], content[at:]
                if head and not head.endswith('\n'):
                    head += '\n'
                content = (head + '\n<!-- @log:begin -->\n<!-- @log:end -->\n'
                           + tail)
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
        # project-notes/ after project-relative conversion). Containment (`..`,
        # root/drive anchors) is NOT tested here — `is_note_deliverable` below
        # owns it, so the rule lives in one place. `touched` is not pre-resolved
        # (touched_capture.py::normalize_path folds separators only), so a
        # traversal really can arrive on this path too.
        if not prel.startswith('project-notes/'):
            continue
        if not is_note_deliverable(prel):
            continue
        seen.add(prel)
        note_writes.append(prel)
        if not resolve_note_owner(prel, project_root, reverse_index):
            unlinked.append(prel)
    return note_writes, unlinked


# --- §1 round computation (shared with hooks/precompact_flush.py) ----------
# Extracted verbatim from `main()` so the PreCompact flush computes the pending
# set with the SAME code the Stop hook uses for its round-active set. A second
# implementation would drift; these two functions are the single source of
# truth for "what has this round touched that is not logged yet" (§2.2).

def resolve_touch_cursor(capture, bind_existed, raw_lines, resolved, sid8,
                         log_seen):
    """Return this session's `.touched` round cursor, clamped to the ledger.

    `capture` is the `.bind` capture dict (read-only here). `log_seen` is
    MUTATED in place by the M-1 bootstrap branch (§1.8): a `.bind` that predates
    the round schema means EARLIER Stops already consumed the ledger under the
    old one-line-per-session rule, so the cursor starts at the END and
    `log_seen` is seeded from the current on-disk counts — replaying it would
    re-capture the whole session history (upgrade storm). No `.bind` at all = no
    earlier Stop ran, so nothing was consumed and the cursor starts at 0 (this
    session's first round must still see its own work), with `log_seen` left
    EMPTY: every `[s:sid8]` line in a task md belongs to this session by
    construction, so seeding here would read the agent's own round-1 log line as
    the baseline and request a capture it does not need.

    F-7: `.touched` may be truncated or removed mid-session — the result is
    clamped to `len(raw_lines)` so the slice can never go negative."""
    touch_cursor = capture.get('touch_cursor')
    if not isinstance(touch_cursor, int):
        if bind_existed:
            touch_cursor = len(raw_lines)
            for key, path in resolved.items():  # `key` is qualified (§3.3)
                log_seen[key] = count_sid_lines(path, sid8)
        else:
            touch_cursor = 0
    return min(touch_cursor, len(raw_lines))


def compute_round_active(new_slice, project_roots, reverse_indexes, sid8,
                         log_seen, tried_tasks, extra=None, hook_appended=None):
    """Round-active set A_r (§1.3) as {"<project>/<basename>": absolute_path}.

        A_r = tasks written in this round's ledger slice
            ∪ owners of the notes written in it (reverse index — the via-a-note
              loss path)
            ∪ `extra`
            − self-logged (`count_sid_lines` > `log_seen`, §1.4)
            − `tried_tasks` (打止め, INV-1)

    `extra` carries candidates the CALLER resolved and filtered, unioned in
    BEFORE the self-log pass: the Stop hook passes this Stop's `[tasks:]` exec
    carry (minus what its deterministic exec-bind already recorded and minus the
    `exec_tried` 打止め set). `hooks/precompact_flush.py` passes nothing —
    the PreCompact payload carries no `last_assistant_message` (§2.1 probe), so
    the exec-carry component of A_r is not computable there (accepted gap F-5).

    `hook_appended` is {absolute_path: n} for `@log` lines the CALLER wrote
    during this same invocation; they are subtracted from the count so the
    hook's own writes are never mistaken for an agent self-log (F-1, §1.4).

    `log_seen` is MUTATED in place for every task judged self-logged (that is
    the ledger advance the Stop hook then persists; PreCompact discards it,
    `.bind` being read-only there — §2.2 step 4).

    D2 (§3.2): `project_roots` is `{project: absolute_root}` and
    `reverse_indexes` is `{project: note reverse index}`. Both the task
    resolution and the note-owner resolution run PER project, so a round is
    computed across every project the ledger slice touched, and every key in the
    returned dict (and in `extra` / `log_seen` / `tried_tasks`) is qualified."""
    active = dict(resolve_touched_tasks(new_slice, project_roots))
    for name, root in project_roots.items():
        ridx = reverse_indexes.get(name) or {}
        slice_notes, _slice_unlinked = _scan_note_writes(
            new_slice, name, root, ridx)
        for prel in slice_notes:
            for owner_path in resolve_note_owner(prel, root, ridx):
                active[qualify(name, os.path.basename(owner_path))] = owner_path
    if extra:
        for key, path in extra.items():
            active[key] = path
    hook_appended = hook_appended or {}
    # self-log detection (§1.4): a `[s:sid8]` count that grew beyond the round's
    # opening baseline by something OTHER than this hook's own writes means the
    # agent logged the work itself (guidelines followed) — no capture needed.
    for key, path in list(active.items()):
        n_now = count_sid_lines(path, sid8) - hook_appended.get(path, 0)
        if n_now > log_seen.get(key, 0):
            log_seen[key] = n_now
            active.pop(key, None)
    # 打止め (INV-1): a task with no bindable anchor is never re-requested.
    return {k: p for k, p in active.items() if k not in tried_tasks}


def capture_sidecar_path(state_dir, session_id, round_n):
    """The per-round capture sidecar path `{session_id}.r{N}.capture` (§4.4.1 D1).

    Round identity travels in the FILE NAME, not in a JSON field the subagent
    would have to copy: the hook decides it, so the agent contract gains no new
    output obligation (it still just writes the `sidecar_path` it was handed)
    and a forgotten/altered field cannot desynchronize the identity.

    D5 (verified against the sweep code): `.r{N}.capture` still ends with
    `.capture`, so an orphan is collected by the existing 7-day
    `_CLEANUP_SUFFIXES` sweep unchanged, and `os.path.splitext` still yields
    `.capture`, so the sweep's 36-char-stem json branch is not entered."""
    return os.path.join(state_dir, f'{session_id}.r{round_n}.capture')


def scan_round_sidecars(state_dir, session_id):
    """Return `[(round:int, path)]` for every `{session_id}.r{N}.capture` on
    disk, ASCENDING by round (§4.4.1 D3: rounds N and N+1 can land together and
    must be applied oldest-first). The name is parsed with an anchored regex, so
    an off-contract name (`r999`, `rX`, a trailing suffix) either parses to a
    round the history bound rejects or is not seen as a sidecar at all."""
    pat = re.compile(rf'^{re.escape(session_id)}\.r(\d+)\.capture$')
    found: list[tuple] = []
    try:
        names = os.listdir(state_dir)
    except OSError:
        return found
    for name in names:
        m = pat.match(name)
        if m:
            found.append((int(m.group(1)), os.path.join(state_dir, name)))
    found.sort(key=lambda t: t[0])
    return found


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


def _resolve_task_ref(ref, project_roots, primary, current_index, explicit=''):
    """Resolve a sidecar task reference to a QUALIFIED key (§3.3).

    Returns `(qualified_key, ambiguous)`. Resolution order:
      1. an explicit `project` field on the entry, when it names a resolved
         project (the sidecar contract's optional field, §3.3/§3.4);
      2. a reference already written `"<project>/<basename>"`;
      3. otherwise a BARE basename → the PRIMARY project first (which is what
         "absent = primary project" means), then a UNIQUE match among the other
         resolved projects;
      4. more than one project carrying that basename → `('', True)`, so the
         caller surfaces it via `membership_skipped` instead of guessing. The
         2026-08-08 survey found zero basename collisions across 387 real tasks,
         so this branch is insurance, not a routine path.

    A reference with leading directories that are NOT a resolved project name
    (e.g. `tasks/1_in_progress/x.md`) degrades to its basename, preserving the
    pre-D2 `os.path.basename` tolerance."""
    r = str(ref).replace('\\', '/').strip('/')
    if not r:
        return '', False
    base = r.rsplit('/', 1)[-1]
    head = r[:len(r) - len(base) - 1] if len(r) > len(base) else ''
    if explicit and explicit in project_roots:
        return qualify(explicit, base), False
    if head and head in project_roots:
        return qualify(head, base), False
    primary_key = qualify(primary, base)
    if primary_key in current_index:
        return primary_key, False
    hits = [k for k in current_index if k.rsplit('/', 1)[-1] == base]
    if len(hits) == 1:
        return hits[0], False
    if len(hits) > 1:
        return '', True
    # Unknown everywhere: keep the primary-qualified form so the caller's index
    # lookup misses and the entry is dropped as "task gone" (unchanged shape).
    return primary_key, False


def _apply_capture(sidecar, current_index, project, project_roots, sid8, iso_ts, items=None):
    """Apply a validated capture sidecar deterministically (§10.3). All writes
    are idempotent (`log_block_has_sid` / `append_note_link` union). Returns
    (summaries, links, proposals, link_skipped, membership_skipped) for
    observability:
      - summaries:    list[qualified task key] @log-bound with a real one-line summary
      - links:        list[(note_rel, qualified task key)] established in a task @notes
      - proposals:    list[str] surfaced (display-only; never auto-created)
      - link_skipped: list[(note_rel, qualified task key)] where append_note_link returned False
      - membership_skipped: list[str] task key / note_rel names skipped because
        they were outside the request-time closed set (F7a §8 boundary
        enforcement), or because a bare basename was ambiguous across projects

    `current_index` is the QUALIFIED union index `{"<project>/<basename>":
    path}` (§3.3) and `project` is the session's primary project. `items` is the
    request-time closed set `{'tasks': [qualified keys], 'notes': [note_rel]}`
    that gated this capture request, or `None` for a legacy sidecar/.bind
    predating `items` — in which case the membership check is bypassed and both
    loops apply exactly as before (fail-open fallback).

    Sidecar contract (§3.3/§3.4): `confirmed[]` / `note_links[]` may carry an
    OPTIONAL `project` field; absent means the primary project. `note_links[]`
    `note` stays PROJECT-RELATIVE under `project-notes/` and the D-7 guard is
    unchanged — it is simply applied against the entry's OWN project root now,
    which is the explicit `project` when given and otherwise the project of the
    resolved owning task.

    `items['notes']` / `tried_notes` stay project-relative (unqualified): they
    are bounding sets only (INV-1 novelty), never a write target, so a
    hypothetical same-rel note in two projects can at worst suppress one
    duplicate request — the actual `@notes` write is addressed through the task,
    which IS qualified."""
    summaries: list[str] = []
    links: list[tuple] = []
    proposals: list[str] = []
    link_skipped: list[tuple] = []
    membership_skipped: list[str] = []
    task_set = (set(items['tasks']) if isinstance(items, dict)
                and isinstance(items.get('tasks'), list) else None)
    note_set = (set(items['notes']) if isinstance(items, dict)
                and isinstance(items.get('notes'), list) else None)

    confirmed = sidecar.get('confirmed')
    if isinstance(confirmed, list):
        for item in confirmed:
            if not isinstance(item, dict):
                continue
            ref = item.get('task')
            summ = item.get('summary')
            if not isinstance(ref, str) or not isinstance(summ, str):
                continue
            explicit = item.get('project')
            explicit = explicit if isinstance(explicit, str) else ''
            key, ambiguous = _resolve_task_ref(
                ref, project_roots, project, current_index, explicit)
            if ambiguous:
                # Visible rather than silently bound to the wrong project.
                membership_skipped.append(os.path.basename(ref.replace('\\', '/')))
                continue
            if task_set is not None and key not in task_set:
                membership_skipped.append(key)
                continue
            path = current_index.get(key)
            if not path:
                continue  # task gone
            note = ' '.join(summ.split())[:200] or '(captured) summary pending'
            # Text-key idempotency (§1.5), replacing the old `log_block_has_sid`
            # skip: a task may legitimately carry one line per round, so only
            # THIS summary's re-application (sidecar unlink failed → re-apply on
            # a later Stop) must be suppressed. Checked here as well as inside
            # `append_auto_binding` so the caller does not report a no-op append
            # as an applied summary and block on it every Stop (INV-1).
            if log_block_has_note(path, sid8, note):
                continue
            if append_auto_binding(path, sid8, iso_ts, note):
                summaries.append(key)

    note_links = sidecar.get('note_links')
    if isinstance(note_links, list):
        for item in note_links:
            if not isinstance(item, dict):
                continue
            note = item.get('note')
            ref = item.get('task')
            if not isinstance(note, str) or not isinstance(ref, str):
                continue
            if not ref or ref.strip().lower() == 'none':
                continue  # task==none → no-op (§10.3)
            explicit = item.get('project')
            explicit = explicit if isinstance(explicit, str) else ''
            key, ambiguous = _resolve_task_ref(
                ref, project_roots, project, current_index, explicit)
            if ambiguous:
                membership_skipped.append(os.path.basename(ref.replace('\\', '/')))
                continue
            path = current_index.get(key)
            if not path:
                continue
            # The entry's OWN project: the explicit field when it names a
            # resolved project, otherwise the project of the owning task (which
            # is where a project-relative note under that task must live).
            # Absent both, this is the primary project — the §3.3 default.
            entry_project = (explicit if explicit in project_roots
                             else key.rsplit('/', 1)[0])
            note_rel = normalize_note_rel(_to_project_rel(note, entry_project))
            # D-7 (capture-context-abs-path.md Q6): reject any note path that
            # is not project-relative under project-notes/ — regardless of
            # `items` membership. Closes the legacy-sidecar (`items=None`)
            # fail-open path through which a subagent's absolute/off-contract
            # note path could otherwise be burned into a task's `@notes`.
            # Logged to stderr (review F-I1): this reject sits BEFORE the
            # membership check, so it would otherwise be a silent drop with
            # no F5 observability at all — the exact failure class this task
            # exists to eliminate.
            # The prefix alone is not the bound: `project-notes/../x.md`
            # satisfies it and still leaves the project, which is a wrong link
            # `@notes` can never take back (§3.1). Containment is therefore
            # checked as a second, separately reported invariant (F-2).
            if not note_rel.startswith('project-notes/'):
                print(f'[progress capture] note-path-reject: {note!r} '
                      f'(not project-relative under project-notes/) [s:{sid8}]',
                      file=sys.stderr)
                continue
            if not is_contained_note_rel(note_rel):
                print(f'[progress capture] note-path-reject: {note!r} '
                      f'(escapes the project root) [s:{sid8}]',
                      file=sys.stderr)
                continue
            if not is_note_deliverable(note_rel):
                continue
            if note_set is not None and note_rel not in note_set:
                membership_skipped.append(note_rel)
                continue
            if append_note_link(path, note_rel):
                links.append((note_rel, key))
            else:
                link_skipped.append((note_rel, key))

    props = sidecar.get('proposals')
    if isinstance(props, list):
        for p in props:
            if isinstance(p, str) and p.strip():
                proposals.append(p.strip())

    return summaries, links, proposals, link_skipped, membership_skipped


def merge_exec_bind(state, state_path, data):
    """Parse a `[tasks: a.md b.md]` carry from the leading lines of
    `last_assistant_message` and union-merge the basenames into
    `state['exec_bind']` (append-only; durable within this session). Persists
    state.json on change.

    Returns `(exec_bind, this_turn)`: the session-cumulative list AND the
    basenames parsed from THIS Stop's message (§1.3). The cumulative list drives
    the once-per-session deterministic exec-bind; the this-turn set is the
    round-active (A_r) exec carry — work claimed on this turn, which is what a
    round is about. `this_turn` is a subset of `exec_bind` and is empty when the
    message carries no (in-window) `[tasks:]` marker."""
    existing = state.get('exec_bind')
    existing = list(existing) if isinstance(existing, list) else []
    msg = data.get('last_assistant_message', '')
    if not isinstance(msg, str):
        return existing, []
    m = _TASKS_RE.search(msg)
    if not m or m.start() >= EXEC_PARSE_WINDOW:
        return existing, []
    new = [t for t in m.group(1).split() if t.endswith('.md')]
    this_turn: list[str] = []
    merged = list(existing)
    for t in new:
        b = os.path.basename(t.replace('\\', '/'))
        if b not in this_turn:
            this_turn.append(b)
        if b not in merged:
            merged.append(b)
    if merged != existing:
        state['exec_bind'] = merged
        try:
            with open(state_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False)
        except OSError:
            pass
        return merged, this_turn
    return existing, this_turn


def _rel(path, cwd):
    return os.path.relpath(path, cwd).replace('\\', '/')


def _to_forward_slash(path):
    """Single source of truth for the sidecar-path display form (review
    F-I3): both `build_capture_context()` and its caller's step-3 prose need
    the identical forward-slashed value, so both call this instead of each
    inlining their own `.replace('\\\\', '/')` — two independent expressions
    for 'the same value' is exactly the kind of duplication that can drift
    silently if only one side is ever edited."""
    return path.replace('\\', '/')


def build_capture_context(sid8, iso_ts, capture_path, project_root,
                           project_roots, task_keys, note_writes, round_n):
    """Build the JSON context block handed to the capture subagent (§10.5).

    `sidecar_path` / `project_root` are forward-slashed absolute paths (same
    objects the hook itself reads/resolves — `capture_path` / `project_root`
    in `main()`), so the subagent's write/read basis can never drift from the
    hook's regardless of its cwd (project-notes/specs/capture-context-abs-path.md
    D-1/D-2). `task_keys` / `note_writes` are emitted via `json.dumps`
    (not space-joined) so the array is valid JSON with 2+ entries (D-6).

    D2 (§3.3): `project_root` STAYS — it is the primary project and the default
    for anything the subagent leaves unqualified — and `project_roots`
    `{name: absolute_root}` is ADDED next to it, because `touched_tasks` entries
    are now qualified `"<project>/<basename>"` and a task in a non-primary
    project has to be resolvable to a root. Every root is forward-slashed
    through the same single source of truth as `project_root`.

    R-1 (§4.4.1 D1/D6): `capture_path` MUST be the per-round path produced by
    `capture_sidecar_path()` — the round is carried by the file name, and the
    emitted `round` field is an ECHO for the subagent's own reasoning only. The
    hook treats the FILE NAME as authoritative and never reads a `round` back
    out of the sidecar, so this adds no output obligation to the agent contract.
    """
    return json.dumps(
        {
            'sid8': sid8,
            'iso_ts': iso_ts,
            'round': round_n,
            'sidecar_path': _to_forward_slash(capture_path),
            'project_root': _to_forward_slash(project_root),
            'project_roots': {name: _to_forward_slash(root)
                              for name, root in dict(project_roots).items()},
            'touched_tasks': list(task_keys),
            'note_writes': list(note_writes),
        },
        ensure_ascii=False, separators=(',', ':'),
    )


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
    touched_path = os.path.join(STATE_DIR, f'{session_id}.touched')
    touched = read_touched(touched_path, cwd)
    raw_lines = read_touched_raw(touched_path, cwd)
    # D2 (§3.2): every project the ledger names (plus the primary), each with
    # its own basename index and F-L3 boundary. `_projects/_state/...` lines are
    # rejected by the `tasks/` probe inside `resolve_project_roots`.
    project_roots = resolve_project_roots(touched, PROGRESS_ROOT, project)
    resolved = resolve_touched_tasks(touched, project_roots)

    # --- exec-binding carry: merge [tasks:] → state.exec_bind, resolve (P8) ---
    is_fork = bool(state.get('parent_session_id'))
    exec_bind, exec_this_turn = merge_exec_bind(state, state_path, data)
    exec_resolved = resolve_exec_tasks(exec_bind, project_root)

    bind_existed = os.path.exists(bind_path)
    bind = _load_bind(bind_path)
    reminded = bind['reminded']
    exec_tried = bind['exec_tried']
    capture = bind['capture']

    # --- round state (§1.2 / §1.6-1.8), carried INSIDE `capture` so the CLOSED
    # `_load_bind`/`_save_bind` whitelist round-trips it without change (§1.7).
    round_n = capture.get('round')
    round_n = round_n if isinstance(round_n, int) else 0
    log_seen = capture.get('log_seen')
    log_seen = dict(log_seen) if isinstance(log_seen, dict) else {}
    # F-4 (§3.4): a `.bind` written before W4 keys the round dicts by BARE
    # basename. Read every bare key as the PRIMARY project's qualified key here;
    # the commit below persists the normalized form, so the bare keys vanish
    # after one Stop without losing or replaying a round.
    log_seen = {qualify_legacy(k, project): v for k, v in log_seen.items()}
    # `round_base`: the `[s:sid8]` count of each item at the moment the OPEN
    # round was requested. `log_seen` cannot serve here: F-1 resyncs it from the
    # hook's own writes at the END of every Stop, and a round spans Stops, so by
    # the time the backstop runs `log_seen` may already include a line written
    # for THIS round (an apply whose sidecar unlink failed, a mid-round
    # exec-bind) and the backstop would add a redundant placeholder next to it.
    # Frozen with `items`, replaced on every request commit.
    round_base = capture.get('round_base')
    round_base = dict(round_base) if isinstance(round_base, dict) else {}
    round_base = {qualify_legacy(k, project): v for k, v in round_base.items()}
    # `history` (§4.4.1 D2): {"<round>": {"tasks": [...], "notes": [...]}} — the
    # frozen `items` of the last `_ROUND_HISTORY_K` rounds, including the CURRENT
    # one, so the apply path has a single lookup for "that round's closed set".
    # Written ONLY at the (E) request commit. `round_base` is deliberately NOT
    # kept here: the membership gate reads `items` alone, and the only consumer
    # of `round_base` is the (D) backstop, which is current-round-only.
    # No `qualify_legacy` pass (§4.4.4): `history` is a NEW key whose entries are
    # written from `active.keys()`, which `compute_round_active` already returns
    # qualified — a bare F-4 key cannot structurally get in. Non-conforming
    # entries from a hand-edited/corrupt `.bind` are dropped here so the prune's
    # `int(k)` below can never raise.
    history = capture.get('history')
    history = dict(history) if isinstance(history, dict) else {}
    history = {str(k): v for k, v in history.items()
               if str(k).isdigit() and isinstance(v, dict)}
    # M-1 bootstrap (§1.8) + F-7 clamp, shared with `precompact_flush.py`.
    touch_cursor = resolve_touch_cursor(
        capture, bind_existed, raw_lines, resolved, sid8, log_seen)
    new_slice = raw_lines[touch_cursor:]
    slice_display: list[str] = []
    for _r in new_slice:
        if _r not in slice_display:
            slice_display.append(_r)

    # `auto_bound`: list[str rel] code-appended this Stop (exec + placeholder +
    # referenced). Drives the (b) report; INV-1.
    auto_bound: list[str] = []
    # `exec_skipped`: NEW exec-bind skips this Stop (no @log:end / write fail).
    # Surfaced once via the injection (F5 / AC-7) then suppressed by exec_tried,
    # so a given task is reported at most once — bounded (INV-1 c).
    exec_skipped: list[str] = []
    # `exec_unresolved`: NEW `[tasks:]` carries from THIS TURN naming no task md
    # in the primary project. The carry is the ONLY detector for
    # execution-by-reference work — nothing lands in `.touched` by construction —
    # so an unresolvable name is a silent LOSS, not a harmless no-op. Entries are
    # `(basename, hint)`; surfaced once on stderr AND in the block reason, then
    # suppressed by the BARE-BASENAME entry appended to `exec_tried`: the same
    # 打止め shape as `exec_skipped` (INV-1 c).
    exec_unresolved: list[tuple] = []
    # `bind_skipped`: NEW touched-task bind failures this Stop (§4.3) — a task
    # whose `@log` damage is too ambiguous even for D3 block generation. Same
    # shape as `exec_skipped`: surfaced once via the injection, then suppressed
    # by `tried_tasks`, so a given task is reported at most once (INV-1).
    bind_skipped: list[str] = []
    # `hook_appended`: {task-path: n} — `@log` lines THIS hook appended during
    # THIS Stop. F-1 integrity rule (§1.4): the hook's own appends raise the
    # `[s:sid8]` count exactly like an agent-written log line would, so without
    # subtracting them from the self-log comparison AND resyncing `log_seen`
    # from them at the end of the Stop, the next round reads "the agent already
    # logged this" and its real work is never summarized — the same silent-loss
    # class D1 exists to fix.
    hook_appended: dict = {}
    # `hook_appended_keys`: {task-path: qualified key} for the same appends. The
    # F-1 resync writes into `log_seen`, which is keyed by qualified name (§3.3),
    # while `compute_round_active` subtracts by PATH — so both directions are
    # recorded at the single append-recording site instead of being re-derived
    # (a basename-only reverse lookup could not tell two projects apart).
    hook_appended_keys: dict = {}
    # Paths the deterministic exec-bind recorded this Stop (§1.3): that line IS
    # this round's entry for those tasks, so they are not also A_r candidates.
    exec_bound_now: set = set()

    def _record_append(path, key):
        hook_appended[path] = hook_appended.get(path, 0) + 1
        hook_appended_keys[path] = key

    # --- exec-binding bind (deterministic; §3.4) ----------------------------
    # Each resolved owning task missing its [s:sid8] line is bound directly by
    # the hook. Fork → skip (W2 delegation; guard inert per U3 but kept). On
    # bind failure (no @log:end / write fail) → skip+log + record in exec_tried
    # so we do not retry every Stop (打止め). INV-1: never blocks on missing.
    # NOTE: `exec_tried` carries TWO disjoint key shapes — `_rel()` paths here
    # (resolved-but-unbindable) and bare basenames from the unresolved-carry
    # report below. A `_rel()` value always starts `_projects/`, so the `in`
    # checks on either side can never cross-match.
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
                # The `[tasks:]` carry is bare by construction, and the exec-bind
                # resolves it against the PRIMARY project only (§1.3: the
                # deterministic exec-bind is unchanged by D2).
                _record_append(path, qualify(project, base))
                exec_bound_now.add(path)
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
    # The LEGACY un-suffixed sidecar name. Current rounds hand out the per-round
    # `{sid}.r{N}.capture` (§4.4.1 D1, `capture_sidecar_path`); this path is only
    # still READ, for a sidecar written from a pre-R-1 context block.
    capture_path = os.path.join(STATE_DIR, f'{session_id}.capture')
    current_index = qualified_task_index(project_roots)
    status = capture.get('status') or ''
    tried_notes = capture.get('tried_notes')
    tried_notes = list(tried_notes) if isinstance(tried_notes, list) else []
    # tried_tasks: touched task basenames that went through a capture cycle and
    # still cannot be placeholder-bound (no `@log:end` anchor). Bounding novelty
    # by this is the no-loop 打止め for un-bindable touched tasks (INV-1), the
    # analogue of `exec_tried` / `tried_notes`.
    tried_tasks = capture.get('tried_tasks')
    tried_tasks = list(tried_tasks) if isinstance(tried_tasks, list) else []
    tried_tasks = [qualify_legacy(t, project) for t in tried_tasks]  # F-4
    requested_ts = capture.get('requested_ts')
    # The request-time closed set, with its task keys normalized once (F-4) so
    # every consumer below — apply membership, `_fold_tried`, the G backstop —
    # sees qualified keys regardless of which version wrote the `.bind`.
    items_open = capture.get('items')
    if isinstance(items_open, dict) and isinstance(items_open.get('tasks'), list):
        items_open = dict(items_open)
        items_open['tasks'] = [qualify_legacy(t, project)
                               for t in items_open['tasks']]

    applied_summaries: list[str] = []
    applied_links: list[tuple] = []
    applied_link_skipped: list[tuple] = []
    applied_membership_skipped: list[str] = []
    proposals: list[str] = []

    def _fold_tried(items):
        for n in (items.get('notes') if isinstance(items, dict) else []) or []:
            if n not in tried_notes:
                tried_notes.append(n)

    # --- (A) apply delivered sidecars, per ROUND (§4.4.1 D1/D3) -------------
    # Round identity is carried by the sidecar's FILE NAME (`{sid}.r{N}.capture`)
    # so a sidecar that lands after its own round closed is still gated on ITS
    # round's frozen set. Before this, the gate read whatever `items` held at
    # apply time, so the common orchestration pattern (long verification between
    # Stops → the subagent outruns the 30s expiry → the next round commits
    # first) discarded the entire judgment layer of a round as `membership-skip`
    # — three times observed live in one session (R-1).
    #
    # The branches are evaluated TOP-DOWN, FIRST MATCH ONLY (F-B): the current
    # round is ALSO in `history`, so a non-exclusive form would match two rows.
    applied_this_stop = False
    round_mismatched: list[str] = []
    # The status as it stood at Stop ENTRY. The legacy branch below must keep
    # its CURRENT condition, so an r-file applied in the loop (which may set
    # `status='done'`) cannot change whether it runs.
    _status_at_entry = status

    def _apply_one(sidecar, items):
        """Apply ONE sidecar against `items` and merge its result into this
        Stop's report (several sidecars can land on the same Stop, so the
        result lists are extended, never rebound)."""
        _s, _l, _p, _ls, _ms = _apply_capture(
            sidecar, current_index, project, project_roots, sid8, iso_ts, items)
        applied_summaries.extend(_s)
        applied_links.extend(_l)
        proposals.extend(_p)
        applied_link_skipped.extend(_ls)
        applied_membership_skipped.extend(_ms)
        for _k in _s:
            _pk = current_index.get(_k)
            if _pk:
                _record_append(_pk, _k)  # F-1: hook write, not a self-log

    for _round_of, _rpath in scan_round_sidecars(STATE_DIR, session_id):
        _sidecar = _load_capture_sidecar(_rpath)
        if _sidecar is None:
            continue  # absent / torn — never partially applied (§10.1)
        if _round_of == round_n:
            # Row 1 — this round's own sidecar. `history` is the single lookup
            # (the open round is stored there too); `items_open` is the fallback
            # for a `.bind` written before `history` existed.
            _items = history.get(str(round_n)) or items_open
            _apply_one(_sidecar, _items)
            # F-A: ONLY a CURRENT-round apply may suppress (B). Letting an old
            # round's late arrival set this would push this round's expiry clock
            # out by one Stop per late sidecar.
            applied_this_stop = True
            # Consume: unlink so a later request cannot re-match a stale
            # sidecar. On unlink failure do NOT mark done — the next Stop
            # re-applies (text-key idempotent), keeping the apply eventual
            # (§10.2 / AC-11).
            try:
                os.remove(_rpath)
            except OSError:
                pass  # leave status; re-apply next Stop
            else:
                _fold_tried(_items)
                # A redelivery while the round is already `done` applies but
                # does NOT transition (D3 row 1) — the existing eventual
                # semantics are preserved, not extended.
                if _status_at_entry in ('requested', 'pending', 'expired'):
                    status = 'done'
        elif str(_round_of) in history:
            # Row 2 — an EARLIER round's sidecar, gated on THAT round's frozen
            # set. `status` is deliberately untouched: moving it here would let
            # round N's late arrival mark round N+1's open request `done`.
            _apply_one(_sidecar, history[str(_round_of)])
            try:
                os.remove(_rpath)
            except OSError:
                pass  # re-apply next Stop (idempotent)
        else:
            # Row 3 — outside the retained window (K exceeded, a non-conforming
            # name such as `r999`, or a `.bind` that lost its history: F-D, an
            # accepted degradation, identical in loss to the pre-R-1 behaviour).
            # Consumed WITHOUT applying, reported once.
            # F-C consume-then-report: the report is bound by a successful
            # unlink, so a failed unlink stays silent and retries next Stop
            # instead of re-reporting on every Stop (INV-1).
            try:
                os.remove(_rpath)
            except OSError:
                pass
            else:
                _rounds = sorted(int(k) for k in history)
                _span = f'r{_rounds[0]}..r{_rounds[-1]}' if _rounds else 'empty'
                _msg = (f'round-mismatch: sidecar r{_round_of} outside history '
                        f'({_span}); discarded')
                round_mismatched.append(_msg)
                print(f'[progress capture] {_msg} [s:{sid8}]', file=sys.stderr)

    # Legacy `{sid}.capture` (no round suffix): unchanged condition, unchanged
    # behaviour — gated on the CURRENT round's `items`. Retirement date TBD
    # (§4.4.4). A same-session sidecar can only carry this name if it was
    # written from a context block generated by a pre-R-1 hook version.
    if _status_at_entry in ('requested', 'pending', 'expired'):
        sidecar = _load_capture_sidecar(capture_path)
        if sidecar is not None:
            applied_this_stop = True
            _apply_one(sidecar, items_open)
            try:
                os.remove(capture_path)
            except OSError:
                pass  # leave status; re-apply next Stop
            else:
                _fold_tried(items_open)
                status = 'done'

    # --- (B) lifecycle transition for an un-delivered request --------------
    if status in ('requested', 'pending') and not applied_this_stop:
        age = datetime.datetime.now().timestamp() - float(requested_ts or 0)
        if age >= _CAPTURE_EXPIRY_S:
            _fold_tried(items_open)
            status = 'expired'  # §10.4 — G backstop takes over below
        else:
            status = 'pending'  # in-flight: do NOT block (AC-9, no double-spawn)

    # --- (C) round-active set A_r AFTER apply, and the novelty set ----------
    # §1.3: A_r = task writes in this round's ledger slice ∪ owners of the
    # notes written in it (the via-a-note loss path) ∪ this Stop's `[tasks:]`
    # exec carry − self-logged − tried_tasks. The note SCAN stays whole-session
    # (`touched`) because the over-bind fires on the expiry Stop, by which time
    # the round that requested it has already consumed its slice. The two
    # consumers of the scan are bounded by DIFFERENT sets: `novel_notes` by
    # `tried_notes`, and the `referenced` over-bind by the round's closed
    # `items['tasks']` (W6, §1.10). `tried_notes` can never bound the over-bind
    # — it is fed only from `items['notes']`, i.e. UNLINKED notes, and the
    # over-bind acts only on notes that HAVE an owner, so the two sets are
    # disjoint by construction.
    # D2 (§3.2): one reverse index per resolved project. `_scan_note_writes` is
    # self-filtering per project — a line from another project keeps its
    # `_projects/<other>/` prefix after `_to_project_rel` and fails the
    # `project-notes/` boundary check — so the whole-session `touched` list can
    # be handed to each project unchanged.
    reverse_indexes = {name: build_reverse_index(root)
                       for name, root in project_roots.items()}
    note_writes_by_project: dict = {}
    note_writes: list[str] = []
    unlinked: list[str] = []
    for _name, _root in project_roots.items():
        _nw, _un = _scan_note_writes(
            touched, _name, _root, reverse_indexes[_name])
        note_writes_by_project[_name] = _nw
        for _p in _nw:
            if _p not in note_writes:
                note_writes.append(_p)
        for _p in _un:
            if _p not in unlinked:
                unlinked.append(_p)
    novel_notes = [n for n in unlinked if n not in tried_notes]

    # W5 (§1.9): the round baseline as it stood when this Stop STARTED, taken
    # before `compute_round_active` raises `log_seen` for anything it judges
    # self-logged. `_round_base` falls back to THIS instead of the live
    # `log_seen`: the self-log pass sets `log_seen[key] = n_now`, so a live
    # fallback compares `n_now > n_now`, never fires, and the backstop appends a
    # `(referenced)` line next to the log line the agent had just written itself.
    # The M-1 bootstrap seeding inside `resolve_touch_cursor` IS included (it is
    # a legacy `.bind`'s legitimate baseline); only this Stop's own mutations are
    # excluded.
    log_seen_at_entry = dict(log_seen)

    exec_carry: dict = {}
    if not is_fork:
        turn_resolved = resolve_exec_tasks(exec_this_turn, project_root)
        for base, path in turn_resolved.items():
            if path in exec_bound_now:
                continue  # the deterministic exec-bind already recorded it
            if _rel(path, cwd) in exec_tried:
                continue  # unbindable; already 打止め on the exec-bind side
            exec_carry[qualify(project, base)] = path
        # F5 (§4.3 generalized to the exec side): a carry naming NO task md in
        # the primary project resolves to nothing, so it never reaches the
        # exec-bind loop above, never enters A_r, and — execution-by-reference
        # leaving no `.touched` line by construction — is picked up by NOTHING
        # else. Report it exactly once; before this it vanished with zero trace.
        #
        # Scoped to THIS TURN's carry, not the cumulative `exec_bind`: a task
        # bound on an earlier Stop and then deleted/renamed would otherwise be
        # re-reported as a miss on a turn that never claimed it.
        #
        # 打止め = a BARE BASENAME in `exec_tried` (INV-1): that list already
        # round-trips through the CLOSED `_load_bind`/`_save_bind` whitelist, so
        # no schema change and no new evaporation trap; its existing entries are
        # `_rel(path, cwd)` values, which always start `_projects/`
        # (PROGRESS_ROOT is `getcwd() + '/_projects'` and `main` returns early
        # without it), so the two shapes cannot collide. Only the REPORT is
        # bounded — resolution keeps retrying every Stop, which is what lets a
        # name claimed before its task file exists still bind later.
        for b in exec_this_turn:
            base = _exec_base(b)
            if not base or base in turn_resolved or base in exec_tried:
                continue
            exec_tried.append(base)
            # Best-effort hint only. `current_index` covers the primary project
            # plus the projects THIS session's ledger named, so a project never
            # written into is not searched (scanning all of `_projects/` would be
            # an unbounded new walk). Resolution stays primary-only (§3.6) — this
            # only tells the agent where to look.
            elsewhere = sorted({k.rsplit('/', 1)[0] for k in current_index
                                if k.rsplit('/', 1)[-1] == base})
            hint = f' (exists in: {", ".join(elsewhere)})' if elsewhere else ''
            exec_unresolved.append((base, hint))
            print(f'[progress capture] exec-skip(unresolved): {base} '
                  f'[s:{sid8}] — [tasks:] carry names no task md under '
                  f'_projects/{project}/tasks/; nothing bound.{hint}',
                  file=sys.stderr)
    active = compute_round_active(
        new_slice, project_roots, reverse_indexes, sid8,
        log_seen, tried_tasks, extra=exec_carry, hook_appended=hook_appended)

    # --- (D) deterministic G backstop once capture has resolved ------------
    # §10.4 / §1.6: the backstop guarantees a line for the round's CLOSED item
    # set once capture is done/expired. Apply runs BEFORE this (§10.2 ordering)
    # so a real summary is never pre-empted by a placeholder; likewise the
    # `referenced` over-bind runs before the generic placeholder so a note owner
    # keeps its more specific provenance. Anything already logged this round
    # (real summary / over-bind / the agent itself) is skipped via
    # `count_sid_lines > log_seen`.
    def _round_base(key):
        """This round's opening `[s:sid8]` count for the qualified `key` (§1.6).

        Falls back to `log_seen_at_entry` — the Stop-entry snapshot, NOT the
        live `log_seen` (W5, §1.9) — for a key the open round never froze: a
        legacy `.bind` that predates `round_base`, or a note owner the
        `referenced` over-bind reaches through the whole-session note scan
        without that owner having been in the round's item set. Both dicts are
        F-4-normalized above."""
        if key in round_base:
            return round_base[key]
        return log_seen_at_entry.get(key, 0)

    if status == 'expired':
        # Round bound (W6, §1.10). The note SCAN stays whole-session — slicing
        # it kills the over-bind outright, because the requesting round's slice
        # was consumed at its own request commit (§1.9) — but the OWNER set is
        # this round's frozen closed set. `items['tasks']` is that set with its
        # note owners already in it (`compute_round_active` unions them before
        # the freeze), and W5 keeps it un-retired, so it is the only
        # round-scoped fact the whole-session scan cannot supply. Without it
        # neither existing guard bounds a CARRIED owner across rounds: the text
        # key carries `(r{N})` so it differs every round, and `_round_base`
        # falls back to a `log_seen` the F-1 resync already advanced to the
        # current count — so every later expiry appended one more false
        # `(referenced)` line to a task whose note that round never touched,
        # forever, `@log` being append-only (measured r1..r5 -> 5 lines).
        # `None` = legacy `.bind` predating `items`: fail open, the same shape
        # as the placeholder backstop's legacy branch below.
        round_task_set = (set(items_open['tasks'])
                          if isinstance(items_open, dict)
                          and isinstance(items_open.get('tasks'), list) else None)
        for _name, _root in project_roots.items():
            _ridx = reverse_indexes[_name]
            for prel in note_writes_by_project[_name]:
                for owner_path in resolve_note_owner(prel, _root, _ridx):
                    okey = qualify(_name, os.path.basename(owner_path))
                    if round_task_set is not None and okey not in round_task_set:
                        continue  # not this round's work (see above)
                    ref_note = (f'(referenced) owner of {prel} via '
                                f'reverse-index; capture expired (r{round_n})')
                    # Text-key bound (W5, INV-1/§1.5). `append_auto_binding` is
                    # text-key idempotent and returns True for the no-op, so
                    # without this pre-check a repeat is recorded in
                    # `auto_bound` and the gate BLOCKS AND RE-REPORTS the same
                    # line on every Stop for as long as `status` stays
                    # `expired`. This loop is re-entered on every such Stop;
                    # presence of the exact key is the monotone stop condition
                    # WITHIN the round (a text key, once written, is never
                    # removed). Across rounds the tag changes — that is the
                    # round bound's job, not this guard's.
                    if log_block_has_note(owner_path, sid8, ref_note):
                        continue
                    if count_sid_lines(owner_path, sid8) > _round_base(okey):
                        continue  # this round already recorded a line for it
                    if append_auto_binding(owner_path, sid8, iso_ts, ref_note):
                        auto_bound.append(_rel(owner_path, cwd))
                        _record_append(owner_path, okey)
    if status in ('done', 'expired'):
        items = items_open
        if isinstance(items, dict) and isinstance(items.get('tasks'), list):
            backstop = [(k, current_index.get(k)) for k in items['tasks']]
        else:
            # Legacy `.bind` predating `items` — same fail-open shape as
            # `_apply_capture`: fall back to the pre-round rule (every touched
            # task still carrying no `[s:sid8]` line at all).
            backstop = [(k, p) for k, p in resolved.items()
                        if not log_block_has_sid(p, sid8)]
        placeholder = f'(auto) touched; summary pending (r{round_n})'
        for key, path in backstop:
            if not path or key in tried_tasks:
                continue
            # Text-key bound (W5), the same monotone stop condition as the
            # `referenced` loop above: `items` is no longer retired, so this
            # backstop is re-entered for the same closed set on every later
            # Stop, and a no-op re-append must never be reported as an action.
            if log_block_has_note(path, sid8, placeholder):
                continue
            if count_sid_lines(path, sid8) > _round_base(key):
                continue  # this round already has a line — no placeholder
            if append_auto_binding(path, sid8, iso_ts, placeholder):
                auto_bound.append(_rel(path, cwd))
                _record_append(path, key)
            else:
                # Cannot bind (ambiguous @log damage that D3 generation cannot
                # resolve) after a full capture cycle — stop requesting it
                # (打止め / no-loop, INV-1) and surface it once (§4.3): this
                # branch used to be a SILENT drop, unlike its exec-bind twin.
                tried_tasks.append(key)
                skip_rel = _rel(path, cwd)
                bind_skipped.append(skip_rel)
                print(f'[progress capture] bind-skip(no-anchor): {skip_rel} '
                      f'[s:{sid8}] — no writable <!-- @log:begin/end --> block; '
                      f'left unbound.', file=sys.stderr)

    # --- F-1 integrity rule (§1.4 (a)) --------------------------------------
    # Resync `log_seen` from every task THIS Stop appended to (apply summaries,
    # placeholders, `referenced` over-binds, exec-binds). Runs AFTER (D) so the
    # backstop above still sees the round's opening baseline, and BEFORE the
    # commit below so the value persisted is the post-write truth. Skipping this
    # is what would make the hook's own writes look like an agent self-log on
    # the next Stop and silently swallow that round.
    for _path in hook_appended:
        _key = hook_appended_keys.get(_path)
        if _key:
            log_seen[_key] = count_sid_lines(_path, sid8)

    # --- (E) request capture when this round still has unlogged activity ----
    # Novelty is bounded by the 打止め sets and by the cursor: a slice is
    # examined once, a touched task already tried (no bindable anchor) and a
    # note already attempted no longer re-trigger a spawn (INV-1).
    spawn = False
    if status in ('', 'done', 'expired') and (active or novel_notes):
        requested_ts = datetime.datetime.now().timestamp()
        round_n += 1
        round_base = {}
        for key, path in active.items():
            # Round-open baseline (§1.6): the count as it stands NOW, so any
            # line that lands before this round closes counts as this round's.
            n = count_sid_lines(path, sid8)
            log_seen[key] = n
            round_base[key] = n
        # §4.4.1 D2: this round's closed set also goes into `history`, keyed by
        # round, and the window is pruned to the last `_ROUND_HISTORY_K` rounds.
        # This is the ONLY write point — a round's set is frozen exactly when it
        # is requested, which is what makes a late sidecar checkable against it.
        history[str(round_n)] = {'tasks': sorted(active.keys()),
                                 'notes': list(novel_notes)}
        history = {k: v for k, v in history.items()
                   if int(k) > round_n - _ROUND_HISTORY_K}
        capture = {
            'status': 'requested',
            'items': {'tasks': sorted(active.keys()), 'notes': novel_notes},
            'requested_ts': requested_ts,
            'tried_notes': tried_notes,
            'tried_tasks': tried_tasks,
            'touch_cursor': len(raw_lines),
            'round': round_n,
            'log_seen': log_seen,
            'round_base': round_base,
            # Evaporation trap (§1.7 / §4.4.1 D2): `_load_bind`/`_save_bind`
            # round-trip `capture` opaquely, but BOTH dict literals here rebuild
            # it from scratch — a key missing from either one is silently gone
            # every Stop.
            'history': history,
        }
        spawn = True
    else:
        items = items_open  # F-4-normalized (qualified) form
        cursor_out = touch_cursor
        if status in ('', 'done', 'expired'):
            # Re-requestable but nothing novel: the slice WAS consumed, so
            # advance the cursor without opening a round (§1.6).
            #
            # W5 (§1.9): the resolved round's `items` / `round_base` are NOT
            # retired here. W2 retired them on the rationale that an un-retired
            # `items` re-placeholders the same round every Stop; that rationale
            # was over-broad. The placeholder is bounded by
            # `count_sid_lines > _round_base` (plus the text key), and
            # `round_base` is frozen at request time — so keeping BOTH is what
            # makes that guard work, while retiring `round_base` is what broke
            # it (the `_round_base` fallback then compares against the live
            # `log_seen` the F-1 resync just advanced). Retiring `items` also
            # never bounded the `referenced` over-bind, which iterates the
            # whole-session note scan and not `items` at all.
            #
            # Retiring `items` DID have a cost: it is the request-time closed
            # set `_apply_capture` gates membership on, so a sidecar that
            # arrives after the expiry Stop had its whole judgment
            # membership-skipped — every `confirmed` summary and every
            # `note_links` entry discarded (the W5 defect, observed live on
            # session e810b706). Keeping the set means a late sidecar still
            # applies, which is the entire point of the async apply-path being
            # eventual (AC-11).
            cursor_out = len(raw_lines)
        # In-flight (`requested`/`pending`): the cursor deliberately does NOT
        # move, so activity during the round is carried into the next one.
        capture = {
            'status': status,
            'items': items,
            'requested_ts': requested_ts,
            'tried_notes': tried_notes,
            'tried_tasks': tried_tasks,
            'touch_cursor': cursor_out,
            'round': round_n,
            'log_seen': log_seen,
            'round_base': round_base,
            # SECOND of the two dict literals (§4.4.1 D2). Carried unchanged —
            # `history` is written only at the request commit above — but it
            # MUST be re-emitted here or every non-requesting Stop drops it.
            'history': history,
        }

    # --- Gate (INV-1): block only to (b) report binds, (c) report exec-skip,
    # (d) spawn capture, or to surface proposals. `requested` is committed before
    # the block, so the next Stop re-enters via the requested/pending branch
    # (no re-block loop). An in-flight `pending` with nothing to report → no
    # block (AC-9).
    report_binds = auto_bound or applied_summaries or applied_links or applied_link_skipped or applied_membership_skipped or bind_skipped or round_mismatched
    if (not spawn and not report_binds and not exec_skipped
            and not exec_unresolved and not proposals):
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
        f'[progress capture] exec-skip(unresolved): {base}{hint}\n'
        for base, hint in exec_unresolved
    )
    auto_lines += ''.join(
        f'[progress capture] bind-skip(no-anchor): {rel}\n' for rel in bind_skipped
    )
    auto_lines += ''.join(
        f'[progress capture] link-skip: {note} -> {b}\n'
        for note, b in applied_link_skipped
    )
    auto_lines += ''.join(
        f'[progress capture] membership-skip: {name}\n' for name in applied_membership_skipped
    )
    auto_lines += ''.join(
        f'[progress capture] {m}\n' for m in round_mismatched
    )

    if spawn:
        # Round-scoped display (§1.3): the subagent summarizes THIS round's
        # work, so it is shown this round's ledger slice, not the whole session.
        shown = slice_display[:MAX_TOUCHED_IN_INJECTION]
        tail = '' if len(slice_display) <= MAX_TOUCHED_IN_INJECTION else \
            f' ...({len(slice_display) - MAX_TOUCHED_IN_INJECTION} more)'
        # Per-round sidecar path (§4.4.1 D1): the round the (E) commit above
        # just opened. Both the context block and the step-3 prose below take it
        # from this single value (review F-I3).
        round_capture_path = capture_sidecar_path(STATE_DIR, session_id, round_n)
        sidecar_path_display = _to_forward_slash(round_capture_path)
        context = build_capture_context(
            sid8, iso_ts, round_capture_path, project_root, project_roots,
            sorted(active.keys()), novel_notes, round_n,
        )
        reason = (
            f'{auto_lines}'
            f'[progress capture] session={sid8} date={date}\n'
            f'touched: {" ".join(shown)}{tail}\n\n'
            f'Spawn the async capture subagent to summarize this round\'s task '
            f'work and map note deliverables to owning tasks. Do NOT update '
            f'`@log` / `@notes` yourself — the taskflow Stop hook applies the '
            f'subagent\'s result deterministically on a later Stop.\n\n'
            f'1. Use the Agent tool with subagent_type `{CAPTURE_AGENT_TYPE}`.\n'
            f'2. In its prompt, give this context block verbatim AND add, in '
            f'prose, what you did in this round (which tasks you advanced, which '
            f'project-notes you wrote/read, any task-worthy work with no task '
            f'file yet):\n'
            f'   {context}\n'
            f'3. The subagent MUST write its judgment as JSON to '
            f'`{sidecar_path_display}` and write nothing else. '
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
        if exec_unresolved:
            note += ('exec-skip(unresolved) = the [tasks:] carry named a file '
                     f'that is not a task md in project `{project}` — nothing '
                     'was bound and no other mechanism records this work. Check '
                     'the basename spelling; the carry resolves in the PRIMARY '
                     'project only, so a task in another project must be bound '
                     'by editing that task file directly. ')
        if bind_skipped:
            note += ('bind-skip(no-anchor) = the touched task has no writable '
                     '<!-- @log:begin/end --> block (damage too ambiguous to '
                     'repair or generate); left unbound. ')
        if auto_bound:
            note += ('auto-bound = deterministic backstop (placeholder / '
                     'referenced); a richer summary is no longer needed. ')
        if applied_summaries or applied_links:
            note += 'applied entries came from the capture subagent. '
        if round_mismatched:
            note += ('round-mismatch = a capture sidecar named a round no '
                     'longer retained in the .bind round history; it was '
                     'discarded unapplied. ')
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
