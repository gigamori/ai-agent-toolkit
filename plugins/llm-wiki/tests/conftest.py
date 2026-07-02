"""Make the llmwiki package importable for tests without installation.

Inserts the package root (plugins/llm-wiki/) onto sys.path so that
`from llmwiki.core import ...` resolves under path-import (no-install).
"""
import os
import sys

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)
