# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb"]
# ///
"""Fork-aware cc-log projector (T2) — replaces the fork-blind extract_cc_log.

Projects a SINGLE session id (plus its agent children — fork-child records carry
the parent session_id, verified 308/308, so filtering by session_id co-locates
them) out of the vendored inspect-cc-log DuckDB views, then:

  1. groups the block rows into chronological turns (text + tool_use + paired
     tool_result), EXCLUDING thinking blocks (S8-c);
  2. strips the stable injected boilerplate markers at the PROJECTION
     normalization stage (F4/U2 — projector side, NOT redaction=D16 which stays
     in frontends.fe_b_prime);
  3. length-independent EXACT dedup within the sid (F4 — the ≥200-char
     min-length guard is WITHDRAWN; identical (role,text) turns collapse to one);
  4. ledger diff (F1-b/T4) — drops turns whose ``ledger.compute_hash(role,text)``
     is already in ``ledger.read_seen_hashes(wiki_root)`` (cross-path / cross-run
     idempotency);
  5. renders markdown for the SURVIVING novel turns (text block + provenance
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
    Path A (single session, /wiki-ingest) uses ``project_owned`` — one DuckDB
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
      turn_dict = {role, uuid, ts, projected_text, tool_uses, hash} — the
      boilerplate-stripped, hash-carrying turn (hash = ledger.compute_hash(role,
      projected_text), assigned HERE so batch and begin agree on the F5 hash).
      tool_uses is a list of {name, tool_input, tuid, result} dicts (JSON-safe).

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

The markdown shape mirrors the OLD extract_cc_log output (``## Turn N [ts]``,
``**Human**:``, ``**Assistant**:``, ``**Tool: <name>**`` + ```tool-result```)
so fe_b_prime's redact→hash→raw/derived/<hash>.md contract is unchanged, with a
per-turn provenance pointer line appended (sid/uuid/ts).
"""

from __future__ import annotations

import json
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
# The whole injected block is stripped, not just the marker line.

# system-reminder is an XML-ish wrapper; strip the whole element (DOTALL).
_RE_SYSTEM_REMINDER = re.compile(
    r"<system-reminder>.*?</system-reminder>", re.DOTALL)

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
# bullets (role-present variant only), and the trailing precedence/rule
# lines (which vary in length: "Precedence: ...", "Follow Mode ...",
# "NEVER rule ...", "Include `[Mode: ...".
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
_MODE_TRAILER_KEYWORDS = (
    "Precedence:", "Follow Mode", "NEVER rule", "Include `[Mode",
    "Role:", "Mode:",
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
# One row per content block of the sid (and its agent children), chronological.
# thinking blocks are EXCLUDED here (S8-c) by not selecting block_type='thinking'
# — we take text / tool_use / tool_result only. record_uuid + role + ts group
# blocks into turns; block_index preserves intra-record order.
#
# The batch form (R1 / F-H1) selects MANY sids in one scan with `session_id IN
# (...)`, carrying session_id in the projection so rows can be split per sid. The
# single-sid form filters `session_id = ?`. Both keep the same block-type filter
# and chronological ordering; the batch adds session_id as the leading sort key so
# each sid's rows are contiguous and internally chronological.
_PROJECT_COLUMNS = """
  record_uuid,
  session_id,
  role,
  strftime(ts, '%Y-%m-%d %H:%M:%S') AS ts_str,
  block_index,
  block_type,
  text,
  tool_name,
  tool_input,
  tool_use_id,
  tool_result_content
"""

_PROJECT_SQL = f"""
SELECT
{_PROJECT_COLUMNS}
FROM cc_block
WHERE session_id = ?
  AND block_type IN ('text', 'tool_use', 'tool_result')
ORDER BY ts ASC, record_uuid ASC, block_index ASC
"""


@dataclass
class _Turn:
    """A grouped turn: one record_uuid's blocks (chronological)."""
    role: str                       # "user" | "assistant"
    uuid: str                       # record_uuid (provenance pointer)
    ts: str                         # local ts string (provenance pointer)
    text_parts: list = field(default_factory=list)      # str
    tool_uses: list = field(default_factory=list)       # (name, input_json, tuid)
    order: int = 0                  # first-seen order (stable sort key)


def _render_tool_use(name: str, tool_input_json) -> str:
    """One-line display for a tool_use block (most-identifying field per tool).

    Mirrors extract_cc_log.render_tool_use so the FE-B' markdown shape is
    unchanged. tool_input arrives as a JSON string (DuckDB json) or None.
    """
    try:
        inp = json.loads(tool_input_json) if isinstance(tool_input_json, str) else (
            tool_input_json if isinstance(tool_input_json, dict) else {})
    except (json.JSONDecodeError, TypeError):
        inp = {}
    if not isinstance(inp, dict):
        inp = {}

    def fld(key: str) -> str:
        v = inp.get(key, "")
        return v if isinstance(v, str) else str(v)

    if name == "Bash":
        return f"Bash: {fld('command')}"
    if name == "Write":
        return f"Write: {fld('file_path')}"
    if name == "Edit":
        return f"Edit: {fld('file_path')}"
    if name == "Read":
        return f"Read: {fld('file_path')}"
    if name == "Glob":
        return f"Glob: {fld('pattern')}"
    if name == "Grep":
        return f"Grep: {fld('pattern')}"
    if name == "Agent":
        sub = inp.get("subagent_type") or inp.get("description") or ""
        return f"Agent: {sub if isinstance(sub, str) else str(sub)}"
    if name == "NotebookEdit":
        return f"NotebookEdit: {fld('notebook_path')}"
    return name or ""


def _tool_result_text(tool_result_content) -> str:
    """Flatten a tool_result content (json array of blocks, str, or None)."""
    if tool_result_content is None:
        return ""
    val = tool_result_content
    if isinstance(val, str):
        # cc_block stores blk->>'content' — for tool_result this is the JSON of
        # the content (array or string). Try to parse an array of text blocks.
        stripped = val.lstrip()
        if stripped.startswith("[") or stripped.startswith("{"):
            try:
                val = json.loads(val)
            except json.JSONDecodeError:
                return val
        else:
            return val
    if isinstance(val, list):
        parts = []
        for part in val:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text") or "")
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    if isinstance(val, dict):
        return val.get("text") or ""
    return str(val)


def _group_rows_to_turns(rows: list) -> list[_Turn]:
    """Group projection rows (for ONE sid) into ordered turns.

    Rows are the projection columns (`_PROJECT_COLUMNS`) for a single session,
    already chronological. A tool_result block is rendered under its tool_use
    (paired by tool_use_id), not as a standalone turn part. Turns are keyed by
    record_uuid (the dedup/render unit — R2: CC record grain), role frozen at
    first-seen (a record_uuid is single-role by construction — verified 0
    multi-role uuids in the live corpus).
    """
    # First pass: collect tool_result text by tool_use_id (a tool_result block
    # lives on the user row after the assistant tool_use; render it under the
    # tool_use like the old extractor did).
    results: dict[str, str] = {}
    for (_uuid, _sid, _role, _ts, _bi, btype, _text, _tn, _ti, tuid,
         trc) in rows:
        if btype == "tool_result" and tuid:
            results[tuid] = _tool_result_text(trc)

    turns: dict[str, _Turn] = {}
    order = 0
    for (uuid, _sid, role, ts_str, _bi, btype, text, tool_name, tool_input, tuid,
         _trc) in rows:
        if btype == "tool_result":
            continue  # rendered under its tool_use, not a standalone turn part
        t = turns.get(uuid)
        if t is None:
            t = _Turn(role=role or "", uuid=uuid or "", ts=ts_str or "",
                      order=order)
            order += 1
            turns[uuid] = t
        if btype == "text":
            if text and text.strip():
                t.text_parts.append(text)
        elif btype == "tool_use":
            t.tool_uses.append((tool_name or "", tool_input, tuid or "",
                                results.get(tuid or "", "")))
    return sorted(turns.values(), key=lambda x: x.order)


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

    Only the conversation text is hashed (role ‖ text). tool_use display and
    tool-results are provenance/rendering, not part of the dedup identity of a
    turn — the dedup unit is the (role, text) turn per the ledger contract
    (compute_hash(role, text)). A turn with no text (tool-use-only assistant
    row) hashes on empty text; those still render but collapse identically.
    """
    joined = "\n".join(turn.text_parts).strip()
    return strip_boilerplate(joined)


def _tool_input_to_jsonsafe(tool_input) -> "str | None":
    """Normalize a tool_input (DuckDB json str, dict, or None) to a JSON str/None.

    ``extract_turns_batch`` serializes turns to JSON for the on-disk hand-off, so
    tool_input must be JSON-safe. A dict is dumped to a compact JSON string; a str
    (the DuckDB json representation) is kept as-is; anything else -> None.
    ``_render_tool_use`` already accepts a JSON string or dict, so a string
    round-trips identically to the direct-projection path.
    """
    if tool_input is None:
        return None
    if isinstance(tool_input, str):
        return tool_input
    if isinstance(tool_input, dict):
        return json.dumps(tool_input, ensure_ascii=False)
    return None


def _turn_to_dict(turn: _Turn, *, ledger) -> dict:
    """Serialize a grouped turn to the JSON-safe hand-off dict (R1).

    Carries the boilerplate-stripped projected text AND the F5 hash
    (``ledger.compute_hash(role, projected_text)``) so the batch extractor and
    begin agree on the exact dedup/ledger key (the hash is assigned ONCE, here).
    tool_uses become {name, tool_input, tuid, result} dicts (tool_input JSON-safe).
    """
    projected_text = _turn_text_for_hash(turn)
    return {
        "role": turn.role,
        "uuid": turn.uuid,
        "ts": turn.ts,
        "projected_text": projected_text,
        "hash": ledger.compute_hash(turn.role, projected_text),
        "tool_uses": [
            {
                "name": name,
                "tool_input": _tool_input_to_jsonsafe(tool_input),
                "tuid": tuid,
                "result": res,
            }
            for (name, tool_input, tuid, res) in turn.tool_uses
        ],
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
        (role, uuid, ts, projected_text, hash, tool_uses). A sid with no rows maps
        to an empty list (still present in the dict, so the caller sees every sid).
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
            f"  AND block_type IN ('text', 'tool_use', 'tool_result')\n"
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


def _render_turn_md(turn: dict, n: int) -> str:
    """Render one surviving turn's markdown (matches old FE-B' shape + pointer).

    Consumes the JSON-safe turn dict (role, uuid, ts, projected_text, tool_uses
    with {name, tool_input, tuid, result}). projected_text is the already-stripped
    text carried on the dict (R1 — strip happened at extraction).
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
        for tu in turn.get("tool_uses") or []:
            name = tu.get("name") or ""
            res = tu.get("result") or ""
            lines += [f"**Tool: {_render_tool_use(name, tu.get('tool_input'))}**", ""]
            if res:
                lines += ["```tool-result", res, "```", ""]
    # Provenance pointer (sid/uuid/ts) — promotion evidence re-fetch handle.
    lines += [f"<!-- provenance: uuid={uuid} ts={ts} -->", ""]
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
    ``extract_turns_batch`` (role, uuid, ts, projected_text, hash, tool_uses),
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
        tool_uses = turn.get("tool_uses") or []
        # A turn with neither text nor tool_uses contributes nothing — skip so it
        # neither renders an empty turn nor pollutes the ledger.
        if not projected_text and not tool_uses:
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
        body.append(_render_turn_md(turn, n))
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

    Steps: project blocks -> group turns (thinking excluded) -> boilerplate strip
    -> within-sid exact dedup -> ledger diff (drop already-seen) -> markdown.

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
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

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
