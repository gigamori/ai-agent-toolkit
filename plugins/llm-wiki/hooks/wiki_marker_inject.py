#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""UserPromptSubmit hook: wiki-active marker injection (D8, design §4 起動 / §6 F2).

Resolves the active wiki via `wiki_root_resolver.resolve(cwd=<hook cwd>)` so
pj / workspace / cwd / child scopes are ALL honored (plan §2-C, W-f) — not just the CWD's
`.llmwiki` marker. This is VSCode-safe / CWD-independent: a wiki linked through
the active pj or the workspace convention path resolves even when the CWD has no
marker. When a wiki resolves, injects a "wiki-active" additionalContext so the
wiki query skill (description-driven) activates and the LLM knows it is operating
inside a wiki-root, AND a discovery line (W-f):

    active wiki: <root> (scope: pj|workspace|cwd|child)

so the active wiki is visible every turn (in VSCode the CWD is invisible). When
`resolve()` returns None (no wiki in any scope), exits 0 with no output (dormant)
— baseline behavior is preserved.

Precedent: role-mode/hooks/mode_inject.py (empty exit when dormant) and
taskflow/hooks/session_init.py (CWD detection + additionalContext injection).

Filing marker (plan §3 B-1, §0 M-a..M-e): in addition to the wiki-active
context above, the prompt is scanned for the inline marker
`llm-wiki:file[=<page-slug>]`. The marker is effective ONLY when wiki-active
(a wiki resolves via the resolver, in ANY scope); outside a wiki, filing is
impossible so the marker is ignored. When detected AND wiki-active, a deterministic filing
directive is APPENDED to the same additionalContext: filing becomes mandatory
via FE-A (wiki-query Step 3), no confirmation is asked (M-d), and the D5
resolved-value declaration is still emitted. A slug fixes the target page name
(code-decided); no slug -> the LLM generates the page name (L-a/M-e).

Detection follows the role-mode precedent (mode_inject.py MODE_RE): the marker
fires only at string start or after whitespace, case-insensitive, so it does
not match mid-token. llm-wiki OWNS the `wiki:` prefix (Phase 1): it now carries
TWO markers — `llm-wiki:file[=<slug>]` (filing) and `wiki:on|off` (the session
toggle below) — both distinct from `mode:`/`role:`/`pj:`, so there is no
collision with those prefixes.

Session toggle (Phase 1 P2, design §4-P2). The prompt is also scanned for
`wiki:on|off` (same start-or-after-whitespace, case-insensitive detection). It
is honored ONLY when a wiki resolves (we are past the dormant early-exit); with
no wiki, `wiki:on|off` is ignored and nothing is emitted (same shape as an
unassigned pj). `wiki:off` suppresses the whole `[wiki-active]` / filing
injection and instead emits a minimal `[wiki:off]` notice; state is per-session
(sticky within a session via `wiki_toggle`, default ON, a new session starts
ON). `wiki:on` restores injection. On (P3) the context gains a `[wiki:on]`
leading-line directive, and — scope=="pj" only — a project-notes coexistence
guide plus a filing-proposal norm (write_mode's explicit confirmation is kept).

I/O contract:
    stdin : CC UserPromptSubmit hook JSON ({ "cwd"?, "prompt"?, "session_id"?, ... })
    stdout: { hookSpecificOutput: { hookEventName, additionalContext } } when a
            wiki resolves; nothing (exit 0) when dormant.

The marker / wiki_root_resolver modules are imported from the `llmwiki.core`
package (path-import bootstrap below; spec §T層).
"""
import json
import os
import re
import sys

# T-layer bootstrap (spec §T層): plugin root = parent of hooks/. Single
# path-injection point so `import llmwiki` resolves under path-import (no-install),
# mirroring the bin/ entrypoints. The path insert lives only here in this hook.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_ROOT = os.path.dirname(_HERE)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from llmwiki.core import (  # noqa: E402  multi-scope resolve(cwd=...) (plan §2-C)
    marker,
    wiki_root_resolver,
    wiki_toggle,
)

# Filing marker `llm-wiki:file[=<page-slug>]` (plan §3 B-1).
# "start-or-after-whitespace" detection (mode_inject.py MODE_RE precedent) so it
# does not fire mid-token; case-insensitive. Slug after `=` is optional.
# Namespace `llm-wiki:` is distinct from `mode:`/`role:`/`pj:` (M-c).
FILE_MARKER_RE = re.compile(
    r"(?:^|\s)llm-wiki:file(?:=([A-Za-z0-9][\w-]*))?", re.IGNORECASE
)

# Session on/off toggle `wiki:on|off` (Phase 1 P2, design §4-P2). Same
# start-or-after-whitespace, case-insensitive detection as FILE_MARKER_RE so it
# does not fire mid-token. `\b` bounds the value so `wiki:onward` never matches.
TOGGLE_MARKER_RE = re.compile(r"(?:^|\s)wiki:(on|off)\b", re.IGNORECASE)


def main() -> None:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except Exception:
        sys.exit(0)

    cwd = data.get("cwd") or os.getcwd()
    # Session id (Phase 1 P1): taskflow writes `_projects/_state/<session_id>.json`,
    # so passing it lets the pj scope read THIS session's state file first (no
    # concurrent-session wiki mix-up). Absent -> resolver falls back to mtime-latest.
    session_id = data.get("session_id") or ""
    # Resolve the active wiki across pj / workspace / cwd (plan §2-C, W-f). This
    # replaces the old direct `marker.detect(cwd)` gate so a wiki linked via the
    # active pj or the workspace convention path is honored even when the CWD has
    # no marker (VSCode-safe / CWD-independent). No prompt_root here: the hook
    # has no `--root` override channel; it resolves from the hook cwd.
    resolution = wiki_root_resolver.resolve(cwd=cwd, session_id=session_id)
    if resolution is None:
        # Dormant: no wiki resolved in any scope -> empty exit, baseline preserved.
        # `wiki:on|off` is intentionally ignored here (nothing emitted), the same
        # shape as an unassigned pj (design §4-P2).
        sys.exit(0)

    root = resolution.root
    scope = resolution.scope
    prompt = data.get("prompt") or ""

    # Session toggle (Phase 1 P2, design §4-P2). A `wiki:on|off` in this prompt
    # updates the per-session state; then we read the effective state. Default
    # ON; sticky within a session; a new session (fresh sid) starts ON.
    toggle_match = TOGGLE_MARKER_RE.search(prompt)
    if toggle_match is not None and session_id:
        wiki_toggle.set_state(root, session_id, on=toggle_match.group(1).lower() == "on")
    active = wiki_toggle.is_on(root, session_id) if session_id else True

    if not active:
        # OFF for this session: suppress the whole read-grounding / filing block;
        # emit only the minimal off notice (design §4-P2 "注入を抑止し、最小限の
        # off 通知のみ"). No `active wiki:` discovery line while off.
        off_context = (
            "[wiki:off] Wiki is OFF for this session. Emit `[wiki:off]` in your "
            "response leading lines (mirrors `[pj:...]`). Do NOT read or write "
            "wiki pages this turn. Send `wiki:on` to re-enable."
        )
        # (fail-visible) The off branch returns BEFORE the FILE_MARKER_RE search
        # below, so a filing marker sent this turn would be silently dropped. Do
        # NOT drop it without a trace: detect it here and surface the drop in the
        # model-visible off notice, so the user's filing intent is not lost
        # (parity with pi pi-extensions/packages/llm-wiki/src/index.ts theme5(g)).
        if FILE_MARKER_RE.search(prompt) is not None:
            off_context += (
                "\nNote: a filing instruction (`llm-wiki:file`) was received this "
                "turn but was DROPPED because wiki is OFF. Re-send `wiki:on` and "
                "the filing marker to file this conversation."
            )
        _emit(off_context)
        return

    m = marker.detect(root)

    context = (
        # Discovery line (W-f): the active wiki path + scope, shown every turn so
        # it is visible even when the CWD is invisible (e.g. VSCode).
        f"active wiki: {root} (scope: {scope})\n"
        "[wiki-active] This directory is an LLM-wiki root "
        f"(.llmwiki version {(m.version if m else None) or '?'}). "
        "Wiki query is read-grounding (implicit): when answering, read pages under "
        "wiki/ and wiki/derived/ and cite by path (path encodes the source/derived "
        "tier). Writes go only through the wiki ingest/filing commands."
        # [wiki:on] leading-line directive (Phase 1 P3), mirrors `[pj:...]`.
        "\n[wiki:on] Emit `[wiki:on]` in your response leading lines "
        "(mirrors `[pj:...]`)."
    )

    # pj-scope coexistence + filing norm (Phase 1 P3, design §4-P3). Only when the
    # wiki is linked through the active pj do we add the taskflow<->wiki split
    # guide and the filing-proposal norm; workspace/cwd scopes keep read-grounding
    # only (unchanged from before Phase 1).
    if scope == "pj":
        context += (
            "\n[wiki<->taskflow] Durable, cross-cutting knowledge (decisions, "
            "conventions, reusable findings) belongs in the wiki (file it); "
            "task-execution context and progress belong in taskflow tasks / "
            "project-notes. When a durable finding or decision emerges, PROPOSE "
            "filing it to the wiki — do not write silently: the write_mode "
            "explicit pre-apply confirmation still applies (ask before filing)."
        )

    # Filing marker (plan §3 B-1, §0 M-d/M-e). Effective ONLY when wiki-active —
    # we are already past the dormant early-exit above, so the wiki is active.
    # Append the filing directive to the SAME additionalContext (one combined
    # block). When no marker, context is unchanged (existing behavior).
    file_match = FILE_MARKER_RE.search(prompt)
    if file_match is not None:
        slug = file_match.group(1)
        directive = (
            "\n\n[llm-wiki:file] The user explicitly requested filing via the "
            "`llm-wiki:file` marker. After answering, you MUST file the answer via "
            "the FE-A path (wiki-query SKILL Step 3) — this is mandatory, not "
            "optional. Per the `llm-wiki:` marker rule this is an explicit request: "
            "do NOT ask for any confirmation (skip the write_mode pre-apply "
            "confirmation); still emit the one-line resolved-value declaration for "
            "the record (D5)."
        )
        if slug:
            directive += (
                f" Target page name is `{slug}` -> `wiki/derived/{slug}.md`. "
                "Do NOT choose the page name yourself."
            )
        else:
            directive += (
                " No slug given: generate the page name from the answer content "
                "(page name is not deterministic here)."
            )
        context += directive

    _emit(context)


def _emit(context: str) -> None:
    """Write the additionalContext block (shared by the on and off paths)."""
    result = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.write(b"\n")


if __name__ == "__main__":
    main()
