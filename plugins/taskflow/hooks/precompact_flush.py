#!/usr/bin/env python3
"""PreCompact hook: flush the current round's unwritten task progress before the
conversation is summarized away.

Design: project-notes/specs/capture-detection-gaps.md §2. This is NOT a
compaction-specific mechanism — it is a SECOND CALL SITE of D1's round
computation (§10 rejected the "dedicated compaction pipeline" alternative
precisely because a duplicated judgment rots). Everything it decides is
computed by `session_progress_capture.py`'s own helpers, imported here;
nothing about the pending set is re-implemented.

What it does (§2.2), deterministically, with no subprocess and no LLM:
  1. Inherit `_projects` / STATE_DIR / STATE_ROOT by importing them from
     `session_progress_capture` (below) rather than resolving a second root
     here; they come from `_find_state_root(os.getcwd()) or os.getcwd()` —
     the walk up to the nearest ancestor (the cwd itself included) holding
     `_projects/_state` — which every root-resolving taskflow hook now shares
     (no env override — `e2e_state_dir_sandbox`).
     Exit 0 on anything missing.
  2. Compute the pending set = D1's round-active set A_r over the ledger slice
     `raw[touch_cursor:]` (`resolve_touch_cursor` + `compute_round_active`).
     DETECTION LIMIT (F-5): the PreCompact payload carries no
     `last_assistant_message` (measured, §2.1 T-PC-1 probe — the payload is
     `{session_id, transcript_path, cwd, prompt_id, hook_event_name, trigger,
     custom_instructions}`), so A_r's `[tasks:]` exec-carry component — work
     claimed by reference with no file write — is NOT computable here and is
     deliberately omitted. Flushing that class stays Stop-only.
  3. Append `(auto) unflushed at compaction; summary pending (r{N})` to each
     pending task through `append_auto_binding`, so the write takes `log_lock`
     (INV-2) and is idempotent on the `[s:<sid8>]: <note>` text key (INV-3).
     N = `capture.round` + 1 — the round the NEXT Stop will commit (F-10), so
     two compactions inside one round produce the same key and therefore a
     single line.
  4. Emit ONE plain-text stdout line naming the pending tasks — as QUALIFIED
     `<project>/<basename>` keys since D2 landed (F-6, §2.2 step 3), the same
     names the round machinery uses — and nothing at all when the pending set
     is empty. Channel and format are fixed by the
     T-PC-1 live probe (§2.1): stdout is joined verbatim into the summarizer's
     instructions AND survives as a `PreCompact [...] completed successfully:`
     message in the post-compaction conversation, while a JSON object is NOT
     parsed (it is pasted verbatim), so JSON output would buy nothing. Silence
     on the common path matters: any stdout invalidates Claude Code's
     precomputed-compaction reuse.

`.bind` is READ-ONLY here (§2.2 step 4). The Stop hook is its single writer
(note-task-link.md §10.1); not writing it is what structurally removes any
PreCompact↔Stop write race and leaves the cursor unmoved, so the Stop that
follows a compaction still forms its normal round over the same slice. The
placeholder written here does not collide with that round's real summary: the
notes differ, hence the text keys differ. And because PreCompact cannot update
`log_seen`, `count_sid_lines` permanently EXCLUDES lines whose note starts with
`_PRECOMPACT_NOTE_PREFIX` (F-1 (b)) — otherwise a compaction would make the
next Stop read "the agent self-logged" and silently drop the round.
"""
import json
import os
import sys

# Sibling import (same pattern as the other hooks — hook scripts run standalone
# with no package context, so this file's own directory goes on sys.path first).
# `session_progress_capture` is imported for its ROUND LOGIC, not copied: a
# second copy of the pending calculation or of the placeholder prefix would
# drift from the Stop hook that has to stay consistent with it.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from note_links import build_reverse_index  # noqa: E402
from session_progress_capture import (  # noqa: E402
    PROGRESS_ROOT,
    STATE_DIR,
    STATE_ROOT,
    _PRECOMPACT_NOTE_PREFIX,
    _load_bind,
    append_auto_binding,
    compute_round_active,
    qualify_legacy,
    read_touched,
    read_touched_raw,
    resolve_project_roots,
    resolve_touch_cursor,
    resolve_touched_tasks,
)
from tstamp import now_iso  # noqa: E402

_STDOUT_PREFIX = ('Preserve verbatim in the summary: unwritten per-task '
                  'progress (results, decisions, remaining steps) for: ')


def main() -> int:
    if not os.path.isdir(PROGRESS_ROOT):
        return 0
    try:
        data = json.loads(sys.stdin.buffer.read().decode('utf-8'))
    except Exception:
        return 0
    if not isinstance(data, dict):
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
    if not isinstance(state, dict):
        return 0
    project = state.get('project', '')
    if not project:
        return 0  # session is outside taskflow's management
    project_root = os.path.join(PROGRESS_ROOT, project)
    if not os.path.isdir(project_root):
        return 0

    # STATE_ROOT, not `os.getcwd()`: this is the base the `.touched` ledger is
    # READ against, and `touched_capture.py` WRITES it against its own
    # STATE_ROOT (`cwd = STATE_ROOT` in its `main()`). The two were identical
    # while every hook anchored on the cwd; once the root follows the ancestor
    # search they differ in exactly the newly-reachable configuration (a
    # subdir-launched session), and every ledger line would silently fail to
    # match. 02-plan.md §6.2.
    cwd = STATE_ROOT
    sid8 = session_id[:8]
    touched_path = os.path.join(STATE_DIR, f'{session_id}.touched')
    raw_lines = read_touched_raw(touched_path, cwd)
    if not raw_lines:
        return 0

    # --- `.bind` READ (never written here — §2.2 step 4) --------------------
    bind_path = os.path.join(STATE_DIR, f'{session_id}.bind')
    bind_existed = os.path.exists(bind_path)
    capture = _load_bind(bind_path)['capture']
    round_n = capture.get('round')
    round_n = round_n if isinstance(round_n, int) else 0
    log_seen = capture.get('log_seen')
    log_seen = dict(log_seen) if isinstance(log_seen, dict) else {}
    tried_tasks = capture.get('tried_tasks')
    tried_tasks = list(tried_tasks) if isinstance(tried_tasks, list) else []
    # F-4 (§3.4): a `.bind` written before D2 keys these by BARE basename. Read
    # them as the primary project's qualified keys, exactly as the Stop hook
    # does. Nothing is written back here — `.bind` is read-only in this hook
    # (§2.2 step 4) — so the normalization is in-memory only and the Stop that
    # follows is what persists it.
    log_seen = {qualify_legacy(k, project): v for k, v in log_seen.items()}
    tried_tasks = [qualify_legacy(t, project) for t in tried_tasks]

    # D2 (§3.2): resolve every project the ledger names, each under its own
    # root, so a cross-project write is flushed too instead of being dropped.
    touched = read_touched(touched_path, cwd)
    project_roots = resolve_project_roots(touched, PROGRESS_ROOT, project)
    reverse_indexes = {name: build_reverse_index(root)
                       for name, root in project_roots.items()}
    resolved = resolve_touched_tasks(touched, project_roots)
    # `log_seen` is mutated by both calls below; the copy above is why that
    # stays in memory. Persisting it is the Stop hook's job, exclusively.
    touch_cursor = resolve_touch_cursor(
        capture, bind_existed, raw_lines, resolved, sid8, log_seen)
    new_slice = raw_lines[touch_cursor:]
    if not new_slice:
        return 0  # nothing consumed since the last committed round

    pending = compute_round_active(
        new_slice, project_roots, reverse_indexes,
        sid8, log_seen, tried_tasks)
    if not pending:
        return 0  # common case: stay completely silent (precompute reuse)

    # Built from the shared prefix constant so the writer here and the
    # `count_sid_lines` exclusion there can never disagree (§1.4 (b)).
    note = f'{_PRECOMPACT_NOTE_PREFIX}; summary pending (r{round_n + 1})'
    iso_ts = now_iso()
    names = sorted(pending)  # qualified `<project>/<basename>` keys (F-6)
    for key in names:
        append_auto_binding(pending[key], sid8, iso_ts, note)

    # One plain-text line. Emitted for the PENDING set, not for the subset that
    # appended successfully: when a task's `@log` anchor is unbindable the
    # summary instruction is the ONLY channel left for that task's progress.
    sys.stdout.write(_STDOUT_PREFIX + ' '.join(names) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
