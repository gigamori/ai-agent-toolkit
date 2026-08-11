# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb"]
# ///
"""Fork-aware pi-log projector — mirrors cc_log_project for the pi session format.

Projects a SINGLE pi session (identified by sid CLI argument) from
``~/.pi/agent/sessions/--<encoded-cwd>--/<ts>_<sid>.jsonl`` via DuckDB, then:

  1. Groups JSONL entries where ``type=="message"`` and
     ``message.role in ("user","assistant")`` into turns, linearized along the
     ``id``/``parentId`` tree's active path.
  2. Fork dedup: follows the ``parentSession`` chain in the session header
     (``type=="session"``, first line of each file), excludes turns already
     present in a parent session by content-hash (same dedup principle as
     cc_log_project).
  3. Renders markdown for the surviving novel turns in the same shape
     fe_pi_log (frontends) expects (``## Turn N [ts]``, ``**Human**:``,
     ``**Assistant**:``).

Session path encoding: a single leading ``/`` or ``\\`` in the cwd is
stripped, then remaining ``[/\\:]`` are replaced by ``-``, and the encoded
cwd is wrapped in ``--..--``.  This matches session-manager.ts
``getDefaultSessionDir`` (pi-mono packages/coding-agent/src/core/
session-manager.ts:424, re-verified 2026-07-03).

Two projection entry points (R1 — pi mirror of cc_log_project's Path B
scan-collapse; pi mirror is filesystem-walk-collapse, not DuckDB-scan-
collapse, since each pi session is its own file, not a shared corpus):
    Path A (single session, /wiki-file) uses ``project_owned`` — one
    session file load is fine for one sid.
    Path B (whole project, /wiki-ingest-sessions) must NOT re-walk the whole
    session directory tree once per sid. So the EXPENSIVE part (the
    directory walk + per-file DuckDB load + hash) is factored into
    ``extract_turns_batch`` (ONE filesystem walk for ALL sids, run once by
    the read-only ``project-batch`` driver verb before the loop), and the
    CHEAP part (within-sid exact dedup + ledger diff + markdown) stays in
    ``project_from_turns``, run per-sid INSIDE ``begin`` so the ledger
    read-after-write (F3) stays sequential.  ``project_owned`` is the
    composition of the two (extract this one sid, then project) so Path A
    is unchanged.

I/O contract:
    extract_turns_batch(sids, *, ledger) -> {sid: [turn_dict, ...]}
      One filesystem walk under the session dir for ALL sids (R1). Each
      turn_dict is {id, parentId, role, ts, text, hash} — hash is assigned
      HERE (ledger.compute_hash(role, text)) so batch and begin agree on
      the F5 hash. A sid with no matching session file maps to [] (does
      NOT raise).

    project_from_turns(wiki_root, sid, turns, *, ledger) -> ProjectionResult
      Consumes already-extracted turns (does NOT touch DuckDB): within-sid
      exact dedup -> ledger diff -> markdown. Raises ProjectionError if a
      non-empty-text turn is missing its ``hash`` key (fail-closed).

    Both extraction entry points blank llm-wiki command-invocation turns
    (D7-pi, ``_blank_command_invocations``) before assigning hashes: pi
    persists the EXPANDED prompt template as the user's turn, so an
    un-blanked invocation would be filed as conversation content by the NEXT
    run of the same command. See that helper for the full rationale, and for
    the accepted residue (third-party template expansions are not detectable).

    project_owned(wiki_root, sid, *, ledger) -> ProjectionResult   # Path A
      Locate the .jsonl file for ``sid``, load it, extract turns on the active
      path, assign hash, then project_from_turns.

      ProjectionResult {
        markdown: str,        # transcript of the NOVEL turns only
        novel_entries: list,  # {hash, first_sid, first_ts} per novel turn
        ledger_skipped: int,  # turns dropped by ledger diff (already owned)
      }
      Raises ProjectionError on file-not-found / parse failure.

    session_dir_for_cwd(cwd) -> Path
      The ``--<encoded-cwd>--`` session directory for a given cwd.

    sids_in_session_dir(dir) -> [(ts_prefix, sid), ...]
      Session files directly inside a session directory.

    session_dir_for_sid(sid) -> Path | None
      Parent dir of ``sid``'s session file, or None if not found (F-6).

NOTE on active-path and branch_summary/compaction (F5 — TBD, pinned at P6):
    The pi log uses an id/parentId tree.  For P0.5 the active path is the
    LONGEST chain from the root entry (id with no parentId or parentId absent
    from the file) to a leaf, breaking ties by last-occurrence order.
    ``branch_summary`` and ``compaction`` entries are skipped (not ``type==
    "message"``).  The exact pinning of F5 rules (which leaf counts as active)
    is deferred to P6 where real pi session data is available for verification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    import duckdb
except ImportError:  # pragma: no cover - dependency declared in script header
    duckdb = None

# SQL views for the pi session log format (sibling file).
_VIEWS_SQL = Path(__file__).resolve().parent / "pi_views.sql"

# Encoding rule matching session-manager.ts getDefaultSessionDir (pi-mono
# packages/coding-agent/src/core/session-manager.ts:424, verified 2026-07-03):
#   const safePath = `--${cwd.replace(/^[/\\]/, "").replace(/[/\\:]/g, "-")}--`;
# i.e. strip a SINGLE leading '/' or '\', THEN replace every remaining
# '/', '\', ':' with '-' (R-OI1-8 — this leading-strip step was previously
# unimplemented here; see _encode_cwd below).
_CWD_LEADING_RE = re.compile(r"^[/\\]")
_CWD_RE = re.compile(r"[/\\:]")


class ProjectionError(Exception):
    """File-not-found / parse failure while projecting a pi session."""


@dataclass
class ProjectionResult:
    """Return of project_owned."""
    markdown: str
    novel_entries: list = field(default_factory=list)
    ledger_skipped: int = 0


# ---------------------------------------------------------------------------
# Session file location
# ---------------------------------------------------------------------------

def _encode_cwd(cwd: str) -> str:
    """Encode a cwd path the same way session-manager.ts does.

    Mirrors ``getDefaultSessionDir`` (session-manager.ts:424): strip a single
    leading ``/`` or ``\\``, then replace every remaining ``/``, ``\\``, ``:``
    with ``-``. The caller wraps the result in ``--...--`` (see
    ``session_dir_for_cwd``).
    """
    stripped = _CWD_LEADING_RE.sub("", cwd, count=1)
    return _CWD_RE.sub("-", stripped)


def _session_dir() -> Path:
    """Return ~/.pi/agent/sessions (or PI_CODING_AGENT_DIR override)."""
    import os
    override = os.environ.get("PI_CODING_AGENT_DIR")
    if override:
        return Path(override) / "sessions"
    return Path.home() / ".pi" / "agent" / "sessions"


def _parse_session_stem(stem: str) -> "tuple[str, str] | None":
    """Split a session file stem ``<ts>_<sid>`` into ``(ts, sid)`` (A2).

    Returns ``None`` if the stem does not split into two non-empty parts
    (a non-session file, e.g. ``journal``). ``partition`` splits on the
    FIRST ``_`` — the ts prefix uses ``-`` (not ``_``) and sids are uuids
    (no ``_``), so this is unambiguous (design spec fact 11).
    """
    ts, sep, sid = stem.partition("_")
    if not sep or not ts or not sid:
        return None
    return ts, sid


def _find_session_file(sid: str) -> Path:
    """Locate the .jsonl file for ``sid`` under the session directory.

    Files are named ``<ts>_<sid>.jsonl`` inside a
    ``--<encoded-cwd>--/`` subdirectory.  We search recursively for
    ``*_<sid>.jsonl`` under the session dir.
    """
    base = _session_dir()
    pattern = f"*_{sid}.jsonl"
    matches = list(base.rglob(pattern))
    if not matches:
        raise ProjectionError(
            f"pi session file not found for sid {sid!r} under {base}"
        )
    # If multiple matches exist (unlikely), use the most recent by mtime.
    return max(matches, key=lambda p: p.stat().st_mtime)


def _walk_all_session_files() -> "dict[str, list[Path]]":
    """One rglob under the session dir; group matches by sid (R1 batch walk).

    Generalizes ``_find_session_file``'s per-sid rglob into a SINGLE
    filesystem walk covering every sid at once (used by
    ``extract_turns_batch``). Non-session stems are skipped via
    ``_parse_session_stem``.
    """
    base = _session_dir()
    by_sid: "dict[str, list[Path]]" = {}
    for p in base.rglob("*.jsonl"):
        parsed = _parse_session_stem(p.stem)
        if parsed is None:
            continue
        _ts, sid = parsed
        by_sid.setdefault(sid, []).append(p)
    return by_sid


# ---------------------------------------------------------------------------
# Session enumeration helpers (design A2 — session_plan pi cwd/pj dispatch)
# ---------------------------------------------------------------------------

def session_dir_for_cwd(cwd: "str | Path") -> Path:
    """Return the session directory for ``cwd`` (session-manager.ts:423-425).

    ``_session_dir() / f"--{_encode_cwd(str(cwd))}--"`` — the same
    ``--<encoded-cwd>--`` subdirectory ``getDefaultSessionDir`` computes.
    Pure path construction; does not create the directory (unlike the JS
    ``mkdirSync`` side effect) and does not check existence.
    """
    return _session_dir() / f"--{_encode_cwd(str(cwd))}--"


def sids_in_session_dir(dir: Path) -> "list[tuple[str, str]]":
    """List ``(ts_prefix, sid)`` pairs for session files directly in ``dir``.

    Filenames are ``<ts>_<sid>.jsonl``; split via ``_parse_session_stem``.
    Non-session stems (no ``_``, e.g. ``journal``) are skipped. Not
    recursive — matches only files directly inside ``dir``.
    """
    out: "list[tuple[str, str]]" = []
    for p in sorted(Path(dir).glob("*.jsonl")):
        parsed = _parse_session_stem(p.stem)
        if parsed is None:
            continue
        out.append(parsed)
    return out


def session_dir_for_sid(sid: str) -> "Path | None":
    """Return the parent dir of ``sid``'s session file, or ``None`` (F-6).

    pi version of cc's ``_cc_project_dir_from_running_session`` (U3
    primary): locate the session file for ``sid`` and return its parent
    dir. Not-found is caught and turned into ``None`` (instead of raising)
    so callers can chain primary -> fallback (session_plan cwd fallback).
    """
    try:
        return _find_session_file(sid).parent
    except ProjectionError:
        return None


# ---------------------------------------------------------------------------
# DuckDB projection over pi JSONL
# ---------------------------------------------------------------------------

def _load_and_project(session_file: Path, sid: str) -> list[dict]:
    """Load the session JSONL via DuckDB and extract message turns.

    Returns a list of turn dicts: {id, parentId, role, ts, text}.
    Only ``type=="message"`` rows with ``role in ("user","assistant")``
    are included.  ``branch_summary`` / ``compaction`` entries are skipped.
    """
    if duckdb is None:  # pragma: no cover
        raise ProjectionError("duckdb not available")

    sql_path = _VIEWS_SQL
    try:
        con = duckdb.connect()
        con.execute(sql_path.read_text(encoding="utf-8").replace(
            "__PI_SESSION_FILE__", str(session_file).replace("\\", "/")
        ))
        rows = con.execute(
            "SELECT entry_id, parent_id, role, ts, text "
            "FROM pi_message "
            "ORDER BY ts ASC, entry_id ASC"
        ).fetchall()
    except Exception as e:  # noqa: BLE001
        raise ProjectionError(
            f"projection failure for pi sid {sid!r}: {e}"
        ) from e

    turns = []
    for (entry_id, parent_id, role, ts, text) in rows:
        turns.append({
            "id": entry_id or "",
            "parentId": parent_id or "",
            "role": role or "",
            "ts": str(ts) if ts else "",
            "text": text or "",
        })
    return turns


def _active_path(turns: list[dict]) -> list[dict]:
    """Select the active path (F5 — pinned at P6).

    Active-path rule (F5, pinned 2026-07-03 against real pi session data):

    Pi session logs interleave ``user``/``assistant`` entries with ``toolResult``
    entries in the id/parentId chain.  Because ``pi_message`` (pi_views.sql)
    only projects ``role IN ('user','assistant')``, most assistant entries have a
    ``parentId`` pointing to an excluded ``toolResult`` node — they all appear as
    "roots" to the DFS tree, which would break longest-chain selection.

    Real pi sessions observed in P6 testing have NO branching (every entry has
    exactly one parent and zero or one child in the full tree). Therefore:

        Active path = all turns in chronological order (ORDER BY ts ASC,
        entry_id ASC) as already returned by the SQL query in _load_and_project.

    The DFS is retained as a no-op pass (returns the same list) for structural
    symmetry with cc_log_project; it degenerates to identity because every turn
    is a "root" with no children in the filtered user/assistant set.

    If branching is ever observed in pi session logs, this rule must be revisited.
    """
    # Pi sessions are linear; the SQL already returns turns in chronological
    # order.  Return as-is (DFS would find all-roots with no children due to
    # toolResult interleaving, and the "longest path" for each root would be
    # length 1, so this is equivalent and much simpler).
    return list(turns)


# ---------------------------------------------------------------------------
# Dedup + markdown rendering
# ---------------------------------------------------------------------------
# D7-pi — blank the llm-wiki command-invocation turn at extraction.
#
# WHY THIS EXISTS AT ALL (pi-specific; cc has no equivalent shape):
# pi persists the EXPANDED prompt template as the user's turn. `prompt()` runs
# `expandPromptTemplate` on the input BEFORE the message is sent or persisted
# (agent-session.ts), and `expandPromptTemplate` returns `template.content`
# with the argument placeholders substituted; `content` is the .md file's body
# with its frontmatter stripped and `.trim()`ed (prompt-templates.ts ->
# utils/frontmatter.ts). So typing `/wiki-file` writes this package's whole
# prompt body into the session log as one user message whose FIRST LINE is
# that body's H1 — `# /wiki-file`. cc records the typed line instead, which is
# why cc_log_project strips a LINE (D7) and this module blanks a TURN.
#
# WHAT BREAKS WITHOUT IT: the D13 cutoff drops that turn for the run that made
# it, but a cutoff is a projection-time cut, not a ledger entry — nothing marks
# the turn as owned. The NEXT invocation therefore sees it as ordinary
# conversation and files several hundred lines of prompt text as wiki content.
# Re-running is the normal mode of use for these commands, so it compounds.
#
# WHY BLANK RATHER THAN DROP: the D13 cutoff anchors on the last USER-role turn
# (role only). Removing the turn moves the anchor onto the user's last real
# question, so the cutoff would then delete the very exchange the run exists to
# file — the same trap cc's `test_cutoff_anchors_on_an_empty_user_turn_too`
# pins. Blanking keeps the anchor and costs nothing downstream:
# `project_from_turns` skips empty-text turns before the hash lookup, so a
# blanked turn is never rendered, never hashed into the ledger, and never
# counted as ledger_skipped.
#
# DETECTION is a literal first-line denylist ANDed with `role == "user"` — the
# same "two signals, never one" shape cc uses for isMeta (D12). An H1 is a
# heading a user could plausibly type, so the rule stays as narrow as it can be
# while remaining deterministic. NOT covered: a THIRD-PARTY template's
# expansion. No detector for those exists (pi hands the projector no marker
# distinguishing expanded template text from typed text), and inventing a
# heuristic would start dropping real user turns. That residue is accepted.
#
# Keep `_INVOCATION_H1S` in sync with the H1 of every prompt this package ships
# (pi-extensions `src/prompts/*.md`); the pi-side contract test pins the two
# together so a renamed command cannot silently disarm this filter.
_INVOCATION_H1S = frozenset({
    "# /wiki-file",
    "# /wiki-ingest-docs",
    "# /wiki-ingest-sessions",
    "# /wiki-lint",
    "# /wiki-promote",
    "# /wiki-reindex",
})


def _is_command_invocation(turn: dict) -> bool:
    """True when this turn is an expanded llm-wiki command body (D7-pi)."""
    if (turn.get("role") or "") != "user":
        return False
    text = turn.get("text") or ""
    if not text:
        return False
    first_line = text.lstrip().split("\n", 1)[0].strip()
    return first_line in _INVOCATION_H1S


def _blank_command_invocations(turns: "list[dict]") -> "list[dict]":
    """Return ``turns`` with every command-invocation turn's text emptied.

    Position and role are preserved (the D13 cutoff anchor); only ``text`` is
    replaced. Returns new dicts for the turns it changes — callers' inputs are
    never mutated in place.
    """
    return [
        {**turn, "text": ""} if _is_command_invocation(turn) else turn
        for turn in turns
    ]


# ---------------------------------------------------------------------------

def _compute_hash(role: str, text: str, *, ledger) -> str:
    return ledger.compute_hash(role, text)


def _assign_hash(turn: dict, *, ledger) -> dict:
    """Attach the F5 hash to a turn dict (assigned ONCE, at extraction).

    Returns a NEW dict (copy of ``turn`` plus a ``hash`` key) so callers
    (``extract_turns_batch``, ``project_owned``) do not mutate the caller's
    turn dicts in place.
    """
    role = turn.get("role") or ""
    text = turn.get("text") or ""
    return {**turn, "hash": _compute_hash(role, text, ledger=ledger)}


def extract_turns_batch(sids: "list[str]", *, ledger) -> "dict[str, list[dict]]":
    """Project MANY pi sids in ONE directory walk (R1 — pi mirror of cc's batch).

    This is the EXPENSIVE half of the pi projector's Path B split: locating
    every requested sid's session file is done via a SINGLE filesystem walk
    (``_walk_all_session_files``) rather than one rglob per sid. Each matched
    file is still loaded individually via DuckDB (pi sessions are one file
    per sid, unlike cc's single corpus, so there is no further scan to
    collapse) and its turns get the F5 hash assigned here so ``begin``'s
    ``project_from_turns`` agrees on the dedup/ledger key without
    re-hashing.

    Args:
        sids: the pi session ids to project.
        ledger: the llmwiki.ingest.ledger module (hash single source of
            truth).

    Returns:
        ``{sid: [turn_dict, ...]}`` — each turn_dict is
        ``{id, parentId, role, ts, text, hash}``. A sid with no matching
        session file maps to an empty list (still present in the dict; does
        NOT raise — mirrors cc_log_project.extract_turns_batch's
        not-found-is-empty-list semantics, cc_log_project.py:438-439).
        Multiple files matching one sid use the most recent by mtime (same
        rule as ``_find_session_file``).

    Raises:
        ProjectionError on DuckDB / parse failure for a matched file.
    """
    result: "dict[str, list[dict]]" = {sid: [] for sid in sids}
    if not sids:
        return result
    by_sid = _walk_all_session_files()
    for sid in sids:
        matches = by_sid.get(sid)
        if not matches:
            continue  # missing sid -> stays [] (no raise)
        session_file = max(matches, key=lambda p: p.stat().st_mtime)
        raw_turns = _load_and_project(session_file, sid)
        active = _blank_command_invocations(_active_path(raw_turns))
        result[sid] = [_assign_hash(turn, ledger=ledger) for turn in active]
    return result


def _render_turn_md(turn: dict, n: int) -> str:
    role = turn.get("role") or ""
    ts = turn.get("ts") or ""
    text = turn.get("text") or ""
    entry_id = turn.get("id") or ""
    lines: list[str] = [f"## Turn {n} [{ts}]", ""]
    if role == "user":
        if text:
            lines += ["**Human**:", text, ""]
    else:
        if text:
            lines += ["**Assistant**:", text, ""]
    lines += [f"<!-- provenance: id={entry_id} ts={ts} -->", ""]
    return "\n".join(lines)


def project_from_turns(wiki_root: "str | Path", sid: str, turns: "list[dict]",
                        *, ledger) -> ProjectionResult:
    """Project already-extracted pi turns to novel-turn markdown (R1 cheap half).

    Consumes turn dicts already carrying their F5 hash (from
    ``extract_turns_batch`` or ``project_owned``'s single-sid extraction):
    within-sid exact dedup -> ledger diff (drop already-seen) -> markdown.
    Does NOT recompute the hash — reads it from ``turn["hash"]``.

    Args:
        wiki_root: the wiki root (to read ledger.read_seen_hashes).
        sid: the session id these turns belong to (recorded on novel
            entries).
        turns: the extracted turn dicts, each
            ``{id, parentId, role, ts, text, hash}``.
        ledger: the llmwiki.ingest.ledger module (seen-set single source of
            truth).

    Returns:
        ProjectionResult(markdown, novel_entries, ledger_skipped).

    Raises:
        ProjectionError if a turn that reaches the hash lookup (i.e. has
        non-empty text) is missing the ``hash`` key (fail-closed; this
        deliberately differs from cc_log_project.project_from_turns, which
        KeyErrors on the same condition — cc_log_project.py:563).
    """
    seen = ledger.read_seen_hashes(wiki_root)
    local_seen: set[str] = set()

    header = ["# Pi Session transcript", ""]
    body: list[str] = []
    novel_entries: list[dict] = []
    ledger_skipped = 0
    n = 0

    for turn in turns:
        text = turn.get("text") or ""
        if not text:
            continue
        if "hash" not in turn:
            raise ProjectionError(
                f"turn missing 'hash' key for pi sid {sid!r} "
                "(extract_turns_batch/project_owned must assign hash "
                "before project_from_turns)"
            )
        h = turn["hash"]
        if h in local_seen:
            continue
        local_seen.add(h)
        if h in seen:
            ledger_skipped += 1
            continue
        n += 1
        body.append(_render_turn_md(turn, n))
        novel_entries.append({
            "hash": h,
            "first_sid": sid,
            # pi uses entry "id" (not a UUID, but occupies the first_uuid slot in
            # LedgerEntry for provenance; cc_log_project uses record_uuid here).
            "first_uuid": turn.get("id") or "",
            "first_ts": turn.get("ts") or "",
        })

    if not body:
        markdown = "\n".join(header)
    else:
        markdown = "\n".join(header + body)

    return ProjectionResult(
        markdown=markdown,
        novel_entries=novel_entries,
        ledger_skipped=ledger_skipped,
    )


def extract_owned(sid: str, *, ledger) -> "list[dict]":
    """Extract ONE pi session's turns (the EXPENSIVE half of Path A; read-only).

    ``project_owned``'s extraction step factored out so begin can run it BEFORE
    acquiring the transaction lock (#19 in-lock ledger diff). Touches NO wiki
    state — ``ledger`` is used solely for hash assignment (F5), never for the
    seen-set. Keeps Path A's F-14 fail-closed surface: a missing session file
    raises ProjectionError (unlike ``extract_turns_batch``, whose
    missing-sid-is-empty-list semantics serve the Path B planner).

    Raises:
        ProjectionError on file-not-found or parse failure.
    """
    session_file = _find_session_file(sid)
    raw_turns = _load_and_project(session_file, sid)
    active = _blank_command_invocations(_active_path(raw_turns))
    return [_assign_hash(turn, ledger=ledger) for turn in active]


def project_owned(wiki_root: "str | Path", sid: str, *, ledger) -> ProjectionResult:
    """Project one pi session to novel-turn markdown + the ledger-entry channel.

    The composition of the two halves for a SINGLE sid (Path A):
    ``extract_owned`` -> ``project_from_turns``. External behavior (markdown,
    novel_entries, ledger_skipped) is identical to the pre-refactor inline
    implementation for the same input (S1 non-regression criterion). NOTE:
    begin no longer calls this composition directly — it runs ``extract_owned``
    before the lock and ``project_from_turns`` inside the lock (#19), so this
    stays as the one-shot composition for ``main()`` / manual inspection (and
    as the behavioral spec the split halves must agree with).

    Args:
        wiki_root: the wiki root (to read ledger.read_seen_hashes).
        sid: the pi session id to project.
        ledger: the llmwiki.ingest.ledger module (hash + seen-set single source
            of truth; injected so the driver/test controls it).

    Returns:
        ProjectionResult(markdown, novel_entries, ledger_skipped).
    Raises:
        ProjectionError on file-not-found or parse failure.
    """
    return project_from_turns(wiki_root, sid, extract_owned(sid, ledger=ledger),
                              ledger=ledger)


def main() -> None:  # pragma: no cover - thin CLI wrapper for manual inspection
    import argparse
    import sys

    from llmwiki.ingest import ledger as _ledger

    # Fix stdio to UTF-8 regardless of the host locale (S1; same idiom as
    # cli.py:main — subsumes the old stdout-only reconfigure below).
    #
    # `newline="\n"` (2026-08-07) pins the line terminator to LF on every
    # platform, matching cli.py:main. Here it affects CONTENT, not just line
    # framing: without `--output` this entrypoint prints the projected transcript
    # markdown straight to stdout, so Windows text mode would rewrite every line
    # ending inside the projection itself.
    #
    # Scope note: this fixes the STDOUT arm only. The `--output` arm goes through
    # `Path.write_text`, which on Windows also translates "\n" to "\r\n"
    # (measured, not assumed) — that arm still writes CRLF. Left alone here
    # because it is a separate, pre-existing behaviour with its own consumers.
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")

    ap = argparse.ArgumentParser(
        description="Project a single pi-log sid to novel-turn markdown.")
    ap.add_argument("wiki_root", help="Wiki root (for the turn ledger).")
    ap.add_argument("sid", help="Pi session id to project.")
    ap.add_argument("-o", "--output", default=None,
                    help="Output file (default stdout).")
    args = ap.parse_args()
    try:
        res = project_owned(args.wiki_root, args.sid, ledger=_ledger)
    except ProjectionError as e:
        print(str(e), file=sys.stderr)
        sys.exit(3)
    if args.output:
        Path(args.output).write_text(res.markdown, encoding="utf-8")
        print(f"{len(res.novel_entries)} novel turns -> {args.output}",
              file=sys.stderr)
    else:
        print(res.markdown)


if __name__ == "__main__":
    main()
