# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Per-session wiki on/off toggle state (Phase 1 P2).

Records the `wiki:on|off` toggle for a session as a per-sid dotfile under the
resolved wiki root, alongside the existing root-level dotfiles
(`.llmwiki.lock`, `.llmwiki.txn`, `.llmwiki.txn.d/`, `.cc-turn-ledger.jsonl`).
Design §4-P2: a per-sid file avoids the read-modify-write races a single shared
file would have across concurrent sessions on the SAME wiki.

State encoding (existence flag — no file contents parsed):

    <root>/.llmwiki.toggle.d/<session_id>.off  exists  -> OFF for that session
    (absent)                                            -> ON  (default)

So `wiki:off` creates the file, `wiki:on` removes it, and a brand-new session
(no file) is ON. The toggle is session-sticky purely by the file persisting
across turns within a session; a new session gets a fresh sid and thus starts
ON (design: permanent off is the wiki config `activation_scope: manual`, not
this per-session toggle).

`prune()` removes `*.off` files older than `PRUNE_AGE_SEC` (mtime) so abandoned
sessions do not accumulate. Every operation is best-effort: any OSError is
swallowed so toggle bookkeeping can never break the host hook (same robustness
policy as the resolver's pj skip).
"""

from __future__ import annotations

import time
from pathlib import Path

TOGGLE_DIRNAME = ".llmwiki.toggle.d"
OFF_SUFFIX = ".off"
# Abandoned-session pruning window: 7 days (design left the value to
# implementation; matches "long enough to never touch a live session").
PRUNE_AGE_SEC = 7 * 24 * 3600


def _toggle_dir(root: Path) -> Path:
    return Path(root) / TOGGLE_DIRNAME


def _off_file(root: Path, session_id: str) -> Path:
    return _toggle_dir(root) / f"{session_id}{OFF_SUFFIX}"


def is_on(root: "str | Path", session_id: str) -> bool:
    """True (ON) unless this session's `.off` marker exists. Default ON."""
    if not session_id:
        return True
    try:
        return not _off_file(Path(root), session_id).exists()
    except OSError:
        return True


def set_state(root: "str | Path", session_id: str, on: bool) -> None:
    """Record ON/OFF for `session_id`: remove the `.off` marker (on) or touch it.

    Best-effort: swallows OSError so toggle bookkeeping never breaks the caller.
    Also prunes stale markers opportunistically on every write.
    """
    if not session_id:
        return
    root = Path(root)
    off = _off_file(root, session_id)
    try:
        if on:
            off.unlink(missing_ok=True)
        else:
            _toggle_dir(root).mkdir(parents=True, exist_ok=True)
            off.touch(exist_ok=True)
    except OSError:
        pass
    prune(root)


def prune(root: "str | Path", max_age_sec: int = PRUNE_AGE_SEC) -> None:
    """Remove `*.off` markers whose mtime is older than `max_age_sec`.

    Best-effort: any OSError (missing dir, unreadable entry) is swallowed.
    """
    d = _toggle_dir(Path(root))
    try:
        now = time.time()
        for f in d.glob(f"*{OFF_SUFFIX}"):
            try:
                if now - f.stat().st_mtime > max_age_sec:
                    f.unlink(missing_ok=True)
            except OSError:
                continue
    except OSError:
        return
