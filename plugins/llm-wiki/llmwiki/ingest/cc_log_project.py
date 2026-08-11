# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb"]
# ///
"""Fork-aware cc-log projector (T2) — replaces the fork-blind extract_cc_log.

Projects a SINGLE session id (plus its agent children — fork-child records carry
the parent session_id, verified 308/308, so filtering by session_id co-locates
them) out of the vendored inspect-cc-log DuckDB views, then:

  1. selects TEXT blocks only (D5 — tool_use/tool_result are excluded at the
     SQL level, not rendered; thinking is excluded by the same filter, S8-c),
     surfacing each record's ``isMeta`` flag as a column;
  2. groups the surviving blocks into chronological turns (one turn per
     ``record_uuid``), dropping a record when ``isMeta`` is true AND its text
     matches the ``_META_NOISE_PATTERNS`` denylist (D12 — expanded SKILL
     bodies, retry/continue nudges, local-command wrappers, Stop-hook
     feedback). The flag alone is NOT sufficient: it also marks genuine human
     steering typed mid-turn (measured), which must survive;
  3. strips the stable injected boilerplate markers at the PROJECTION
     normalization stage (F4/U2 — projector side, NOT redaction=D16 which stays
     in frontends.fe_b_prime);
  4. length-independent EXACT dedup within the sid (F4 — the ≥200-char
     min-length guard is WITHDRAWN; identical (role,text) turns collapse to one);
  5. ledger diff (F1-b/T4) — drops turns whose ``ledger.compute_hash(role,text)``
     is already in ``ledger.read_seen_hashes(wiki_root)`` (cross-path / cross-run
     idempotency);
  6. renders markdown for the SURVIVING novel turns (text block + provenance
     pointer sid/uuid/ts) in the shape frontends.fe_b_prime expects.

DEDUP UNIT (R2 / F-M1): the dedup + ledger unit is the CC RECORD grain
(``record_uuid``), NOT a conversation turn. A synthesized "replay" record — one
``user`` record whose ``message.content`` array carries many text blocks laid out
as ``USER:``/``ASSISTANT:``/``TOOL RESULT`` prose (observed up to 402 blocks in a
single record, 1,220 such records in the live corpus) — is treated as ONE unit
(its blocks concatenated in block_index order). Splitting into conversation grain
is a NON-GOAL: the prose layout is an injected convention (brittle), the sub-turns
carry no ``record_uuid`` for the provenance pointer, and in the corpus whole-record
duplication dominates (so record-grain dedup already collapses the common case).

The projector does NOT redact and does NOT append to the ledger. Redaction (D16)
is fe_b_prime's mandatory stage; the ledger append is the DRIVER's finish(success)
via the ``pending_ledger_entries`` sidecar channel (T4 contract). The projector
merely (a) reads seen hashes, (b) drops seen turns, (c) produces the markdown, and
(d) surfaces the novel turns' LedgerEntry list so the driver can populate
``pending_ledger_entries``.

Hash basis (F5): the hash for BOTH dedup and the ledger is the PRE-redaction
projected text (post-boilerplate-strip), ``md5(role ‖ 0x1F ‖ text)`` UTF-8+NFC.
Redaction is deterministic, so the pre/post-redaction hash of a turn is
consistent; we hash the projected text. The hash is delegated to
``ledger.compute_hash`` (single source of truth — NOT reimplemented here).

Two projection entry points (R1 / F-H1 — Path B scan-collapse, case A):
    Path A (single session, /wiki-file) uses ``project_owned`` — one DuckDB
    scan is fine for one sid.
    Path B (whole project, /wiki-ingest-sessions) must NOT re-scan the whole
    ~/.claude/projects corpus once per sid. So the EXPENSIVE part (the DuckDB
    scan + block grouping + boilerplate strip + hash) is factored into
    ``extract_turns_batch`` (ONE scan for ALL sids, run once by the read-only
    ``project-batch`` driver verb before the loop), and the CHEAP part (within-sid
    exact dedup + ledger diff + markdown) stays in ``project_from_turns``, run
    per-sid INSIDE ``begin`` so the ledger read-after-write (F3) stays sequential.
    ``project_owned`` is the composition of the two (extract this one sid, then
    project) so Path A is unchanged.

I/O contract:
    extract_turns_batch(sids) -> {sid: [turn_dict, ...]}     # R1: one scan, all sids
      turn_dict = {role, uuid, ts, projected_text, hash} — the boilerplate-stripped,
      hash-carrying turn (hash = ledger.compute_hash(role, projected_text), assigned
      HERE so batch and begin agree on the F5 hash). No tool fields (D5): the
      projection is text-only.

    project_from_turns(wiki_root, sid, turns, *, ledger) -> ProjectionResult
      Consumes the extracted turns (does NOT open DuckDB): within-sid exact dedup
      -> ledger diff -> markdown. This is the per-sid step run inside begin.

    project_owned(wiki_root, sid, *, ledger) -> ProjectionResult   # Path A
      = project_from_turns(wiki_root, sid, extract_turns_batch([sid])[sid], ...)

      out: ProjectionResult {
             markdown: str,                 # transcript of the NOVEL turns only
             novel_entries: list[dict],     # {hash, first_sid, first_uuid,
                                            #  first_ts} per novel turn — the
                                            #  driver puts these on the sidecar's
                                            #  pending_ledger_entries (T4)
             ledger_skipped: int,           # count of turns dropped by the LEDGER
                                            #  diff (already-owned) this run (F6) —
                                            #  begin surfaces it on stdout so a
                                            #  Path B re-run is not a silent no-op
           }
      Raises ProjectionError on DuckDB / read failure.

The markdown shape mirrors the OLD extract_cc_log output's text turns
(``## Turn N [ts]``, ``**Human**:``, ``**Assistant**:``) so fe_b_prime's
redact→hash→raw/derived/<hash>.md contract is unchanged; the tool-call lines
that extractor also emitted are gone (D5). A per-turn provenance pointer line
is appended (sid/uuid/ts).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from llmwiki.ingest import cc_paths

try:
    import duckdb
except ImportError:  # pragma: no cover - dependency declared in script header
    duckdb = None


# Vendored inspect-cc-log views (T1). Sibling of this module. The views read the
# CC session logs lazily through one anchor glob literal in cc_record, exactly as
# skills/inspect-cc-log/scripts/query.py applies them; a single sid is selected
# with WHERE session_id = ? (sid lives inside the JSON, not the filename, so no
# file pruning — accepted per design.md). Fork/agent children carry the parent
# session_id, so this one predicate co-locates them.
#
# The file keeps `'~/.claude/projects/**/*.jsonl'` verbatim (so it stays valid
# stand-alone SQL and stays byte-equal to the canonical skills copy); every load
# goes through `cc_paths.read_cc_views_sql`, which — and only when
# `$CLAUDE_CONFIG_DIR` is set — rewrites that literal into the glob list of the
# universes that actually hold logs. See cc_paths and
# `_projects/llm-wiki/project-notes/specs/cc-config-dir-ingest.md`.
_VIEWS_SQL = Path(__file__).resolve().parent / "cc_views.sql"


class ProjectionError(Exception):
    """DuckDB / view read failure while projecting a sid."""


# --- boilerplate strip (F4 / U2) ------------------------------------------------
# Stable injected markers to remove at the projection normalization stage so that
# turns whose ONLY difference is injected boilerplate collapse under exact dedup
# (false-merge avoidance runs the other way: we remove the injected wrapper, then
# exact-match the real content). Verified against real source:
#   - taskflow session_init.py: "[Progress Session] session_id=... " prepended to
#     the user turn as additionalContext (+ index/routing/guidelines tails).
#   - taskflow guidelines_reminder.md: "[taskflow guidelines reminder] ..." block
#     and the "<!-- taskflow guidelines ... -->" HTML comments.
#   - CC harness: "<system-reminder> ... </system-reminder>" wrapper blocks.
#   - role-mode _meta.md / _meta_role.md: the mode header block. Two variants
#     since the 2026-07-30 role-less split: "Two response axes:" (role
#     present) or "Mode = HOW you process..." (no role — role-less turns
#     never see Role-axis text at all).
#   - CC harness: the `<command-name>` / `<command-args>` /
#     `<local-command-stdout>` wrapper elements a locally-handled slash command
#     emits, and the `/wiki-file` invocation line itself (D7).
# The whole injected block is stripped, not just the marker line.

# system-reminder is an XML-ish wrapper; strip the whole element (DOTALL).
_RE_SYSTEM_REMINDER = re.compile(
    r"<system-reminder>.*?</system-reminder>", re.DOTALL)

# Local-command wrapper elements (D7). A slash command that the CLI handles
# locally (`/model`, …) emits THREE records; only `<local-command-caveat>`
# carries `isMeta`, so the other two cannot be caught by D12's flag-gated
# denylist (measured). They are harness wrapper ELEMENTS of exactly the
# `<system-reminder>` class, so they strip here — unconditionally, by element,
# no role or flag condition. A record that is nothing but these wrappers strips
# to empty and is dropped by the empty-turn guard.
_RE_LOCAL_COMMAND = re.compile(
    r"<(command-name|command-message|command-args|local-command-stdout"
    r"|local-command-stderr)>.*?</\1>", re.DOTALL)

# The `/wiki-file` invocation line (D7). D6's cutoff is POSITIONAL, so it only
# protects the CURRENT run: on the next invocation the previous run's
# instruction turn is no longer last and — never having been filed — is not in
# the ledger either, so it would enter the payload as a novel turn. A slash
# command is a stable matchable string (measured: the typed invocation sits as
# PLAIN TEXT inside the user turn, with no `<command-name>` wrapper for skill
# commands), so content-based exclusion makes it permanent where D6's is
# positional. Scope is the invocation LINE only — the expanded SKILL body is a
# separate isMeta record and is D12's job.
_RE_WIKI_FILE_INVOCATION = re.compile(
    r"^[ \t]*/(?:llm-wiki:)?wiki-file(?:[ \t][^\n]*)?$", re.MULTILINE)

# HTML-comment markers injected by taskflow (single-line comments).
_RE_TASKFLOW_COMMENT = re.compile(
    r"<!--\s*taskflow guidelines[^>]*-->", re.IGNORECASE)

# The [taskflow guidelines reminder] block: the marker line plus the immediately
# following PROHIBIT/FORMAT/AUTHORITY/... reminder lines, up to a blank line.
_RE_GUIDELINES_REMINDER_BLOCK = re.compile(
    r"\[taskflow guidelines reminder\].*?(?:\n\s*\n|\Z)", re.DOTALL)

# The [Progress Session] header line injected every turn by session_init.py. It
# is a single injected line (session_id=... sid8=... state_file=... etc., plus
# appended index/routing/guidelines context that is handled by the other markers
# / the boundary strip below). Remove from the marker to end of that line.
_RE_PROGRESS_SESSION = re.compile(r"\[Progress Session\][^\n]*")

# The role-mode mode header block. The block is the header line plus its
# contiguous injected lines: blank lines, the "- Role:" / "- Mode:" axis
# bullets (role-present variant only), the trailing precedence/rule lines
# ("Precedence: ...", "Follow Mode ...", "NEVER rule ...",
# "Include `[Mode: ..."), and the active `role: <value>` / `mode: <name>`
# declaration line the hook generates (mode_inject.py's active_lines --
# always lowercase, never user-controlled case).
# We consume the header and every following line that is blank, a bullet, or
# starts with one of those stable mode-trailer keywords; real user content
# (separated by a blank line and not a bullet/keyword line) is left intact.
#
# Two header-line forms since the 2026-07-30 role-less split (each is a
# literal, matched via re.escape so a stray trailing "." can't slip through
# as a wildcard):
#   - "Two response axes:" -- role present (_meta_role.md).
#   - "Mode = HOW you process — rules, constraints, procedures." -- no role
#     (_meta.md, role-less). Matched whole-line so it can't accidentally
#     swallow real user content that happens to start with "Mode".
#
# "role: " / "mode: " carry a TRAILING SPACE, and are lowercase (not
# "Role:" / "Mode:"). Both details are load-bearing:
#   - lowercase: the active declaration line mode_inject.py emits is always
#     the lowercase literal prefix (its active_lines build `f'role: {...}'` /
#     `f'mode: {...}'`). No uppercase form is ever produced.
#   - trailing space: this is what keeps the strip off the USER's own text.
#     The injected active line always has the space; the user's invocation
#     slug never can -- `mode:survey` is the only form role-mode's MODE_RE
#     accepts (`mode: survey` with a space simply does not register as a
#     slug). Since additionalContext is PREPENDED to the user turn, the two
#     are adjacent, so a space-less "mode:" keyword would let this block
#     consume a user prompt line that merely starts with their own slug.
#     Verified 2026-08-09: with "mode:" (no space), a multi-line prompt
#     beginning `mode:survey investigate...` lost that first line.
# Residual (accepted, not fixed here): consumption still has no explicit
# end-of-injection terminator, so a user prompt whose first lines are BULLETS
# is absorbed by the `-` alternative below. Pinned by
# test_boilerplate_does_not_eat_user_bullet_lines_is_known_gap.
_MODE_TRAILER_KEYWORDS = (
    "Precedence:", "Follow Mode", "NEVER rule", "Include `[Mode",
    "role: ", "mode: ",
)
_MODE_HEADER_LINES = (
    r"Two response axes:[^\n]*",
    re.escape("Mode = HOW you process — rules, constraints, procedures."),
)
_RE_MODE_BLOCK = re.compile(
    r"^(?:" + "|".join(_MODE_HEADER_LINES) + r")\n"
    r"(?:[ \t]*\n|[ \t]*-[^\n]*\n|[ \t]*(?:"
    + "|".join(re.escape(k) for k in _MODE_TRAILER_KEYWORDS)
    + r")[^\n]*\n)*",
    re.MULTILINE,
)

_BOILERPLATE_PATTERNS = (
    _RE_SYSTEM_REMINDER,
    _RE_GUIDELINES_REMINDER_BLOCK,
    _RE_TASKFLOW_COMMENT,
    _RE_PROGRESS_SESSION,
    _RE_MODE_BLOCK,
    _RE_LOCAL_COMMAND,
    _RE_WIKI_FILE_INVOCATION,
)


def strip_boilerplate(text: str) -> str:
    """Remove the stable injected boilerplate blocks from a turn's text (F4/U2).

    Runs at projection normalization (projector side), BEFORE the hash is
    computed and BEFORE fe_b_prime redaction (D16). Idempotent; collapses the
    surrounding whitespace left by removed blocks.
    """
    if not text:
        return ""
    out = text
    for pat in _BOILERPLATE_PATTERNS:
        out = pat.sub("", out)
    # Collapse 3+ newlines left by removed blocks to a single blank line, and
    # trim edges. Interior single/double newlines are preserved (turn shape).
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


# --- projection: sid -> ordered turns ------------------------------------------
# One row per TEXT content block of the sid (and its agent children), chronological.
# D5: only block_type='text' is selected — tool_use/tool_result never reach this
# query. thinking blocks are EXCLUDED too (S8-c), unreachable via the same filter.
# record_uuid + role + ts group blocks into turns; block_index preserves
# intra-record order.
#
# D12: `isMeta` is SURFACED as a column here (it is not a cc_views.sql column —
# the vendored file stays byte-equal to the canonical
# `skills/inspect-cc-log/scripts/views.sql`; this is llm-wiki-owned SQL layered
# on top, reading the raw JSON the L0 `cc_record` view exposes as `j`).
#
# It is a SIGNAL, NOT a verdict. Measured on the live corpus (Verified facts,
# specs/wiki-file-current-session.md): `isMeta: true` marks "injected mid-turn",
# which covers BOTH pure harness noise (expanded SKILL bodies, retry/continue
# nudges, `<local-command-*>` wrappers, Stop-hook feedback) AND genuine
# human steering typed while the agent was working (`mode:survey 1の実測だけやれ`,
# `続き: …`, coordinator-relayed instructions). Dropping every isMeta record
# would therefore delete real utterance. The verdict is the AND of this flag
# with the `_META_NOISE_PATTERNS` denylist below (`_is_meta_noise`).
# NOTE the parentheses around every `->>` extraction used in the WHERE: DuckDB
# mis-plans a bare `j->>'k'` inside a conjunction (it tries to cast the whole
# `j` column to a number -> ConversionException). Measured here, and documented
# in skills/inspect-cc-log/SKILL.md's "parenthesize ->> in WHERE" gotcha.
_IS_META_EXPR = """
  coalesce(record_uuid IN (
    SELECT j->>'uuid' FROM cc_record
    WHERE try_cast((j->>'isMeta') AS boolean) AND (j->>'uuid') IS NOT NULL
  ), false) AS is_meta
"""

# The batch form (R1 / F-H1) selects MANY sids in one scan with `session_id IN
# (...)`, carrying session_id in the projection so rows can be split per sid. The
# single-sid form filters `session_id = ?`. Both keep the same block-type filter
# and chronological ordering; the batch adds session_id as the leading sort key so
# each sid's rows are contiguous and internally chronological.
_PROJECT_COLUMNS = f"""
  record_uuid,
  session_id,
  role,
  strftime(ts, '%Y-%m-%d %H:%M:%S') AS ts_str,
  block_index,
  block_type,
  text,
{_IS_META_EXPR}
"""

_PROJECT_SQL = f"""
SELECT
{_PROJECT_COLUMNS}
FROM cc_block
WHERE session_id = ?
  AND block_type = 'text'
ORDER BY ts ASC, record_uuid ASC, block_index ASC
"""


# --- D12 meta-noise denylist (record grain) -------------------------------------
# A record is dropped ONLY when `is_meta` is true AND its text matches one of
# these ANCHORED patterns. Both conditions are load-bearing:
#   - the flag alone over-drops (it also marks real human steering — measured);
#   - the pattern alone risks a false positive on a user who PASTES one of these
#     shapes as their own content (the flag proves the harness injected it).
# Every entry below was verified against the live corpus as 100% harness-emitted.
# Deliberately NOT listed (they carry real content, so they are kept and their
# residue is accepted per D9): `The coordinator sent a message while you were
# working:` (relayed instructions the agent then acted on) and
# `<task-notification>` (its `<result>` block carries a subagent's actual
# return value).
#
# This is an OPEN denylist: an unlisted noise shape survives into the payload.
# The cost of a miss is bounded — one raw-tier record and one ledger row, the
# same posture D7/D9 already accept — and Stage1's semantic distillation keeps
# it out of page content. D13's cutoff anchor does not depend on this list being
# exhaustive (see its non-empty-user-turn anchor rule).
_META_NOISE_PATTERNS = (
    # The expanded SKILL body a slash-command invocation injects (the F1 finding:
    # a user-role TEXT record, not a tool_result).
    re.compile(r"^Base directory for this skill:"),
    re.compile(r"^Skill /\S+ is already loaded above; instructions unchanged\."),
    re.compile(r"^\(Re-invocation of /\S+"),
    # The caveat wrapper a local command prepends, and taskflow's Stop-hook
    # feedback. NOTE: a local command's OTHER two records (`<command-name>…`
    # and `<local-command-stdout>…`) are measured to carry NO isMeta flag, so
    # they cannot be caught here — they are harness wrapper ELEMENTS of the
    # same class as `<system-reminder>` and belong to `_BOILERPLATE_PATTERNS`
    # (D7), which strips unconditionally.
    re.compile(r"^<local-command-caveat>"),
    re.compile(r"^Stop hook feedback:"),
    # Harness retry / continue nudges (no conversational content at all).
    re.compile(r"^Your tool call was malformed"),
    re.compile(r"^The previous response failed to produce a valid tool call"),
    re.compile(r"^Continue from where you left off"),
    re.compile(r"^\[Your previous response had no visible output"),
    # Image-attachment coordinate note the harness prepends.
    re.compile(r"^\[Image: original \d+x\d+"),
)


def _is_meta_noise(text: str) -> bool:
    """True iff `text` matches a known harness-noise shape (D12 denylist).

    Callers MUST also require the record's `is_meta` flag — this function is
    only half of the verdict (see `_META_NOISE_PATTERNS`).
    """
    if not text:
        return False
    probe = text.lstrip()
    return any(pat.match(probe) for pat in _META_NOISE_PATTERNS)


@dataclass
class _Turn:
    """A grouped turn: one record_uuid's text blocks (chronological)."""
    role: str                       # "user" | "assistant"
    uuid: str                       # record_uuid (provenance pointer)
    ts: str                         # local ts string (provenance pointer)
    text_parts: list = field(default_factory=list)      # str
    order: int = 0                  # first-seen order (stable sort key)
    is_meta: bool = False           # D12 signal (harness-injected mid-turn)


def _group_rows_to_turns(rows: list) -> list[_Turn]:
    """Group projection rows (for ONE sid) into ordered turns.

    Rows are the projection columns (`_PROJECT_COLUMNS`) for a single session,
    already text-only (D5), chronological. Turns are keyed by record_uuid (the
    dedup/render unit — R2: CC record grain), role frozen at first-seen (a
    record_uuid is single-role by construction — verified 0 multi-role uuids in
    the live corpus).

    D12 drop happens HERE, at record grain and only on the AND of the row's
    `is_meta` flag with the `_META_NOISE_PATTERNS` denylist, evaluated on the
    record's FULL joined text (so a multi-block injected record is judged as
    one unit, matching the R2 dedup grain).
    """
    turns: dict[str, _Turn] = {}
    order = 0
    for (uuid, _sid, role, ts_str, _bi, _btype, text, is_meta) in rows:
        t = turns.get(uuid)
        if t is None:
            t = _Turn(role=role or "", uuid=uuid or "", ts=ts_str or "",
                      order=order, is_meta=bool(is_meta))
            order += 1
            turns[uuid] = t
        if text and text.strip():
            t.text_parts.append(text)
    kept = [t for t in turns.values()
            if not (t.is_meta and _is_meta_noise("\n".join(t.text_parts)))]
    return sorted(kept, key=lambda x: x.order)


def _fetch_turns(sid: str) -> list[_Turn]:
    """Run the vendored views + single-sid projection, group blocks into turns."""
    if duckdb is None:  # pragma: no cover
        raise ProjectionError("duckdb not available")
    try:
        con = duckdb.connect()
        con.execute(cc_paths.read_cc_views_sql(_VIEWS_SQL))
        rows = con.execute(_PROJECT_SQL, [sid]).fetchall()
    except Exception as e:  # noqa: BLE001 - surface as ProjectionError per contract
        raise ProjectionError(f"projection failure for sid {sid!r}: {e}") from e
    return _group_rows_to_turns(rows)


# --- markdown rendering ---------------------------------------------------------
def _turn_text_for_hash(turn: _Turn) -> str:
    """The projected, boilerplate-stripped text that is the hash/dedup basis.

    The dedup unit is the (role, text) turn per the ledger contract
    (compute_hash(role, text)). A turn with no text hashes on empty text —
    unreachable post-D5/D12 (a turn only exists if it had a text block), so the
    constant-hash collapse the old tool-inclusive projection produced cannot
    recur.
    """
    joined = "\n".join(turn.text_parts).strip()
    return strip_boilerplate(joined)


def _turn_to_dict(turn: _Turn, *, ledger) -> dict:
    """Serialize a grouped turn to the JSON-safe hand-off dict (R1).

    Carries the boilerplate-stripped projected text AND the F5 hash
    (``ledger.compute_hash(role, projected_text)``) so the batch extractor and
    begin agree on the exact dedup/ledger key (the hash is assigned ONCE, here).
    """
    projected_text = _turn_text_for_hash(turn)
    return {
        "role": turn.role,
        "uuid": turn.uuid,
        "ts": turn.ts,
        "projected_text": projected_text,
        "hash": ledger.compute_hash(turn.role, projected_text),
    }


def extract_turns_batch(sids: "list[str]", *, ledger) -> "dict[str, list[dict]]":
    """Project MANY sids out of the vendored views in ONE DuckDB scan (R1/F-H1).

    This is the EXPENSIVE half of the projector (the full ~/.claude/projects
    corpus scan + block grouping + boilerplate strip + hash), factored out so
    Path B (whole project) pays it ONCE before the per-sid loop instead of once
    per begin. The CHEAP half (dedup + ledger diff + markdown) is
    ``project_from_turns``, run per-sid inside begin so the ledger read-after-write
    (F3) stays sequential.

    Args:
        sids: the session ids to project (agent children fold in via shared sid).
        ledger: the llmwiki.ingest.ledger module (hash single source of truth).

    Returns:
        {sid: [turn_dict, ...]} — each turn_dict is the JSON-safe hand-off shape
        (role, uuid, ts, projected_text, hash). A sid with no rows maps to an
        empty list (still present in the dict, so the caller sees every sid).
    """
    if duckdb is None:  # pragma: no cover
        raise ProjectionError("duckdb not available")
    result: dict[str, list[dict]] = {sid: [] for sid in sids}
    if not sids:
        return result
    try:
        con = duckdb.connect()
        con.execute(cc_paths.read_cc_views_sql(_VIEWS_SQL))
        placeholders = ",".join("?" for _ in sids)
        sql = (
            f"SELECT\n{_PROJECT_COLUMNS}\n"
            f"FROM cc_block\n"
            f"WHERE session_id IN ({placeholders})\n"
            f"  AND block_type = 'text'\n"
            # NOTE: no isMeta predicate here. D12 surfaces the flag as a COLUMN
            # (`_IS_META_EXPR`, already carried by `_PROJECT_COLUMNS` above) and
            # the drop decision happens in `_group_rows_to_turns`. Splicing that
            # expression into the WHERE position would be a SQL syntax error.
            f"ORDER BY session_id ASC, ts ASC, record_uuid ASC, block_index ASC"
        )
        rows = con.execute(sql, list(sids)).fetchall()
    except Exception as e:  # noqa: BLE001 - surface as ProjectionError per contract
        raise ProjectionError(f"batch projection failure for {len(sids)} sids: {e}") from e

    # Split rows per sid (session_id is column index 1), preserving order, then
    # group each sid's rows into turns and serialize.
    per_sid: dict[str, list] = {sid: [] for sid in sids}
    for row in rows:
        sid = row[1]
        if sid in per_sid:
            per_sid[sid].append(row)
    for sid, sid_rows in per_sid.items():
        turns = _group_rows_to_turns(sid_rows)
        result[sid] = [_turn_to_dict(t, ledger=ledger) for t in turns]
    return result


def _render_turn_md(turn: dict, n: int, *, sid: str) -> str:
    """Render one surviving turn's markdown (matches old FE-B' shape + pointer).

    Consumes the JSON-safe turn dict (role, uuid, ts, projected_text).
    projected_text is the already-stripped text carried on the dict (R1 — strip
    happened at extraction).
    """
    projected_text = turn.get("projected_text") or ""
    role = turn.get("role") or ""
    ts = turn.get("ts") or ""
    uuid = turn.get("uuid") or ""
    lines: list[str] = [f"## Turn {n} [{ts}]", ""]
    if role == "user":
        if projected_text:
            lines += ["**Human**:", projected_text, ""]
    else:  # assistant (and any non-user role)
        if projected_text:
            lines += ["**Assistant**:", projected_text, ""]
    # Provenance pointer (sid/uuid/ts) — promotion evidence re-fetch handle.
    # F5: sid is now included so inspect-cc-log can re-fetch this record with a
    # session-filtered query instead of a full-corpus uuid scan.
    lines += [f"<!-- provenance: sid={sid} uuid={uuid} ts={ts} -->", ""]
    return "\n".join(lines)


@dataclass
class ProjectionResult:
    """Return of project_owned — markdown + the novel-entry channel for T3.

    markdown       : the transcript of the NOVEL (post-dedup, post-ledger-diff)
                     turns; hand this to frontends.fe_b_prime(wiki_root, markdown).
    novel_entries  : list of {hash, first_sid, first_uuid, first_ts} dicts — the
                     driver copies these onto the sidecar's pending_ledger_entries
                     (T4); finish(success) journals + appends them to the ledger.
                     Empty when nothing novel (dedup no-op).
    ledger_skipped : count of turns dropped by the LEDGER diff (already owned by a
                     prior ingest) in THIS projection (F6). This is ONLY the
                     ledger-diff drop — it does NOT include the within-sid exact
                     dedup collapse, which is a different signal. The driver
                     surfaces it on begin's stdout JSON so a Path B incremental
                     re-run is not silently a no-op (RS-d).
    """
    markdown: str
    novel_entries: list = field(default_factory=list)
    ledger_skipped: int = 0


def project_from_turns(wiki_root: "str | Path", sid: str, turns: "list[dict]",
                        *, ledger) -> ProjectionResult:
    """Project already-extracted turns to novel-turn markdown (R1 — cheap half).

    Does NOT open DuckDB. Consumes the JSON-safe turn dicts from
    ``extract_turns_batch`` (role, uuid, ts, projected_text, hash),
    then: within-sid exact dedup -> ledger diff (drop already-seen) -> markdown.
    This is the per-sid step run INSIDE begin, so the ledger read-after-write (F3)
    stays sequential across the Path B loop.

    Args:
        wiki_root: the wiki root (to read ledger.read_seen_hashes).
        sid: the session_id these turns belong to (recorded on novel entries).
        turns: the extracted turn dicts (each carries its F5 hash).
        ledger: the llmwiki.ingest.ledger module (seen-set single source of truth).

    Returns:
        ProjectionResult(markdown, novel_entries, ledger_skipped).
    """
    seen = ledger.read_seen_hashes(wiki_root)

    header = ["# CC Session transcript", ""]
    body: list[str] = []
    novel_entries: list[dict] = []
    # ledger-diff drop count (F6): turns already owned by a prior ingest. Counted
    # separately from the within-sid exact-dedup collapse below (different signal).
    ledger_skipped = 0
    # within-sid exact dedup: a hash seen earlier in THIS projection collapses.
    local_seen: set[str] = set()
    n = 0
    for turn in turns:
        projected_text = turn.get("projected_text") or ""
        # A turn with no text contributes nothing — skip so it neither renders
        # an empty turn nor pollutes the ledger. (Post-D5/D12 this is normally
        # unreachable — a turn only exists if it had a text block — but stays
        # as a defensive guard for a turn whose sole text was boilerplate.)
        if not projected_text:
            continue
        # The hash was assigned at extraction (F5 single source of truth) — use it.
        h = turn["hash"]
        # length-independent exact dedup (F4): collapse identical branches within
        # the sid (min-length guard WITHDRAWN — short affirmations collapse too;
        # first copy is retained so the decision signal survives).
        if h in local_seen:
            continue
        local_seen.add(h)
        # ledger diff (F1-b): drop turns already owned by a prior ingest. Count the
        # drop (F6) so begin can surface the ledger-skip TURN count to the Path B
        # loop (an incremental re-run must not look like a silent no-op; RS-d).
        if h in seen:
            ledger_skipped += 1
            continue
        n += 1
        body.append(_render_turn_md(turn, n, sid=sid))
        novel_entries.append({
            "hash": h,
            "first_sid": sid,
            "first_uuid": turn.get("uuid") or "",
            "first_ts": turn.get("ts") or "",
        })

    if not body:
        # No novel turns — empty transcript body. fe_b_prime will hash it; the
        # driver treats a dedup no-op / empty result as it does today. novel is [].
        markdown = "\n".join(header)
    else:
        markdown = "\n".join(header + body)
    return ProjectionResult(markdown=markdown, novel_entries=novel_entries,
                            ledger_skipped=ledger_skipped)


def extract_owned(sid: str, *, ledger) -> "list[dict]":
    """Extract ONE sid's turns (the EXPENSIVE half of Path A; read-only).

    ``project_owned``'s extraction step factored out so begin can run it BEFORE
    acquiring the transaction lock (#19 in-lock ledger diff): it opens DuckDB
    and reads the session corpus but touches NO wiki state — ``ledger`` is used
    solely for ``compute_hash`` (F5), never for the seen-set. The ledger DIFF
    (the read side of the ledger read-modify-write) lives in
    ``project_from_turns``, which begin runs INSIDE the lock.
    """
    return [_turn_to_dict(t, ledger=ledger) for t in _fetch_turns(sid)]


def project_owned(wiki_root: "str | Path", sid: str, *, ledger) -> ProjectionResult:
    """Project one sid to novel-turn markdown + the ledger-entry channel (Path A).

    The composition of the two halves for a SINGLE sid: ``extract_owned`` (one
    DuckDB scan — fine for one sid), then ``project_from_turns``. NOTE: begin
    no longer calls this composition directly — it runs ``extract_owned``
    before the lock and ``project_from_turns`` inside the lock (#19), so this
    stays as the one-shot composition for ``main()`` / manual inspection (and
    as the behavioral spec the split halves must agree with). Path B does NOT
    call this either — it batch-extracts all sids once (``extract_turns_batch``)
    then calls ``project_from_turns`` per sid.

    Steps: project TEXT blocks only, thinking/tool_use/tool_result/isMeta excluded
    at the SQL level (D5/D12) -> group into turns -> boilerplate strip -> within-sid
    exact dedup -> ledger diff (drop already-seen) -> markdown.

    Args:
        wiki_root: the wiki root (to read ledger.read_seen_hashes).
        sid: the session_id to project (agent children fold in via shared sid).
        ledger: the llmwiki.ingest.ledger module (hash + seen-set single source
            of truth; injected so the driver/test controls it).

    Returns:
        ProjectionResult(markdown, novel_entries, ledger_skipped).
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
    # because it is a separate, pre-existing behaviour with its own consumers;
    # do not read this change as having normalised both.
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")

    ap = argparse.ArgumentParser(
        description="Project a single cc-log sid to novel-turn markdown (T2).")
    ap.add_argument("wiki_root", help="Wiki root (for the turn ledger).")
    ap.add_argument("sid", help="Session id to project.")
    ap.add_argument("-o", "--output", default=None, help="Output file (default stdout).")
    args = ap.parse_args()
    try:
        res = project_owned(args.wiki_root, args.sid, ledger=_ledger)
    except ProjectionError as e:
        print(str(e), file=sys.stderr)
        sys.exit(3)
    if args.output:
        Path(args.output).write_text(res.markdown, encoding="utf-8")
        print(f"{len(res.novel_entries)} novel turns -> {args.output}", file=sys.stderr)
    else:
        print(res.markdown)


if __name__ == "__main__":
    main()
