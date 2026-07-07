"""Static tool-contract test for the llm-wiki subagent definitions.

Guards the bash-removal design point: the Stage2 apply agent and the lint agent
are Read-only (no Bash in the frontmatter ``tools:``, no ```bash fence in the
body) — verb execution lives in the orchestrator commands, where the
write_tool.WriteSession code gate fires. Also pins the pre-existing Read-only
state of the Stage1 extract agent as a regression check.
"""
import os
import re

import pytest

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGENTS_DIR = os.path.join(_PKG_ROOT, "agents")


def _load_agent_md(name):
    """Return (frontmatter, body) of an agent definition markdown."""
    path = os.path.join(_AGENTS_DIR, name)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$", text, re.DOTALL)
    assert m, f"{name}: missing frontmatter block"
    return m.group(1), m.group(2)


def _tools(frontmatter, name):
    for line in frontmatter.splitlines():
        if line.startswith("tools:"):
            return [t.strip() for t in line[len("tools:"):].split(",")]
    pytest.fail(f"{name}: no tools: line in frontmatter")


@pytest.mark.parametrize(
    "agent_md",
    ["wiki-ingest-apply.md", "wiki-lint.md", "wiki-ingest-extract.md"],
)
def test_frontmatter_tools_is_read_only(agent_md):
    frontmatter, _ = _load_agent_md(agent_md)
    tools = _tools(frontmatter, agent_md)
    assert "Bash" not in tools, f"{agent_md}: tools must not include Bash"
    assert tools == ["Read"], (
        f"{agent_md}: tools must be exactly ['Read'], got {tools}"
    )


@pytest.mark.parametrize("agent_md", ["wiki-ingest-apply.md", "wiki-lint.md"])
def test_body_has_no_bash_fence(agent_md):
    _, body = _load_agent_md(agent_md)
    assert "```bash" not in body, (
        f"{agent_md}: body must not contain a ```bash fence "
        "(verb execution belongs to the orchestrator command)"
    )
