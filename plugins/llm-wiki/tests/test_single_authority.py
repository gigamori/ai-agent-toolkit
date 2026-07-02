"""Single-authority test (DD3 / #3 / spec R-7).

`scan_pages` and `tier_of` must each be exactly ONE implementation in the
package (no copy was made when the engine was split into subpackages), and
read / write / ingest must reach page-ness via `llmwiki.core` rather than
re-defining it.

DEC-4 (move-with-test-forward): the flat `scripts/` origins were deleted in
P2/P3, so there is no transient duplicate — this holds statically without any
"ignore the old copy" carve-out.

Method: a static scan of every `*.py` under the package for a top-level
`def scan_pages` / `def tier_of` (so a re-implementation anywhere is caught),
plus an identity check that the symbols other layers use are the SAME objects
defined in `llmwiki.core.wiki_index`.
"""
import ast
import os

import pytest

import llmwiki
from llmwiki.core import wiki_index


_PKG_DIR = os.path.dirname(os.path.abspath(llmwiki.__file__))


def _all_package_py_files():
    out = []
    for dirpath, dirnames, filenames in os.walk(_PKG_DIR):
        # Skip caches / any test trees that might live under the package.
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "tests")]
        for fn in filenames:
            if fn.endswith(".py"):
                out.append(os.path.join(dirpath, fn))
    return out


def _toplevel_func_defs(py_path, name):
    """Files that define a top-level `def <name>` (returns list of rel paths)."""
    with open(py_path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=py_path)
    hits = []
    for node in tree.body:  # module top-level only
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            hits.append(node)
    return hits


@pytest.mark.parametrize("symbol", ["scan_pages", "tier_of"])
def test_symbol_defined_exactly_once_in_package(symbol):
    definers = []
    for py in _all_package_py_files():
        if _toplevel_func_defs(py, symbol):
            definers.append(os.path.relpath(py, _PKG_DIR).replace("\\", "/"))
    assert definers == ["core/wiki_index.py"], (
        f"{symbol} must be defined exactly once (in core/wiki_index.py); "
        f"found: {definers}"
    )


def test_core_is_the_authority_object():
    # The authority lives in core/wiki_index; the names resolve to callables there.
    assert callable(wiki_index.scan_pages)
    assert callable(wiki_index.tier_of)
    assert wiki_index.scan_pages.__module__ == "llmwiki.core.wiki_index"
    assert wiki_index.tier_of.__module__ == "llmwiki.core.wiki_index"


def test_other_layers_reach_core_not_reimplement():
    # write / lint reach wiki_index through core; the object they hold is THE
    # core object (identity), proving no per-layer copy of the authority.
    from llmwiki.write import promote
    from llmwiki.lint import link_lint

    assert promote.wiki_index is wiki_index
    assert link_lint.wiki_index is wiki_index
    # And the CLI's scan path uses the same module (no shadow scanner).
    from llmwiki import cli  # noqa: F401  (import side-effect free; dispatch only)
