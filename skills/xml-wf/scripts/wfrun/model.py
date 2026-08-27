"""Dataclass model for XML workflow v2.

The parser (parser.py) is the only producer of these objects. All schema
defaults live here so executor/lint share a single source of truth.
"""
from __future__ import annotations

from dataclasses import dataclass, field


ON_ERROR_VALUES = ("fail", "ignore", "debug")
OUTPUT_TYPES = ("file", "value")
# Who settles a step's `DECISION:` request. Vocabulary and default are shared
# with mode-orchestrator's --decider so the two skills cannot disagree about
# what an unattended run did; `human` is the default because an `llm` decider
# judges work produced under the very contract it runs under
# (xml-wf-decision-request.md §4).
DECIDER_HUMAN = "human"
DECIDER_LLM = "llm"
DECIDER_VALUES = (DECIDER_HUMAN, DECIDER_LLM)

# How many requests one llm decider may settle per step visit before the run
# stops and hands the fork to a person. Human answers never consume it
# (xml-wf-decision-request.md §7).
DECISION_LLM_CAP = 2

DEFAULT_RETRY = 0
DEFAULT_TIMEOUT = 600
DEFAULT_ON_ERROR = "fail"
DEFAULT_OUTPUT_TYPE = "file"
DEFAULT_ASK_MODEL = "basic"
DEFAULT_PARALLEL_WORKERS = 2
DEFAULT_REPLAN_MAX_STEPS = 20
DEFAULT_DECIDER = "human"
DEFAULT_DECIDER_MODEL = "ultra"
DEBUG_ROLE = "debug"

# Tools that can mutate the filesystem (directly or by delegation). Used to
# gate --permission-mode forwarding and the mode-write-tools lint.
WRITE_CAPABLE_TOOLS = frozenset({
    "Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "Task", "Agent",
})


def tools_can_write(tools: str | None) -> bool:
    """True when an --allowedTools list permits writes — or is unrestricted.

    Entries may carry specifiers ("Bash(git:*)"); only the leading name counts.
    """
    if not tools:
        return True
    names = {t.strip().split("(")[0] for t in tools.split(",") if t.strip()}
    return bool(names & WRITE_CAPABLE_TOOLS)


@dataclass
class Param:
    name: str
    required: bool = False
    default: str | None = None


@dataclass
class Rules:
    id: str
    src: str | None = None
    text: str | None = None  # inline body when src is absent


@dataclass
class Step:
    id: str
    task: str
    role: str | None = None       # named role: a .claude/agents/*.md definition
    role_text: str | None = None  # inline <role> body (at most one of the two)
    mode: str | None = None       # execution mode (modes.py fragment)
    model: str | None = None
    effort: str | None = None
    output: str | None = None
    output_type: str = DEFAULT_OUTPUT_TYPE
    schema: str | None = None  # JSON schema string (already resolved from @path)
    rules: list[str] = field(default_factory=list)
    tools: str | None = None
    expect_file: str | None = None  # comma-separated paths that must exist after success
    retry: int = DEFAULT_RETRY
    timeout: int = DEFAULT_TIMEOUT
    on_error: str = DEFAULT_ON_ERROR
    # None means "inherit the workflow-level setting" — distinct from an
    # explicit value, which is why neither carries the default directly.
    decider: str | None = None
    decider_model: str | None = None


@dataclass
class Replan:
    """One-level dynamic replanning: a builder agent generates a continuation
    workflow from results so far; the runner validates it (recursion and
    params are rejected) and executes it inline with shared variables."""
    id: str
    task: str
    role: str | None = None
    role_text: str | None = None
    model: str | None = None
    effort: str | None = None
    max_steps: int = DEFAULT_REPLAN_MAX_STEPS
    outputs: list[str] = field(default_factory=list)
    retry: int = DEFAULT_RETRY
    timeout: int = DEFAULT_TIMEOUT
    on_error: str = DEFAULT_ON_ERROR


@dataclass
class SetVar:
    var: str
    value: str | None = None  # interpolation only
    expr: str | None = None   # safe expression evaluation


@dataclass
class Seq:
    children: list = field(default_factory=list)


@dataclass
class If:
    test: str | None
    ask: str | None
    then: Seq
    else_: Seq | None = None
    ask_model: str = DEFAULT_ASK_MODEL


@dataclass
class While:
    test: str | None
    ask: str | None
    max: int
    body: Seq = field(default_factory=Seq)
    ask_model: str = DEFAULT_ASK_MODEL


@dataclass
class Each:
    as_: str
    items: str | None = None   # {var} holding a JSON array
    glob: str | None = None    # glob pattern, resolved at runtime
    range_: str | None = None  # integer (or {var} interpolating to one)
    body: Seq = field(default_factory=Seq)


@dataclass
class Parallel:
    max_workers: int = DEFAULT_PARALLEL_WORKERS
    children: list[Step] = field(default_factory=list)


@dataclass
class Workflow:
    name: str
    version: str
    max: int
    budget_usd: float | None = None
    params: list[Param] = field(default_factory=list)
    rules: dict[str, Rules] = field(default_factory=dict)
    body: Seq = field(default_factory=Seq)
    decider: str | None = None
    decider_model: str | None = None

    def iter_steps(self):
        """Yield every Step and Replan in the tree (static, ignores control flow)."""
        yield from _walk_steps(self.body)


def resolve_decider(wf: Workflow, step=None) -> tuple[str, str]:
    """(decider, decider-model) actually in force: step attribute, else
    workflow attribute, else the defaults (xml-wf-decision-request.md §4).

    Only attribute-precedence resolution lives here, not at the display
    sites -- step attr > workflow attr > default. The decider-model
    returned is still a tier name: turning it into the concrete name a run
    would dispatch is `modelmap.resolve(..., "llm")`'s job, applied
    separately at `wfrun plan` and `wfrun viz` themselves, so that they do
    not disagree with what a run would do -- plan output is the run-llm
    orchestrator's only view of the workflow, so a divergence there is
    invisible rather than merely wrong.
    """
    decider = (getattr(step, "decider", None) if step is not None else None) \
        or wf.decider or DEFAULT_DECIDER
    decider_model = (getattr(step, "decider_model", None) if step is not None else None) \
        or wf.decider_model or DEFAULT_DECIDER_MODEL
    return decider, decider_model


def role_label(node) -> str | None:
    """The `role=` display item for a Step/Replan, or None when it declares no
    role and the item should be omitted entirely.

    Role has three states, not two — named, inline, absent — so every display
    site must go through this rather than an `if node.role` ternary, which
    would label an absent role "inline".
    """
    if node.role:
        return f"role={node.role}"
    if node.role_text:
        return "role=inline"
    return None


def _walk_steps(node):
    if isinstance(node, (Step, Replan)):
        yield node
    elif isinstance(node, Seq):
        for child in node.children:
            yield from _walk_steps(child)
    elif isinstance(node, If):
        yield from _walk_steps(node.then)
        if node.else_ is not None:
            yield from _walk_steps(node.else_)
    elif isinstance(node, While):
        yield from _walk_steps(node.body)
    elif isinstance(node, Each):
        yield from _walk_steps(node.body)
    elif isinstance(node, Parallel):
        for child in node.children:
            yield from _walk_steps(child)
