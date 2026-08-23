import ast
import os

import pytest

import llmwiki
from llmwiki.core import wiki_index


_PKG_DIR = os.path.dirname(os.path.abspath(llmwiki.__file__))


def _all_package_py_files():
    out = []
    for dirpath, dirnames, filenames in os.walk(_PKG_DIR):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "tests")]
        for fn in filenames:
            if fn.endswith(".py"):
                out.append(os.path.join(dirpath, fn))
    return out


def _toplevel_func_defs(py_path, name):
    with open(py_path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=py_path)
    hits = []
    for node in tree.body:
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
    assert callable(wiki_index.scan_pages)
    assert callable(wiki_index.tier_of)
    assert wiki_index.scan_pages.__module__ == "llmwiki.core.wiki_index"
    assert wiki_index.tier_of.__module__ == "llmwiki.core.wiki_index"


def test_other_layers_reach_core_not_reimplement():
    from llmwiki.write import promote
    from llmwiki.lint import link_lint

    assert promote.wiki_index is wiki_index, "identity, not equality: a per-layer copy fails here"
    assert link_lint.wiki_index is wiki_index, "identity, not equality: a per-layer copy fails here"
    from llmwiki import cli  # noqa: F401
