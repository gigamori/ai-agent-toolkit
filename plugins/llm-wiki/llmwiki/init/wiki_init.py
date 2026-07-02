# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Wiki initializer — the generation主体 (plan T2, §2-B, W-c).

Given an ALREADY-RESOLVED target wiki-root, initialize a brand-new wiki there.
The scope decision is the SKILL's job (`/wiki-init`, context-driven, AskUser);
this module only takes the resolved root + a scope label and does the
deterministic init. Resolution lives in `wiki_root_resolver` (plan T1) and is
NOT duplicated here; generation lives ONLY here (W-c) — the resolver never
generates (R-6).

Steps:
  1. Refuse to overwrite: if `<root>/.llmwiki` already exists -> no-op error
     (NEVER overwrite an existing wiki).
  2. Copy the contract templates from `plugins/llm-wiki/templates/` into the
     target root (`.llmwiki`, `SCHEMA.md`, `index.md`, `log.md`, `raw/`,
     `wiki/` incl. `wiki/derived/`). The templates dir is found via the
     sibling pattern (`<init>/../../templates`).
  3. Report (`InitResult`): the created wiki-root + scope label.

**git-independent**: this module invokes git in ZERO places. A wiki-root is just a
directory; the transaction engine (transaction.py) is a file journal, not git. The
former nested `git init` + parent `.git/info/exclude` registration were removed —
with no repo-root guarantee, `git -C <wiki-root>` could misfire into an enclosing
parent repo. Versioning the wiki is the user's own concern; the shipped
`<wiki-root>/.gitignore` keeps a surrounding parent repo clean without any `.git`
mutation.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
# llmwiki/init/ -> llmwiki/ -> plugins/llm-wiki/ -> templates/
TEMPLATES_DIR = SCRIPT_DIR.parent.parent / "templates"

MARKER_NAME = ".llmwiki"


class WikiInitError(Exception):
    """Initialization could not proceed (e.g. wiki already exists)."""


@dataclass
class InitResult:
    root: Path       # the created wiki-root (absolute)
    scope: str       # scope label passed in by the SKILL


def _template_names() -> list[str]:
    """Top-level entry names the template copy would create in the target root."""
    return [p.name for p in TEMPLATES_DIR.iterdir()]


def _collisions(root: Path) -> list[str]:
    """Existing target entries that the template copy would overwrite (M5).

    `shutil.copytree(dirs_exist_ok=True)` silently REPLACES same-named files, so
    an unrelated pre-existing `index.md` / `SCHEMA.md` / `.gitignore` / etc. in a
    non-wiki target dir would be clobbered. Return the sorted collision names so
    the caller can refuse rather than destroy foreign content.
    """
    if not TEMPLATES_DIR.is_dir():
        raise WikiInitError(f"templates dir not found: {TEMPLATES_DIR}")
    return sorted(n for n in _template_names() if (root / n).exists())


def _copy_templates(root: Path) -> None:
    """Copy the contract templates into `root` (step 2).

    Copies the whole `templates/` tree (`.llmwiki`, SCHEMA.md, index.md, log.md,
    raw/ incl. raw/derived & raw/assets, wiki/ incl. wiki/derived, and the shipped
    `.gitignore`). `.gitkeep` placeholders come along verbatim so empty dirs
    survive. `dirs_exist_ok=True` lets the target root pre-exist (the SKILL may
    have just `mkdir`-ed it); the caller already guaranteed no collision.
    """
    if not TEMPLATES_DIR.is_dir():
        raise WikiInitError(f"templates dir not found: {TEMPLATES_DIR}")
    shutil.copytree(TEMPLATES_DIR, root, dirs_exist_ok=True)


def init_wiki(root: "str | Path", scope: str) -> InitResult:
    """Initialize a new wiki at the resolved `root`.

    Args:
        root:  the ALREADY-RESOLVED target wiki-root (the SKILL decides scope and
               passes the path; resolution is not done here).
        scope: the scope label (e.g. "pj" | "workspace" | "prompt"), recorded in
               the report only.
    """
    root = Path(root).resolve()

    # 1) Refuse to overwrite an existing wiki (NEVER overwrite).
    if (root / MARKER_NAME).exists():
        raise WikiInitError(
            f"a wiki already exists at {root} ({MARKER_NAME} present) — refusing to overwrite"
        )

    # Ensure the target root (and any intermediate dirs) exists.
    root.mkdir(parents=True, exist_ok=True)

    # 1b) Refuse if any template-named file already exists in the target (M5) —
    #     copytree(dirs_exist_ok=True) would silently overwrite foreign content.
    #     Nothing is written on refusal (the copy has not run yet).
    collisions = _collisions(root)
    if collisions:
        raise WikiInitError(
            f"target {root} already contains: {', '.join(collisions)} -- refusing "
            f"to overwrite (initialize into an empty or collision-free directory)"
        )

    # 2) Copy the contract templates.
    _copy_templates(root)

    return InitResult(root=root, scope=scope)


def _format_report(res: InitResult) -> str:
    return "\n".join([
        f"wiki initialized: {res.root}",
        f"scope: {res.scope}",
        "git: not used (the engine never invokes git; version the wiki yourself if desired)",
    ])


def main(argv: "list[str] | None" = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Initialize a new llm-wiki at a resolved target root.",
    )
    parser.add_argument("root", help="the resolved target wiki-root path")
    parser.add_argument(
        "--scope", default="prompt",
        help="scope label decided by the SKILL (recorded in the report)",
    )
    args = parser.parse_args(argv)

    try:
        res = init_wiki(args.root, args.scope)
    except WikiInitError as e:
        print(f"error: {e}", file=__import__("sys").stderr)
        return 2

    print(_format_report(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
