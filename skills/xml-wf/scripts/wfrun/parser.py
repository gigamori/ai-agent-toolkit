"""Strict XML v2 parser: ElementTree -> model dataclasses.

Unknown elements and attributes are hard errors — the schema is closed by
design so that typos fail at parse time, not mid-run.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from . import model


class ParseError(Exception):
    pass


_STEP_ATTRS = {
    "id", "role", "mode", "model", "effort", "output", "output-type", "schema",
    "rules", "tools", "expect-file", "retry", "timeout", "on-error",
}
_CONTROL_TAGS = {"step", "set", "seq", "if", "while", "each", "parallel", "replan"}
_REPLAN_ATTRS = {"id", "role", "model", "effort", "max-steps", "outputs",
                 "retry", "timeout", "on-error"}


def parse_file(path: str | Path) -> model.Workflow:
    path = Path(path)
    try:
        tree = ET.parse(path)
    except ET.ParseError as e:
        raise ParseError(f"{path}: XML syntax error: {e}") from e
    return _parse_workflow(tree.getroot(), base_dir=path.parent)


def parse_string(text: str, base_dir: str | Path = ".") -> model.Workflow:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ParseError(f"XML syntax error: {e}") from e
    return _parse_workflow(root, base_dir=Path(base_dir))


def _err(el: ET.Element, msg: str) -> ParseError:
    return ParseError(f"<{el.tag}>: {msg}")


def _check_attrs(el: ET.Element, allowed: set[str]):
    unknown = set(el.attrib) - allowed
    if unknown:
        hint = " (renamed: use role= instead of agent=)" if "agent" in unknown else ""
        raise _err(el, f"unknown attribute(s): {', '.join(sorted(unknown))}{hint}")


def _require(el: ET.Element, name: str) -> str:
    value = el.get(name)
    if value is None or value == "":
        raise _err(el, f"required attribute '{name}' is missing")
    return value


def _int_attr(el: ET.Element, name: str, default: int | None = None) -> int:
    raw = el.get(name)
    if raw is None:
        if default is None:
            raise _err(el, f"required attribute '{name}' is missing")
        return default
    try:
        return int(raw)
    except ValueError:
        raise _err(el, f"attribute '{name}' must be an integer, got '{raw}'")


def _no_text(el: ET.Element):
    if (el.text or "").strip():
        raise _err(el, "unexpected text content")


def _parse_workflow(root: ET.Element, base_dir: Path) -> model.Workflow:
    if root.tag != "workflow":
        raise ParseError(f"root element must be <workflow>, got <{root.tag}>")
    _check_attrs(root, {"name", "version", "max", "budget-usd"})
    version = _require(root, "version")
    if version != "2":
        raise ParseError(f"unsupported workflow version '{version}' (expected \"2\")")

    wf = model.Workflow(
        name=_require(root, "name"),
        version=version,
        max=_int_attr(root, "max"),
        budget_usd=float(root.get("budget-usd")) if root.get("budget-usd") else None,
    )

    for el in root:
        if el.tag == "param":
            _check_attrs(el, {"name", "required", "default"})
            _no_text(el)
            wf.params.append(model.Param(
                name=_require(el, "name"),
                required=el.get("required", "false") == "true",
                default=el.get("default"),
            ))
        elif el.tag == "rules":
            _check_attrs(el, {"id", "src"})
            rid = _require(el, "id")
            if rid in wf.rules:
                raise _err(el, f"duplicate rules id '{rid}'")
            src = el.get("src")
            text = (el.text or "").strip() or None
            if src and text:
                raise _err(el, f"rules '{rid}': use either src or inline body, not both")
            if not src and not text:
                raise _err(el, f"rules '{rid}': needs src attribute or inline body")
            wf.rules[rid] = model.Rules(id=rid, src=src, text=text)
        elif el.tag in _CONTROL_TAGS:
            wf.body.children.append(_parse_node(el, base_dir))
        else:
            raise _err(el, "unknown element under <workflow>")
    return wf


def _parse_node(el: ET.Element, base_dir: Path):
    if el.tag == "step":
        return _parse_step(el, base_dir)
    if el.tag == "set":
        return _parse_set(el)
    if el.tag == "seq":
        _check_attrs(el, set())
        return _parse_block(el, base_dir)
    if el.tag == "if":
        return _parse_if(el, base_dir)
    if el.tag == "while":
        return _parse_while(el, base_dir)
    if el.tag == "each":
        return _parse_each(el, base_dir)
    if el.tag == "parallel":
        return _parse_parallel(el, base_dir)
    if el.tag == "replan":
        return _parse_replan(el)
    raise _err(el, "unknown element")


def _parse_block(el: ET.Element, base_dir: Path) -> model.Seq:
    """Parse the children of a container element (seq/then/else/do) as a Seq."""
    seq = model.Seq()
    _no_text(el)
    for child in el:
        if child.tag not in _CONTROL_TAGS:
            raise _err(child, f"unknown element under <{el.tag}>")
        seq.children.append(_parse_node(child, base_dir))
    return seq


def _parse_task_and_role(el: ET.Element, kind: str) -> tuple[str, str | None]:
    """Shared <step>/<replan> children: <task> (required) + <role> (optional).

    Enforces the role contract: at most one of role= (a named .claude/agents
    definition) or an inline <role> body. Declaring neither is allowed — the
    node then runs role-less, under the three-axis framework header. An empty
    role="" attribute is accepted as an explicit role-less declaration (every
    downstream consumer treats `role` by truthiness, not `is None`), on equal
    footing with omitting the attribute entirely.
    """
    task_el = role_el = None
    for child in el:
        if child.tag == "task":
            if task_el is not None:
                raise _err(el, "multiple <task> elements")
            task_el = child
        elif child.tag == "role":
            if role_el is not None:
                raise _err(el, "multiple <role> elements")
            role_el = child
        else:
            raise _err(child, f"only <task> and <role> are allowed inside <{kind}>")
    if task_el is None or not (task_el.text or "").strip():
        raise _err(el, f"{kind} '{el.get('id')}': <task> body is required")
    _check_attrs(task_el, set())
    role_text = None
    if role_el is not None:
        _check_attrs(role_el, set())
        role_text = (role_el.text or "").strip()
        if not role_text:
            raise _err(el, f"{kind} '{el.get('id')}': <role> body is empty")
    role_attr = el.get("role")
    if role_attr and role_text is not None:
        raise _err(el, f"{kind} '{el.get('id')}': role= (a named definition) and "
                       "an inline <role> body are mutually exclusive; use at "
                       "most one")
    return task_el.text.strip(), role_text


def _parse_step(el: ET.Element, base_dir: Path) -> model.Step:
    _check_attrs(el, _STEP_ATTRS)
    task, role_text = _parse_task_and_role(el, "step")

    schema = el.get("schema")
    if schema and schema.startswith("@"):
        schema_path = base_dir / schema[1:]
        if not schema_path.is_file():
            raise _err(el, f"schema file not found: {schema_path}")
        schema = schema_path.read_text(encoding="utf-8")
    if schema:
        try:
            json.loads(schema)
        except json.JSONDecodeError as e:
            raise _err(el, f"schema is not valid JSON: {e}")

    output_type = el.get("output-type", model.DEFAULT_OUTPUT_TYPE)
    if output_type not in model.OUTPUT_TYPES:
        raise _err(el, f"output-type must be one of {model.OUTPUT_TYPES}")
    on_error = el.get("on-error", model.DEFAULT_ON_ERROR)
    if on_error not in model.ON_ERROR_VALUES:
        raise _err(el, f"on-error must be one of {model.ON_ERROR_VALUES}")

    return model.Step(
        id=_require(el, "id"),
        task=task,
        role=el.get("role"),
        role_text=role_text,
        mode=el.get("mode"),
        model=el.get("model"),
        effort=el.get("effort"),
        output=el.get("output"),
        output_type=output_type,
        schema=schema,
        rules=[r.strip() for r in el.get("rules", "").split(",") if r.strip()],
        tools=el.get("tools"),
        expect_file=el.get("expect-file"),
        retry=_int_attr(el, "retry", model.DEFAULT_RETRY),
        timeout=_int_attr(el, "timeout", model.DEFAULT_TIMEOUT),
        on_error=on_error,
    )


def _parse_replan(el: ET.Element) -> model.Replan:
    _check_attrs(el, _REPLAN_ATTRS)
    task, role_text = _parse_task_and_role(el, "replan")
    on_error = el.get("on-error", model.DEFAULT_ON_ERROR)
    if on_error not in model.ON_ERROR_VALUES:
        raise _err(el, f"on-error must be one of {model.ON_ERROR_VALUES}")
    return model.Replan(
        id=_require(el, "id"),
        task=task,
        role=el.get("role"),
        role_text=role_text,
        model=el.get("model"),
        effort=el.get("effort"),
        max_steps=_int_attr(el, "max-steps", model.DEFAULT_REPLAN_MAX_STEPS),
        outputs=[v.strip() for v in el.get("outputs", "").split(",") if v.strip()],
        retry=_int_attr(el, "retry", model.DEFAULT_RETRY),
        timeout=_int_attr(el, "timeout", model.DEFAULT_TIMEOUT),
        on_error=on_error,
    )


def _parse_set(el: ET.Element) -> model.SetVar:
    _check_attrs(el, {"var", "value", "expr"})
    _no_text(el)
    if len(el):
        raise _err(el, "<set> takes no child elements")
    value, expr = el.get("value"), el.get("expr")
    if (value is None) == (expr is None):
        raise _err(el, "<set> requires exactly one of value= or expr=")
    return model.SetVar(var=_require(el, "var"), value=value, expr=expr)


def _cond_attrs(el: ET.Element, extra: set[str]) -> tuple[str | None, str | None, str]:
    _check_attrs(el, {"test", "ask", "ask-model"} | extra)
    test, ask = el.get("test"), el.get("ask")
    if (test is None) == (ask is None):
        raise _err(el, "requires exactly one of test= or ask=")
    if el.get("ask-model") and ask is None:
        raise _err(el, "ask-model is only valid together with ask=")
    return test, ask, el.get("ask-model", model.DEFAULT_ASK_MODEL)


def _parse_if(el: ET.Element, base_dir: Path) -> model.If:
    test, ask, ask_model = _cond_attrs(el, set())
    then = else_ = None
    _no_text(el)
    for child in el:
        if child.tag == "then":
            if then is not None:
                raise _err(el, "multiple <then>")
            then = _parse_block(child, base_dir)
        elif child.tag == "else":
            if else_ is not None:
                raise _err(el, "multiple <else>")
            else_ = _parse_block(child, base_dir)
        else:
            raise _err(child, "only <then>/<else> allowed inside <if>")
    if then is None:
        raise _err(el, "<then> is required")
    return model.If(test=test, ask=ask, then=then, else_=else_, ask_model=ask_model)


def _single_do(el: ET.Element, base_dir: Path) -> model.Seq:
    _no_text(el)
    children = list(el)
    if len(children) != 1 or children[0].tag != "do":
        raise _err(el, "exactly one <do> child is required")
    return _parse_block(children[0], base_dir)


def _parse_while(el: ET.Element, base_dir: Path) -> model.While:
    test, ask, ask_model = _cond_attrs(el, {"max"})
    return model.While(
        test=test, ask=ask, max=_int_attr(el, "max"),
        body=_single_do(el, base_dir), ask_model=ask_model,
    )


def _parse_each(el: ET.Element, base_dir: Path) -> model.Each:
    _check_attrs(el, {"items", "glob", "range", "as"})
    sources = [s for s in (el.get("items"), el.get("glob"), el.get("range")) if s is not None]
    if len(sources) != 1:
        raise _err(el, "requires exactly one of items= / glob= / range=")
    return model.Each(
        as_=_require(el, "as"),
        items=el.get("items"),
        glob=el.get("glob"),
        range_=el.get("range"),
        body=_single_do(el, base_dir),
    )


def _parse_parallel(el: ET.Element, base_dir: Path) -> model.Parallel:
    _check_attrs(el, {"max-workers"})
    _no_text(el)
    children = []
    for child in el:
        if child.tag != "step":
            raise _err(child, "only <step> is allowed inside <parallel>")
        children.append(_parse_step(child, base_dir))
    if not children:
        raise _err(el, "<parallel> requires at least one <step>")
    return model.Parallel(
        max_workers=_int_attr(el, "max-workers", model.DEFAULT_PARALLEL_WORKERS),
        children=children,
    )
