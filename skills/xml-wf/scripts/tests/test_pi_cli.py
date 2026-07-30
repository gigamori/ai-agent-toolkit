"""Tests for wfrun ask's Pi backend (pi_cli.py; mode-orchestrator-runs/
phase5-item1-cc-inventory-design.md) and for run-pi (pi_cli.py;
mode-orchestrator-runs/phase6-run-pi-design.md).

subprocess.run and shutil.which are monkeypatched; no real pi CLI is invoked
-- same pattern as test_claude_cli.py."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun import parser, pi_cli  # noqa: E402
from wfrun.executor import WorkflowFailure  # noqa: E402


def _fake_completed(returncode=0, stdout="", stderr=""):
    proc = mock.Mock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


class ResolvePiLauncherTests(unittest.TestCase):
    def setUp(self):
        pi_cli._resolution_cache.clear()

    def tearDown(self):
        pi_cli._resolution_cache.clear()

    def test_not_on_path_is_none(self):
        with mock.patch.object(pi_cli.shutil, "which", return_value=None):
            self.assertIsNone(pi_cli.resolve_pi_launcher())

    def test_real_executable_launches_directly(self):
        with mock.patch.object(pi_cli.shutil, "which",
                               return_value="/usr/local/bin/pi"):
            self.assertEqual(pi_cli.resolve_pi_launcher(), ["/usr/local/bin/pi"])

    def test_windows_shim_is_bypassed_via_node(self):
        shim = r"C:\npm\pi.CMD"
        entry = os.path.join(r"C:\npm", *pi_cli._NPM_ENTRY)

        def fake_which(name):
            return {"pi": shim, "node": r"C:\nodejs\node.exe"}.get(name)

        with mock.patch.object(pi_cli.shutil, "which", side_effect=fake_which), \
             mock.patch.object(pi_cli.os.path, "isfile", return_value=True):
            self.assertEqual(pi_cli.resolve_pi_launcher(),
                             [r"C:\nodejs\node.exe", entry])

    def test_shim_without_resolvable_entry_fails_closed(self):
        # Refusing beats launching through the shim: it silently truncates a
        # multi-line prompt at the first newline (measured).
        def fake_which(name):
            return {"pi": r"C:\npm\pi.CMD", "node": r"C:\nodejs\node.exe"}.get(name)

        with mock.patch.object(pi_cli.shutil, "which", side_effect=fake_which), \
             mock.patch.object(pi_cli.os.path, "isfile", return_value=False):
            self.assertIsNone(pi_cli.resolve_pi_launcher())

    def test_shim_without_node_fails_closed(self):
        def fake_which(name):
            return {"pi": r"C:\npm\pi.CMD"}.get(name)

        with mock.patch.object(pi_cli.shutil, "which", side_effect=fake_which), \
             mock.patch.object(pi_cli.os.path, "isfile", return_value=True):
            self.assertIsNone(pi_cli.resolve_pi_launcher())


class AskLlmPiTests(unittest.TestCase):
    def setUp(self):
        pi_cli._resolution_cache.clear()
        self.which_patch = mock.patch.object(
            pi_cli.shutil, "which", return_value="/usr/local/bin/pi")
        self.which_patch.start()

    def tearDown(self):
        self.which_patch.stop()
        pi_cli._resolution_cache.clear()

    def test_unlaunchable_fails_without_subprocess_call(self):
        pi_cli._resolution_cache.clear()
        with mock.patch.object(pi_cli.shutil, "which", return_value=None):
            with mock.patch.object(pi_cli.subprocess, "run") as run:
                answer, reason, cost = pi_cli.ask_llm_pi("q?")
        run.assert_not_called()
        self.assertIsNone(answer)
        self.assertEqual(cost, 0.0)
        self.assertIn("not launchable", reason)

    def test_success_path_pure_json(self):
        stdout = '{"answer": true, "reason": "because"}'
        with mock.patch.object(pi_cli.subprocess, "run",
                               return_value=_fake_completed(0, stdout)):
            answer, reason, cost = pi_cli.ask_llm_pi("q?")
        self.assertTrue(answer)
        self.assertEqual(reason, "because")
        self.assertEqual(cost, 0.0)

    def test_success_path_json_wrapped_in_code_fence(self):
        # This is the shape the model actually returns (measured), not a
        # hypothetical -- the brace-extraction pass is load bearing.
        stdout = '```json\n{"answer": false, "reason": "nope"}\n```\n'
        with mock.patch.object(pi_cli.subprocess, "run",
                               return_value=_fake_completed(0, stdout)):
            answer, reason, cost = pi_cli.ask_llm_pi("q?")
        self.assertFalse(answer)
        self.assertEqual(reason, "nope")

    def test_prompt_is_a_positional_argument_not_an_at_file(self):
        # `@file` attaches the file as content to reason about, not as the
        # turn's instruction -- a probe using it got a prompt-injection
        # refusal. The prompt must ride on argv.
        captured = {}

        def capture(cmd, **kwargs):
            captured["cmd"] = cmd
            return _fake_completed(0, '{"answer": true, "reason": "x"}')

        with mock.patch.object(pi_cli.subprocess, "run", side_effect=capture):
            pi_cli.ask_llm_pi("is the sky blue?")
        cmd = captured["cmd"]
        self.assertFalse(any(str(a).startswith("@") for a in cmd),
                         f"no argv entry may use @file include syntax: {cmd}")
        self.assertIn("is the sky blue?", cmd[-1])
        self.assertEqual(cmd[-2], "haiku")

    def test_first_attempt_malformed_second_succeeds(self):
        bad = _fake_completed(0, "not json at all")
        good = _fake_completed(0, '{"answer": true, "reason": "ok"}')
        with mock.patch.object(pi_cli.subprocess, "run",
                               side_effect=[bad, good]) as run:
            answer, reason, cost = pi_cli.ask_llm_pi("q?")
        self.assertEqual(run.call_count, 2)
        self.assertTrue(answer)
        self.assertEqual(reason, "ok")

    def test_always_malformed_gives_up_after_two_attempts(self):
        bad = _fake_completed(0, "still not json")
        with mock.patch.object(pi_cli.subprocess, "run",
                               side_effect=[bad, bad]) as run:
            answer, reason, cost = pi_cli.ask_llm_pi("q?")
        self.assertEqual(run.call_count, 2)
        self.assertIsNone(answer)

    def test_timeout_reports_failure(self):
        with mock.patch.object(
                pi_cli.subprocess, "run",
                side_effect=subprocess.TimeoutExpired(cmd="pi", timeout=300)):
            answer, reason, cost = pi_cli.ask_llm_pi("q?", timeout=300)
        self.assertIsNone(answer)
        self.assertIn("timeout", reason)

    def test_nonzero_exit_is_treated_as_failure_and_retried(self):
        fail = _fake_completed(1, "", "some pi error")
        with mock.patch.object(pi_cli.subprocess, "run",
                               side_effect=[fail, fail]) as run:
            answer, reason, cost = pi_cli.ask_llm_pi("q?")
        self.assertEqual(run.call_count, 2)
        self.assertIsNone(answer)

    def test_stdin_is_closed(self):
        # An open stdin pipe makes `pi -p` block forever before dispatch.
        captured = {}

        def capture(cmd, **kwargs):
            captured.update(kwargs)
            return _fake_completed(0, '{"answer": true, "reason": "x"}')

        with mock.patch.object(pi_cli.subprocess, "run", side_effect=capture):
            pi_cli.ask_llm_pi("q?")
        self.assertEqual(captured["stdin"], subprocess.DEVNULL)


def _turn_end(**message) -> str:
    """One JSONL line: a turn_end event wrapping the given message fields."""
    return json.dumps({"type": "turn_end", "message": message})


def _jsonl(*lines: str) -> str:
    return "\n".join(lines) + "\n"


class ConvertToolsTests(unittest.TestCase):
    """_convert_tools(): TOOL_NAME_MAP (design phase6-run-pi-design.md §4.2)."""

    def test_glob_converts_to_find(self):
        converted, error, warnings = pi_cli._convert_tools("Glob")
        self.assertIsNone(error)
        self.assertEqual(converted, "find")
        self.assertEqual(warnings, [])

    def test_multiple_names_all_converted(self):
        converted, error, warnings = pi_cli._convert_tools("Read,Write,Glob")
        self.assertIsNone(error)
        self.assertEqual(converted, "read,write,find")
        self.assertEqual(warnings, [])

    def test_unconvertible_name_is_rejected(self):
        converted, error, warnings = pi_cli._convert_tools("Read,MultiEdit")
        self.assertIsNone(converted)
        self.assertIn("MultiEdit", error)
        self.assertEqual(warnings, [])

    def test_unknown_name_is_rejected(self):
        converted, error, warnings = pi_cli._convert_tools("Frobnicate")
        self.assertIsNone(converted)
        self.assertIn("Frobnicate", error)
        self.assertEqual(warnings, [])

    def test_empty_after_conversion_is_rejected(self):
        converted, error, warnings = pi_cli._convert_tools(" , ,")
        self.assertIsNone(converted)
        self.assertIn("empty", error)
        self.assertEqual(warnings, [])

    def test_specifier_is_stripped_to_bare_tool_name(self):
        # design phase6 review point 3, 2026-07-30: pi has no per-command
        # tool matching, so a CC-style argument specifier is widened to the
        # whole tool rather than rejected (rejecting would lose access to a
        # tool the workflow genuinely needs, e.g. git).
        converted, error, warnings = pi_cli._convert_tools("Bash(git:*)")
        self.assertIsNone(error)
        self.assertEqual(converted, "bash")
        self.assertEqual(len(warnings), 1)
        self.assertIn("Bash(git:*)", warnings[0])
        self.assertIn("bash", warnings[0])

    def test_specifier_and_bare_form_do_not_duplicate(self):
        converted, error, warnings = pi_cli._convert_tools("Bash(git:*),Bash")
        self.assertIsNone(error)
        self.assertEqual(converted, "bash")
        self.assertEqual(len(warnings), 1)

    def test_two_different_specifiers_same_tool_collapse_and_both_warn(self):
        converted, error, warnings = pi_cli._convert_tools(
            "Bash(git:*),Bash(npm:*)")
        self.assertIsNone(error)
        self.assertEqual(converted, "bash")
        self.assertEqual(len(warnings), 2)

    def test_unknown_leading_name_with_specifier_still_rejected(self):
        converted, error, warnings = pi_cli._convert_tools("MultiEdit(foo:*)")
        self.assertIsNone(converted)
        self.assertIn("MultiEdit", error)
        self.assertEqual(warnings, [])

    def test_no_specifier_produces_no_warnings(self):
        converted, error, warnings = pi_cli._convert_tools("Read,Write")
        self.assertIsNone(error)
        self.assertEqual(warnings, [])


class PiToolWideningNotesTests(unittest.TestCase):
    """pi_tool_widening_notes(): preflight surfacing of _convert_tools()'s
    specifier-widening warnings (design phase6 review point 3, 2026-07-30).

    Workflows are built through parser.parse_string(), same convention as
    test_executor.py's wrap(), rather than hand-constructed model.Workflow/
    model.Step dataclasses -- Workflow has no steps= kwarg (it holds a Seq
    body; iter_steps() walks that tree), so going through the real parser
    is both simpler and avoids drifting from the actual field layout."""

    def wrap(self, inner):
        return f'<workflow name="t" version="2" max="20">{inner}</workflow>'

    def _step_xml(self, step_id, tools=None):
        attr = f' tools="{tools}"' if tools else ""
        return f'<step id="{step_id}"{attr}><role>R</role><task>t</task></step>'

    def test_step_with_specifier_produces_one_note(self):
        wf = parser.parse_string(self.wrap(
            self._step_xml("s1", tools="Bash(git:*)")))
        notes = pi_cli.pi_tool_widening_notes(wf, {})
        self.assertEqual(len(notes), 1)
        self.assertIn("s1", notes[0])
        self.assertIn("Bash(git:*)", notes[0])

    def test_step_without_tools_produces_no_note(self):
        wf = parser.parse_string(self.wrap(self._step_xml("s1")))
        notes = pi_cli.pi_tool_widening_notes(wf, {})
        self.assertEqual(notes, [])

    def test_step_with_bare_tools_produces_no_note(self):
        wf = parser.parse_string(self.wrap(
            self._step_xml("s1", tools="Read,Write")))
        notes = pi_cli.pi_tool_widening_notes(wf, {})
        self.assertEqual(notes, [])

    def test_multiple_steps_each_get_their_own_note(self):
        wf = parser.parse_string(self.wrap(
            self._step_xml("s1", tools="Bash(git:*)")
            + self._step_xml("s2", tools="Bash(npm:*)")))
        notes = pi_cli.pi_tool_widening_notes(wf, {})
        self.assertEqual(len(notes), 2)
        self.assertTrue(any("s1" in n for n in notes))
        self.assertTrue(any("s2" in n for n in notes))


class SumUsageTests(unittest.TestCase):
    """_sum_usage(): per-turn_end usage is incremental, not cumulative
    (design phase6 review point 5, 2026-07-30) -- summed here rather than
    read from the terminal turn_end alone."""

    def _te(self, usage):
        return {"message": {"usage": usage}}

    def test_single_turn_end_passes_through(self):
        total = pi_cli._sum_usage([self._te(
            {"input": 10, "output": 2, "cacheRead": 1, "cacheWrite": 0,
             "totalTokens": 13, "cost": {"input": 0.1, "output": 0.2,
                                        "cacheRead": 0, "cacheWrite": 0,
                                        "total": 0.3}})])
        self.assertEqual(total["input"], 10)
        self.assertEqual(total["totalTokens"], 13)
        self.assertEqual(total["cost"]["total"], 0.3)

    def test_multiple_turn_ends_sum_field_by_field(self):
        total = pi_cli._sum_usage([
            self._te({"input": 10, "output": 1, "totalTokens": 11,
                      "cost": {"total": 0.001}}),
            self._te({"input": 20, "output": 2, "totalTokens": 22,
                      "cost": {"total": 0.002}}),
        ])
        self.assertEqual(total["input"], 30)
        self.assertEqual(total["output"], 3)
        self.assertEqual(total["totalTokens"], 33)
        self.assertAlmostEqual(total["cost"]["total"], 0.003)

    def test_missing_fields_default_to_zero_not_an_error(self):
        total = pi_cli._sum_usage([self._te({}), self._te({"cost": {}})])
        self.assertEqual(total["input"], 0)
        self.assertEqual(total["cost"]["total"], 0)

    def test_empty_list_is_all_zero(self):
        total = pi_cli._sum_usage([])
        self.assertEqual(total["totalTokens"], 0)
        self.assertEqual(total["cost"]["total"], 0)

    def test_missing_message_or_usage_key_defaults_to_zero(self):
        total = pi_cli._sum_usage([{}, {"message": {}}])
        self.assertEqual(total["input"], 0)
        self.assertEqual(total["cost"]["total"], 0)


class ClassifyResultPiTests(unittest.TestCase):
    """classify_result_pi(): the full classification table (design §4.1)."""

    def test_missing_turn_end_is_behavioral(self):
        stdout = _jsonl(json.dumps({"type": "session"}),
                        json.dumps({"type": "agent_start"}))
        res = pi_cli.classify_result_pi(0, stdout, "")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "behavioral")
        self.assertEqual(res.raw["last_event_type"], "agent_start")

    def test_terminal_turn_end_wins_over_tool_round_trips(self):
        # Measured 2026-07-30: a tool-using step emits one turn_end per agent
        # loop iteration. The intermediate ones carry stopReason=toolUse and
        # an empty content list; only the last carries the reply. Reading the
        # first turn_end classified every tool-using step as `empty result` --
        # the real E2E failure this test locks down.
        stdout = _jsonl(
            json.dumps({"type": "session"}),
            _turn_end(role="assistant", content=[], stopReason="toolUse"),
            json.dumps({"type": "tool_execution_start"}),
            _turn_end(role="assistant",
                      content=[{"type": "text", "text": "output/poem.txt"}],
                      stopReason="stop"),
            json.dumps({"type": "agent_settled"}))
        res = pi_cli.classify_result_pi(0, stdout, "")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.text, "output/poem.txt")

    def test_stream_cut_off_mid_tool_use_is_not_empty_result(self):
        # Last turn_end still toolUse => the loop never finished. Same empty
        # body as a genuine empty answer, but a different cause, so it must
        # not be reported as "empty result".
        stdout = _jsonl(
            _turn_end(role="assistant", content=[], stopReason="toolUse"),
            json.dumps({"type": "tool_execution_update"}))
        res = pi_cli.classify_result_pi(0, stdout, "")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "behavioral")
        self.assertIn("mid-tool-use", res.error)
        self.assertNotIn("empty result", res.error)

    def test_no_events_at_all_is_behavioral(self):
        res = pi_cli.classify_result_pi(0, "", "")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "behavioral")
        self.assertIsNone(res.raw["last_event_type"])

    def test_stop_reason_error_is_behavioral(self):
        stdout = _jsonl(_turn_end(
            content=[{"type": "text", "text": ""}],
            stopReason="error", errorMessage="invalid api key"))
        res = pi_cli.classify_result_pi(1, stdout, "")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "behavioral")
        self.assertIn("invalid api key", res.error)

    def test_stop_reason_aborted_is_behavioral(self):
        stdout = _jsonl(_turn_end(
            content=[{"type": "text", "text": "partial"}], stopReason="aborted"))
        res = pi_cli.classify_result_pi(0, stdout, "")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "behavioral")

    def test_error_prefix_is_guardrail(self):
        stdout = _jsonl(_turn_end(
            content=[{"type": "text", "text": "ERROR: boom"}], stopReason="stop"))
        res = pi_cli.classify_result_pi(0, stdout, "")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "guardrail")
        self.assertEqual(res.error, "ERROR: boom")

    def test_blocked_is_refusal(self):
        stdout = _jsonl(_turn_end(
            content=[{"type": "text",
                     "text": "[BLOCKED: mode-rule x] reason"}],
            stopReason="stop"))
        res = pi_cli.classify_result_pi(0, stdout, "")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "refusal")

    def test_empty_body_is_behavioral(self):
        stdout = _jsonl(_turn_end(content=[{"type": "text", "text": ""}],
                                  stopReason="stop"))
        res = pi_cli.classify_result_pi(0, stdout, "")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "behavioral")
        self.assertEqual(res.error, "empty result")

    def test_success_path_text_and_cost(self):
        stdout = _jsonl(_turn_end(
            content=[{"type": "text", "text": "4"}], stopReason="stop",
            usage={"input": 10, "output": 2, "cacheRead": 0, "cacheWrite": 0,
                  "totalTokens": 12, "cost": {"total": 0.00123875}},
            model="pi-claude-agent-sdk/haiku"))
        res = pi_cli.classify_result_pi(0, stdout, "")
        self.assertTrue(res.ok)
        self.assertEqual(res.text, "4")
        self.assertEqual(res.cost_usd, 0.00123875)

    def test_usage_summed_across_tool_round_trips(self):
        # design phase6 review point 5, 2026-07-30 (real values from a
        # captured tool-using step, tooluse.jsonl): turn_end usage is
        # per-iteration, not a running total, so the terminal turn_end alone
        # under-reports whenever a step uses tools.
        stdout = _jsonl(
            _turn_end(role="assistant", content=[], stopReason="toolUse",
                      usage={"input": 2247, "output": 189, "cacheRead": 0,
                            "cacheWrite": 0, "totalTokens": 2436,
                            "cost": {"input": 0, "output": 0, "cacheRead": 0,
                                    "cacheWrite": 0, "total": 0}}),
            _turn_end(role="assistant",
                      content=[{"type": "text", "text": "done"}],
                      stopReason="stop",
                      usage={"input": 2438, "output": 160, "cacheRead": 0,
                            "cacheWrite": 0, "totalTokens": 2598,
                            "cost": {"input": 0, "output": 0, "cacheRead": 0,
                                    "cacheWrite": 0, "total": 0}}))
        res = pi_cli.classify_result_pi(0, stdout, "")
        self.assertTrue(res.ok)
        self.assertEqual(res.text, "done")  # reply still from the terminal turn_end only
        self.assertEqual(res.raw["usage"]["input"], 2247 + 2438)
        self.assertEqual(res.raw["usage"]["output"], 189 + 160)
        self.assertEqual(res.raw["usage"]["totalTokens"], 2436 + 2598)

    def test_cost_usd_is_summed_not_terminal_only(self):
        stdout = _jsonl(
            _turn_end(role="assistant", content=[], stopReason="toolUse",
                      usage={"cost": {"total": 0.001}}),
            _turn_end(role="assistant",
                      content=[{"type": "text", "text": "done"}],
                      stopReason="stop",
                      usage={"cost": {"total": 0.002}}))
        res = pi_cli.classify_result_pi(0, stdout, "")
        self.assertTrue(res.ok)
        self.assertAlmostEqual(res.cost_usd, 0.003)

    def test_multiple_text_blocks_are_concatenated(self):
        stdout = _jsonl(_turn_end(
            content=[{"type": "text", "text": "hello "},
                    {"type": "text", "text": "world"}],
            stopReason="stop"))
        res = pi_cli.classify_result_pi(0, stdout, "")
        self.assertTrue(res.ok)
        self.assertEqual(res.text, "hello world")

    def test_raw_reduction_excludes_thinking_blocks_and_full_jsonl(self):
        stdout = _jsonl(
            json.dumps({"type": "message_start"}),
            _turn_end(
                content=[{"type": "thinking", "thinking": "pondering..."},
                        {"type": "text", "text": "answer"}],
                stopReason="stop", errorMessage=None,
                usage={"cost": {"total": 0.0}},
                model="pi-claude-agent-sdk/sonnet", provider="anthropic"))
        res = pi_cli.classify_result_pi(0, stdout, "")
        self.assertTrue(res.ok)
        # usage is the summed shape (_sum_usage, review point 5) -- every
        # field present at 0 even though the fixture's usage={"cost": {...}}
        # only specified "cost".
        self.assertEqual(
            res.raw,
            {"content": [{"type": "text", "text": "answer"}],
             "stopReason": "stop", "errorMessage": None,
             "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
                      "totalTokens": 0,
                      "cost": {"input": 0, "output": 0, "cacheRead": 0,
                              "cacheWrite": 0, "total": 0.0}},
             "model": "pi-claude-agent-sdk/sonnet", "provider": "anthropic"})
        # the full JSONL (message_start line, thinking block) is not in raw
        self.assertNotIn("message_start", json.dumps(res.raw))
        self.assertNotIn("pondering", json.dumps(res.raw))

    def test_stray_non_json_line_is_tolerated(self):
        stdout = "not json at all\n" + _turn_end(
            content=[{"type": "text", "text": "ok"}], stopReason="stop")
        res = pi_cli.classify_result_pi(0, stdout, "")
        self.assertTrue(res.ok)
        self.assertEqual(res.text, "ok")


class RunPiTests(unittest.TestCase):
    """run_pi(): design phase6-run-pi-design.md §4."""

    def setUp(self):
        pi_cli._resolution_cache.clear()
        self.which_patch = mock.patch.object(
            pi_cli.shutil, "which", return_value="/usr/local/bin/pi")
        self.which_patch.start()

    def tearDown(self):
        self.which_patch.stop()
        pi_cli._resolution_cache.clear()

    def _ok_completed(self):
        stdout = _jsonl(_turn_end(
            content=[{"type": "text", "text": "done"}], stopReason="stop"))
        return _fake_completed(0, stdout)

    def _patch_tree_kill(self, capture):
        """run_pi's own subprocess execution always goes through
        claude_cli._run_with_tree_kill (unconditionally -- see run_pi's
        docstring on why `kill_tree` is not honored as an off switch for
        this backend). `capture` has _run_with_tree_kill's own signature:
        (cmd, prompt, timeout, cwd)."""
        return mock.patch.object(pi_cli.claude_cli, "_run_with_tree_kill",
                                 side_effect=capture)

    def test_schema_is_rejected_without_launching(self):
        with self._patch_tree_kill(None) as tree_kill:
            res = pi_cli.run_pi("hi", schema='{"type": "object"}')
        tree_kill.assert_not_called()
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "env")
        self.assertIn("schema=", res.error)

    def test_unlaunchable_fails_without_subprocess_call(self):
        pi_cli._resolution_cache.clear()
        with mock.patch.object(pi_cli.shutil, "which", return_value=None):
            with self._patch_tree_kill(None) as tree_kill:
                res = pi_cli.run_pi("hi")
        tree_kill.assert_not_called()
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "env")
        self.assertIn("not launchable", res.error)

    def test_no_session_and_no_skills_always_in_argv(self):
        captured = {}

        def capture(cmd, prompt, timeout, cwd):
            captured["cmd"] = cmd
            return self._ok_completed()

        with self._patch_tree_kill(capture):
            pi_cli.run_pi("do the thing")
        self.assertIn("--no-session", captured["cmd"])
        self.assertIn("--no-skills", captured["cmd"])
        self.assertNotIn("--no-extensions", captured["cmd"])

    def test_effort_passed_through_unconverted(self):
        captured = {}

        def capture(cmd, prompt, timeout, cwd):
            captured["cmd"] = cmd
            return self._ok_completed()

        with self._patch_tree_kill(capture):
            pi_cli.run_pi("task", effort="xhigh")
        cmd = captured["cmd"]
        idx = cmd.index("--thinking")
        self.assertEqual(cmd[idx + 1], "xhigh")  # forwarded verbatim, no mapping

    def test_tools_converted_to_pi_names_in_argv(self):
        captured = {}

        def capture(cmd, prompt, timeout, cwd):
            captured["cmd"] = cmd
            return self._ok_completed()

        with self._patch_tree_kill(capture):
            pi_cli.run_pi("task", tools="Read,Glob")
        cmd = captured["cmd"]
        idx = cmd.index("--tools")
        self.assertEqual(cmd[idx + 1], "read,find")

    def test_unconvertible_tools_rejected_without_launching(self):
        with self._patch_tree_kill(None) as tree_kill:
            res = pi_cli.run_pi("task", tools="Read,NotebookEdit")
        tree_kill.assert_not_called()
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "env")
        self.assertIn("NotebookEdit", res.error)

    def test_no_tools_means_no_tools_flag(self):
        captured = {}

        def capture(cmd, prompt, timeout, cwd):
            captured["cmd"] = cmd
            return self._ok_completed()

        with self._patch_tree_kill(capture):
            pi_cli.run_pi("task")  # tools=None: unrestricted, same as run_claude
        self.assertNotIn("--tools", captured["cmd"])

    def test_prompt_is_positional_not_at_file(self):
        captured = {}

        def capture(cmd, prompt, timeout, cwd):
            captured["cmd"] = cmd
            return self._ok_completed()

        with self._patch_tree_kill(capture):
            pi_cli.run_pi("do the thing please")
        cmd = captured["cmd"]
        self.assertFalse(any(str(a).startswith("@") for a in cmd))
        self.assertIn("do the thing please", cmd)

    def test_permission_mode_is_silently_ignored(self):
        captured = {}

        def capture(cmd, prompt, timeout, cwd):
            captured["cmd"] = cmd
            return self._ok_completed()

        with self._patch_tree_kill(capture):
            res = pi_cli.run_pi("task", permission_mode="acceptEdits")
        self.assertTrue(res.ok)
        self.assertFalse(any("permission" in str(a).lower()
                            for a in captured["cmd"]))

    def test_system_prompt_written_to_temp_file_and_cleaned_up(self):
        written = {}

        def capture(cmd, prompt, timeout, cwd):
            idx = cmd.index("--append-system-prompt")
            path = cmd[idx + 1]
            written["text"] = Path(path).read_text(encoding="utf-8")
            written["existed_during_call"] = os.path.isfile(path)
            written["path"] = path
            return self._ok_completed()

        with self._patch_tree_kill(capture):
            pi_cli.run_pi("task", system_prompt="<role>x</role>")
        self.assertEqual(written["text"], "<role>x</role>")
        self.assertTrue(written["existed_during_call"])
        self.assertFalse(os.path.isfile(written["path"]))  # cleaned up after

    def test_stdin_never_left_open_even_with_kill_tree_defaulted_false(self):
        # Regression lock for the orphan-process fix (design phase6 review
        # point 1, 2026-07-30): a real E2E without this fix left a Bash-tool-
        # spawned `sleep 120` alive after node.exe/pi had already been reaped
        # by a plain subprocess.run(timeout=). Tree-kill is now unconditional
        # in run_pi regardless of the kill_tree argument -- this call omits
        # it (default False) and must still go through _run_with_tree_kill,
        # whose Popen+communicate(input="") immediately closes stdin (EOF)
        # rather than leaving a pipe open and unclosed.
        captured = {}

        def capture(cmd, prompt, timeout, cwd):
            captured["prompt"] = prompt
            return self._ok_completed()

        with self._patch_tree_kill(capture) as tree_kill:
            res = pi_cli.run_pi("task")
        tree_kill.assert_called_once()
        self.assertEqual(captured["prompt"], "")
        self.assertTrue(res.ok)

    def test_timeout_is_classified(self):
        # _run_with_tree_kill itself re-raises TimeoutExpired after tree-
        # killing (claude_cli.py) -- run_pi's except clause must still catch
        # it there, now that plain subprocess.run is no longer in this path.
        with self._patch_tree_kill(subprocess.TimeoutExpired(cmd="pi", timeout=5)):
            res = pi_cli.run_pi("task", timeout=5)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "timeout")

    def test_kill_tree_uses_tree_kill_helper_with_empty_stdin_prompt(self):
        captured = {}

        def fake_tree_kill(cmd, prompt, timeout, cwd):
            captured["cmd"] = cmd
            captured["prompt"] = prompt
            return self._ok_completed()

        with mock.patch.object(pi_cli.claude_cli, "_run_with_tree_kill",
                               side_effect=fake_tree_kill), \
             mock.patch.object(pi_cli.subprocess, "run") as plain_run:
            res = pi_cli.run_pi("task", kill_tree=True)
        plain_run.assert_not_called()
        self.assertTrue(res.ok)
        # the real prompt already rides on argv; nothing is fed over stdin
        self.assertEqual(captured["prompt"], "")
        self.assertIn("task", captured["cmd"])

    def test_success_end_to_end_through_classify_result_pi(self):
        with self._patch_tree_kill(lambda cmd, prompt, timeout, cwd: self._ok_completed()):
            res = pi_cli.run_pi("task")
        self.assertTrue(res.ok)
        self.assertEqual(res.text, "done")


class DiagnoseStubPiTests(unittest.TestCase):
    """diagnose_stub_pi(): the second line of defense behind the startup
    fail-fast (design §1, §2.3) -- verifies it fails loudly if ever reached."""

    def test_raises_workflow_failure(self):
        with self.assertRaises(WorkflowFailure):
            pi_cli.diagnose_stub_pi(step=None, prompt="p", failure=None, cwd=None)

    def test_raises_even_without_cwd_kwarg(self):
        with self.assertRaises(WorkflowFailure):
            pi_cli.diagnose_stub_pi(None, "p", None)


class PiCompatErrorsTests(unittest.TestCase):
    """pi_compat_errors(): startup fail-fast (design §2.2, §2.3)."""

    def _wf(self, body: str):
        xml = f'<workflow name="t" version="2" max="10">{body}</workflow>'
        return parser.parse_string(xml)

    def test_compatible_workflow_has_no_errors(self):
        wf = self._wf('<step id="s1"><role>W</role><task>x</task></step>')
        self.assertEqual(pi_cli.pi_compat_errors(wf), [])

    def test_schema_step_rejected_with_build_guidance(self):
        wf = self._wf(
            '<step id="s1" output="n" output-type="value" '
            'schema=\'{"type":"object","properties":{"n":{"type":"integer"}},'
            '"required":["n"]}\'>'
            '<role>W</role><task>count</task></step>')
        errors = pi_cli.pi_compat_errors(wf)
        self.assertEqual(len(errors), 1)
        expected = (
            "error: step 's1' declares schema=, which the pi backend cannot enforce\n"
            "       (no forced-structured-output equivalent exists).\n"
            "       Rebuild this workflow as pi-compatible: run the skill in build mode\n"
            "       on this XML and ask for a pi-compatible version. The conversion\n"
            "       rules are in references/run-pi.md, \"Replacing schema=\".")
        self.assertEqual(errors[0], expected)

    def test_on_error_debug_step_rejected_with_three_hints(self):
        wf = self._wf(
            '<step id="s1" on-error="debug"><role>W</role><task>x</task></step>')
        errors = pi_cli.pi_compat_errors(wf)
        self.assertEqual(len(errors), 1)
        expected = (
            "error: step 's1' uses on-error=\"debug\", which the pi backend does not\n"
            "       support (debug diagnosis has no pi implementation).\n"
            "       Rebuild this workflow as pi-compatible. Replacement hints:\n"
            "       - on-error=\"fail\" (default) — stop the run and let resume handle it\n"
            "       - retry=N — for steps that fail transiently, a plain retry often\n"
            "         covers what a debug-retry cycle did\n"
            "       - on-error=\"ignore\" + a follow-up verification step — when the run\n"
            "         should continue and the failure needs recording instead of fixing\n"
            "       See references/run-pi.md, \"Replacing on-error=debug\".")
        self.assertEqual(errors[0], expected)
        # the three replacement hints, explicitly
        self.assertIn('on-error="fail"', errors[0])
        self.assertIn("retry=N", errors[0])
        self.assertIn('on-error="ignore"', errors[0])

    def test_replan_on_error_debug_is_not_flagged(self):
        # <replan> cannot carry schema= at all (parser.py's _REPLAN_ATTRS
        # excludes it), and its on-error="debug" is already inert under
        # every backend (_exec_replan never calls self._diagnose) -- not a
        # pi-specific gap this check needs to close.
        wf = self._wf(
            '<replan id="r1" on-error="debug"><role>W</role><task>x</task></replan>')
        self.assertEqual(pi_cli.pi_compat_errors(wf), [])

    def test_multiple_violations_combine_in_step_order(self):
        wf = self._wf(
            '<step id="s1" schema=\'{"type":"object"}\'>'
            '<role>W</role><task>a</task></step>'
            '<step id="s2" on-error="debug"><role>W</role><task>b</task></step>')
        errors = pi_cli.pi_compat_errors(wf)
        self.assertEqual(len(errors), 2)
        self.assertIn("step 's1'", errors[0])
        self.assertIn("step 's2'", errors[1])


if __name__ == "__main__":
    unittest.main()
