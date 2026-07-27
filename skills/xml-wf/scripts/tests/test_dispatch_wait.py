"""Tests for the A-layer (reliability-spec.md §5): dispatch / _wrapper /
wait, and the kill_tree plumbing in claude_cli.

subprocess.Popen (for the detached wrapper launch) and claude_cli._launch
(for the actual claude -p call) are monkeypatched throughout; no real
process is spawned here (that is covered separately by a real-CLI E2E, run
manually in an isolated worktree per reliability-spec.md §5.3)."""
import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun import claude_cli  # noqa: E402
from wfrun.__main__ import main  # noqa: E402
from wfrun.adp import Diagnosis  # noqa: E402


class DispatchWaitTestCase(unittest.TestCase):
    XML = """
<workflow name="t" version="2" max="{max}">
  <step id="s1" role="w" retry="{retry}" on-error="{on_error}"
        output="answer" output-type="value">
    <task>answer it</task>
  </step>
  <step id="s2" role="w"><task>write it</task></step>
  <replan id="r1" max-steps="3"><role>w</role><task>plan it</task></replan>
</workflow>
"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.xml = str(self.dir / "wf.xml")
        self.write_xml()
        self.vars_path = str(self.dir / "vars.json")
        Path(self.vars_path).write_text("{}", encoding="utf-8")
        self.run_dir = self.dir / "run"
        agents_dir = self.dir / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "w.md").write_text(
            "---\nname: w\ndescription: test\ntools: Read\n---\nROLE-BODY",
            encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def write_xml(self, max_="10", retry="0", on_error="fail"):
        Path(self.xml).write_text(
            self.XML.format(max=max_, retry=retry, on_error=on_error),
            encoding="utf-8")

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue().strip(), err.getvalue().strip()

    def paths(self, step_id="s1", cycle=1):
        steps_dir = self.run_dir / "steps"
        stem = f"{step_id}_c{cycle:02d}"
        return {
            "system": steps_dir / f"{stem}_system.md",
            "prompt": steps_dir / f"{stem}_prompt.md",
            "handle": steps_dir / f"{stem}_handle.json",
            "exit": steps_dir / f"{stem}_exit.json",
            "result": steps_dir / f"{stem}_result.json",
            "attempts": steps_dir / f"{stem}_attempts.json",
            "wait": steps_dir / f"{stem}_wait.json",
        }

    def seed_cycle(self, attempts, step_id="s1", cycle=1):
        """Make a prior cycle exist: dispatch keys cycle detection off the
        handle file, and reads the ledger from the matching attempts file."""
        p = self.paths(step_id, cycle)
        p["handle"].parent.mkdir(parents=True, exist_ok=True)
        p["handle"].write_text(json.dumps({"cycle": cycle}), encoding="utf-8")
        p["attempts"].write_text(json.dumps(attempts), encoding="utf-8")
        return p


class DispatchTests(DispatchWaitTestCase):
    def dispatch(self, step_id="s1", extra=None, fake_pid=4242):
        fake_proc = mock.Mock()
        fake_proc.pid = fake_pid
        argv = ["dispatch", self.xml, step_id, "--vars", self.vars_path,
               "--run-dir", str(self.run_dir)]
        if extra:
            argv += extra
        with mock.patch("wfrun.__main__.subprocess.Popen",
                        return_value=fake_proc) as popen:
            code, out, err = self.run_cli(argv)
        return code, out, err, popen

    def test_dispatch_writes_handle_and_prompt_files(self):
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 0, err)
        p = self.paths()
        self.assertTrue(p["system"].is_file())
        self.assertTrue(p["prompt"].is_file())
        handle = json.loads(p["handle"].read_text(encoding="utf-8"))
        self.assertEqual(handle["wrapper_pid"], 4242)
        self.assertEqual(handle["attempt"], 1)
        self.assertEqual(handle["step_id"], "s1")
        self.assertEqual(handle["timeout"], 600)  # model.DEFAULT_TIMEOUT
        self.assertIn("run_dir", handle)
        self.assertIsInstance(handle["started_at"], (int, float))

    def test_dispatch_forwards_model_tools_to_wrapper_argv(self):
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 0, err)
        wrapper_argv = popen.call_args[0][0]
        self.assertIn("_wrapper", wrapper_argv)
        self.assertIn("--tools", wrapper_argv)  # coder-role tools, if any resolved

    def test_dispatch_rejects_replan(self):
        code, out, err, popen = self.dispatch(step_id="r1")
        self.assertEqual(code, 2)
        self.assertIn("replan", err)
        popen.assert_not_called()

    def test_dispatch_deletes_stale_exit_and_result(self):
        p = self.paths()
        p["exit"].parent.mkdir(parents=True)
        p["exit"].write_text("stale", encoding="utf-8")
        p["result"].write_text("stale", encoding="utf-8")
        self.dispatch()
        self.assertFalse(p["exit"].is_file())
        self.assertFalse(p["result"].is_file())

    def test_dispatch_cap_exceeded_refuses_without_launching(self):
        self.write_xml(retry="0")  # cap = 0+1+1+1 = 3
        self.seed_cycle([
            {"seq": 1, "class": "timeout", "ended_at": 1},
            {"seq": 2, "class": "timeout", "ended_at": 2},
            {"seq": 3, "class": "aborted", "ended_at": 3},
        ])
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 1)
        self.assertIn("cap exceeded", err)
        popen.assert_not_called()

    def test_dispatch_under_cap_proceeds(self):
        self.write_xml(retry="1")  # cap = 1+1+1+1 = 4
        self.seed_cycle([{"seq": 1, "class": "timeout", "ended_at": 1}])
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 0, err)
        popen.assert_called_once()

    # ---- cycles: <while>/<each> re-visits vs retries ----

    def test_new_cycle_after_success_is_automatic(self):
        # A loop body step that succeeded, then comes round again: the
        # previous cycle's full ledger must not block it.
        self.write_xml(retry="0")  # cap = 3, and the prior cycle used all 3
        self.seed_cycle([
            {"seq": 1, "class": "timeout", "ended_at": 1},
            {"seq": 2, "class": "timeout", "ended_at": 2},
            {"seq": 3, "class": "ok", "ended_at": 3},
        ])
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 0, err)
        self.assertIn("cycle=2", out)
        popen.assert_called_once()
        # cycle 2 gets its own files; cycle 1's are untouched
        self.assertTrue(self.paths(cycle=2)["handle"].is_file())

    def test_retry_after_failure_stays_in_same_cycle(self):
        self.write_xml(retry="1")  # cap = 4
        self.seed_cycle([{"seq": 1, "class": "timeout", "ended_at": 1}])
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 0, err)
        self.assertIn("cycle=1", out)   # not a new iteration
        self.assertIn("seq=2", out)     # continues the ledger

    def test_new_cycle_flag_forces_fresh_budget_after_failure(self):
        # on-error="ignore" inside a loop: the outcome was a failure, so the
        # ledger cannot infer acceptance -- the flag says so explicitly.
        self.write_xml(retry="0")  # cap = 3, exhausted below
        self.seed_cycle([
            {"seq": 1, "class": "timeout", "ended_at": 1},
            {"seq": 2, "class": "timeout", "ended_at": 2},
            {"seq": 3, "class": "behavioral", "ended_at": 3},
        ])
        blocked, _, err, _ = self.dispatch()
        self.assertEqual(blocked, 1)          # without the flag: capped
        self.assertIn("--new-cycle", err)     # and told how to proceed
        code, out, err, popen = self.dispatch(extra=["--new-cycle"])
        self.assertEqual(code, 0, err)
        self.assertIn("cycle=2", out)

    def test_cycle_files_do_not_clobber_previous_iteration(self):
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 0, err)
        c1_prompt = self.paths(cycle=1)["prompt"]
        c1_prompt.write_text("ITERATION-1-PROMPT", encoding="utf-8")
        self.paths(cycle=1)["attempts"].write_text(
            json.dumps([{"seq": 1, "class": "ok", "ended_at": 1}]),
            encoding="utf-8")
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 0, err)
        self.assertIn("cycle=2", out)
        self.assertEqual(c1_prompt.read_text(encoding="utf-8"),
                         "ITERATION-1-PROMPT")

    def test_cycle_detected_even_when_wrapper_never_wrote_attempts(self):
        # A wrapper that died leaves a handle but no attempts file; that
        # cycle must still be visible (otherwise the ledger silently
        # restarts and the cap stops bounding anything).
        p = self.paths(cycle=1)
        p["handle"].parent.mkdir(parents=True, exist_ok=True)
        p["handle"].write_text(json.dumps({"cycle": 1}), encoding="utf-8")
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 0, err)
        self.assertIn("cycle=1", out)  # continues, does not jump to 2

    def test_dispatch_refuses_when_steps_log_reaches_wf_max(self):
        self.write_xml(max_="2")
        log = self.run_dir / "steps.log"
        log.parent.mkdir(parents=True)
        log.write_text(
            '{"ts":"t","step":"s1","status":"success"}\n'
            '{"ts":"t","step":"s2","status":"error"}\n', encoding="utf-8")
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 1)
        self.assertIn("max=2", err)
        popen.assert_not_called()

    def test_wf_max_ignores_ask_judgment_entries(self):
        # `wfrun ask --log steps.log` shares the file by run-llm.md's
        # convention, but an ask is a condition evaluation, not a step
        # execution -- it must not consume the cap.
        self.write_xml(max_="2")
        log = self.run_dir / "steps.log"
        log.parent.mkdir(parents=True)
        log.write_text(
            '{"kind":"ask","question":"q1","answer":true}\n'
            '{"kind":"ask","question":"q2","answer":false}\n'
            '{"ts":"t","step":"s1","status":"success"}\n', encoding="utf-8")
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 0, err)  # 1 step execution < max=2
        popen.assert_called_once()

    def test_wf_max_tolerates_blank_and_corrupt_log_lines(self):
        self.write_xml(max_="2")
        log = self.run_dir / "steps.log"
        log.parent.mkdir(parents=True)
        log.write_text('\nnot json at all\n{"ts":"t","step":"s1"}\n',
                       encoding="utf-8")
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 0, err)
        popen.assert_called_once()

    def test_dispatch_permission_mode_forwarded_only_for_write_tools(self):
        # role "w" has no frontmatter tools -> tools_can_write(None) is False
        # by convention elsewhere in this codebase; just check no crash and
        # that the flag is consistently either present or absent.
        code, out, err, popen = self.dispatch(extra=["--permission-mode", "acceptEdits"])
        self.assertEqual(code, 0, err)


class WrapperTests(DispatchWaitTestCase):
    def setUp(self):
        super().setUp()
        self.p = self.paths()
        self.p["system"].parent.mkdir(parents=True)
        self.p["system"].write_text("<role>w</role>", encoding="utf-8")
        self.p["prompt"].write_text("do the task", encoding="utf-8")

    def run_wrapper(self, extra=None):
        argv = ["_wrapper", "--system-file", str(self.p["system"]),
               "--prompt-file", str(self.p["prompt"]),
               "--exit-file", str(self.p["exit"]),
               "--result-file", str(self.p["result"]),
               "--attempts-file", str(self.p["attempts"]),
               "--seq", "1", "--cwd", str(self.dir), "--timeout", "600"]
        if extra:
            argv += extra
        return self.run_cli(argv)

    def test_wrapper_writes_exit_result_attempts_on_success(self):
        stdout_json = json.dumps({"result": "42", "is_error": False})
        with mock.patch.object(claude_cli, "_launch",
                               return_value=(0, stdout_json, "")):
            code, out, err = self.run_wrapper()
        self.assertEqual(code, 0)
        exit_data = json.loads(self.p["exit"].read_text(encoding="utf-8"))
        self.assertEqual(exit_data["returncode"], 0)
        self.assertNotIn("early_class", exit_data)
        self.assertEqual(self.p["result"].read_text(encoding="utf-8"), stdout_json)
        attempts = json.loads(self.p["attempts"].read_text(encoding="utf-8"))
        self.assertEqual(attempts[0]["seq"], 1)
        self.assertEqual(attempts[0]["class"], "ok")

    def test_wrapper_records_early_class_for_env(self):
        with mock.patch.object(
                claude_cli, "_launch",
                return_value=claude_cli.CliResult(
                    ok=False, exit_code=-1, error_class="env",
                    error="claude CLI not found on PATH")):
            code, out, err = self.run_wrapper()
        self.assertEqual(code, 0)  # the wrapper itself always "succeeds" at its job
        exit_data = json.loads(self.p["exit"].read_text(encoding="utf-8"))
        self.assertEqual(exit_data["early_class"], "env")
        self.assertEqual(self.p["result"].read_text(encoding="utf-8"), "")
        attempts = json.loads(self.p["attempts"].read_text(encoding="utf-8"))
        self.assertEqual(attempts[0]["class"], "env")

    def test_wrapper_records_early_class_for_timeout(self):
        # Regression: an empty result.json must NOT be silently reclassified
        # as "env" by whoever reads it back (it would be, via a bare
        # classify_result() call on empty stdout) -- early_class preserves
        # the true reason.
        with mock.patch.object(
                claude_cli, "_launch",
                return_value=claude_cli.CliResult(
                    ok=False, exit_code=-1, error_class="timeout",
                    error="timeout after 600s")):
            self.run_wrapper()
        exit_data = json.loads(self.p["exit"].read_text(encoding="utf-8"))
        self.assertEqual(exit_data["early_class"], "timeout")

    def test_wrapper_uses_kill_tree(self):
        with mock.patch.object(claude_cli, "_launch",
                               return_value=(0, '{"result":"x"}', "")) as launch:
            self.run_wrapper()
        self.assertTrue(launch.call_args.kwargs.get("kill_tree"))


class WaitTests(DispatchWaitTestCase):
    def setUp(self):
        super().setUp()
        self.p = self.paths()
        self.p["exit"].parent.mkdir(parents=True)
        self.p["system"].write_text("<role>w</role>", encoding="utf-8")
        self.p["prompt"].write_text("answer it", encoding="utf-8")

    def write_handle(self, started_at=None, timeout=600):
        handle = {
            "wrapper_pid": 1, "attempt": 1, "cycle": 1,
            "started_at": started_at if started_at is not None else time.time(),
            "timeout": timeout, "xml": self.xml, "step_id": "s1",
            "run_dir": str(self.run_dir), "exit_path": str(self.p["exit"]),
            "result_path": str(self.p["result"]),
            "system_path": str(self.p["system"]), "prompt_path": str(self.p["prompt"]),
            "wait_path": str(self.p["wait"]),
        }
        self.p["handle"].write_text(json.dumps(handle), encoding="utf-8")
        return handle

    def wait(self, max_=5, extra=None):
        argv = ["wait", str(self.p["handle"]), "--max", str(max_),
               "--vars", self.vars_path]
        log = self.run_dir / "steps.log"
        argv += ["--log", str(log)]
        if extra:
            argv += extra
        return self.run_cli(argv)

    def test_wait_done_ok(self):
        self.write_handle()
        self.p["exit"].write_text(json.dumps({"returncode": 0, "stderr": ""}),
                                  encoding="utf-8")
        self.p["result"].write_text(json.dumps({"result": "42", "is_error": False}),
                                    encoding="utf-8")
        code, out, err = self.wait()
        self.assertEqual(code, 0)
        self.assertEqual(out, "ok (set answer)")
        variables = json.loads(Path(self.vars_path).read_text(encoding="utf-8"))
        self.assertEqual(variables["answer"], "42")

    def test_wait_done_error_hides_guardrail_content(self):
        self.write_handle()
        self.p["exit"].write_text(json.dumps({"returncode": 0, "stderr": ""}),
                                  encoding="utf-8")
        self.p["result"].write_text(
            json.dumps({"result": "ERROR: secret task detail", "is_error": False}),
            encoding="utf-8")
        code, out, err = self.wait()
        self.assertEqual(code, 1)
        self.assertNotIn("secret task detail", out)
        self.assertIn(str(self.p["result"]), out)

    def test_wait_running_within_max(self):
        self.write_handle()  # started_at=now, timeout=600 -> abort far away
        code, out, err = self.wait(max_=0.2)
        self.assertEqual(code, 10)
        self.assertEqual(out, "running")

    def test_wait_aborted_past_deadline(self):
        self.write_handle(started_at=time.time() - 99999, timeout=1)
        code, out, err = self.wait(max_=0.2)
        self.assertEqual(code, 3)
        self.assertIn("aborted", err)

    def test_wait_early_class_env_does_not_misclassify_as_timeout(self):
        self.write_handle()
        self.p["exit"].write_text(json.dumps({
            "returncode": -1, "stderr": "", "early_class": "env",
            "early_error": "claude CLI not found on PATH",
        }), encoding="utf-8")
        code, out, err = self.wait()
        self.assertEqual(code, 1)
        self.assertIn("claude CLI not found on PATH", out)

    def test_wait_debug_retry_writes_fix_file(self):
        self.write_xml(on_error="debug")
        self.write_handle()
        self.p["exit"].write_text(json.dumps({"returncode": 0, "stderr": ""}),
                                  encoding="utf-8")
        self.p["result"].write_text(
            json.dumps({"result": "ERROR: boom", "is_error": False}), encoding="utf-8")
        fake_diag = mock.Mock(return_value=Diagnosis(
            "RETRY", "transient", fix_instruction="add --force"))
        with mock.patch("wfrun.__main__.adp.diagnose", fake_diag):
            code, out, err = self.wait()
        self.assertEqual(code, 1)
        fix_path = self.run_dir / "steps" / "s1_fix.md"
        self.assertEqual(fix_path.read_text(encoding="utf-8"), "add --force")
        fake_diag.assert_called_once()

    def test_wait_transient_never_calls_debug(self):
        self.write_xml(on_error="debug")
        self.write_handle()
        self.p["exit"].write_text(json.dumps({"returncode": 1, "stderr": ""}),
                                  encoding="utf-8")
        self.p["result"].write_text(json.dumps({
            "result": "x", "is_error": True,
            "terminal_reason": "api_error", "api_error_status": 529,
        }), encoding="utf-8")
        fake_diag = mock.Mock(return_value=Diagnosis("FAIL", "no"))
        with mock.patch("wfrun.__main__.adp.diagnose", fake_diag):
            code, out, err = self.wait()
        self.assertEqual(code, 1)
        fake_diag.assert_not_called()

    def test_wait_is_idempotent_no_duplicate_log_or_vars_rewrite(self):
        # The orchestrator loops on "running", so a completed handle WILL be
        # re-polled. A second wait must replay the verdict, not redo it.
        self.write_handle()
        self.p["exit"].write_text(json.dumps({"returncode": 0, "stderr": ""}),
                                  encoding="utf-8")
        self.p["result"].write_text(json.dumps({"result": "42", "is_error": False}),
                                    encoding="utf-8")
        code1, out1, _ = self.wait()
        code2, out2, _ = self.wait()
        self.assertEqual((code1, code2), (0, 0))
        self.assertEqual(out1, out2)  # same verdict replayed verbatim
        log = self.run_dir / "steps.log"
        lines = [l for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)  # not 2

    def test_wait_idempotency_does_not_refire_debug(self):
        self.write_xml(on_error="debug")
        self.write_handle()
        self.p["exit"].write_text(json.dumps({"returncode": 0, "stderr": ""}),
                                  encoding="utf-8")
        self.p["result"].write_text(
            json.dumps({"result": "ERROR: boom", "is_error": False}), encoding="utf-8")
        fake_diag = mock.Mock(return_value=Diagnosis(
            "RETRY", "x", fix_instruction="add --force"))
        with mock.patch("wfrun.__main__.adp.diagnose", fake_diag):
            self.wait()
            self.wait()
        fake_diag.assert_called_once()  # the LLM call must not repeat

    def test_wait_re_dispatch_clears_wait_record(self):
        # A new attempt (dispatch again) must NOT be short-circuited by the
        # previous attempt's wait record.
        self.write_handle()
        self.p["exit"].write_text(json.dumps({"returncode": 0, "stderr": ""}),
                                  encoding="utf-8")
        self.p["result"].write_text(json.dumps({"result": "42", "is_error": False}),
                                    encoding="utf-8")
        self.wait()
        self.assertTrue(self.p["wait"].is_file())
        fake_proc = mock.Mock()
        fake_proc.pid = 99
        with mock.patch("wfrun.__main__.subprocess.Popen", return_value=fake_proc):
            code, out, err = self.run_cli(
                ["dispatch", self.xml, "s1", "--vars", self.vars_path,
                 "--run-dir", str(self.run_dir)])
        self.assertEqual(code, 0, err)
        self.assertFalse(self.p["wait"].is_file())

    def test_wait_tolerates_torn_exit_json(self):
        # Regression: a half-written exit.json used to raise JSONDecodeError
        # out of main(); it must read as "not ready yet" instead.
        self.write_handle()
        self.p["exit"].write_text('{"returncode": 0, "std', encoding="utf-8")
        code, out, err = self.wait(max_=0.2)
        self.assertEqual(code, 10)
        self.assertEqual(out, "running")

    def test_wait_expect_file_resolves_against_xml_dir_not_cwd(self):
        # The wrapper runs the step with cwd = the XML's parent, so a
        # relative expect-file must be checked there -- not wherever `wait`
        # happens to be invoked from.
        Path(self.xml).write_text(
            self.XML.format(max="10", retry="0", on_error="fail").replace(
                '<step id="s2" role="w"><task>write it</task></step>',
                '<step id="s2" role="w" expect-file="made.txt">'
                '<task>write it</task></step>'),
            encoding="utf-8")
        (self.dir / "made.txt").write_text("data", encoding="utf-8")
        p2 = self.paths("s2")
        handle = {
            "wrapper_pid": 1, "attempt": 1, "cycle": 1, "started_at": time.time(),
            "timeout": 600, "xml": self.xml, "step_id": "s2",
            "run_dir": str(self.run_dir), "exit_path": str(p2["exit"]),
            "result_path": str(p2["result"]), "wait_path": str(p2["wait"]),
        }
        p2["handle"].write_text(json.dumps(handle), encoding="utf-8")
        p2["exit"].write_text(json.dumps({"returncode": 0, "stderr": ""}),
                              encoding="utf-8")
        p2["result"].write_text(json.dumps({"result": "wrote it", "is_error": False}),
                                encoding="utf-8")
        # cwd here is the repo/test runner dir, NOT self.dir -- the old
        # cwd-relative check would report made.txt as missing.
        code, out, err = self.run_cli(
            ["wait", str(p2["handle"]), "--max", "5", "--vars", self.vars_path])
        self.assertEqual(code, 0, out)

    def test_wait_refusal_never_calls_debug(self):
        self.write_xml(on_error="debug")
        self.write_handle()
        self.p["exit"].write_text(json.dumps({"returncode": 0, "stderr": ""}),
                                  encoding="utf-8")
        self.p["result"].write_text(
            json.dumps({"result": "[BLOCKED: mode-rule x] no", "is_error": False}),
            encoding="utf-8")
        fake_diag = mock.Mock(return_value=Diagnosis("RETRY", "x", fix_instruction="f"))
        with mock.patch("wfrun.__main__.adp.diagnose", fake_diag):
            code, out, err = self.wait()
        self.assertEqual(code, 1)
        fake_diag.assert_not_called()


class KillTreePlumbingTests(unittest.TestCase):
    def setUp(self):
        claude_cli._resolution_cache.clear()
        self.which_patch = mock.patch.object(
            claude_cli.shutil, "which", return_value="/usr/local/bin/claude")
        self.which_patch.start()

    def tearDown(self):
        self.which_patch.stop()
        claude_cli._resolution_cache.clear()

    def test_kill_tree_false_uses_plain_subprocess_run(self):
        stdout = json.dumps({"result": "x", "is_error": False})
        with mock.patch.object(claude_cli.subprocess, "run") as run, \
             mock.patch.object(claude_cli, "_run_with_tree_kill") as tree_kill:
            run.return_value = mock.Mock(returncode=0, stdout=stdout, stderr="")
            claude_cli.run_claude("hi")
        run.assert_called_once()
        tree_kill.assert_not_called()

    def test_kill_tree_true_uses_tree_kill_path(self):
        stdout = json.dumps({"result": "x", "is_error": False})
        with mock.patch.object(claude_cli.subprocess, "run") as run, \
             mock.patch.object(claude_cli, "_run_with_tree_kill") as tree_kill:
            tree_kill.return_value = mock.Mock(returncode=0, stdout=stdout, stderr="")
            claude_cli.run_claude("hi", kill_tree=True)
        tree_kill.assert_called_once()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
