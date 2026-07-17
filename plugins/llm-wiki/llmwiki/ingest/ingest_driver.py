# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb"]
# ///
"""ingest_driver — the deterministic ingest CLI (plan C1, §3 contract).

Owns ALL code steps + transaction state for an ingest. It does NOT author page
content and does NOT dispatch subagents (that stays in the orchestrator prompt).
State between `begin` and `finish`/`abort` is carried on disk via the
`.llmwiki.txn` sidecar — ZERO LLM-threaded transaction state (plan R-d / T2
completion criterion).

Verbs (the four transaction verbs below + the read-only `enumerate`,
`session-plan` and `project-batch` helpers, plan §2 A-1; the four transaction
verbs honor plan R-f's verb budget <=4 — the read-only helpers are OUTSIDE that
budget, opening no transaction):

  begin <root> <source> [--kind=auto|fe_b|fe_b_prime|fe_pi_log] [--write_mode=..]
        [--apply_fanout_k=..] [--doc_type=..] [--external=..] [--turns=<path>]
    marker.detect -> config_resolver.resolve_all + declare_all
    -> config_resolver.check_consistency (raises ConfigInconsistency) BEFORE
       locking (plan §3 / D-c: violation surfaces before any side effect)
    -> transaction.acquire_lock THEN transaction.checkpoint (lock-first, so only
       the lock holder ever creates the fixed-path journal dir; F1 fix)
    -> front-end (FE-B: frontends.fe_b ; FE-B': cc_log_project.extract_owned
       BEFORE the lock, then project_from_turns IN-LOCK (#19: the ledger diff
       is the read side of a read-modify-write and must not race a concurrent
       finish), then frontends.fe_b_prime). The FE runs redaction/secret-scan +
       content-hash dedup itself.
    -> write the raw artifact (unless dedup no-op) + write the sidecar
    -> print JSON {declaration[], raw_rel_path, declaration_hash,
       stage1_blob_path, origin, doc_type, max_count, max_bytes, apply_fanout_k,
       dedup_noop, redaction_flags[], ledger_skipped} (E1/D-1: `raw_rel_path`=
       `fe.rel_path` + code-side `declaration_hash` REPLACE the old inline
       `redacted_body`). `stage1_blob_path` (#1 follow-up) is the ABSOLUTE path the
       caller Writes the Stage1 blob to (under `--out_dir` if given, else the system
       temp dir) — code-authored so the LLM never reconstructs a temp path.
    If dedup_noop, the raw already existed so it was NOT written and NO sidecar
    was written; begin auto-closes the transaction itself (rollback +
    release_lock, C1) and returns `auto_closed: true`, so the caller only reports
    and does NOT call finish (there is no sidecar to finish).
    `--turns=<path>` (FE-B' Path B, R1/F-H1): use the pre-extracted turns at
    <path> (from `project-batch`) instead of re-scanning the corpus — begin then
    runs only the cheap per-sid projection half (project_from_turns, in-lock).
    Path A omits it (begin extracts the one sid itself via extract_owned before
    the lock; the diff+render half is the same in-lock project_from_turns).
    `--kind=fe_pi_log` dispatches to `pi_log_project` instead of `cc_log_project`
    for BOTH Path A (extract_owned) and Path B (project_from_turns) — same call
    shape, table-dispatched (design B / OI-1). F-14: for `--kind=fe_pi_log`,
    `<source>` MUST be a bare sid, never a file path — pi session filenames are
    `<ts>_<sid>.jsonl` (NOT `<sid>.jsonl` like cc), so `Path(source).stem` on a
    pi filename would extract the wrong value. A path passed by mistake fails
    closed as a pi ProjectionError (file-not-found for the bogus "sid") -> exit 3.
    F-1: when `--turns` is given, the turns JSON's `"origin"` is checked against
    the resolved `--kind`; a mismatch is a fail-closed DriverError (a missing
    `"origin"` key, from older project-batch output, is treated as `fe_b_prime`
    for backward compatibility).

  plan-fanout <root> <stage1_proposal_path_or_json>
    touched <= k -> one cluster; touched > k -> ceil(touched/k) clusters each
    <= k. touched > max_count -> DriverError (F2 ingest-grain budget / human gate,
    since the per-worker WriteSession budget would otherwise be multiplied by the
    cluster count). Print {clusters: [[rel_path,...], ...]}.

  finish <root> <outcome:success|fail>
    reconstruct lock handle + checkpoint from the sidecar -> join (confirm
    expected pages on disk) -> wiki_index.regenerate -> wiki_log.append
    (FE-dispatched prefix) -> transaction.commit (discard journal, success) OR
    transaction.rollback (replay journal, fail) -> transaction.release_lock
    (always) -> delete the sidecar. Print {committed:true} or {rolled_back:true}.

  abort <root>
    rollback + release_lock + delete sidecar (manual recovery, D-g). Safe when a
    sidecar exists; no-op-with-message if none.

  enumerate <root> <glob>   (read-only helper, plan §2 A-1 / G-a/G-b/G-e/G-d)
    Expand the glob in Python (`Path.glob`, no shell expansion; OS-independent +
    deterministic via sorting). Force-exclude wiki-internal paths (G-b: raw/,
    wiki/, .git/, .qmd/, SCHEMA.md, .llmwiki[.lock/.txn], log.md, index.md,
    .cc-turn-ledger.jsonl); files only. (F7: the turn ledger is a driver state
    file — excluding it prevents enumerate from self-ingesting it.)
    A directory-only argument (`./docs/`) is sugar for `<dir>/**/*` restricted to
    a text-type extension allowlist (G-e). `**` recurses (G-d). Zero matches is
    an explicit error. No lock / checkpoint / write — pure enumeration. Print
    {files: [rel_path,...], excluded: <count>, pattern: <effective glob>}.

  session-plan <root> [--pj <name>] [--workspace] [--scope <scope>]
        [--kind=auto|fe_b_prime|fe_pi_log] [--sid <sid>]
        (read-only Path B resolver, T6 / F1-b/F2-B, design C; D2/D3/D4/D5/D6
        workspace-session-ingest.md)
    Resolve the SET of session ids for a Path B project ingest and return them
    ORDERED BY session-start ts ASCENDING — and nothing else. It does NOT
    partition ownership / emit an owned-turn manifest (F1-b/F2-B: ownership is
    decided per-begin by the ledger diff); it does NOT open a transaction (read
    only, outside the verb budget).
    `--kind` (default `auto` -> `fe_b_prime`, same "auto->fe_b_prime 既定" as
    project-batch below; this verb is projection-only so FE-B has no meaning
    here): `auto`/`fe_b_prime` resolves the CC-log session set (unchanged from
    before this flag existed); `fe_pi_log` resolves the pi-log session set
    instead (pi_log_project-backed).
      - `--kind=auto|fe_b_prime`:
        - --workspace (explicit, D3), OR --pj/--workspace both omitted AND
          --scope=workspace (no-args A-follow, D2): union EVERY sid in
          `_projects/_state/*.json`, NO `project` filter (`_sids_workspace`) —
          harness-neutral, sidesteps the lossy CC slug reverse-mapping
          entirely; scope "workspace".
        - --pj <name> given (explicit, unchanged): filter `_projects/_state/*.json`
          (the taskflow state dir under the process CWD, exactly as
          wiki_root_resolver locates it) by `project == <name>`; the matching
          state files' filename STEMS are the sids (state file is `<sid>.json`,
          `{"project": ...}`).
        - --pj/--workspace both omitted, --scope in (pj, prompt) (no-args,
          D2): resolve THIS session's taskflow-APPLIED project via
          `_active_project_for_sid(--sid)` (`_projects/_state/<sid>.json`
          `project` field, NOT a name derived from the wiki path), then the
          same `--pj`-style enumeration; unresolvable -> DriverError guiding
          the caller to `--pj <name>` (fail-closed, NOT a silent narrow-to-cwd
          fall-back — that silent narrowing was the diagnosed symptom).
        - --pj/--workspace both omitted, --scope in (None, cwd) (D4,
          UNCHANGED from before kind/sid/scope existed): resolve the CC
          project dir from the RUNNING session's own log location as ground
          truth (U3) — find `~/.claude/projects/*/<current-
          sid>.jsonl` (current-sid = $CLAUDE_CODE_SESSION_ID — D5 fix, was
          the wrong env name — or `--sid` if given — F-13: an explicit `--sid`
          takes priority over the env var) and take its PARENT dir
          (CC-internal-encoding independent), then the sids are that dir's
          `*.jsonl` stems. SECONDARY fallback only: reverse-generate the
          slug from the CWD (path separators + the drive colon -> `-`).
        ts to sort by = each sid's earliest record timestamp (`cc_session.started`
        = min(ts) in the vendored views) — the authoritative in-log event clock,
        not file mtime (mtime drifts on copy/resume/fork/checkout).
      - `--kind=fe_pi_log` (UNCHANGED — D3's workspace path is cc-only for now,
        see the module's Follow-ups):
        - --pj <name> given: same `_sids_for_pj` enumeration (harness-neutral),
          then F-2 locality filter — intersect with the sids that actually have a
          pi session file (from the same directory walk used for ordering below);
          filtered-out sids (present in taskflow state but with no pi session
          file, e.g. a foreign harness's sid) are counted and reported as
          `filtered_out: <n>` (never silently dropped).
        - --pj omitted: primary `--sid <sid>` (F-13, pi has no env fallback,
          arg-only) -> `pi_log_project.session_dir_for_sid(sid)`; if that is
          `None` (not found, F-6) or `--sid` was omitted, fallback to
          `pi_log_project.session_dir_for_cwd(cwd)`. If the fallback dir also
          does not exist, DriverError (fail-closed, mirroring the CC path's
          zero-match message shape).
        ts to sort by = the session filename's `<ts>_<sid>.jsonl` prefix
        ASCENDING (no DuckDB; design fact 11) — NOT `cc_session.started`
        (pi sids are not in the cc corpus).
    `--sid <sid>` (F-13): an explicit override for the cwd-path's "current
    session" resolution, taking priority over the env var; it ALSO feeds the
    no-args `--scope pj|prompt` active-project resolution (D2, cc kind only).
    Omitted, both kinds' cwd-path behavior is unchanged from before this flag
    existed (env-only for cc; `session_dir_for_cwd` fallback only for pi), and
    the no-args pj/prompt branch cannot resolve (fails closed).
    `--workspace` (D3, cc kind only, explicit; a `--pj`-independent boolean
    flag) and `--scope <scope>` (D2, the caller's resolved WIKI_SCOPE — one of
    `prompt|pj|workspace|cwd` — used ONLY in the no-args case, i.e. `--pj` and
    `--workspace` both absent) together implement the no-args scope tree; an
    explicit `--pj` or `--workspace` always overrides `--scope`.
    Zero matches is an explicit error (fail-closed, like `enumerate`). Print
    {sids: [sid,...], scope: "pj"|"workspace"|"cwd", pattern: <state-glob or
    project-dir>, filtered_out: <n>} (`filtered_out` is present for the
    `fe_pi_log` pj-path; 0 for every other path).

  project-batch <root> <sid> [<sid>...] [--kind=auto|fe_b_prime|fe_pi_log]
        (read-only Path B scan-collapse; R1/F-H1)
    Extract the turns for ALL given sids in ONE scan
    (`cc_log_project.extract_turns_batch` for `auto`/`fe_b_prime`,
    `pi_log_project.extract_turns_batch` for `fe_pi_log`; default `auto` ->
    `fe_b_prime` since this verb is projection-only, same "auto->fe_b_prime
    既定" as session-plan above) so Path B does not re-scan the corpus once per
    begin (N sids -> N scans). Writes each sid's extracted turns (boilerplate-
    stripped, F5-hash-carrying) to a per-sid JSON file under a fresh temp dir
    OUTSIDE the wiki root (never journaled / never enumerated); each file also
    carries `"origin": <resolved --kind>` (F-1) so the paired `begin --turns`
    can verify it was extracted under the SAME kind (fail-closed on mismatch).
    Prints {out_dir, turns: {sid: <path>}, scanned}. The Path B loop passes
    each begin `--turns=<path>` (and the SAME `--kind`) so begin runs only the
    cheap per-sid half (project_from_turns) — the ledger read-after-write (F3)
    stays sequential. Opens NO transaction (outside the R-f verb budget). The
    loop hands the temp dir to `project-batch-cleanup` (C3).

  project-batch-cleanup <out_dir>   (C3 step 1: code owns temp-dir deletion)
    Delete a `project-batch` temp dir — replaces the orchestrator prompt's bare
    `rm -rf "$OUT_DIR"`. REFUSES (DriverError, no deletion) unless <out_dir>'s
    basename starts with `_BATCH_TURNS_PREFIX` AND its parent is
    `tempfile.gettempdir()` (the two properties `project-batch`'s mkdtemp
    guarantees), then `shutil.rmtree(ignore_errors=True)`. As a backstop
    (C3 step 2 / F3: the temp turn JSON is pre-redaction) `project-batch` also
    prunes stale (>24h) `llmwiki-turns-*` dirs at its start. Opens NO transaction.

Sidecar `.llmwiki.txn` (JSON, beside `.llmwiki.lock`):
  {journal_dir, origin, doc_type, max_count, max_bytes,
   apply_fanout_k, fe_hash, pid, lock_token, pending_ledger_entries}

`pending_ledger_entries` (S8-b / T4) carries the novel turn-content-hash
entries from `begin` to `finish` on disk. `finish(success)` journals the turn
ledger (`.cc-turn-ledger.jsonl`) then appends them inside the single
transaction; on rollback the journal reverts the append (F3). The T2 projector
populates this list; the current FE paths emit none, so `begin` writes it empty.

`lock_token` (DEC-R1=D) records the ownership token `begin` wrote into the lock
file, so `finish` / `abort` refuse to operate on a transaction they do not own
(token mismatch) — defense-in-depth atop the residue-shape reclaim guard.

NOTE: the FE-B' projector module is `cc_log_project` (fork-aware; projects a
single sid out of the vendored DuckDB views, dedups + ledger-diffs the turns).
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from llmwiki.core import marker
from llmwiki.core import config_resolver
from llmwiki.core import content_hash
from llmwiki.write import transaction
from llmwiki.ingest import frontends
from llmwiki.core import wiki_index
from llmwiki.core import wiki_log
from llmwiki.ingest import cc_log_project
from llmwiki.ingest import pi_log_project
from llmwiki.ingest import ledger


SIDECAR_NAME = ".llmwiki.txn"

# Front-end origins (log prefix dispatch keys). FE-B = 3rd-party source,
# FE-B' = cc-log jsonl transcript, fe_pi_log = pi-log jsonl transcript
# (design §4 / OI-1 design B).
ORIGIN_FE_B = "fe_b"
ORIGIN_FE_B_PRIME = "fe_b_prime"
ORIGIN_FE_PI_LOG = "fe_pi_log"

# Projection-origin dispatch tables (OI-1 design B, 案Y). Membership in
# `_PROJECTOR_BY_ORIGIN` also DOUBLES as "is this a projection origin"
# (change point 5: `origin in _PROJECTOR_BY_ORIGIN` replaces the old
# `origin == ORIGIN_FE_B_PRIME` gates now that fe_pi_log is a second
# projection origin).
_PROJECTOR_BY_ORIGIN = {
    ORIGIN_FE_B_PRIME: cc_log_project,
    ORIGIN_FE_PI_LOG: pi_log_project,
}
_FE_BY_ORIGIN = {
    ORIGIN_FE_B_PRIME: frontends.fe_b_prime,
    ORIGIN_FE_PI_LOG: frontends.fe_pi_log,
}
_LOG_HEADER_BY_ORIGIN = {
    ORIGIN_FE_B: wiki_log.header_for_fe_b,
    ORIGIN_FE_B_PRIME: wiki_log.header_for_fe_b_prime,
    ORIGIN_FE_PI_LOG: wiki_log.header_for_fe_pi_log,
}
_PROJECTION_ERRORS = (cc_log_project.ProjectionError, pi_log_project.ProjectionError)


# Exit-code contract (theme1 i:39, generalized to this entrypoint 2026-07-16).
# rc 0 = success. rc 2 = verb-specific SENTINEL only — a state notice (no
# marker / no sidecar / REFUSED / zero-match / busy) that callers consume as
# data, NOT a failure; bare DriverError and transaction.LockHeld surface here.
# rc 3 = OPERATIONAL error (runtime/environment/verification failure, e.g.
# non-UTF-8 source, corrupt sidecar, apply-verification mismatch); raised as
# DriverOpError. Usage / protocol errors (bad args, unknown verb, malformed
# --turns/--kind, Stage1 contract violations) return EX_USAGE so a contract
# drift surfaces as a hard failure instead of masquerading as a sentinel;
# raised as DriverUsageError. 64 = sysexits.h EX_USAGE, byte-identical to
# cli.py's EX_USAGE.
EX_USAGE = 64


class DriverError(Exception):
    """A driver-level state SENTINEL (surfaced to the CLI as exit 2).

    Bare `DriverError` (not one of the subclasses below) is a normal-data
    state notice a caller consumes as data (e.g. "no .llmwiki marker" /
    "no .llmwiki.txn sidecar" / a REFUSED / zero-match result) — NOT a
    failure.
    """


class DriverUsageError(DriverError):
    """A usage / protocol error (surfaced to the CLI as EX_USAGE=64).

    Bad argv shape, malformed --turns/--kind input, or a Stage1 output that
    violates its contract (e.g. a tier mismatch). Subclasses DriverError so
    any caller still catching the base class keeps working, but `main()`
    dispatches this to EX_USAGE via a most-specific-first except chain.
    """


class DriverOpError(DriverError):
    """A runtime / environment / verification failure (exit 3).

    Not a usage error and not a normal-data state sentinel: a non-UTF-8
    source, a corrupt sidecar field, a missing optional dependency (duckdb),
    or an apply-time verification mismatch. Subclasses DriverError for the
    same backward-compat reason as DriverUsageError.
    """


# Canonical ingest verb set (single source of truth). main()'s own usage /
# unknown-verb banners derive from this tuple, and the bin/ shim imports it to
# BUILD both its usage banner and its "did you mean" hint (no literal re-listing
# anywhere), so a new verb is declared in exactly one place.
INGEST_VERBS = (
    "begin",
    "plan-fanout",
    "finish",
    "apply-finish",
    "abort",
    "enumerate",
    "session-plan",
    "project-batch",
    "project-batch-cleanup",
)

# begin's accepted --flags (DEC-a: begin-only strict parse; the central
# _parse_opts stays permissive for every other verb, e.g. session-plan's
# space-form `--pj`). A --flag outside this set, or any of these supplied with
# no value, is a usage error (EX_USAGE) rather than a silently-ignored token.
_BEGIN_OPTS = frozenset({
    "kind", "write_mode", "apply_fanout_k", "doc_type",
    "external", "turns", "out_dir",
})


# --------------------------------------------------------------------------- #
# sidecar (on-disk transaction state — replaces LLM-threaded state)
# --------------------------------------------------------------------------- #
def _sidecar_path(wiki_root: "str | Path") -> Path:
    return Path(wiki_root) / SIDECAR_NAME


def _write_sidecar(wiki_root: "str | Path", state: dict) -> None:
    _sidecar_path(wiki_root).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _read_sidecar(wiki_root: "str | Path") -> "dict | None":
    path = _sidecar_path(wiki_root)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _delete_sidecar(wiki_root: "str | Path") -> None:
    try:
        _sidecar_path(wiki_root).unlink()
    except FileNotFoundError:
        pass


def _checkpoint_from_sidecar(state: dict) -> transaction.Checkpoint:
    return transaction.Checkpoint(journal_dir=state.get("journal_dir", ""))


def _lock_handle(wiki_root: "str | Path") -> transaction.LockHandle:
    return transaction.LockHandle(path=Path(wiki_root) / transaction.LOCK_NAME)


# --------------------------------------------------------------------------- #
# verb: begin
# --------------------------------------------------------------------------- #
def _resolve_kind(kind: str) -> str:
    """Resolve the --kind selector to a front-end origin.

    `auto` defaults to FE-B (3rd-party source) — FE-B' is opt-in for cc-log
    jsonl since its extractor pass is jsonl-specific (design §4 / FE-B').
    """
    if kind in ("auto", "", None, "fe_b"):
        return ORIGIN_FE_B
    if kind == "fe_b_prime":
        return ORIGIN_FE_B_PRIME
    if kind == "fe_pi_log":
        return ORIGIN_FE_PI_LOG
    raise DriverUsageError(f"unknown --kind: {kind!r} (auto|fe_b|fe_b_prime|fe_pi_log)")


def begin(wiki_root: str, source: str, *, kind: str = "auto",
          write_mode: "str | None" = None,
          apply_fanout_k: "str | None" = None,
          doc_type: "str | None" = None,
          external: "str | None" = None,
          turns: "str | None" = None,
          out_dir: "str | None" = None) -> dict:
    root = Path(wiki_root)

    # 1) marker.detect — the directory must be a wiki root (D8).
    mk = marker.detect(root)
    if mk is None:
        raise DriverError(f"no .llmwiki marker at {wiki_root} (not a wiki root)")

    # 2) config_resolver.resolve_all + declare_all.
    wiki_config = config_resolver.load_config(mk.schema_path)
    prompt_values: dict[str, str] = {}
    if write_mode:
        prompt_values["write_mode"] = write_mode
    if apply_fanout_k:
        prompt_values["apply_fanout_k"] = apply_fanout_k
    resolutions = config_resolver.resolve_all(prompt_values, wiki_config)
    declaration_block = config_resolver.declare_all(resolutions)
    declaration = declaration_block.splitlines() if declaration_block else []
    # E1/E4 (D-2/F4): a short, code-side hash of the resolved-value declaration
    # block. The Path B orchestrator echoes sid 1's declaration in full, then for
    # later sids emits one line iff this hash matches sid 1's (an equality check
    # only — the LLM never re-derives the declaration, avoiding a fail-open text
    # compare). content_hash is the toolkit's canonical sha-256; truncated to the
    # same 12-hex short form the log title already uses (fe_hash[:12]).
    declaration_hash = content_hash.content_hash(declaration_block)[:12]
    # The resolved-value declaration is announced before any write op (D5).
    print(declaration_block, file=sys.stderr)

    # 3) validate the consistency invariant BEFORE locking (D-c). A violation
    #    raises ConfigInconsistency before any side effect (no checkpoint, no
    #    lock, no FE) so begin aborts cleanly with the tree untouched.
    config_resolver.check_consistency(resolutions)

    origin = _resolve_kind(kind)

    # 3b) read the source as pure input (no side effect). FE-B reads text+ext;
    #     FE-B' extracts the jsonl transcript to markdown. A non-UTF-8 (binary)
    #     source is a clean per-file DriverError (R6) — so a glob loop counts it
    #     as a failure and continues (G-f) instead of dying with a traceback.
    if origin == ORIGIN_FE_B:
        try:
            fe_b_content = Path(source).read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise DriverOpError(
                f"source is not UTF-8 text (binary?): {source}") from exc
        except (FileNotFoundError, IsADirectoryError, PermissionError) as exc:
            # DEC-b: a missing / directory / unreadable source is an OPERATIONAL
            # error (exit 3, clean stderr) — not an uncaught traceback. Same
            # per-file DriverError shape as the UnicodeDecodeError above so a
            # glob loop counts it as a failure and continues (G-f).
            raise DriverOpError(
                f"source not readable (missing/dir/permission): {source}") from exc
        fe_b_ext = Path(source).suffix.lstrip(".") or "txt"
    else:  # FE-B' / fe_pi_log (projection origins; table-dispatched, design B)
        # Normalize the source to a session id. Path A surface: `source` is a
        # session jsonl path whose filename stem IS the sid for cc (empirically
        # `<sid>.jsonl`, 100%). Path B's session-plan sid (T6) reuses this branch
        # by passing the sid as the source (its stem is the sid too, so the same
        # derivation holds). F-14: for `--kind=fe_pi_log` `source` MUST be a bare
        # sid (never a path) — pi session filenames are `<ts>_<sid>.jsonl`, so
        # `Path(source).stem` on a pi filename would extract the wrong value
        # (`<ts>_<sid>`, not `<sid>`); this is NOT satisfied by the cc-filename
        # convention this line was originally written for. Here (pre-lock) the
        # projector only EXTRACTS that sid (+ agent/fork children, per-origin)
        # out of its backing store and strips boilerplate — the dedup + turn-
        # ledger diff (T4) + markdown render run IN-LOCK below (#19).
        sid = Path(source).stem
        projector = _PROJECTOR_BY_ORIGIN[origin]
        if turns is not None:
            # Path B scan-collapse (R1/F-H1): the turns were already extracted by
            # the read-only `project-batch` verb in ONE scan before the loop;
            # `--turns` is the path to this sid's turn-JSON. begin does NOT
            # re-scan — it only runs the cheap per-sid half (dedup + ledger diff +
            # markdown) so the ledger read-after-write (F3) stays sequential.
            turns_path = Path(turns)
            try:
                extracted = json.loads(turns_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise DriverUsageError(
                    f"--turns file unreadable or not JSON: {turns}") from exc
            # Guard the sid<->turns pairing: the turn-JSON's sid must match the
            # source's sid (a mismatched pairing would silently mis-attribute a
            # session's turns). The batch file records its sid under "sid"; each
            # turn's provenance also carries the owning sid on the first entry.
            turns_sid = extracted.get("sid") if isinstance(extracted, dict) else None
            turns_origin = (extracted.get("origin") if isinstance(extracted, dict)
                            else None)
            turn_list = (extracted.get("turns") if isinstance(extracted, dict)
                         else extracted)
            if turns_sid is not None and turns_sid != sid:
                raise DriverUsageError(
                    f"--turns sid mismatch: file is for {turns_sid!r} but source "
                    f"resolves to {sid!r}")
            # F-1: cross-origin --turns must fail closed. A flag drop/mixup
            # between session-plan -> project-batch -> begin would otherwise let
            # a cc-extracted turn dict (key `projected_text`) reach the pi
            # projector's `project_from_turns` (which reads `turn["text"]`),
            # silently skipping every turn as textless -> an empty "successful"
            # ingest. A missing "origin" key (older project-batch output,
            # pre-dating this field) is treated as `fe_b_prime` (backward-compat
            # default; F-1 design note / R-OI1-11).
            effective_turns_origin = (
                turns_origin if turns_origin is not None else ORIGIN_FE_B_PRIME)
            if effective_turns_origin != origin:
                raise DriverUsageError(
                    f"--turns origin mismatch: file is for "
                    f"{effective_turns_origin!r} but --kind resolves to "
                    f"{origin!r} (fail-closed, F-1)")
            if not isinstance(turn_list, list):
                raise DriverUsageError("--turns JSON must be a list or {sid, turns:[...]}")
        else:
            # Path A: run ONLY the extract half here (read-only: DuckDB / session
            # walk; NO wiki state — `ledger` is used solely for compute_hash).
            # The ledger DIFF moves INSIDE the lock below (#19): reading
            # read_seen_hashes before acquire_lock was a TOCTOU — a concurrent
            # ingest could finish() between our diff and our lock, leaving this
            # begin to re-file turns the other ingest now owns (duplicate pages
            # + last-wins first_sid corruption in the ledger).
            # DEC-b: keep the projection-origin source contract symmetric with
            # FE-B — a missing / inaccessible backing store (source absent) is an
            # OPERATIONAL error (exit 3, clean stderr), never an uncaught
            # traceback. (ProjectionError already lands rc3 via _PROJECTION_ERRORS
            # in main(); this only covers raw read OSErrors.)
            try:
                turn_list = projector.extract_owned(sid, ledger=ledger)
            except (FileNotFoundError, IsADirectoryError, PermissionError) as exc:
                raise DriverOpError(
                    f"projection source unavailable (missing backing store?): "
                    f"{source}") from exc

    # 4) acquire_lock THEN checkpoint (lock-first). Only the lock holder ever
    #    creates/touches the fixed-path journal dir, so a second ingest that fails
    #    to lock never races on it (F1 fix; the old checkpoint-before-lock order
    #    existed only for the now-removed git stash). LockHeld propagates before
    #    any journal is created — nothing to clean up.
    handle = transaction.acquire_lock(root)
    try:
        cp = transaction.checkpoint(root)
    except Exception:
        transaction.release_lock(handle)
        raise

    try:
        # 4b) projection cheap half IN-LOCK (#19): within-sid exact dedup ->
        #     ledger diff -> markdown. The seen-set read (`read_seen_hashes`
        #     inside project_from_turns) now happens strictly AFTER acquire_lock,
        #     so begin's diff can no longer race a concurrent ingest's finish
        #     (its ledger append is ordered before our read by the lock). Path A
        #     and Path B share this single in-lock code path; a failure here is
        #     cleaned up by the except below (rollback + release + sidecar).
        if origin != ORIGIN_FE_B:
            proj = projector.project_from_turns(root, sid, turn_list,
                                                ledger=ledger)

        # 5) run the matching front-end (the source was already read in 3b; the
        #    front-end assembly is pure: redaction/secret-scan + content-hash
        #    dedup, no source read). frontends.py owns redact-before-hash (D16/D18).
        if origin == ORIGIN_FE_B:
            fe = frontends.fe_b(root, fe_b_content, fe_b_ext,
                                external_locator=external)
        else:  # projection origins: table-dispatched FE over the projected
               # novel-turn markdown (fe_b_prime for cc, fe_pi_log for pi).
            fe = _FE_BY_ORIGIN[origin](root, proj.markdown)

        # doc_type: the projection-origin floor fixes transcript; FE-B takes the
        # prompt value or the FE-provided frontmatter doc_type if any.
        resolved_doc_type = (
            fe.frontmatter.get("doc_type")
            or doc_type
            or ("transcript" if origin in _PROJECTOR_BY_ORIGIN else "")
        )

        max_count = int(resolutions["max_count"].value)
        max_bytes = int(resolutions["max_bytes"].value)
        k = int(resolutions["apply_fanout_k"].value)

        # 6) write the raw artifact (FE does not write). On dedup no-op the raw
        #    already exists, so skip the write AND the sidecar; begin auto-closes
        #    the transaction itself below (C1) — the caller no longer runs
        #    finish(fail) to reclaim the lock. Journal the create BEFORE writing so
        #    a failed finish/abort removes the orphan raw (required for D18 dedup
        #    correctness).
        if not fe.exists:
            transaction.journal_before_write(root, [fe.rel_path])
            raw_path = root / Path(fe.rel_path)
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(fe.body, encoding="utf-8")

        # 7) write the sidecar (on-disk transaction state) ONLY when a raw was
        #    actually written (skip on dedup no-op — C1: begin auto-closes the txn
        #    below, so there is no transaction to hand to a finish and thus no
        #    sidecar to persist). `lock_token` records the ownership token so
        #    finish/abort can refuse a foreign txn (DEC-R1=D).
        #    `pending_ledger_entries` carries the novel turn-content-hash entries
        #    (S8-b / T4) from begin to finish on-disk (ZERO LLM-threaded state);
        #    finish(success) journals + appends them to the turn ledger. The
        #    projector's in-lock cheap half (project_from_turns, 4b above)
        #    populates this at projection time (it diffs each projected turn's
        #    hash against ledger.read_seen_hashes and emits the novel
        #    LedgerEntry list); the FE-B path has no projection, so it emits none.
        if not fe.exists:
            _write_sidecar(root, {
                "journal_dir": cp.journal_dir,
                "origin": origin,
                "doc_type": resolved_doc_type,
                "max_count": max_count,
                "max_bytes": max_bytes,
                "apply_fanout_k": k,
                "fe_hash": fe.hash,
                "pid": handle_pid(handle),
                "lock_token": handle.token,
                # Projection origins (fe_b_prime, fe_pi_log) carry the projector's
                # novel turn-content-hash entries; FE-B has no projection so it
                # emits none.
                "pending_ledger_entries": (
                    proj.novel_entries if origin in _PROJECTOR_BY_ORIGIN else []
                ),
            })
    except Exception:
        # Any failure after locking: replay the journal (removes the just-written
        # raw, if any) and release the lock so begin does not strand the wiki. The
        # sidecar (if written) is removed too.
        transaction.rollback(root, cp)
        transaction.release_lock(handle)
        _delete_sidecar(root)
        raise

    # 7b) C1 dedup no-op auto-close: on `fe.exists` begin wrote no raw, no
    #     sidecar, and appended no ledger entries — there is no transaction to
    #     hand back, so close it here with finish(fail)'s exact terminal effect:
    #     transaction.rollback (a no-op journal replay — dedup journaled nothing)
    #     THEN transaction.release_lock. This removes the LLM dependency where a
    #     missed `finish fail` stranded the lock (LockHeld / F2). A non-dedup
    #     begin still hands a held lock + sidecar to the caller's finish.
    if fe.exists:
        transaction.rollback(root, cp)
        transaction.release_lock(handle)

    # 8) print the JSON contract (plan §3).
    out = {
        "declaration": declaration,
        # E1 (D-1): the raw artifact's wiki-relative path REPLACES the inline
        # `redacted_body`. The raw was already journaled+written above (or already
        # exists on a dedup no-op — `fe.rel_path` is always resolvable either way),
        # so a downstream stage Reads `<WIKI_ROOT>/<raw_rel_path>` instead of the
        # begin stdout carrying the whole body (keeps stdout at a few hundred bytes,
        # so the harness never truncates it). Old `redacted_body` contract dropped.
        "raw_rel_path": fe.rel_path,
        # E1/E4: short declaration hash for the sid-to-sid "declaration unchanged"
        # equality check (see the declaration_hash computation above).
        "declaration_hash": declaration_hash,
        # E1 follow-up (#1 stage1-blob path): the ABSOLUTE path where the
        # orchestrator MUST Write the Stage1 proposed-edits blob, then pass verbatim
        # to plan-fanout + each Stage2 worker. Code authors it so the LLM never
        # reconstructs a temp path across turns — on Windows a reconstructed
        # `AppData\Local\Temp\...` was resolved against the CWD (not %LOCALAPPDATA%),
        # so the blob Read failed once per sid and drove an improvised recovery turn
        # (the very cost E1/E2 remove). For Path B `--out_dir` is project-batch's
        # out_dir (so the blob rides project-batch-cleanup); Path A omits it and
        # falls back to the system temp dir. `Path(source).stem` keys it per sid /
        # per source file exactly as the two SKILLs named it.
        "stage1_blob_path": str(
            Path(out_dir if out_dir else tempfile.gettempdir())
            / f"stage1-{Path(source).stem}.json"
        ),
        "origin": origin,
        "doc_type": resolved_doc_type,
        "max_count": max_count,
        "max_bytes": max_bytes,
        "apply_fanout_k": k,
        "dedup_noop": fe.exists,
        # C1: begin auto-closed the txn on a dedup no-op (rollback + release_lock
        # above), so the caller must NOT run finish — true iff dedup_noop.
        "auto_closed": fe.exists,
        "redaction_flags": [asdict(f) for f in fe.redaction_flags],
        # FE-B' per-run ledger-skipped TURN count (F6): how many projected turns
        # were dropped because a prior ingest already owns them (turn-content-hash
        # ledger diff). Surfaced so the Path B loop (wiki-ingest-sessions.md) can sum
        # it across sids and report it — an incremental re-run must not look like a
        # silent no-op (RS-d). Gated to the projection origins exactly like
        # pending_ledger_entries; FE-B has no projection so it is 0.
        "ledger_skipped": (
            proj.ledger_skipped if origin in _PROJECTOR_BY_ORIGIN else 0
        ),
    }
    return out


def handle_pid(handle: transaction.LockHandle) -> "int | None":
    """Read the pid the lock file recorded (acquire_lock writes {pid, token})."""
    try:
        data = json.loads(handle.path.read_text(encoding="utf-8"))
        return int(data["pid"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------- #
# verb: plan-fanout
# --------------------------------------------------------------------------- #
def _load_touched(stage1_proposal: str) -> list[str]:
    """Accept either a path to a JSON file or an inline JSON string.

    The proposal carries the Stage1 touched-page set. Accepted shapes:
      - {"touched": [rel_path, ...]}
      - [rel_path, ...]
    """
    text = stage1_proposal
    p = Path(stage1_proposal)
    if p.is_file():
        text = p.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DriverUsageError(f"stage1 proposal is neither a file nor JSON: {exc}") from exc
    if isinstance(data, dict):
        touched = data.get("touched", [])
    elif isinstance(data, list):
        touched = data
    else:
        raise DriverUsageError("stage1 proposal JSON must be a list or {touched: [...]}")
    if not all(isinstance(t, str) for t in touched):
        raise DriverUsageError("touched entries must be rel_path strings")
    return list(touched)


def plan_fanout(wiki_root: str, stage1_proposal: str) -> dict:
    """touched <= k -> one cluster; touched > k -> ceil(touched/k) clusters,

    each cluster <= k (plan §3 / F6 cluster split = code, D23). Total touched is
    first gated against max_count (F2 ingest-grain budget) -> human gate."""
    root = Path(wiki_root)
    state = _read_sidecar(root)
    if state is None:
        raise DriverError("no .llmwiki.txn sidecar; call begin first")
    k = int(state["apply_fanout_k"])
    if k <= 0:
        raise DriverUsageError(f"apply_fanout_k must be positive, got {k}")
    touched = _load_touched(stage1_proposal)
    # #2 follow-up (tier enforcement in code, F2 principle): the derived-tier
    # prefix is a DETERMINISTIC function of the origin — projection origins
    # (fe_b_prime / fe_pi_log, the `_PROJECTOR_BY_ORIGIN` membership that also
    # decides the WriteSession "derived" tier, D20) may only write under
    # `wiki/derived/`. `_load_touched` takes the Stage1-proposed rel_paths verbatim,
    # so a proposal that omits the `wiki/derived/` prefix produced `planned_clusters`
    # the Stage2 workers (correctly told the derived tier) could never match —
    # surfacing only as a late `apply-finish` cluster_pageset REJECT. Enforce the
    # tier HERE, fail-closed, before persisting planned_clusters or any write, so the
    # error is early + precise rather than a confusing downstream mismatch. Do NOT
    # silently auto-prefix: rewriting paths next to the D20 gate risks mis-filing.
    origin = state.get("origin")
    if origin in _PROJECTOR_BY_ORIGIN:
        mis = [t for t in touched
               if not t.replace("\\", "/").startswith("wiki/derived/")]
        if mis:
            raise DriverUsageError(
                f"tier mismatch: origin {origin!r} writes the derived tier, so "
                f"every Stage1 touched page must be under 'wiki/derived/'; "
                f"got: {mis}")
    n = len(touched)
    # F2: max_count is the per-apply-worker WriteSession budget (write_tool.py);
    # fanout would otherwise multiply it (ceil(n/k) workers x max_count each), so
    # the D19 "budget overflow -> human gate" never fires at the INGEST grain.
    # Gate the whole touched set here — the one place the total n is known before
    # any Stage2 write — so a runaway Stage1 proposal is escalated, not silently
    # fanned out. (k <= max_count holds by check_consistency, so a legal single
    # cluster never trips this.)
    max_count = int(state["max_count"])
    if n > max_count:
        raise DriverError(
            f"budget overflow: touched pages ({n}) > max_count ({max_count}); "
            f"escalate to the human gate")
    if n <= k:
        clusters = [touched] if touched else []
    else:
        num = math.ceil(n / k)
        clusters = [touched[i * k:(i + 1) * k] for i in range(num)]
    # C2 (Option C): persist the planned cluster set so `finish` can prove every
    # cluster was dispatched. The 0-based ordinal is the list INDEX of each
    # cluster in `planned_clusters`; `ingest-apply` appends that ordinal to
    # `applied_clusters` per run, and finish checks the ordinal set is covered.
    # Read-modify-write the sidecar begin already wrote (do NOT clobber its
    # transaction keys — journal_dir, lock_token, pending_ledger_entries, ...).
    state["planned_clusters"] = clusters
    _write_sidecar(root, state)
    return {"clusters": clusters}


# --------------------------------------------------------------------------- #
# verb: finish
# --------------------------------------------------------------------------- #
def _log_header_for_origin(origin: str) -> tuple[str, str]:
    header_fn = _LOG_HEADER_BY_ORIGIN.get(origin)
    if header_fn is None:
        raise DriverOpError(f"unknown origin in sidecar: {origin!r}")
    return header_fn()


def finish(wiki_root: str, outcome: str, *,
           expected_pages: "list[str] | None" = None,
           title: "str | None" = None) -> dict:
    if outcome not in ("success", "fail"):
        raise DriverUsageError(f"outcome must be success|fail, got {outcome!r}")
    root = Path(wiki_root)

    # reconstruct lock handle + checkpoint from the sidecar (zero LLM-threaded
    # state — single transaction, D23 central join).
    state = _read_sidecar(root)
    if state is None:
        raise DriverError("no .llmwiki.txn sidecar; nothing to finish")
    # DEC-R1=D ownership check: the on-disk lock must be the one begin acquired.
    # When BOTH tokens exist and disagree, this residue belongs to a DIFFERENT
    # ingest -> refuse WITHOUT touching its lock/journal/sidecar (do not
    # commit/rollback/release someone else's transaction).
    expected_token = state.get("lock_token")
    actual_token = transaction.read_lock_token(root)
    if (expected_token is not None and actual_token is not None
            and expected_token != actual_token):
        raise DriverError(
            "lock ownership mismatch: .llmwiki.lock is held by a different "
            "ingest; refusing to finish (recover the owning transaction via `abort`)")
    cp = _checkpoint_from_sidecar(state)
    handle = _lock_handle(root)

    try:
        if outcome == "success":
            # Any failure on the success path (missing join, regenerate, log
            # append, or commit) must roll back to the checkpoint before the
            # finally releases the lock + deletes the sidecar — otherwise a
            # partial index/log/raw + a stale lock/sidecar would strand with no
            # recovery handle. This makes finish honour its invariant: exactly one
            # of commit / rollback before release_lock (wiki-ingest.md §finish),
            # symmetric with begin's post-lock except.
            try:
                # join (D23 central). Two modes:
                #  - explicit `expected_pages` (a list, incl. empty []): the
                #    current on-disk page check — backward compatible with the
                #    caller-supplied LLM-collected written set.
                #  - `expected_pages` omitted (None): C2 (Option C) cluster-receipt
                #    check. Every planned cluster (plan-fanout persisted, 0-based
                #    ordinal = index in `planned_clusters`) MUST carry an
                #    ingest-apply receipt in `applied_clusters`. A missing ordinal
                #    is a whole cluster that was never dispatched -> rollback. A
                #    cluster whose apply committed an empty manifest still has a
                #    receipt (present, empty written), so a legitimately empty
                #    manifest is NOT a false positive. No plan-fanout (dedup / no
                #    clusters) -> planned empty -> no check (current-equivalent).
                if expected_pages is not None:
                    missing = [p for p in expected_pages
                               if not (root / Path(p)).exists()]
                    if missing:
                        raise DriverOpError(
                            f"expected pages missing on disk: {missing}")
                else:
                    planned = state.get("planned_clusters") or []
                    applied = set(state.get("applied_clusters") or [])
                    missing_clusters = [i for i in range(len(planned))
                                        if i not in applied]
                    if missing_clusters:
                        raise DriverOpError(
                            "cluster(s) never dispatched (no ingest-apply "
                            f"receipt): ordinals {missing_clusters} "
                            f"(planned {len(planned)}, applied {sorted(applied)})")
                # central index/log regenerate + append (inside the single tx).
                # Journal index.md/log.md AND the turn ledger before the central
                # writers mutate them (raw + pages were already journaled by
                # begin / ingest-apply). The ledger is a DRIVER-written state file
                # exactly like index.md/log.md — same journal-before-write path,
                # so rollback restores its pre-append backup (F3: on fail the
                # novel entries are NOT appended). The Stage2 allowlist write tool
                # is never involved.
                pending = [ledger.LedgerEntry(**e)
                           for e in state.get("pending_ledger_entries", [])]
                journal_targets = ["index.md", "log.md"]
                if pending:
                    journal_targets.append(ledger.LEDGER_NAME)
                transaction.journal_before_write(root, journal_targets)
                wiki_index.regenerate(root)
                op, tag = _log_header_for_origin(state["origin"])
                wiki_log.append(root / "log.md", op, tag,
                                title or f"ingest {state.get('fe_hash', '')[:12]}")
                # Append the novel turn-content-hash entries (S8-b) LAST, still
                # inside the same transaction, so a failure above reaches the
                # except -> rollback and the ledger append is never committed.
                ledger.append_entries(root, pending)
                transaction.commit(root, f"ingest: {title or state.get('fe_hash','')[:12]}")
                result = {"committed": True}
            except Exception:
                transaction.rollback(root, cp)
                raise
        else:  # fail
            transaction.rollback(root, cp)
            result = {"rolled_back": True}
    finally:
        # release_lock always; delete sidecar on every terminal path (R-a).
        transaction.release_lock(handle)
        _delete_sidecar(root)
    return result


# --------------------------------------------------------------------------- #
# verb: abort
# --------------------------------------------------------------------------- #
def abort(wiki_root: str) -> dict:
    """Manual recovery (D-g): rollback + release_lock + delete sidecar.

    Sidecar-INDEPENDENT (F1): `begin` writes the sidecar LAST (after the lock, the
    journal dir and the raw artifact), so a hard crash in that window leaves a held
    lock + a journal (+ orphan raw) but NO sidecar. Keying recovery on the sidecar
    alone would strand that lock forever and leave the orphan raw to poison D18
    dedup. So abort recovers from ANY of {sidecar, journal dir, lock file}: it
    replays the journal (fixed path under the root — removes the orphan raw) and
    releases the lock regardless of whether the sidecar was ever written. Truly
    nothing present => no-op-with-message. Idempotent (rollback is a no-op replay).
    """
    root = Path(wiki_root)
    state = _read_sidecar(root)
    journal_present = (root / transaction.JOURNAL_DIR).is_dir()
    lock_present = (root / transaction.LOCK_NAME).is_file()
    if state is None and not journal_present and not lock_present:
        return {"aborted": False, "message": "no .llmwiki.txn sidecar; nothing to abort"}
    # DEC-R1=D ownership check: refuse only when BOTH a sidecar token AND a live
    # lock token exist and disagree (this residue is a DIFFERENT ingest). When
    # the sidecar is absent (F1 crash shape) there is no token to compare, so
    # sidecar-independent recovery from {journal, lock} still proceeds.
    if state is not None:
        expected_token = state.get("lock_token")
        actual_token = transaction.read_lock_token(root)
        if (expected_token is not None and actual_token is not None
                and expected_token != actual_token):
            return {"aborted": False,
                    "message": "lock ownership mismatch; residue belongs to a "
                               "different ingest — not aborting"}
    # journal_dir is the fixed path under the root; the sidecar copy is only a hint.
    cp = (_checkpoint_from_sidecar(state) if state is not None
          else transaction.Checkpoint(journal_dir=str(root / transaction.JOURNAL_DIR)))
    handle = _lock_handle(root)
    try:
        transaction.rollback(root, cp)
    finally:
        transaction.release_lock(handle)
        _delete_sidecar(root)
    return {"aborted": True, "recovered_without_sidecar": state is None}


# --------------------------------------------------------------------------- #
# verb: enumerate  (read-only glob expansion; plan §2 A-1, G-a/G-b/G-e/G-d)
# --------------------------------------------------------------------------- #
# Force-excluded wiki-internal paths (G-b): self-ingestion guard. Any candidate
# whose POSIX relative path is, or lives under, one of these is dropped.
_EXCLUDED_DIRS = ("raw", "wiki", ".git", ".llmwiki.txn.d", ".llmwiki.toggle.d", ".qmd")
_EXCLUDED_FILES = (
    "SCHEMA.md", ".llmwiki", ".llmwiki.lock", ".llmwiki.txn",
    "log.md", "index.md", ".cc-turn-ledger.jsonl",
)

# Text-type default extension allowlist (G-e). A directory-only argument
# (e.g. `./docs/`) is sugar for `<dir>/**/*` restricted to these extensions;
# non-text (images etc.) is never picked. Fixed, documented allowlist.
_TEXT_EXTENSIONS = frozenset({
    ".md", ".markdown", ".txt", ".text", ".json", ".jsonl",
})


def _is_excluded_internal(rel_posix: str) -> bool:
    """True if a wiki-relative POSIX path is a force-excluded internal path (G-b)."""
    parts = rel_posix.split("/")
    # Drop anything under an excluded directory (raw/, wiki/, .git/) at any depth.
    if any(seg in _EXCLUDED_DIRS for seg in parts):
        return True
    # Drop excluded files matched anywhere in the tree (e.g. nested SCHEMA.md).
    if parts[-1] in _EXCLUDED_FILES:
        return True
    return False


def enumerate_files(wiki_root: str, glob: str) -> dict:
    """Expand a glob in Python (G-a), drop internal paths (G-b), files only.

    - `glob` is expanded with `pathlib.Path.glob` relative to `wiki_root`
      (NO shell expansion; OS-independent + deterministic via sorting).
    - A directory-only argument (trailing `/`, or an existing directory with no
      glob metacharacters) is sugar for `<dir>/**/*` restricted to the text-type
      extension allowlist (G-e).
    - An explicit glob with its own extension is honored as-is; the G-b internal
      exclusions still apply.
    - `**` recursion works (G-d). Zero matches is an explicit error (G-d).

    Read-only: no lock, no checkpoint, no writes (the per-file transaction stays
    in begin->finish; TA2 wires the loop).

    Output: {files: [<rel path>...], excluded: <count>, pattern: <effective glob>}.
    """
    root = Path(wiki_root)

    # Directory-only sugar (G-e): a trailing slash, or a metacharacter-free token
    # that resolves to an existing directory, becomes `<dir>/**/*` + allowlist.
    glob_meta = any(c in glob for c in "*?[")
    is_dir_only = glob.endswith(("/", "\\")) or (
        not glob_meta and (root / glob).is_dir()
    )
    if is_dir_only:
        dir_token = glob.rstrip("/\\")
        effective_pattern = f"{dir_token}/**/*" if dir_token else "**/*"
        restrict_to_text = True
    else:
        effective_pattern = glob
        restrict_to_text = False

    matches = sorted(root.glob(effective_pattern))

    files: list[str] = []
    excluded = 0
    for path in matches:
        if not path.is_file():          # files only (drop directory entries)
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            # A match outside the root (e.g. via `..`/absolute glob) is not a
            # wiki-relative path; treat as excluded rather than emit it.
            excluded += 1
            continue
        rel_posix = rel.as_posix()
        if _is_excluded_internal(rel_posix):
            excluded += 1
            continue
        if restrict_to_text and path.suffix.lower() not in _TEXT_EXTENSIONS:
            excluded += 1
            continue
        files.append(rel_posix)

    if not files:
        raise DriverError(
            f"glob matched zero files: {glob!r} "
            f"(effective pattern {effective_pattern!r}, {excluded} excluded)"
        )

    return {"files": files, "excluded": excluded, "pattern": effective_pattern}


# --------------------------------------------------------------------------- #
# verb: session-plan  (read-only Path B session-set resolver; T6 / F1-b / F2-B)
# --------------------------------------------------------------------------- #
# The taskflow state dir is a WORKSPACE concept (keyed off the process CWD),
# NOT under the wiki `<root>`: it lives at `<cwd>/_projects/_state/*.json`, the
# exact location wiki_root_resolver._latest_state_project reads (each file is
# `<sid>.json` with a `{"project": ...}` body). We locate it the same way.
_STATE_SUBPATH = ("_projects", "_state")

# The CC session-log root (U3 ground truth). `~` is expanded at runtime; no
# absolute path is baked in (repo secret rule). Mirrors the vendored views'
# `~/.claude/projects/**/*.jsonl` glob and revert_cc_log_extract's default.
_CC_PROJECTS_DIR = "~/.claude/projects"

# The env var that exposes the RUNNING session's id to a runtime script.
# D5 fix (workspace-session-ingest.md): the OS process env var Claude Code
# actually sets for a `uv run --script` bin is `CLAUDE_CODE_SESSION_ID` (probe,
# 2026-07-10: `CLAUDE_SESSION_ID` UNSET / `CLAUDE_CODE_SESSION_ID` SET len=36).
# `${CLAUDE_SESSION_ID}` in skills/create-skill/advanced-mode.md's "String
# Substitutions" table is a DIFFERENT thing — a harness PROMPT-template
# substitution (replaced in the .md text before the model sees it), NOT an OS
# process env var; the two were previously conflated (the old comment cited
# that table as authority for the env var name, which it is not).
_CURRENT_SID_ENV = "CLAUDE_CODE_SESSION_ID"


def _state_dir(cwd: "Path | None" = None) -> Path:
    """The taskflow state dir `<cwd>/_projects/_state` (process CWD, not wiki root).

    Mirrors wiki_root_resolver._latest_state_project's location rule so pj
    resolution agrees with the rest of the toolkit on where the workspace is.
    """
    base = cwd if cwd is not None else Path.cwd()
    return base.joinpath(*_STATE_SUBPATH)


def _sids_for_pj(project: str, cwd: "Path | None" = None) -> list[str]:
    """The sids whose state file's `project` field == <project>.

    Each `_projects/_state/<sid>.json` is `{"project": ..., ...}`; the filename
    STEM is the sid (verified: state file is written as `<session_id>.json` by
    taskflow session_init.py). A malformed / unreadable state file is skipped
    (never fatal — a foreign or half-written file must not sink the resolve).
    """
    state_dir = _state_dir(cwd)
    sids: list[str] = []
    try:
        candidates = sorted(p for p in state_dir.glob("*.json") if p.is_file())
    except OSError:
        candidates = []
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("project") == project:
            sids.append(path.stem)
    return sids


def _sids_workspace(cwd: "Path | None" = None) -> list[str]:
    """D3: the union of every sid under `_projects/_state/*.json`, NO project filter.

    A `project`-filter-free variant of `_sids_for_pj` (same state-dir glob, same
    malformed-file skip) used for the `workspace` scope (D2/D3/--workspace): a
    workspace whose sub-projects were launched from different directories is
    still fully covered, because this enumerates taskflow's harness-neutral
    `_state` records rather than reconstructing a CC project-dir slug per
    launch-dir (sidesteps the lossy `re.sub` slug reverse-mapping entirely).
    """
    state_dir = _state_dir(cwd)
    sids: list[str] = []
    try:
        candidates = sorted(p for p in state_dir.glob("*.json") if p.is_file())
    except OSError:
        candidates = []
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            sids.append(path.stem)
    return sids


def _active_project_for_sid(sid: "str | None", cwd: "Path | None" = None) -> "str | None":
    """D2: THIS session's taskflow-applied project (`_state/<sid>.json`.`project`).

    Reads ONLY the exact per-session state file taskflow's session_init.py
    writes — no mtime-latest fallback (that would silently resolve a DIFFERENT
    concurrent session's project, reintroducing the cross-talk D6/P1 closes).
    Returns None (never raises) when `sid` is falsy, the file is absent /
    unreadable / not a JSON object, or it has no non-empty `project` field — the
    caller (session_plan's no-args pj/prompt branch) treats None as "fail closed,
    ask for --pj".
    """
    if not sid:
        return None
    state_file = _state_dir(cwd) / f"{sid}.json"
    if not state_file.is_file():
        return None
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    project = data.get("project")
    if not isinstance(project, str) or not project.strip():
        return None
    return project.strip()


def _cc_project_dir_from_running_session(sid: "str | None" = None) -> "Path | None":
    """U3 PRIMARY: the CC project dir of the RUNNING session, as ground truth.

    Find `~/.claude/projects/*/<current-sid>.jsonl` (current-sid = the `sid`
    argument if given, else $CLAUDE_CODE_SESSION_ID (D5) — F-13 arg-over-env: an explicit
    `sid` takes priority over the env var, but the DEFAULT no-arg call is
    byte-identical to before this parameter existed) and return its PARENT dir
    — the CC-internal-encoding of the dir name is irrelevant because we locate
    the dir by the session file that lives in it, never by reconstructing the
    slug. Returns None if no sid is resolved (arg absent + env unset) or no
    such jsonl exists (the caller then tries the fallback).
    """
    effective_sid = sid or os.environ.get(_CURRENT_SID_ENV)
    if not effective_sid:
        return None
    root = Path(os.path.expanduser(_CC_PROJECTS_DIR))
    if not root.is_dir():
        return None
    matches = sorted(root.rglob(f"{effective_sid}.jsonl"))
    if not matches:
        return None
    return matches[0].parent


def _cc_project_dir_from_cwd(cwd: "Path | None" = None) -> "Path | None":
    """U3 SECONDARY fallback: reverse-generate the CC dir slug from the CWD.

    CC encodes a project dir as the absolute cwd with path separators AND the
    drive colon replaced by `-`. This is a best-effort fallback used ONLY when
    the running session's own log cannot be located (primary path). Returns the
    dir Path if it exists under `~/.claude/projects`, else None.
    """
    base = (cwd if cwd is not None else Path.cwd()).resolve()
    # Replace every path separator and the drive colon with `-` (e.g.
    # `C:\a\b` / `/home/a/b` -> `C--a-b` / `-home-a-b`).
    slug = re.sub(r"[\\/:]", "-", str(base))
    root = Path(os.path.expanduser(_CC_PROJECTS_DIR))
    candidate = root / slug
    return candidate if candidate.is_dir() else None


def _sids_in_project_dir(project_dir: Path) -> list[str]:
    """The session sids in a CC project dir = the stems of its session `*.jsonl`.

    A session log's filename IS `<sid>.jsonl` (100%, per the task doc). Agent
    child logs are named `agent-*.jsonl` (they carry the parent sid INSIDE the
    JSON, not in the filename) — excluded here so only real sessions are planned;
    the projector folds each session's agent children in by the shared sid.
    """
    sids: list[str] = []
    for path in sorted(project_dir.glob("*.jsonl")):
        stem = path.stem
        if stem.startswith("agent-") or stem == "journal":
            continue
        sids.append(stem)
    return sids


def _order_sids_by_started_ts(sids: list[str]) -> list[str]:
    """Order sids by each session's earliest record ts ASCENDING (F2-B).

    The sort key is `cc_session.started` (= min(ts) over the session's records)
    from the vendored DuckDB views — the authoritative in-log event clock, which
    (unlike file mtime) is stable across copy/resume/fork/checkout and is what
    the whole view stack + revert already order by. A sid with no `started` row
    (e.g. a log DuckDB could not parse) sorts LAST but is still returned (never
    dropped) so the plan stays complete; ties fall back to the sid string for a
    deterministic order.
    """
    if not sids:
        return []
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - declared in the PEP723 header
        raise DriverOpError(f"duckdb unavailable for session-plan ordering: {exc}") from exc
    try:
        con = duckdb.connect()
        # Reuse the projector's vendored views (single source of truth for the
        # cc-log DuckDB schema — cc_log_project._VIEWS_SQL points at cc_views.sql).
        con.execute(cc_log_project._VIEWS_SQL.read_text(encoding="utf-8"))
        placeholders = ",".join("?" for _ in sids)
        rows = con.execute(
            f"SELECT session_id, started FROM cc_session "
            f"WHERE session_id IN ({placeholders})",
            sids,
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 - surface as a driver error
        raise DriverOpError(f"session-plan ts ordering failed: {exc}") from exc
    started: dict[str, object] = {sid: ts for sid, ts in rows}
    # None-started sids sort last (True > False); then by ts, then by sid.
    return sorted(
        sids,
        key=lambda s: (started.get(s) is None, started.get(s), s),
    )


def _resolve_projection_kind(kind: str) -> str:
    """Resolve --kind for the projection-only verbs (session-plan/project-batch).

    These verbs have no FE-B raw-source origin (they operate purely on
    projector session ids), so unspecified/`auto` defaults to
    `ORIGIN_FE_B_PRIME` rather than `begin`'s `ORIGIN_FE_B` default (change
    point 7/9, design B/C "auto->fe_b_prime 既定"). Reuses `_resolve_kind` for
    the actual vocabulary/validation (1-vocabulary principle) then remaps the
    FE-B result.
    """
    resolved = _resolve_kind(kind)
    return ORIGIN_FE_B_PRIME if resolved == ORIGIN_FE_B else resolved


def _pi_walk_ts_by_sid() -> "dict[str, str]":
    """ONE walk under the pi session dir -> {sid: ts_prefix} (design C, F-2).

    Reuses `pi_log_project`'s internal batch-walk helper (the same single-walk
    contract `extract_turns_batch` uses, R1) so the pj-path locality filter and
    the fe_pi_log ordering below share this one walk's cost (the plan's "追加
    walk コストはゼロ"; mirrors this module's existing cross-module reuse of
    `cc_log_project._VIEWS_SQL`). Multiple files matching one sid use the most
    recent by mtime (same tie-break as `pi_log_project.extract_turns_batch`).
    """
    by_sid = pi_log_project._walk_all_session_files()
    ts_by_sid: "dict[str, str]" = {}
    for sid, matches in by_sid.items():
        chosen = max(matches, key=lambda p: p.stat().st_mtime)
        parsed = pi_log_project._parse_session_stem(chosen.stem)
        if parsed is not None:
            ts_by_sid[sid] = parsed[0]
    return ts_by_sid


def _order_sids(sids, kind: str) -> list[str]:
    """Dispatch session ordering by kind (design C, change point 9).

    `auto`/`fe_b_prime` (cc): `sids` is a bare sid list; delegates UNCHANGED to
    `_order_sids_by_started_ts` (pure delegation, no logic change — R-OI1-10,
    non-regression is confirmed by the existing cc-ordering pytest staying
    green).
    `fe_pi_log`: `sids` is a list of `(ts, sid)` pairs (design fact 11) —
    session filenames are `<ts>_<sid>.jsonl` and the ts prefix is the pi
    ground-truth clock (no DuckDB corpus to query, unlike cc). Orders by ts
    ASCENDING, then the sid string as the tie-break (mirrors the cc None-last/
    ts/sid tie-break shape; the F-2 locality filter already excludes ts-less
    sids upstream, so there is no None case to handle here).
    """
    if kind == ORIGIN_FE_PI_LOG:
        return [sid for _ts, sid in sorted(sids, key=lambda pair: (pair[0], pair[1]))]
    return _order_sids_by_started_ts(sids)


def session_plan(wiki_root: str, *, pj: "str | None" = None,
                 kind: str = "auto", sid: "str | None" = None,
                 workspace: bool = False,
                 scope: "str | None" = None) -> dict:
    """Resolve the Path B session-id SET, ordered by session-start ts ascending.

    Read-only (no lock / checkpoint / write / transaction). Ownership is NOT
    partitioned here (F1-b/F2-B): each begin decides ownership dynamically via
    the ledger diff. `wiki_root` is accepted for surface parity with the other
    verbs and to confirm it is a wiki root, but the state dir and session-log
    dir are resolved off the process CWD / $HOME (not under the wiki root).

    `kind` (design C, OI-1 change point 9): `auto`/`fe_b_prime` resolves cc-log
    sids — this verb's historical behavior, UNCHANGED (`_resolve_projection_kind`
    defaults unspecified/`auto` to `fe_b_prime`, this verb being projection-
    only). `fe_pi_log` resolves pi-log sids instead (pi_log_project-backed).

    `sid` (F-13, arg-over-env): an explicit override for the cwd-path's
    "current session" resolution. cc path: takes priority over
    $CLAUDE_CODE_SESSION_ID (passed down to `_cc_project_dir_from_running_session`,
    D5); OMITTED, behavior is byte-identical to before this parameter existed
    (env-only, same zero-arg call). pi path: arg-only (no env fallback, F-13
    "写像表") — `pi_log_project.session_dir_for_sid(sid)` primary, falling back
    to `session_dir_for_cwd` if `sid` is omitted or not found (F-6). `sid` also
    feeds `_active_project_for_sid` on the no-args scope-`pj`/`prompt` cc branch
    below (D2).

    `workspace` (D2/D3, explicit `--workspace`, cc-only for now — see the
    module's fe_pi_log Follow-up) and `scope` (D2, the caller's resolved
    `WIKI_SCOPE` — "prompt"|"pj"|"workspace"|"cwd"|None — threaded for the
    no-args case) together implement the no-args scope tree (D2) so a
    workspace-scoped wiki's Path B set follows the resolved wiki scope instead
    of narrowing to one cwd-slug dir. `workspace=True` is an explicit override
    (like `pj`) and wins over `scope`.

    Resolution (kind cc; kind pi UNCHANGED, see below — D3's workspace path is
    cc-only):
      - `workspace=True` (explicit `--workspace`, D2/D3) OR
        (workspace omitted, pj omitted, `scope == "workspace"`, A-follow) ->
                     `_sids_workspace()`: every sid in `_projects/_state/*.json`,
                     NO project filter (D3); scope "workspace".
      - `pj` given (explicit, unchanged) -> `_projects/_state/*.json` filtered
                     by `project == pj`; scope "pj".
      - workspace/pj both omitted, `scope in (None, "cwd")` (D4, UNCHANGED from
                     before kind/sid/scope existed) -> the running session's CC
                     project dir (U3 primary, `sid` override or env), else the
                     cwd-reverse-generated dir (U3 secondary); scope "cwd".
      - workspace/pj both omitted, `scope in ("pj", "prompt")` (D2; `prompt` is
                     PROVISIONALLY treated like `pj`, see the module's
                     Follow-ups) -> resolve THIS session's taskflow-applied
                     project via `_active_project_for_sid(sid)` (the
                     `_projects/_state/<sid>.json` `project` field, NOT a name
                     derived from the wiki path), then `_sids_for_pj(project)`;
                     scope "pj". Unresolvable (`sid` missing, no state file, no
                     `project` field) -> DriverError guiding the caller to
                     `--pj <name>` (fail-closed, NOT a silent fall-back to the
                     narrow cwd-slug set).
      - kind pi, pj given  -> the same `_sids_for_pj` enumeration (harness-
                     neutral) intersected with the sids that actually have a
                     pi session file (F-2 locality filter, via the single
                     `_pi_walk_ts_by_sid` walk); filtered-out sids are counted,
                     not silently dropped; scope "pj".
      - kind pi, pj omitted-> `session_dir_for_sid(sid)` primary (if `sid`
                     given) else/on not-found `session_dir_for_cwd(cwd)`
                     fallback (F-6); scope "cwd".
    Zero matches -> DriverError (fail-closed, like enumerate).

    Output: {sids: [sid,...], scope: "pj"|"cwd", pattern: <state-glob|dir>,
             filtered_out: <n>}. `filtered_out` counts sids present in
             taskflow state but with no discoverable pi session file (only
             possible on the `fe_pi_log` pj-path); it is 0 on every other path.
    """
    root = Path(wiki_root)
    mk = marker.detect(root)
    if mk is None:
        raise DriverError(f"no .llmwiki marker at {wiki_root} (not a wiki root)")

    origin = _resolve_projection_kind(kind)

    if origin == ORIGIN_FE_PI_LOG:
        filtered_out = 0
        if pj:
            candidate_sids = _sids_for_pj(pj)
            scope = "pj"
            pattern = str(_state_dir()) + os.sep + "*.json"
            ts_by_sid = _pi_walk_ts_by_sid()
            pairs = [(ts_by_sid[s], s) for s in candidate_sids if s in ts_by_sid]
            filtered_out = len(candidate_sids) - len(pairs)
            if not pairs:
                raise DriverError(
                    f"session-plan matched zero sessions for --pj {pj!r} "
                    f"(no _projects/_state/*.json with project=={pj!r} "
                    "having a discoverable pi session file)")
        else:
            primary = pi_log_project.session_dir_for_sid(sid) if sid else None
            if primary is not None:
                session_dir = primary
                source = "sid"
            else:
                session_dir = pi_log_project.session_dir_for_cwd(Path.cwd())
                source = "cwd-fallback"
            if not session_dir.is_dir():
                raise DriverError(
                    "session-plan could not resolve the pi session dir: "
                    + (f"--sid {sid!r} did not locate a session file, and "
                       if sid else "")
                    + f"the cwd session dir {session_dir} does not exist "
                    "(fail-closed)")
            pairs = pi_log_project.sids_in_session_dir(session_dir)
            scope = "cwd"
            pattern = str(session_dir)
            if not pairs:
                raise DriverError(
                    f"session-plan matched zero sessions in pi session dir "
                    f"{session_dir} (resolved via {source})")
        ordered = _order_sids(pairs, origin)
        return {"sids": ordered, "scope": scope, "pattern": pattern,
                "filtered_out": filtered_out}

    # kind cc: extended with the no-args scope tree + explicit --workspace
    # (D2/D3/D4). `scope_out` is the LOCAL result-scope var (kept distinct from
    # the `scope` PARAMETER — the caller's resolved WIKI_SCOPE — so the no-args
    # branch can read its input while building its own output).
    state_pattern = str(_state_dir()) + os.sep + "*.json"
    if workspace or (not pj and scope == "workspace"):
        # D3: explicit --workspace, or no-args follow of a workspace-scoped wiki.
        sids = _sids_workspace()
        scope_out = "workspace"
        pattern = state_pattern
        if not sids:
            raise DriverError(
                "session-plan matched zero sessions for the workspace scope "
                f"(no _projects/_state/*.json under {_state_dir()})")
    elif pj:
        sids = _sids_for_pj(pj)
        scope_out = "pj"
        pattern = state_pattern
        if not sids:
            raise DriverError(
                f"session-plan matched zero sessions for --pj {pj!r} "
                f"(no _projects/_state/*.json with project=={pj!r})")
    elif scope in ("pj", "prompt"):
        # D2 no-args pj/prompt: the taskflow-APPLIED project for THIS session
        # (never a name derived from the wiki path); unresolvable -> fail
        # closed with guidance, NOT a silent narrow-to-cwd-slug fall-back
        # (that silent narrowing was the symptom D2 fixes). `prompt` is
        # PROVISIONALLY folded into this branch (see module Follow-ups).
        project = _active_project_for_sid(sid)
        if project is None:
            raise DriverError(
                "session-plan could not resolve an active taskflow project "
                f"for scope {scope!r} (no _projects/_state/<sid>.json "
                "project for this session); specify --pj <name>")
        sids = _sids_for_pj(project)
        scope_out = "pj"
        pattern = state_pattern
        if not sids:
            raise DriverError(
                f"session-plan matched zero sessions for the active project "
                f"{project!r} (no _projects/_state/*.json with "
                f"project=={project!r})")
    else:
        # D4, UNCHANGED from before kind/sid/scope existed: no-args with
        # scope in (None, "cwd") — the running session's CC project dir (U3
        # primary), else the cwd-reverse-generated slug dir (U3 secondary).
        project_dir = (_cc_project_dir_from_running_session(sid) if sid
                       else _cc_project_dir_from_running_session())
        source = "running-session"
        if project_dir is None:
            project_dir = _cc_project_dir_from_cwd()
            source = "cwd-fallback"
        if project_dir is None:
            raise DriverError(
                "session-plan could not resolve the CC project dir: "
                f"${_CURRENT_SID_ENV} did not locate a session jsonl under "
                f"{_CC_PROJECTS_DIR}, and the cwd-reverse-generated slug dir "
                "does not exist (fail-closed)")
        sids = _sids_in_project_dir(project_dir)
        scope_out = "cwd"
        pattern = str(project_dir)
        if not sids:
            raise DriverError(
                f"session-plan matched zero sessions in CC project dir "
                f"{project_dir} (resolved via {source})")

    ordered = _order_sids(sids, origin)
    return {"sids": ordered, "scope": scope_out, "pattern": pattern,
            "filtered_out": 0}


# --------------------------------------------------------------------------- #
# verb: project-batch  (read-only Path B scan-collapse; R1 / F-H1)
# --------------------------------------------------------------------------- #
# Path B must not re-scan the whole ~/.claude/projects corpus once per begin
# (N sids -> N full scans). This read-only verb runs the EXPENSIVE projection
# (one corpus scan for ALL sids via cc_log_project.extract_turns_batch), writes
# each sid's extracted turns to a per-sid JSON file under a fresh temp dir, and
# returns the {sid: path} map. The Path B loop then passes each begin
# `--turns=<path>` so begin runs only the cheap per-sid half (dedup + ledger diff
# + markdown), keeping the ledger read-after-write (F3) sequential. Opens NO
# transaction (outside the R-f verb budget), exactly like enumerate/session-plan.
_BATCH_TURNS_PREFIX = "llmwiki-turns-"

# Backstop prune threshold (C3 step 2 / F4): a `llmwiki-turns-*` temp dir older
# than this is removed at the next project-batch. 24h is deliberately long so a
# still-running concurrent batch's live dir is never deleted mid-flight —
# project_batch is intentionally lock-free (R-f), so age is the only safe
# liveness signal available here.
_BATCH_STALE_PRUNE_SECONDS = 24 * 60 * 60


def _prune_stale_batch_dirs(now: "float | None" = None) -> int:
    """Remove stale `llmwiki-turns-*` temp dirs under `tempfile.gettempdir()`.

    C3 step 2 backstop: `project-batch-cleanup` is the primary deletion path (the
    temp turn JSON is pre-redaction, F3), but a crashed / interrupted Path B loop
    can still leave a batch dir behind. Any such dir whose mtime is older than
    `_BATCH_STALE_PRUNE_SECONDS` (24h) is `rmtree`'d here at the next
    project-batch. The long threshold avoids deleting a concurrently-running
    batch's live dir (project_batch is intentionally lock-free, F4). Best-effort:
    unreadable / racing entries are skipped, never fatal. Returns the count pruned.
    """
    cutoff = (time.time() if now is None else now) - _BATCH_STALE_PRUNE_SECONDS
    tmp_root = Path(tempfile.gettempdir())
    pruned = 0
    try:
        candidates = sorted(tmp_root.glob(f"{_BATCH_TURNS_PREFIX}*"))
    except OSError:
        return 0
    for d in candidates:
        try:
            if not d.is_dir() or d.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(d, ignore_errors=True)
        pruned += 1
    return pruned


def project_batch(wiki_root: str, sids: "list[str]", *,
                  kind: str = "auto") -> dict:
    """Extract turns for ALL sids in one scan; write per-sid JSON; return the map.

    Read-only (no lock / checkpoint / write to the wiki / transaction). The turn
    files are written OUTSIDE the wiki root (a temp dir), so they are never
    journaled and never enumerated; the Path B loop owns their cleanup.

    `kind` (change point 7, design B): resolved via `_resolve_projection_kind`
    (unspecified/`auto` defaults to `fe_b_prime` — this verb is projection-only,
    "auto->fe_b_prime 既定"). Dispatches to
    `_PROJECTOR_BY_ORIGIN[resolved].extract_turns_batch` — cc_log_project for
    `fe_b_prime`, pi_log_project for `fe_pi_log` (same call shape, table B).

    Output: {out_dir: <temp dir>, turns: {sid: <per-sid turn-json path>},
             scanned: <sid count>}. Each per-sid JSON file is
             `{"sid":..., "origin": <resolved kind>, "turns":[...]}` — the
             `"origin"` stamp (F-1) lets the paired `begin --turns` verify it
             was extracted under the SAME kind (fail-closed on mismatch).
    """
    root = Path(wiki_root)
    mk = marker.detect(root)
    if mk is None:
        raise DriverError(f"no .llmwiki marker at {wiki_root} (not a wiki root)")
    if not sids:
        raise DriverUsageError("project-batch requires at least one sid")

    # C3 step 2 backstop: prune stale (>24h) llmwiki-turns-* temp dirs left by a
    # crashed / interrupted prior loop before creating this run's dir (F4-safe).
    _prune_stale_batch_dirs()

    origin = _resolve_projection_kind(kind)
    projector = _PROJECTOR_BY_ORIGIN[origin]

    # One scan for all sids (the expensive half; R1). ledger is the hash single
    # source of truth — the extracted turns carry their F5 hash so begin's
    # project_from_turns agrees on the dedup/ledger key without re-hashing.
    extracted = projector.extract_turns_batch(sids, ledger=ledger)

    out_dir = Path(tempfile.mkdtemp(prefix=_BATCH_TURNS_PREFIX))
    turns_map: dict[str, str] = {}
    for sid in sids:
        turn_list = extracted.get(sid, [])
        out_path = out_dir / f"{sid}.json"
        # Record the owning sid + origin alongside the turns so begin's --turns
        # path can guard the sid<->file pairing AND the origin pairing
        # (fail-closed on mismatch, F-1).
        out_path.write_text(
            json.dumps({"sid": sid, "origin": origin, "turns": turn_list},
                      ensure_ascii=False),
            encoding="utf-8",
        )
        turns_map[sid] = str(out_path)
    return {"out_dir": str(out_dir), "turns": turns_map, "scanned": len(sids)}


# --------------------------------------------------------------------------- #
# verb: project-batch-cleanup  (C3 step 1: code owns the temp-dir deletion)
# --------------------------------------------------------------------------- #
def project_batch_cleanup(out_dir: str) -> dict:
    """Delete a `project-batch` temp dir — code owns the deletion (C3 step 1).

    Replaces the orchestrator prompt's bare `rm -rf "$OUT_DIR"`. Deletion is
    bounded by TWO guards checked BEFORE any removal (either failing is a REFUSED
    DriverError with NO deletion): the basename must start with
    `_BATCH_TURNS_PREFIX` (the prefix `project-batch`'s mkdtemp stamped) AND the
    parent dir must be `tempfile.gettempdir()` (where that mkdtemp places it). So
    the verb refuses any path the driver did not itself create as a batch dir —
    it never trusts the caller-supplied `<out_dir>` blindly the way `rm -rf` did.
    On a validated dir: `shutil.rmtree(ignore_errors=True)`. The parent match is
    over resolved paths (symlinked temp roots, e.g. macOS /var -> /private/var).
    """
    target = Path(out_dir)
    if not target.name.startswith(_BATCH_TURNS_PREFIX):
        raise DriverError(
            f"project-batch-cleanup REFUSED: {out_dir!r} basename does not start "
            f"with {_BATCH_TURNS_PREFIX!r} (not a project-batch temp dir)")
    tmp_root = Path(tempfile.gettempdir())
    if target.resolve().parent != tmp_root.resolve():
        raise DriverError(
            f"project-batch-cleanup REFUSED: {out_dir!r} is not directly under "
            f"the system temp dir {str(tmp_root)!r} (refusing to delete)")
    shutil.rmtree(target, ignore_errors=True)
    return {"cleaned": str(target)}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_opts(argv: list[str]) -> tuple[list[str], dict[str, str]]:
    positional: list[str] = []
    opts: dict[str, str] = {}
    for tok in argv:
        if tok.startswith("--"):
            key, _, val = tok[2:].partition("=")
            opts[key] = val
        else:
            positional.append(tok)
    return positional, opts


def main(argv: "list[str] | None" = None) -> int:
    # Fix stdio to UTF-8 regardless of the host locale (S1; same idiom as
    # cli.py:main). This subsumes the old stdout-only reconfigure that sat just
    # before the JSON print: stdin is STRICT (fail fast on corrupted input),
    # stdout/stderr replace (reporting never crashes). Wins over PYTHONIOENCODING.
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(f"usage: ingest_driver.py <{'|'.join(INGEST_VERBS)}> ...",
              file=sys.stderr)
        return EX_USAGE
    verb, rest = argv[0], argv[1:]
    # E3 compound verb: `apply-finish` owns its own arg parsing (repeated
    # `--manifest`, order = ordinal — the flat `_parse_opts` dict below would
    # collapse repeats) AND its own dual-output contract (stdout `{"rolled_back":
    # true}` + non-zero exit on a REJECTED, which the common tail's single JSON
    # print cannot express). It is imported LAZILY here (never at module top) so
    # `apply_finish`'s top-level `import ingest_driver` forms no load-time cycle.
    if verb == "apply-finish":
        from llmwiki.ingest.apply_finish import run_apply_finish_cli
        return run_apply_finish_cli(rest)
    pos, opts = _parse_opts(rest)
    try:
        if verb == "begin":
            # DEC-a: begin-only strict parse (unknown-flag reject + empty-value
            # detection + exact arity). Do NOT extend this to session-plan (its
            # space-form `--pj` leaves an empty-value opt on purpose, L1666-1668).
            _begin_usage = (
                "usage: begin <root> <source> "
                "[--kind=auto|fe_b|fe_b_prime|fe_pi_log] [--write_mode=...] "
                "[--apply_fanout_k=N] [--doc_type=...] [--external=...] "
                "[--turns=<path>] [--out_dir=<dir>]")
            unknown = sorted(k for k in opts if k not in _BEGIN_OPTS)
            if unknown:
                raise DriverUsageError(
                    "begin: unknown flag(s): "
                    + ", ".join("--" + k for k in unknown)
                    + " (use --key=value; origin is set via --kind=). "
                    + _begin_usage)
            empty = sorted(k for k in opts if opts[k] == "")
            if empty:
                raise DriverUsageError(
                    "begin: flag(s) need a value in --key=value form: "
                    + ", ".join("--" + k for k in empty)
                    + ". " + _begin_usage)
            if len(pos) != 2:
                raise DriverUsageError(
                    f"begin requires exactly the positional args <root> <source> "
                    f"(2), got {len(pos)}. " + _begin_usage)
            result = begin(
                pos[0], pos[1],
                kind=opts.get("kind", "auto"),
                write_mode=opts.get("write_mode"),
                apply_fanout_k=opts.get("apply_fanout_k"),
                doc_type=opts.get("doc_type"),
                external=opts.get("external"),
                turns=opts.get("turns"),
                out_dir=opts.get("out_dir"),
            )
        elif verb == "plan-fanout":
            if len(pos) < 2:
                raise DriverUsageError("plan-fanout requires <root> <stage1_proposal_path_or_json>")
            result = plan_fanout(pos[0], pos[1])
        elif verb == "finish":
            if len(pos) < 2:
                raise DriverUsageError("finish requires <root> <outcome:success|fail>")
            expected = opts.get("expected_pages")
            expected_pages = expected.split(",") if expected else None
            result = finish(pos[0], pos[1],
                            expected_pages=expected_pages,
                            title=opts.get("title"))
        elif verb == "abort":
            if len(pos) < 1:
                raise DriverUsageError("abort requires <root>")
            result = abort(pos[0])
        elif verb == "enumerate":
            if len(pos) < 2:
                raise DriverUsageError("enumerate requires <root> <glob>")
            result = enumerate_files(pos[0], pos[1])
        elif verb == "session-plan":
            if len(pos) < 1:
                raise DriverUsageError(
                    "session-plan requires <root> [--pj <name>] [--workspace] "
                    "[--scope <scope>]")
            # Accept both `--pj=name` and the spec's space form `--pj name`.
            # In the space form the parser leaves `--pj` as an empty-value opt and
            # the name lands as the next positional (pos[1]); use it as the pj name.
            pj_val = opts.get("pj")
            if "pj" in opts and not pj_val and len(pos) >= 2:
                pj_val = pos[1]
            # `--workspace` (D3) is a bare boolean flag (no value); `_parse_opts`
            # records it as opts["workspace"] = "" either way.
            result = session_plan(pos[0], pj=pj_val or None,
                                  kind=opts.get("kind", "auto"),
                                  sid=opts.get("sid"),
                                  workspace="workspace" in opts,
                                  scope=opts.get("scope"))
        elif verb == "project-batch":
            if len(pos) < 2:
                raise DriverUsageError("project-batch requires <root> <sid> [<sid>...]")
            result = project_batch(pos[0], pos[1:], kind=opts.get("kind", "auto"))
        elif verb == "project-batch-cleanup":
            if len(pos) < 1:
                raise DriverUsageError("project-batch-cleanup requires <out_dir>")
            result = project_batch_cleanup(pos[0])
        else:
            print(f"unknown verb: {verb!r} ({'|'.join(INGEST_VERBS)})",
                  file=sys.stderr)
            return EX_USAGE
    except config_resolver.ConfigInconsistency as e:
        # A resolved-config contract violation (D-c) — operational, not a
        # usage error (the CLI args were fine) and not a normal-data
        # sentinel (2026-07-16 D3).
        print(f"config-inconsistency: {e}", file=sys.stderr)
        return 3
    except _PROJECTION_ERRORS as e:
        print(f"extract: {e}", file=sys.stderr)
        return 3
    except transaction.StaleJournal as e:
        # Recoverable via `abort` — a state notice, not a failure (2026-07-16 D3).
        print(f"stale-journal: {e}", file=sys.stderr)
        return 2
    except DriverUsageError as e:
        print(str(e), file=sys.stderr)
        return EX_USAGE
    except DriverOpError as e:
        print(str(e), file=sys.stderr)
        return 3
    except (DriverError, transaction.LockHeld) as e:
        # Bare DriverError (SENTINEL) and LockHeld (busy) are both normal-data
        # state notices, not failures (2026-07-16 D3 — LockHeld unified with
        # the :729 lock-ownership REFUSED sentinel).
        print(str(e), file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
