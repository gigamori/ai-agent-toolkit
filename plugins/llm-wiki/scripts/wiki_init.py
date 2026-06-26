# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Wiki initializer — the generation主体 (plan T2, §2-B, W-c/W-d/W-e, R-1/R-6).

Given an ALREADY-RESOLVED target wiki-root, initialize a brand-new wiki there.
The scope decision is the SKILL's job (`/wiki-init`, context-driven, AskUser);
this module only takes the resolved root + a scope label and does the
deterministic init. Resolution lives in `wiki_root_resolver` (plan T1) and is
NOT duplicated here; generation lives ONLY here (W-c) — the resolver never
generates (R-6).

Steps (plan §2-B):
  1. Refuse to overwrite: if `<root>/.llmwiki` already exists -> no-op error
     (NEVER overwrite an existing wiki).
  2. Copy the contract templates from `plugins/llm-wiki/templates/` into the
     target root (`.llmwiki`, `SCHEMA.md`, `index.md`, `log.md`, `raw/`,
     `wiki/` incl. `wiki/derived/`). The templates dir is found via the
     sibling-scripts pattern (`<scripts>/../templates`).
  3. Nested independent git repo (W-d): `git init` IN the target root + an
     initial commit. The wiki is its OWN repo; the ingest transaction
     (transaction.py) operates on THIS repo — a wiki nested as a plain
     subdirectory of a parent repo would let rollback's `git reset --hard`
     destroy the parent (transaction.py rollback), hence a separate repo.
  4. Force-ignore registration in the PARENT repo (W-e / R-1): detect the
     parent repo by running `git rev-parse --show-toplevel` from the target
     root's PARENT directory — NOT inside the new wiki repo, which would
     resolve to the wiki repo itself (R-1 order hazard). The parent is detected
     BEFORE the nested init for safety (so the nested `.git` can never confuse
     detection). If a parent repo is found and the wiki-root is inside it,
     append the wiki-root's RELATIVE path to the parent's `.git/info/exclude`
     (Q1: repo-local, non-shared, not committed). Idempotent (never double-add).
  5. Report (`InitResult`): created wiki-root, scope label, whether a parent
     repo was found, and the exclude path written (if any).

git is invoked via subprocess in the same dependency-free idiom as
transaction.py (`git -C <dir> ...`). This is a NEW init, separate from the
ingest transaction.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
# scripts/ -> plugins/llm-wiki/ -> templates/
TEMPLATES_DIR = SCRIPT_DIR.parent / "templates"

MARKER_NAME = ".llmwiki"
EXCLUDE_REL = Path(".git") / "info" / "exclude"
EXCLUDE_HEADER = "# llm-wiki: nested wiki repos (W-e; repo-local, not committed)"


class WikiInitError(Exception):
    """Initialization could not proceed (e.g. wiki already exists)."""


class GitError(Exception):
    """A required git operation failed."""


@dataclass
class InitResult:
    root: Path                       # the created wiki-root (absolute)
    scope: str                       # scope label passed in by the SKILL
    parent_repo: "Path | None"       # parent repo toplevel, or None if none found
    exclude_path: "Path | None"      # the `.git/info/exclude` written, or None
    exclude_entry: "str | None"      # the relative path appended, or None


def _git(cwd: "str | Path", args: list, *, check: bool = False) -> "str | None":
    """Run `git -C <cwd> <args>`; return stdout or None on failure.

    Same idiom as transaction.py:_git — None on non-fatal probing; raises
    GitError when `check` and the op is required (so the caller never assumes
    success of an init-critical step).
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as e:
        if check:
            raise GitError(f"git {' '.join(args)} failed: {e}") from e
        return None
    if r.returncode != 0:
        if check:
            raise GitError(f"git {' '.join(args)} -> {r.returncode}: {r.stderr.strip()}")
        return None
    return r.stdout.strip()


def _detect_parent_repo(root: Path) -> "Path | None":
    """Detect the PARENT repo of `root`, run from root's PARENT directory.

    R-1 / W-e: `git rev-parse --show-toplevel` MUST be run from the parent
    directory. Run inside `root` after the nested `git init`, it would resolve
    to the wiki repo itself. The parent dir may not yet be tracked; if it is not
    inside any repo, git returns a non-zero exit and we get None.

    Returns the parent repo toplevel (absolute Path) or None.
    """
    parent = root.parent
    out = _git(parent, ["rev-parse", "--show-toplevel"])
    if not out:
        return None
    return Path(out).resolve()


def _copy_templates(root: Path) -> None:
    """Copy the contract templates into `root` (plan §2-B step 2).

    Copies the whole `templates/` tree (`.llmwiki`, SCHEMA.md, index.md,
    log.md, raw/ incl. raw/derived & raw/assets, wiki/ incl. wiki/derived).
    `.gitkeep` placeholders and the shipped `.gitignore` come along verbatim so
    empty dirs survive in the nested repo. `dirs_exist_ok=True` lets the target
    root pre-exist (the SKILL may have just `mkdir`-ed it), but step 1 has
    already guaranteed there is no existing wiki to overwrite.
    """
    if not TEMPLATES_DIR.is_dir():
        raise WikiInitError(f"templates dir not found: {TEMPLATES_DIR}")
    shutil.copytree(TEMPLATES_DIR, root, dirs_exist_ok=True)


def _nested_git_init(root: Path) -> None:
    """`git init` + an initial commit IN `root` (W-d: independent repo).

    Local identity is set on the new repo only (does not touch global config)
    so the initial commit succeeds in any environment; gpg signing is disabled
    for the same reason. This mirrors how the transaction tests build a repo.
    """
    _git(root, ["init", "-q"], check=True)
    _git(root, ["config", "user.email", "llm-wiki@local"], check=True)
    _git(root, ["config", "user.name", "llm-wiki"], check=True)
    _git(root, ["config", "commit.gpgsign", "false"], check=True)
    _git(root, ["add", "-A"], check=True)
    _git(root, ["commit", "-q", "-m", "chore: initialize llm-wiki"], check=True)


def _register_parent_exclude(root: Path, parent_repo: Path) -> "tuple[Path, str] | None":
    """Append the wiki-root's relative path to the parent's `.git/info/exclude`.

    W-e / Q1: repo-local, non-shared, not committed. Idempotent — never adds a
    duplicate line. Returns (exclude_path, entry) if written or already present,
    or None if the wiki-root is not actually inside the parent repo.
    """
    try:
        rel = root.relative_to(parent_repo)
    except ValueError:
        # wiki-root is not inside the parent repo -> nothing to ignore.
        return None
    # POSIX-style, anchored to the repo root, trailing slash = a directory.
    entry = "/" + rel.as_posix().rstrip("/") + "/"

    exclude_path = parent_repo / EXCLUDE_REL
    exclude_path.parent.mkdir(parents=True, exist_ok=True)

    existing = ""
    if exclude_path.is_file():
        existing = exclude_path.read_text(encoding="utf-8")

    # Idempotent: if the exact entry is already a line, do not double-add.
    existing_lines = {ln.strip() for ln in existing.splitlines()}
    if entry in existing_lines:
        return (exclude_path, entry)

    prefix = "" if (existing == "" or existing.endswith("\n")) else "\n"
    addition = f"{prefix}{EXCLUDE_HEADER}\n{entry}\n"
    with exclude_path.open("a", encoding="utf-8") as f:
        f.write(addition)
    return (exclude_path, entry)


def init_wiki(root: "str | Path", scope: str) -> InitResult:
    """Initialize a new wiki at the resolved `root` (plan §2-B).

    Args:
        root:  the ALREADY-RESOLVED target wiki-root (the SKILL decides scope
               and passes the path; resolution is not done here).
        scope: the scope label (e.g. "pj" | "workspace" | "prompt"), recorded
               in the report only.

    Order (R-1): detect the PARENT repo first (from root's parent), THEN copy
    templates, THEN nested `git init`, THEN register the parent exclude. Detect
    before init so the new nested `.git` can never be mistaken for the parent.
    """
    root = Path(root).resolve()

    # 1) Refuse to overwrite an existing wiki (NEVER overwrite).
    if (root / MARKER_NAME).exists():
        raise WikiInitError(
            f"a wiki already exists at {root} ({MARKER_NAME} present) — refusing to overwrite"
        )

    # Ensure the target root (and any intermediate dirs) exists BEFORE parent
    # detection. `_detect_parent_repo` runs `git -C <root.parent> ...`, which
    # fails on a not-yet-existent directory when the wiki-root is nested more
    # than one level below the parent repo (e.g. `<parent>/sub/mywiki`). Making
    # the path first lets detection run from a real directory; the nested
    # `git init` still happens AFTER detection, so R-1 (detect before the nested
    # `.git` exists) is preserved.
    root.mkdir(parents=True, exist_ok=True)

    # 4-pre) Detect the parent repo BEFORE the nested init (R-1 order hazard).
    parent_repo = _detect_parent_repo(root)

    # 2) Copy the contract templates.
    _copy_templates(root)

    # 3) Nested independent git repo (W-d).
    _nested_git_init(root)

    # 4) Register the parent exclude (W-e / Q1), if inside a parent repo.
    exclude_path: "Path | None" = None
    exclude_entry: "str | None" = None
    if parent_repo is not None:
        written = _register_parent_exclude(root, parent_repo)
        if written is not None:
            exclude_path, exclude_entry = written
        else:
            # Found a repo but the wiki-root is not inside it -> not the parent.
            parent_repo = None

    return InitResult(
        root=root,
        scope=scope,
        parent_repo=parent_repo,
        exclude_path=exclude_path,
        exclude_entry=exclude_entry,
    )


def _format_report(res: InitResult) -> str:
    lines = [
        f"wiki initialized: {res.root}",
        f"scope: {res.scope}",
        "nested git repo: yes (initial commit made)",
    ]
    if res.parent_repo is not None and res.exclude_entry is not None:
        lines.append(f"parent repo: {res.parent_repo}")
        lines.append(f"force-ignored in parent: {res.exclude_entry} -> {res.exclude_path}")
    else:
        lines.append("parent repo: none (no force-ignore needed)")
    return "\n".join(lines)


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
    except GitError as e:
        print(f"error: git init failed: {e}", file=__import__("sys").stderr)
        return 3

    print(_format_report(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
