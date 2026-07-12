# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""llmwiki CLI verb dispatch (P4; package-cli-architecture spec §CLI verb 契約).

A thin verb dispatcher over the migrated package functions. Each verb is a thin
wrapper around an already-verified public symbol — no behavior is added; the
decision logic lives in the wrapped module, and this file only marshals argv ->
call -> stdout in a byte-equivalent way to the heredocs / argv-CLIs it replaces.

D-2 (read-only profile) is enforced structurally by **branch-local lazy
imports**: each verb imports only what it needs INSIDE its branch, so the
read-path verbs (`resolve-root`, `scan-pages`, `marker-detect`) never pull
`llmwiki.write` / `llmwiki.ingest` into their import closure. The static
read-profile-closure test (D-2, spec §test) relies on this — do NOT hoist these
imports to module top.

T-layer (spec §T層): the `sys.path.insert(0, <plugin_root>)` bootstrap is NOT
here. It is performed ONCE at the bin/ entrypoints (the per-harness shim) so
that `import llmwiki` resolves under path-import (no-install). `cli.py` assumes
the package is already importable and only dispatches.

Verbs (bin/llmwiki, dep-free):
    resolve-root [--root R] [--sid S] -> wiki_root_resolver.resolve
    scan-pages   <root>       -> wiki_index.scan_pages / tier_of
    search       <root> --q Q -> read.query (index) | read.qmd_search (qmd), internal dispatch
    marker-detect <dir>       -> marker.detect
    file         <root>       -> frontends.fe_a -> write_tool/transaction/...
    declare      <root>       -> marker.detect / config_resolver.declare (read-only)
    promote-check <root> <rel> -> promote.{derived_to_source_path,detect_contamination} (read-only, no move)
    promote      <root> <rel> -> promote.promote (move; AFTER human approval)
    lint         <root>       -> link_lint.{build_graph,lint} + wiki_index.check_integrity
    init         <root>       -> wiki_init.main (argv-CLI, subordinated)
    ingest-apply <root> <origin>  -> write_tool.WriteSession (STDIN JSON manifest)
    apply-finish <root> <origin> --manifest <path>... [--title=<t>]
                              -> llmwiki.ingest.apply_finish (E3: apply every
                                 cluster manifest in ordinal order then central
                                 finish; rollback on any REJECTED / F2 mismatch)
    floor-check               -> transcript_floor.check_decision_claim (STDIN JSON)
    reindex      <root>       -> qmd_search.ensure_collection/update (.qmd/ only; S5)

The `search` verb (task-B) dispatches index|qmd INTERNALLY (DEC-3 B-lite): the
skill calls one `search --q` entrypoint and this verb chooses `read.query`
(index-direct enumeration, byte-identical to `scan-pages`) or `read.qmd_search`
(the optional external qmd backend, D-Q8 predicate) and prints `<tier> <rel_path>`
per line either way. It stays a read-path verb (branch-local imports of
`read`/`core` only; never write/ingest — D-2).
"""

from __future__ import annotations

import json
import sys

# Exit-code contract (theme1 i:39). rc 0 = success. rc 2 = verb-specific SENTINEL
# only — a state notice (NO-WIKI / NO-MARKER / NOT-A-WIKI / REFUSED) that callers
# consume as data, NOT a failure. Usage / protocol errors (bad args, unknown verb)
# return EX_USAGE so a contract drift (e.g. an upstream re-copy that changes a
# verb's argv) surfaces as a hard failure instead of masquerading as a sentinel.
# 64 = sysexits.h EX_USAGE.
EX_USAGE = 64


def _resolve_root(argv: list[str]) -> int:
    # read-path verb: import closure must stay clear of write/ingest (D-2).
    from llmwiki.core import wiki_root_resolver

    # `resolve-root [--root R] [--sid S]`: `--root` is the explicit root override
    # (documented surface); `--sid` is the session id threaded to the resolver so
    # the pj scope reads `_projects/_state/<sid>.json` first (theme1 i:63 — without
    # it concurrent sessions on different pj cross-talk via mtime-latest). Both may
    # also use `--flag=value`. A bare positional is still accepted as the root
    # (back-compat). An empty value falls through to existence-based resolution.
    arg = None
    sid = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--root":
            val = argv[i + 1] if i + 1 < len(argv) else None
            arg = val if val else None
            i += 2
            continue
        if a.startswith("--root="):
            val = a[len("--root="):]
            arg = val if val else None
            i += 1
            continue
        if a == "--sid":
            val = argv[i + 1] if i + 1 < len(argv) else None
            sid = val if val else None
            i += 2
            continue
        if a.startswith("--sid="):
            val = a[len("--sid="):]
            sid = val if val else None
            i += 1
            continue
        if arg is None and a:
            arg = a
        i += 1
    res = wiki_root_resolver.resolve(arg, session_id=sid)
    if res is None:
        print("NO-WIKI", file=sys.stderr)
        return 2
    print(f"{res.root}\t{res.scope}")
    return 0


def _scan_pages(argv: list[str]) -> int:
    # read-path verb (D-2).
    from llmwiki.core import marker, wiki_index

    if not argv:
        print("usage: scan-pages <root>", file=sys.stderr)
        return EX_USAGE
    root = argv[0]
    # Receiver-side validation (theme1 i:45): a missing/broken marker must fail
    # CLOSED (NOT-A-WIKI sentinel), not enumerate an empty page set as if the wiki
    # were merely empty. A tab+scope-contaminated root (the WIKI_ROOT tab-混入 bug)
    # lands here and stops loudly instead of grounding the model on "empty wiki".
    if marker.detect(root) is None:
        print("NOT-A-WIKI", file=sys.stderr)
        return 2
    for pe in wiki_index.scan_pages(root):     # covers wiki/ AND wiki/derived/
        print(pe.tier, pe.rel_path)            # tier = wiki_index.tier_of(path)
    return 0


def _search(argv: list[str]) -> int:
    # read-path verb (D-2): closure stays clear of write/ingest. Dispatches
    # index|qmd INTERNALLY (DEC-3 B-lite) so the skill calls ONE `search`
    # entrypoint. Output = `<tier> <rel_path>` per line (same shape as scan-pages),
    # so the index backend is byte-identical grounding to today (Invariant #4).
    from llmwiki.core import config_resolver as cr
    from llmwiki.core import marker
    from llmwiki.read import query as read_query

    # parse: search <root> --q <phrased> [--k N]
    root: "str | None" = None
    q: "str | None" = None
    k = 10
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--q":
            q = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
            continue
        if a.startswith("--q="):
            q = a[len("--q="):]
            i += 1
            continue
        if a == "--k":
            try:
                k = int(argv[i + 1])
            except (IndexError, ValueError):
                pass
            i += 2
            continue
        if a.startswith("--k="):
            try:
                k = int(a[len("--k="):])
            except ValueError:
                pass
            i += 1
            continue
        if root is None:
            root = a
        i += 1

    if root is None or not q:
        print("usage: search <root> --q <phrased> [--k N]", file=sys.stderr)
        return EX_USAGE

    # Receiver-side validation (theme1 i:45): fail CLOSED on a missing/broken
    # marker instead of silently continuing with the default config and returning
    # an empty page set (which the model reads as "the wiki is empty"). Symmetric
    # with the write verbs' NOT-A-WIKI fail-closed.
    m = marker.detect(root)
    if m is None:
        print("NOT-A-WIKI", file=sys.stderr)
        return 2
    # Resolve the backend axes from the wiki-local config.
    wiki_cfg = cr.load_config(m.schema_path)
    res = cr.resolve_all({}, wiki_cfg)
    backend = res["search_backend"].value
    qmd_bin = res["qmd_bin"].value

    pages = None
    if backend == "qmd":
        from llmwiki.read import qmd_search as qs

        if not qs.is_available(qmd_bin):
            # D-Q8: qmd selected but binary unresolvable -> loud-announce, never
            # silent, then degrade to index-direct.
            print(f"[search] search_backend=qmd but '{qmd_bin}' not on PATH; "
                  f"using index-direct fallback", file=sys.stderr)
        elif qs.should_use(root, res):
            try:
                if not qs.is_initialized(root):
                    # D-Q6 first lazy activation: /wiki-reindex was never run, so
                    # build the project-local index + embeddings inline (one-time,
                    # ~GB models). Announce before the blocking embed shell-out.
                    print("[search] first-run: building qmd index + embeddings "
                          "(one-time, downloads ~GB models)…", file=sys.stderr)
                    qs.ensure_collection(root, qmd_bin)   # init + add wiki/ + embed
                qs.update(root, qmd_bin)          # D-Q6 lazy incremental refresh
                pages = qs.query(root, qmd_bin, q, k)
            except qs.QmdError as e:
                print(f"[search] qmd backend error: {e}; using index-direct "
                      f"fallback (try /wiki-reindex)", file=sys.stderr)
                pages = None
            if pages == []:
                print("[search] qmd returned no pages; using index-direct "
                      "fallback (index not built? run /wiki-reindex)",
                      file=sys.stderr)
                pages = None
        # else: below page threshold -> silent index-direct (not an error, D-Q8)

    if pages is None:
        pages = read_query.enumerate_pages(root)

    for tier, rel in pages:
        print(tier, rel)                          # same shape as scan-pages
    return 0


def _marker_detect(argv: list[str]) -> int:
    # read-path verb (D-2).
    from llmwiki.core import marker

    if not argv:
        print("usage: marker-detect <dir>", file=sys.stderr)
        return EX_USAGE
    m = marker.detect(argv[0])
    if m is None:
        print("NO-MARKER", file=sys.stderr)
        return 2
    print(f"{m.root}\t{m.version}\t{m.schema_path}")
    return 0


def _file(argv: list[str]) -> int:
    # write-path verb: filing (FE-A -> write_tool/transaction). Imports the
    # write/ingest closure INSIDE the branch so the read verbs above stay clear.
    from pathlib import Path

    from llmwiki.core import config_resolver as cr
    from llmwiki.core import marker, wiki_index, wiki_log
    from llmwiki.ingest import frontends
    from llmwiki.write import transaction
    from llmwiki.write.write_tool import WriteSession, WriteRejected

    if len(argv) < 3:
        print("usage: file <root> <page> <title> (page content on STDIN)",
              file=sys.stderr)
        return EX_USAGE
    root, page, title = argv[0], argv[1], argv[2]
    content = sys.stdin.read()

    m = marker.detect(root)
    if m is None:
        print("NOT-A-WIKI", file=sys.stderr)
        return 2
    res = cr.resolve_all({}, cr.load_config(m.schema_path))
    print(cr.declare(res["write_mode"]))   # REQUIRED before any write (D5)

    # FE-A runs redaction (D16) then content-hash dedup (D18) over the answer
    # text. `fe.body` is the REDACTED content; `fe.rel_path` is the
    # raw/derived/<hash>.md provenance snapshot (D1). Surface redaction flags to
    # the human gate. On a dedup hit the raw already exists -> genuine no-op.
    fe = frontends.fe_a(root, content)     # provenance:derived
    if fe.redaction_flags:
        kinds = ", ".join(sorted({f.kind for f in fe.redaction_flags}))
        print(f"redaction-flags: {len(fe.redaction_flags)} ({kinds})")
    if fe.exists:
        print("dedup no-op")
        return 0
    try:
        with transaction.transaction(root, f"file|derived | {title}"):
            # 1) raw provenance snapshot (D1/D18): journal the create FIRST so a
            #    failed filing rolls it back — engine-written (like the driver's
            #    begin), NOT through the page allowlist (raw/ is protected there).
            transaction.journal_before_write(root, [fe.rel_path])
            raw_path = Path(root) / Path(fe.rel_path)
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(fe.body, encoding="utf-8")
            # 2) the PAGE is the REDACTED body (D16), through the allowlist gate
            #    (origin=derived -> wiki/derived/ only, D20).
            sess = WriteSession(root, origin="derived")
            sess.add(page, fe.body)
            written = sess.commit()
            transaction.journal_before_write(root, ["index.md", "log.md"])
            wiki_index.regenerate(root)
            op, tag = wiki_log.header_for_fe_a()    # ("file","derived")
            wiki_log.append(root + "/log.md", op, tag, title)
        print("written:", written)
    except WriteRejected as e:
        print("REJECTED", e.gate, e.reason)
        raise
    return 0


def _declare(argv: list[str]) -> int:
    # read-only verb (no move): wiki-promote Step1 (D5 declare) mapping. Imports
    # core only — the write/ closure is NOT pulled in (the resolved-value
    # declaration is a pure config read). Byte-equivalent to wiki-promote.md
    # Step1 heredoc: marker.detect -> resolve_all({}, load_config) -> declare.
    from llmwiki.core import config_resolver as cr
    from llmwiki.core import marker

    if not argv:
        print("usage: declare <root>", file=sys.stderr)
        return EX_USAGE
    root = argv[0]
    m = marker.detect(root)
    if m is None:
        print("NOT-A-WIKI")        # stdout + rc2 (mirrors Step1 heredoc)
        return 2
    res = cr.resolve_all({}, cr.load_config(m.schema_path))
    print(cr.declare(res["write_mode"]))   # [wiki] write_mode = <value> (<source>)
    return 0


def _promote_check(argv: list[str]) -> int:
    # read-only verb (no move): wiki-promote Step2 pre-approval preview. Imports
    # ONLY promote.{derived_to_source_path,detect_contamination} — promote.promote
    # (the move) is never imported or called here, so the human-approval-precedes
    # -move envelope (D15/D20/D21) is preserved. Byte-equivalent to wiki-promote.md
    # Step2 heredoc: dest: <path> + contamination: <reasons-list-repr>.
    from pathlib import Path

    from llmwiki.write import promote

    if len(argv) < 2:
        print("usage: promote-check <root> <wiki/derived/X.md>", file=sys.stderr)
        return EX_USAGE
    root, rel = argv[0], argv[1]
    text = (Path(root) / rel).read_text(encoding="utf-8")
    print("dest:", promote.derived_to_source_path(rel))
    print("contamination:", promote.detect_contamination(text))
    return 0


def _promote(argv: list[str]) -> int:
    # write-path verb.
    from llmwiki.core import wiki_index, wiki_log
    from llmwiki.write import promote, transaction
    from llmwiki.write.promote import PromoteRejected

    if len(argv) < 2:
        print("usage: promote <root> <wiki/derived/X.md> [title]",
              file=sys.stderr)
        return EX_USAGE
    root, rel = argv[0], argv[1]
    title = argv[2] if len(argv) > 2 else rel
    try:
        with transaction.transaction(root, f"promote | {rel}"):
            result = promote.promote(root, rel)     # move + flip + rewrite
            transaction.journal_before_write(root, ["index.md", "log.md"])
            wiki_index.regenerate(root)             # tier flips after move
            wiki_log.append(root + "/log.md", "promote", "source", title)
        print("promoted ->", result.dest_rel)
        print("rewritten:", result.rewritten)
    except PromoteRejected as e:
        print("PROMOTE-REJECTED:", e.reason)
        raise
    return 0


def _lint(argv: list[str]) -> int:
    # read-centric but lint lives outside read/; it imports lint + core only
    # (no write/ingest). link_lint findings AND index integrity are both reported.
    from llmwiki.core import wiki_index
    from llmwiki.lint import link_lint

    if not argv:
        print("usage: lint <root>", file=sys.stderr)
        return EX_USAGE
    root = argv[0]
    lr = link_lint.lint(root)               # LintReport{missing, orphans}
    print("missing-crossrefs:", lr.missing)
    print("orphans:", lr.orphans)
    ir = wiki_index.check_integrity(root)   # IntegrityReport{ok, missing, stale}
    print("integrity-ok:", ir.ok)
    print("index-missing:", ir.missing, "index-stale:", ir.stale)
    print("tier-mismatch:", getattr(ir, "tier_mismatch", []))
    return 0


def _init(argv: list[str]) -> int:
    # subordinate the existing wiki_init argv-CLI verbatim (root [--scope]).
    from llmwiki.init import wiki_init

    # wiki_init.main uses argparse, which raises SystemExit(2) on a bad argument.
    # That collides with our rc2 SENTINEL contract (wiki_init's own
    # WikiInitError -> return 2 is a genuine "wiki already exists" sentinel). Remap
    # the argparse usage exit to EX_USAGE so a real usage error is not read as a
    # sentinel; leave WikiInitError's own return 2 untouched. argparse also exits 0
    # for --help; pass that through.
    try:
        return wiki_init.main(argv)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else EX_USAGE)
        return EX_USAGE if code == 2 else code


def _ingest_apply(argv: list[str]) -> int:
    # write-path verb. STDIN JSON manifest [{rel_path, content}]; budget from
    # the .llmwiki.txn sidecar. transaction/regenerate/log are NOT touched here —
    # the orchestrator performs the D23 central commit (spec §verb 契約).
    from pathlib import Path

    from llmwiki.write import transaction
    from llmwiki.write.write_tool import WriteSession, WriteRejected

    if len(argv) < 2:
        print("usage: ingest-apply <root> <origin> [<cluster_ordinal>] "
              "(manifest JSON on STDIN)", file=sys.stderr)
        return EX_USAGE
    root, fe_origin = argv[0], argv[1]
    # C2 (Option C): the OPTIONAL 3rd arg is the 0-based cluster ordinal that
    # plan-fanout assigned to this cluster. When present, this run appends a
    # dispatch receipt to the sidecar (below) so `finish` can prove the cluster
    # ran; when absent, behavior is unchanged (single-file / non-clustered
    # callers and the existing origin/utf8 tests pass no ordinal).
    cluster_ordinal: "int | None" = None
    if len(argv) >= 3 and argv[2] != "":
        try:
            cluster_ordinal = int(argv[2])
        except ValueError:
            print(f"ingest-apply: cluster ordinal must be an integer, got "
                  f"{argv[2]!r}", file=sys.stderr)
            return EX_USAGE
    # F3: refuse to write unjournaled. ingest-apply writes page files under the
    # orchestrator's lock but does NOT open a transaction; the journal dir must
    # already exist (the driver `begin` created it). Without it, page writes would
    # be unrecoverable on a failed finish -> hard-refuse instead of writing.
    if not (Path(root) / transaction.JOURNAL_DIR).is_dir():
        print("REFUSED no-journal: ingest-apply requires an open transaction "
              "(run `begin` first)", file=sys.stderr)
        return 2
    # origin mapping (trust by location): projection origins carry UNTRUSTED
    # transcript content and may target ONLY wiki/derived/ — fe_b_prime (cc)
    # AND fe_pi_log (pi) both map to "derived". Only fe_b (3rd-party source
    # file, explicitly ingested) maps to the "source" tier. Previously
    # fe_pi_log fell through to "source", letting an untrusted pi transcript
    # write outside wiki/derived/ (D20 violation).
    ws_origin = "derived" if fe_origin in ("fe_b_prime", "fe_pi_log") else "source"
    manifest = json.loads(sys.stdin.read())
    # budget comes from the sidecar (driver-owned state), never threaded:
    txn = json.loads((Path(root) / ".llmwiki.txn").read_text(encoding="utf-8"))
    sess = WriteSession(root, max_count=int(txn["max_count"]),
                        max_bytes=int(txn["max_bytes"]), origin=ws_origin)
    try:
        for entry in manifest:
            sess.add(entry["rel_path"], entry["content"])
        written = sess.commit()    # writes page FILES to disk; lock held by orch.
        print("written:", written)
        # C2 (Option C): append this cluster's dispatch receipt to the sidecar
        # (driver-owned state, never LLM-threaded — same file the budget read
        # above came from). `finish` (expected_pages omitted) checks every
        # plan-fanout ordinal has a receipt. An empty manifest still records its
        # ordinal (written may be []), so a legitimate empty apply is not read as
        # a dropped cluster. Read-modify-write preserves the begin/plan-fanout
        # keys already in the sidecar.
        if cluster_ordinal is not None:
            from llmwiki.ingest.ingest_driver import _write_sidecar
            txn.setdefault("applied_clusters", []).append(cluster_ordinal)
            txn.setdefault("applied_written", []).extend(written)
            _write_sidecar(root, txn)
    except WriteRejected as e:
        print("REJECTED", e.gate, e.reason)
        raise
    return 0


def _apply_finish(argv: list[str]) -> int:
    # write-path compound verb (spec E3): apply every manifest in ordinal order
    # via write_tool.WriteSession, then run the central finish (join / index /
    # log / ledger / commit) — or roll the whole transaction back on any REJECTED
    # / F2 mismatch. The whole implementation + its stdout/stderr contract lives
    # in the shared `llmwiki.ingest.apply_finish` module (reused verbatim by the
    # driver's `apply-finish` verb); this branch just forwards argv. Imported
    # INSIDE the branch so the read verbs' import closure stays clear (D-2).
    from llmwiki.ingest.apply_finish import run_apply_finish_cli

    return run_apply_finish_cli(argv)


def _floor_check(argv: list[str]) -> int:
    # ingest-layer (transcript_floor) but dep-free; imported INSIDE the branch.
    from llmwiki.ingest import transcript_floor as tf

    candidates = json.loads(sys.stdin.read())   # [{span, speaker}]
    for entry in candidates:
        span = entry["span"]
        speaker = entry.get("speaker")
        r = tf.check_decision_claim(span, speaker=speaker)
        if not r.admissible:
            print("FLOOR-VIOLATION", r.gate, "::", span)
    return 0


def _toggle(argv: list[str]) -> int:
    # toggle verb: set or get the per-session wiki on/off state (F3/DEC-F).
    # Usage:
    #   toggle set <root> <session_id> on|off  -> set state, print "on" or "off"
    #   toggle get <root> <session_id>         -> print "on" or "off"
    # Branch-local import (D-2): wiki_toggle is under core (no write/ingest closure).
    from llmwiki.core import wiki_toggle as wt

    if len(argv) < 1:
        print("usage: toggle set <root> <session_id> on|off", file=sys.stderr)
        print("       toggle get <root> <session_id>", file=sys.stderr)
        return EX_USAGE

    sub = argv[0]
    if sub == "set":
        if len(argv) < 4:
            print("usage: toggle set <root> <session_id> on|off", file=sys.stderr)
            return EX_USAGE
        root, session_id, state = argv[1], argv[2], argv[3]
        if state not in ("on", "off"):
            print(f"toggle set: state must be 'on' or 'off', got {state!r}",
                  file=sys.stderr)
            return EX_USAGE
        wt.set_state(root, session_id, on=(state == "on"))
        print(state)
        return 0
    elif sub == "get":
        if len(argv) < 3:
            print("usage: toggle get <root> <session_id>", file=sys.stderr)
            return EX_USAGE
        root, session_id = argv[1], argv[2]
        print("on" if wt.is_on(root, session_id) else "off")
        return 0
    else:
        print(f"toggle: unknown sub-command {sub!r}; expected 'set' or 'get'",
              file=sys.stderr)
        return EX_USAGE


def _reindex(argv: list[str]) -> int:
    # optional-search maintenance verb (S5 /wiki-reindex): builds/refreshes the
    # qmd index under <root>/.qmd/ ONLY — it never touches wiki pages, so it is
    # OUTSIDE the two code gates (R10). Imports core + read only (no write/ingest).
    # No-op-safe: backend=index or qmd absent -> announce + exit 0 (no crash).
    from llmwiki.core import config_resolver as cr
    from llmwiki.core import marker
    from llmwiki.read import qmd_search as qs

    if not argv:
        print("usage: reindex <root>", file=sys.stderr)
        return EX_USAGE
    root = argv[0]
    m = marker.detect(root)
    if m is None:
        print("NOT-A-WIKI", file=sys.stderr)
        return 2
    res = cr.resolve_all({}, cr.load_config(m.schema_path))
    backend = res["search_backend"].value
    qmd_bin = res["qmd_bin"].value
    if backend != "qmd":
        print(f"[reindex] search_backend={backend} (not qmd); nothing to reindex")
        return 0
    if not qs.is_available(qmd_bin):
        print(f"[reindex] search_backend=qmd but '{qmd_bin}' not on PATH; skipped")
        return 0
    try:
        qs.ensure_collection(root, qmd_bin)   # qmd init + collection add wiki/ + embed
        qs.update(root, qmd_bin)              # incremental index refresh
    except qs.QmdError as e:
        print(f"[reindex] qmd error: {e}", file=sys.stderr)
        return 1
    print(f"[reindex] qmd index refreshed under {root}/.qmd/ (collection = wiki/)")
    return 0


# verb -> handler. Each handler does its own branch-local imports (D-2).
_VERBS = {
    "resolve-root": _resolve_root,
    "scan-pages": _scan_pages,
    "search": _search,
    "marker-detect": _marker_detect,
    "file": _file,
    "declare": _declare,
    "promote-check": _promote_check,
    "promote": _promote,
    "lint": _lint,
    "init": _init,
    "ingest-apply": _ingest_apply,
    "apply-finish": _apply_finish,
    "floor-check": _floor_check,
    "reindex": _reindex,
    "toggle": _toggle,
}

_USAGE = (
    "usage: llmwiki <resolve-root|scan-pages|search|marker-detect|file|declare|"
    "promote-check|promote|lint|init|ingest-apply|apply-finish|floor-check|"
    "reindex|toggle> ..."
)


def main(argv: "list[str] | None" = None) -> int:
    # Fix stdio to UTF-8 regardless of the host locale (S1). On Windows, piped
    # stdio defaults to the ANSI codepage (e.g. cp932 on Japanese systems),
    # which rejects or corrupts UTF-8 page content flowing through the write
    # verbs (`file`, `ingest-apply` read STDIN; every verb prints paths).
    # stdin stays STRICT so corrupted input fails fast instead of silently
    # mangling a page; stdout/stderr use replace so reporting never crashes.
    # reconfigure takes precedence over PYTHONIOENCODING (contract-tested).
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(_USAGE, file=sys.stderr)
        return EX_USAGE
    verb, rest = argv[0], argv[1:]
    handler = _VERBS.get(verb)
    if handler is None:
        print(f"unknown verb: {verb!r}\n{_USAGE}", file=sys.stderr)
        return EX_USAGE
    return handler(rest)


if __name__ == "__main__":
    raise SystemExit(main())
