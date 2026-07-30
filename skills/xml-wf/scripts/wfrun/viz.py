"""Deterministic mermaid rendering of a workflow (`wfrun viz`).

Complements `wfrun plan` (the ascii tree, which is also the run-llm control
skeleton — that format is protocol and must not change): a flowchart of the
actual control flow — branch diamonds, loop-back edges, parallel fan-out —
for docs, PRs, and the build-mode approval gate.

Rendered from the parsed model in pure Python. Labels carry control-plane
facts only (ids, roles, modes, models, conditions) — never task bodies, so
the output is safe to surface even around run-llm's content firewall.
"""
from __future__ import annotations

from . import model

_MAX_LABEL = 48


def _esc(text: str) -> str:
    """Label-safe text: quotes and braces break mermaid label parsing."""
    text = " ".join(text.split()).replace('"', "'")
    return text.replace("{", "#123;").replace("}", "#125;")


def _trunc(text: str) -> str:
    """_esc + length cap, for labels of unbounded length (conditions, paths)."""
    text = _esc(text)
    if len(text) > _MAX_LABEL:
        text = text[:_MAX_LABEL - 1] + "…"
    return text


def _step_label(step: model.Step) -> str:
    attrs = [x for x in (model.role_label(step),) if x]
    if step.mode:
        attrs.append(f"mode={step.mode}")
    if step.model:
        attrs.append(f"model={step.model}")
    if step.retry:
        attrs.append(f"retry={step.retry}")
    if step.on_error != model.DEFAULT_ON_ERROR:
        attrs.append(f"on-error={step.on_error}")
    lines = [f"<b>{_esc(step.id)}</b>", _esc(" ".join(attrs))]
    extra = []
    if step.output:
        extra.append(f"→ {step.output}")
    if step.expect_file:
        extra.append(f"expect: {step.expect_file}")
    if extra:
        lines.append(_trunc(" ".join(extra)))
    return "<br/>".join(lines)


def _replan_label(node: model.Replan) -> str:
    attrs = [x for x in (model.role_label(node),) if x]
    attrs.append(f"max-steps={node.max_steps}")
    if node.on_error != model.DEFAULT_ON_ERROR:
        attrs.append(f"on-error={node.on_error}")
    lines = [f"<b>replan {_esc(node.id)}</b>", _esc(" ".join(attrs))]
    if node.outputs:
        lines.append(_esc("→ " + ", ".join(node.outputs)))
    return "<br/>".join(lines)


def _cond_label(test: str | None, ask: str | None) -> str:
    return _trunc(test if test is not None else f"ask: {ask}")


class _Builder:
    def __init__(self):
        self.lines: list[str] = []
        self.n = 0
        self.replan_ids: list[str] = []

    def nid(self) -> str:
        self.n += 1
        return f"n{self.n}"

    def emit(self, line: str):
        self.lines.append("    " + line)

    def edge(self, src: str, dst: str, label: str | None = None):
        arrow = f"-->|{label}|" if label else "-->"
        self.emit(f"{src} {arrow} {dst}")

    # -- walk: returns (entry_id | None, [(exit_id, edge_label), ...]) -------
    def walk(self, node):
        if isinstance(node, model.Seq):
            entry, exits = None, []
            for child in node.children:
                e, x = self.walk(child)
                if e is None:
                    continue
                if entry is None:
                    entry = e
                for xid, lbl in exits:
                    self.edge(xid, e, lbl)
                exits = x
            return entry, exits
        if isinstance(node, model.Step):
            nid = self.nid()
            self.emit(f'{nid}["{_step_label(node)}"]')
            return nid, [(nid, None)]
        if isinstance(node, model.Replan):
            nid = self.nid()
            self.emit(f'{nid}["{_replan_label(node)}"]')
            self.replan_ids.append(nid)
            return nid, [(nid, None)]
        if isinstance(node, model.SetVar):
            nid = self.nid()
            self.emit(f'{nid}("{_esc("set " + node.var)}")')
            return nid, [(nid, None)]
        if isinstance(node, model.If):
            cond = self.nid()
            self.emit(f'{cond}{{"{_cond_label(node.test, node.ask)}"}}')
            exits = []
            then_entry, then_exits = self.walk(node.then)
            if then_entry is None:
                exits.append((cond, "yes"))
            else:
                self.edge(cond, then_entry, "yes")
                exits += then_exits
            if node.else_ is None:
                exits.append((cond, "no"))
            else:
                else_entry, else_exits = self.walk(node.else_)
                if else_entry is None:
                    exits.append((cond, "no"))
                else:
                    self.edge(cond, else_entry, "no")
                    exits += else_exits
            return cond, exits
        if isinstance(node, model.While):
            cond = self.nid()
            self.emit(f'{cond}{{"while {_cond_label(node.test, node.ask)}'
                      f' (max {node.max})"}}')
            body_entry, body_exits = self.walk(node.body)
            if body_entry is not None:
                self.edge(cond, body_entry, "yes")
                for xid, lbl in body_exits:
                    self.edge(xid, cond, lbl)
            return cond, [(cond, "done")]
        if isinstance(node, model.Each):
            source = node.items or node.glob or f"range {node.range_}"
            head = self.nid()
            self.emit(f'{head}{{{{"{_trunc(f"each {node.as_} in {source}")}"}}}}')
            body_entry, body_exits = self.walk(node.body)
            if body_entry is not None:
                self.edge(head, body_entry, "next")
                for xid, lbl in body_exits:
                    self.edge(xid, head, lbl)
            return head, [(head, "done")]
        if isinstance(node, model.Parallel):
            fork = self.nid()
            self.emit(f'{fork}((" "))')
            sub = self.nid()
            self.emit(f'subgraph {sub}["parallel max-workers={node.max_workers}"]')
            child_ids = []
            for step in node.children:
                cid = self.nid()
                self.emit(f'{cid}["{_step_label(step)}"]')
                child_ids.append(cid)
            self.emit("end")
            join = self.nid()
            self.emit(f'{join}((" "))')
            for cid in child_ids:
                self.edge(fork, cid)
                self.edge(cid, join)
            return fork, [(join, None)]
        raise ValueError(f"unknown node type {type(node).__name__}")


def mermaid(wf: model.Workflow) -> str:
    b = _Builder()
    b.emit(f'S(("start<br/>{_esc(wf.name)}"))')
    entry, exits = b.walk(wf.body)
    b.emit('E((end))')
    if entry is None:
        b.edge("S", "E")
    else:
        b.edge("S", entry)
        for xid, lbl in exits:
            b.edge(xid, "E", lbl)
    if b.replan_ids:
        b.emit("classDef replan stroke-dasharray: 5 5")
        b.emit(f"class {','.join(b.replan_ids)} replan")
    return "flowchart TD\n" + "\n".join(b.lines) + "\n"
