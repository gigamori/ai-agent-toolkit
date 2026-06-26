#!/usr/bin/env python3
"""UserPromptSubmit hook: wiki-active marker injection (D8, design §4 起動 / §6 F2).

Resolves the active wiki via `wiki_root_resolver.resolve(cwd=<hook cwd>)` so
pj / workspace / cwd scopes are ALL honored (plan §2-C, W-f) — not just the CWD's
`.llmwiki` marker. This is VSCode-safe / CWD-independent: a wiki linked through
the active pj or the workspace convention path resolves even when the CWD has no
marker. When a wiki resolves, injects a "wiki-active" additionalContext so the
wiki query skill (description-driven) activates and the LLM knows it is operating
inside a wiki-root, AND a discovery line (W-f):

    active wiki: <root> (scope: pj|workspace|cwd)

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
not match mid-token. Namespace is exactly `llm-wiki:` (M-c) — distinct from
`mode:`/`role:`/`pj:`/`wiki:`, so there is no collision with those prefixes.

I/O contract:
    stdin : CC UserPromptSubmit hook JSON ({ "cwd"?, "prompt"?, ... })
    stdout: { hookSpecificOutput: { hookEventName, additionalContext } } when a
            marker is found in cwd; nothing (exit 0) when dormant.

The marker module is imported from the sibling scripts/ dir.
"""
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import marker  # noqa: E402
import wiki_root_resolver  # noqa: E402  multi-scope resolve(cwd=...) (plan §2-C)

# Filing marker `llm-wiki:file[=<page-slug>]` (plan §3 B-1).
# "start-or-after-whitespace" detection (mode_inject.py MODE_RE precedent) so it
# does not fire mid-token; case-insensitive. Slug after `=` is optional.
# Namespace `llm-wiki:` is distinct from `mode:`/`role:`/`pj:`/`wiki:` (M-c).
FILE_MARKER_RE = re.compile(
    r"(?:^|\s)llm-wiki:file(?:=([A-Za-z0-9][\w-]*))?", re.IGNORECASE
)


def main() -> None:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except Exception:
        sys.exit(0)

    cwd = data.get("cwd") or os.getcwd()
    # Resolve the active wiki across pj / workspace / cwd (plan §2-C, W-f). This
    # replaces the old direct `marker.detect(cwd)` gate so a wiki linked via the
    # active pj or the workspace convention path is honored even when the CWD has
    # no marker (VSCode-safe / CWD-independent). No prompt_root here: the hook
    # has no `--root` override channel; it resolves from the hook cwd.
    resolution = wiki_root_resolver.resolve(cwd=cwd)
    if resolution is None:
        # Dormant: no wiki resolved in any scope -> empty exit, baseline preserved.
        sys.exit(0)

    root = resolution.root
    scope = resolution.scope
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
    )

    # Filing marker (plan §3 B-1, §0 M-d/M-e). Effective ONLY when wiki-active —
    # we are already past the dormant early-exit above, so the wiki is active.
    # Append the filing directive to the SAME additionalContext (one combined
    # block). When no marker, context is unchanged (existing behavior).
    prompt = data.get("prompt") or ""
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

    result = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.write(b"\n")
    sys.exit(0)


if __name__ == "__main__":
    main()
