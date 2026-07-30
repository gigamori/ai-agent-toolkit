"""Unit tests for the claude-independent core: parser, interp, lint.

Run: python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun import interp, lint, model, parser  # noqa: E402

MINIMAL = """
<workflow name="t" version="2" max="10">
  <param name="env" default="prod"/>
  <rules id="r1">RULE-BODY</rules>
  <step id="s1" role="worker" output="data_path">
    <task>Save the {env} data to output/data.csv.</task>
  </step>
  <if test="'{env}' == 'prod'">
    <then>
      <step id="s2" role="worker" rules="r1">
        <task>Verify {data_path}.</task>
      </step>
    </then>
    <else>
      <set var="skipped" value="true"/>
    </else>
  </if>
  <each range="3" as="i">
    <do>
      <step id="s3" role="worker">
        <task>Process batch {i} (index {i_index}).</task>
      </step>
    </do>
  </each>
  <while test="int({count}) &lt; 2" max="5">
    <do>
      <set var="count" expr="{count} + 1"/>
    </do>
  </while>
</workflow>
"""


class TestParser(unittest.TestCase):
    def parse(self, text):
        return parser.parse_string(text)

    def test_minimal_roundtrip(self):
        wf = self.parse(MINIMAL.replace(
            '<while test="int({count}) &lt; 2"',
            '<set var="count" value="0"/><while test="int({count}) &lt; 2"'))
        self.assertEqual(wf.name, "t")
        self.assertEqual(wf.max, 10)
        self.assertEqual([s.id for s in wf.iter_steps()], ["s1", "s2", "s3"])
        self.assertEqual(wf.rules["r1"].text, "RULE-BODY")

    def test_unknown_element_rejected(self):
        with self.assertRaises(parser.ParseError):
            self.parse('<workflow name="t" version="2" max="1"><foo/></workflow>')

    def test_unknown_attribute_rejected(self):
        with self.assertRaises(parser.ParseError):
            self.parse('<workflow name="t" version="2" max="1">'
                       '<step id="a" role="w" session="new"><task>x</task></step>'
                       '</workflow>')

    def test_step_requires_id_and_task(self):
        for bad in (
            '<step role="w"><task>x</task></step>',            # no id
            '<step id="a" role="w"/>',                         # no task
            '<step id="a" role="w"><role>r</role><task>x</task></step>',  # both forms
            '<step id="a"><role>  </role><task>x</task></step>',          # empty inline
        ):
            with self.assertRaises(parser.ParseError, msg=bad):
                self.parse(f'<workflow name="t" version="2" max="1">{bad}</workflow>')

    def test_step_role_is_optional(self):
        """Neither role= nor an inline <role> — a role-less step is valid."""
        wf = self.parse('<workflow name="t" version="2" max="1">'
                        '<step id="a" mode="execute"><task>x</task></step></workflow>')
        step = next(iter(wf.iter_steps()))
        self.assertIsNone(step.role)
        self.assertIsNone(step.role_text)
        self.assertIsNone(model.role_label(step))

    def test_step_empty_role_attr_is_role_less(self):
        """role="" is an explicit role-less declaration, not a parse error —
        equivalent to omitting role= entirely (spec.md, "Role is optional")."""
        wf = self.parse('<workflow name="t" version="2" max="1">'
                        '<step id="a" role="" mode="execute"><task>x</task></step>'
                        '</workflow>')
        step = next(iter(wf.iter_steps()))
        self.assertFalse(step.role)
        self.assertIsNone(step.role_text)
        self.assertIsNone(model.role_label(step))
        findings = lint.lint(wf, check_roles=False)
        self.assertEqual([f.code for f in findings if f.level == "error"], [])

    def test_replan_empty_role_attr_is_role_less(self):
        wf = self.parse('<workflow name="t" version="2" max="1">'
                        '<replan id="r1" role=""><task>t</task></replan>'
                        '</workflow>')
        node = next(iter(wf.iter_steps()))
        self.assertFalse(node.role)
        self.assertIsNone(node.role_text)
        self.assertIsNone(model.role_label(node))

    def test_step_inline_role_and_mode(self):
        wf = self.parse('<workflow name="t" version="2" max="1">'
                        '<step id="a" mode="execute">'
                        '<role>PERSONA</role><task>x</task></step></workflow>')
        step = next(iter(wf.iter_steps()))
        self.assertIsNone(step.role)
        self.assertEqual(step.role_text, "PERSONA")
        self.assertEqual(step.mode, "execute")

    def test_if_requires_exactly_one_condition(self):
        for attrs in ('', 'test="1" ask="q"'):
            with self.assertRaises(parser.ParseError, msg=attrs):
                self.parse(f'<workflow name="t" version="2" max="1">'
                           f'<if {attrs}><then/></if></workflow>')

    def test_while_requires_max(self):
        with self.assertRaises(parser.ParseError):
            self.parse('<workflow name="t" version="2" max="1">'
                       '<while test="1"><do/></while></workflow>')

    def test_set_value_expr_exclusive(self):
        for attrs in ('var="x"', 'var="x" value="1" expr="2"'):
            with self.assertRaises(parser.ParseError, msg=attrs):
                self.parse(f'<workflow name="t" version="2" max="1">'
                           f'<set {attrs}/></workflow>')

    def test_version_required(self):
        with self.assertRaises(parser.ParseError):
            self.parse('<workflow name="t" max="1"/>')

    def test_invalid_schema_json(self):
        with self.assertRaises(parser.ParseError):
            self.parse('<workflow name="t" version="2" max="1">'
                       '<step id="a" role="w" schema="{bad json"><task>x</task></step>'
                       '</workflow>')

    def test_parallel_only_steps(self):
        with self.assertRaises(parser.ParseError):
            self.parse('<workflow name="t" version="2" max="1">'
                       '<parallel><set var="x" value="1"/></parallel></workflow>')


class TestInterp(unittest.TestCase):
    def test_interpolate(self):
        self.assertEqual(interp.interpolate("a {x} b", {"x": 1}), "a 1 b")

    def test_undefined_raises(self):
        with self.assertRaises(interp.InterpError):
            interp.interpolate("{missing}", {})

    def test_json_braces_untouched(self):
        text = '{"key": "value", "n": 1}'
        self.assertEqual(interp.interpolate(text, {}), text)

    def test_escaped_braces(self):
        self.assertEqual(interp.interpolate("{{x}}", {"x": 1}), "{x}")

    def test_safe_eval_arithmetic(self):
        self.assertEqual(interp.safe_eval("{n} + 1", {"n": "41"}), 42)

    def test_safe_eval_string_compare(self):
        self.assertTrue(interp.safe_eval("'{s}' == 'ok'", {"s": "ok"}))

    def test_safe_eval_rejects_dunder_and_calls(self):
        for expr in ("__import__('os')", "().__class__", "open('/etc/passwd')",
                     "[x for x in []]"):
            with self.assertRaises(interp.InterpError, msg=expr):
                interp.safe_eval(expr, {})

    def test_check_expr_syntax(self):
        self.assertIsNone(interp.check_expr_syntax("int({n}) > 3"))
        self.assertIsNotNone(interp.check_expr_syntax("open({f})"))


class TestModelMap(unittest.TestCase):
    def test_resolution_per_runner(self):
        import json as _json
        import tempfile
        from wfrun import modelmap
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "mm.json"
            path.write_text(_json.dumps(
                {"cc": {"opus": "big-local"}, "llm": {"opus": "gpt-5-high"}}),
                encoding="utf-8")
            self.assertEqual(modelmap.resolve("opus", "cc", path), "big-local")
            self.assertEqual(modelmap.resolve("opus", "llm", path), "gpt-5-high")
            # unmapped names and None pass through
            self.assertEqual(modelmap.resolve("sonnet", "cc", path), "sonnet")
            self.assertIsNone(modelmap.resolve(None, "cc", path))
            # missing file = identity; broken file = loud error
            self.assertEqual(
                modelmap.resolve("opus", "cc", Path(d) / "absent.json"), "opus")
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(modelmap.ModelMapError):
                modelmap.resolve("opus", "cc", path)

    def test_bundled_map_is_identity(self):
        from wfrun import modelmap
        for name in modelmap.CANONICAL_MODELS:
            for runner in modelmap.RUNNERS:
                self.assertEqual(modelmap.resolve(name, runner), name)


class TestViz(unittest.TestCase):
    XML = """
<workflow name="t" version="2" max="20">
  <step id="s1" role="w" mode="survey" model="haiku" output="p" expect-file="out.csv">
    <task>SECRET-TASK-TEXT make data</task>
  </step>
  <if test="int({p}) > 3">
    <then><step id="s2" role="w"><task>x</task></step></then>
    <else><set var="sk" value="1"/></else>
  </if>
  <while test="'{sk}' == '1'" max="3">
    <do><set var="sk" value="0"/></do>
  </while>
  <parallel>
    <step id="pa" role="w"><task>a</task></step>
    <step id="pb" role="w"><task>b</task></step>
  </parallel>
  <each range="2" as="i">
    <do><step id="s3" role="w"><task>t {i}</task></step></do>
  </each>
  <replan id="r1" role="b" outputs="extra"><task>plan it</task></replan>
</workflow>
"""

    def test_flowchart_covers_all_constructs(self):
        from wfrun import viz
        wf = parser.parse_string(self.XML)
        out = viz.mermaid(wf)
        self.assertTrue(out.startswith("flowchart TD"))
        for token in ("<b>s1</b>", "mode=survey", "model=haiku",
                      "expect: out.csv",           # step facts
                      "|yes|", "|no|",             # if branches
                      "while", "|done|",           # while + loop exit
                      "subgraph", "max-workers",   # parallel
                      "each i in range 2",         # each
                      "<b>replan r1</b>", "stroke-dasharray"):
            self.assertIn(token, out, token)
        # control-plane only: task bodies never leak into the diagram
        self.assertNotIn("SECRET-TASK-TEXT", out)
        # deterministic: same model -> identical text
        self.assertEqual(out, viz.mermaid(parser.parse_string(self.XML)))

    def test_empty_workflow_connects_start_end(self):
        from wfrun import viz
        wf = parser.parse_string('<workflow name="t" version="2" max="1"></workflow>')
        out = viz.mermaid(wf)
        self.assertIn("S --> E", out)


class TestLint(unittest.TestCase):
    def lint(self, text):
        wf = parser.parse_string(text)
        return lint.lint(wf, check_roles=False)

    def wrap(self, inner):
        return f'<workflow name="t" version="2" max="10">{inner}</workflow>'

    def codes(self, findings, level=None):
        return [f.code for f in findings if level is None or f.level == level]

    def test_clean_workflow(self):
        findings = self.lint(self.wrap(
            '<set var="count" value="0"/>' + '''
            <step id="s1" role="w" output="p"><task>make {count}</task></step>
            <if test="int({count}) > 0"><then>
              <step id="s2" role="w"><task>use {p}</task></step>
            </then></if>'''))
        self.assertEqual(self.codes(findings, "error"), [])

    def test_undefined_variable(self):
        findings = self.lint(self.wrap(
            '<step id="s1" role="w"><task>use {nope}</task></step>'))
        self.assertIn("var-undefined", self.codes(findings, "error"))

    def test_branch_only_definition_warns(self):
        findings = self.lint(self.wrap('''
            <set var="c" value="1"/>
            <if test="int({c}) > 0">
              <then><step id="s1" role="w" output="p"><task>x</task></step></then>
            </if>
            <step id="s2" role="w"><task>use {p}</task></step>'''))
        self.assertIn("var-maybe-undefined", self.codes(findings, "warn"))
        self.assertNotIn("var-undefined", self.codes(findings, "error"))

    def test_duplicate_step_id(self):
        findings = self.lint(self.wrap(
            '<step id="a" role="w"><task>x</task></step>'
            '<step id="a" role="w"><task>y</task></step>'))
        self.assertIn("duplicate-id", self.codes(findings, "error"))

    def test_undefined_rules_ref(self):
        findings = self.lint(self.wrap(
            '<step id="a" role="w" rules="nope"><task>x</task></step>'))
        self.assertIn("rules-undefined", self.codes(findings, "error"))

    def test_parallel_conflict_and_cross_ref(self):
        findings = self.lint(self.wrap('''
            <parallel>
              <step id="a" role="w" output="o"><task>x</task></step>
              <step id="b" role="w" output="o"><task>use {o}</task></step>
            </parallel>'''))
        codes = self.codes(findings, "error")
        self.assertIn("parallel-output-conflict", codes)
        self.assertIn("var-undefined", codes)  # {o} not visible inside parallel

    def test_each_loop_var_scoped(self):
        findings = self.lint(self.wrap('''
            <each range="2" as="i">
              <do><step id="a" role="w"><task>{i}/{i_index}</task></step></do>
            </each>
            <step id="b" role="w"><task>after {i}</task></step>'''))
        self.assertIn("var-undefined", self.codes(findings, "error"))

    def test_bad_expr(self):
        findings = self.lint(self.wrap(
            '<if test="open({x})"><then></then></if>'))
        self.assertIn("bad-expr", self.codes(findings, "error"))

    def test_replan_parses_and_flows_outputs(self):
        wf = parser.parse_string(self.wrap(
            '<replan id="r1" role="builder" max-steps="8" outputs="a,b">'
            '<task>plan it</task></replan>'
            '<step id="s1" role="w"><task>use {a} and {b}</task></step>'))
        node = next(iter(wf.iter_steps()))
        self.assertEqual((node.id, node.max_steps, node.outputs), ("r1", 8, ["a", "b"]))
        findings = lint.lint(wf, check_roles=False)
        self.assertEqual(self.codes(findings, "error"), [])

    def test_replan_requires_task(self):
        for bad in ('<replan id="r1" role="b"/>',
                    # mode= is deliberately not a <replan> attribute
                    '<replan id="r1" role="b" mode="plan"><task>t</task></replan>'):
            with self.assertRaises(parser.ParseError, msg=bad):
                parser.parse_string(self.wrap(bad))

    def test_replan_role_is_optional(self):
        wf = parser.parse_string(self.wrap(
            '<replan id="r1"><task>t</task></replan>'))
        node = next(iter(wf.iter_steps()))
        self.assertIsNone(node.role)
        self.assertIsNone(node.role_text)
        self.assertIsNone(model.role_label(node))

    def test_mode_unknown(self):
        findings = self.lint(self.wrap(
            '<step id="s1" role="w" mode="nonsense"><task>x</task></step>'))
        self.assertIn("mode-unknown", self.codes(findings, "error"))

    def test_mode_known_and_aliases(self):
        for mode in ("execute", "survey", "plan", "debug", "review",
                     "review-dev", "verify", "implement"):
            findings = self.lint(self.wrap(
                f'<step id="s1" role="w" mode="{mode}"><task>x</task></step>'))
            self.assertEqual(self.codes(findings, "error"), [], msg=mode)

    def test_interactive_modes_rejected(self):
        # ask/brainstorm/discuss/organize need a live human exchange and are
        # not bundled; a batch step must not reference them.
        for mode in ("ask", "brainstorm", "discuss", "organize"):
            findings = self.lint(self.wrap(
                f'<step id="s1" role="w" mode="{mode}"><task>x</task></step>'))
            self.assertIn("mode-unknown", self.codes(findings, "error"), msg=mode)

    def test_expect_file_parsed_and_var_checked(self):
        wf = parser.parse_string(self.wrap(
            '<step id="s1" role="w" output="dir" output-type="value">'
            '<task>pick a dir</task></step>'
            '<step id="s2" role="w" expect-file="{dir}/out.csv,report.md">'
            '<task>make files</task></step>'))
        self.assertEqual(next(s.expect_file for s in wf.iter_steps()
                              if s.id == "s2"), "{dir}/out.csv,report.md")
        self.assertEqual(self.codes(lint.lint(wf, check_roles=False), "error"), [])
        findings = self.lint(self.wrap(
            '<step id="s1" role="w" expect-file="{nope}/x"><task>t</task></step>'))
        self.assertIn("var-undefined", self.codes(findings, "error"))

    def test_expect_file_cannot_use_own_output(self):
        # the check runs before the output variable is committed
        findings = self.lint(self.wrap(
            '<step id="s1" role="w" output="p" expect-file="{p}">'
            '<task>t</task></step>'))
        self.assertIn("var-undefined", self.codes(findings, "error"))

    def test_model_not_canonical_warns(self):
        findings = self.lint(self.wrap(
            '<step id="s1" role="w" model="gpt-5"><task>x</task></step>'
            '<step id="s2" role="w" model="sonnet"><task>y</task></step>'))
        warns = [f for f in findings if f.code == "model-not-canonical"]
        self.assertEqual(len(warns), 1)
        self.assertIn("s1", warns[0].message)

    def test_mode_write_tools_warns(self):
        findings = self.lint(self.wrap(
            '<step id="s1" role="w" mode="survey" tools="Read,Bash(git:*)">'
            '<task>x</task></step>'
            '<step id="s2" role="w" mode="survey" tools="Read,Grep">'
            '<task>y</task></step>'
            '<step id="s3" role="w" mode="execute" tools="Read,Write">'
            '<task>z</task></step>'))
        warns = [f for f in findings if f.code == "mode-write-tools"]
        self.assertEqual(len(warns), 1)
        self.assertIn("s1", warns[0].message)

    def test_tools_not_inherited_warns(self):
        """Fires whenever no named role can supply tools and tools= is unset —
        for an inline role (s1) and for a role-less step (s3) alike."""
        findings = self.lint(self.wrap(
            '<step id="s1"><role>persona</role><task>x</task></step>'
            '<step id="s2" tools="Read"><role>persona</role><task>y</task></step>'
            '<step id="s3"><task>z</task></step>'
            '<step id="s4" role="w"><task>w</task></step>'))
        warns = [f.message for f in findings if f.code == "tools-not-inherited"]
        self.assertEqual(len(warns), 2)
        self.assertTrue(any("s1" in m for m in warns))
        self.assertTrue(any("s3" in m for m in warns))

    def test_role_less_step_is_clean(self):
        """A role-less step raises no error — only the tools= warning."""
        findings = self.lint(self.wrap(
            '<step id="s1" mode="survey" tools="Read"><task>x</task></step>'))
        self.assertEqual(self.codes(findings, "error"), [])
        self.assertEqual(self.codes(findings, "warn"), [])

    def test_agent_attr_rename_hint(self):
        with self.assertRaises(parser.ParseError) as ctx:
            parser.parse_string(self.wrap(
                '<step id="s1" agent="w"><task>x</task></step>'))
        self.assertIn("use role= instead of agent=", str(ctx.exception))

    def test_role_missing_only_for_named(self):
        wf = parser.parse_string(self.wrap(
            '<step id="s1" role="ghost"><task>x</task></step>'
            '<step id="s2"><role>inline persona</role><task>y</task></step>'))
        findings = lint.lint(wf, check_roles=True)
        errors = [f for f in findings if f.code == "role-missing"]
        self.assertEqual(len(errors), 1)
        self.assertIn("s1", errors[0].message)

    def test_as_child_forbids_replan_and_param(self):
        wf = parser.parse_string(
            '<workflow name="c" version="2" max="5"><param name="p"/>'
            '<replan id="r" role="b"><task>t</task></replan></workflow>')
        codes = self.codes(lint.lint(wf, check_roles=False, as_child=True), "error")
        self.assertIn("replan-forbidden", codes)
        self.assertIn("param-forbidden", codes)

    def test_defined_vars_seed(self):
        wf = parser.parse_string(self.wrap(
            '<step id="s1" role="w"><task>use {inherited}</task></step>'))
        self.assertIn("var-undefined",
                      self.codes(lint.lint(wf, check_roles=False), "error"))
        self.assertEqual(
            self.codes(lint.lint(wf, check_roles=False,
                                 defined_vars={"inherited"}), "error"), [])

    def test_max_too_small_warn(self):
        text = ('<workflow name="t" version="2" max="1">'
                '<step id="a" role="w"><task>x</task></step>'
                '<step id="b" role="w"><task>y</task></step></workflow>')
        wf = parser.parse_string(text)
        findings = lint.lint(wf, check_roles=False)
        self.assertIn("max-too-small", self.codes(findings, "warn"))


if __name__ == "__main__":
    unittest.main()
