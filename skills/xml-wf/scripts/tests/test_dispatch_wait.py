import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun import claude_cli  # noqa: E402
from wfrun.__main__ import main  # noqa: E402
from wfrun.adp import Diagnosis  # noqa: E402
from wfrun import stepio  # noqa: E402

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def _lock_paths(run_dir, step_id="s1"):
    steps_dir = Path(run_dir) / "steps"
    steps_dir.mkdir(parents=True, exist_ok=True)
    return {
        "run_lock": steps_dir / f"{step_id}_run.lock",
        "marker": steps_dir / f"{step_id}_run.json",
        "state_lock": steps_dir / f"{step_id}_state.lock",
    }


def _write_marker(path, token, cycle=1, attempt=1, pid=None):
    path.write_text(json.dumps({
        "token": token, "cycle": cycle, "attempt": attempt,
        "pid": pid if pid is not None else os.getpid(),
        "acquired_at": time.time(),
    }), encoding="utf-8")


class _AttemptLock:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()
        self._fh = open(self.path, "r+b")

    def acquire(self):
        self._fh.seek(0)
        if os.name == "nt":
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self):
        self._fh.seek(0)
        if os.name == "nt":
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)

    def close(self):
        self._fh.close()


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
        p = self.paths(step_id, cycle)
        p["handle"].parent.mkdir(parents=True, exist_ok=True)
        p["handle"].write_text(json.dumps({"cycle": cycle}), encoding="utf-8")
        p["attempts"].write_text(json.dumps(attempts), encoding="utf-8")
        return p

    def log_entries(self):
        log = self.run_dir / "steps.log"
        if not log.is_file():
            return []
        return [json.loads(line)
                for line in log.read_text(encoding="utf-8").splitlines()
                if line.strip()]


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
        self.assertEqual(handle["timeout"], 600)
        self.assertIn("run_dir", handle)
        self.assertIsInstance(handle["started_at"], (int, float))

    def test_dispatch_forwards_model_tools_to_wrapper_argv(self):
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 0, err)
        wrapper_argv = popen.call_args[0][0]
        self.assertIn("_wrapper", wrapper_argv)
        self.assertIn("--tools", wrapper_argv)

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
        self.write_xml(retry="0")
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
        self.write_xml(retry="1")
        self.seed_cycle([{"seq": 1, "class": "timeout", "ended_at": 1}])
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 0, err)
        popen.assert_called_once()

    def test_new_cycle_after_success_is_automatic(self):
        self.write_xml(retry="0")
        self.seed_cycle([
            {"seq": 1, "class": "timeout", "ended_at": 1},
            {"seq": 2, "class": "timeout", "ended_at": 2},
            {"seq": 3, "class": "ok", "ended_at": 3},
        ])
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 0, err)
        self.assertIn("cycle=2", out)
        popen.assert_called_once()
        self.assertTrue(self.paths(cycle=2)["handle"].is_file())

    def test_retry_after_failure_stays_in_same_cycle(self):
        self.write_xml(retry="1")
        self.seed_cycle([{"seq": 1, "class": "timeout", "ended_at": 1}])
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 0, err)
        self.assertIn("cycle=1", out)
        self.assertIn("seq=2", out)

    def test_new_cycle_flag_forces_fresh_budget_after_failure(self):
        self.write_xml(retry="0")
        self.seed_cycle([
            {"seq": 1, "class": "timeout", "ended_at": 1},
            {"seq": 2, "class": "timeout", "ended_at": 2},
            {"seq": 3, "class": "behavioral", "ended_at": 3},
        ])
        blocked, _, err, _ = self.dispatch()
        self.assertEqual(blocked, 1)
        self.assertIn("--new-cycle", err)
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
        p = self.paths(cycle=1)
        p["handle"].parent.mkdir(parents=True, exist_ok=True)
        p["handle"].write_text(json.dumps({"cycle": 1}), encoding="utf-8")
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 0, err)
        self.assertIn("cycle=1", out)

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
        self.write_xml(max_="2")
        log = self.run_dir / "steps.log"
        log.parent.mkdir(parents=True)
        log.write_text(
            '{"kind":"ask","question":"q1","answer":true}\n'
            '{"kind":"ask","question":"q2","answer":false}\n'
            '{"ts":"t","step":"s1","status":"success"}\n', encoding="utf-8")
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 0, err)
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
        code, out, err, popen = self.dispatch(extra=["--permission-mode", "acceptEdits"])
        self.assertEqual(code, 0, err)

    def test_dispatch_refuses_after_a_denied_attempt(self):
        self.write_xml(retry="2")
        self.seed_cycle([{"seq": 1, "class": "denied", "ended_at": 1}])
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 1,
                         "a refused tool call is not retried, retry budget or not")
        self.assertIn("denied", err)
        popen.assert_not_called()

    def test_new_cycle_dispatches_after_a_denied_attempt(self):
        self.seed_cycle([{"seq": 1, "class": "denied", "ended_at": 1}])
        code, out, err, popen = self.dispatch(extra=["--new-cycle"])
        self.assertEqual(code, 0, err)
        popen.assert_called_once()

    def file_decision(self, rid, answered=True):
        dec = self.run_dir / "decisions"
        dec.mkdir(parents=True, exist_ok=True)
        (dec / f"{rid}_request.md").write_text("DECISION: which one?",
                                               encoding="utf-8")
        if answered:
            answer = dec / f"{rid}_answer.md"
            answer.write_text("option: 1\ngo", encoding="utf-8")
            (dec / f"{rid}_verdict.json").write_text(json.dumps(
                {"request_id": rid, "answer_path": str(answer)}), encoding="utf-8")

    def test_dispatch_refuses_to_rerun_a_class_that_is_never_retried(self):
        self.write_xml(retry="2")
        for cls in ("env", "guardrail", "refusal", "unchecked"):
            with self.subTest(cls=cls):
                self.seed_cycle([{"seq": 1, "class": cls, "ended_at": 1}])
                code, out, err, popen = self.dispatch()
                self.assertEqual(code, 1, f"{cls} is never re-run, retry budget or not")
                self.assertIn(cls, err)
                popen.assert_not_called()

    def test_dispatch_refuses_a_retry_once_retries_are_spent(self):
        for retry, ledger in (("1", ["timeout", "timeout"]), ("0", ["timeout"])):
            with self.subTest(retry=retry, ledger=ledger):
                self.write_xml(retry=retry)
                self.seed_cycle([{"seq": i + 1, "class": cls, "ended_at": i}
                                 for i, cls in enumerate(ledger)])
                code, out, err, popen = self.dispatch()
                self.assertEqual(code, 1,
                                 "a failed attempt is retried only while failures <= retry")
                self.assertIn("retries", err)
                popen.assert_not_called()

    def test_decision_reruns_do_not_spend_the_retry_budget(self):
        self.write_xml(retry="1")
        self.seed_cycle([{"seq": 1, "class": "decision", "ended_at": 1},
                         {"seq": 2, "class": "timeout", "ended_at": 2}])
        self.file_decision("s1_c01_d01")
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 0, err)
        popen.assert_called_once()

    def test_dispatch_refuses_while_a_decision_awaits_its_ruling(self):
        self.seed_cycle([{"seq": 1, "class": "decision", "ended_at": 1}])
        self.file_decision("s1_c01_d01", answered=False)
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 1, "a re-run before the ruling executes the step twice")
        self.assertIn("s1_c01_d01", err)
        self.assertIn("record --answer", err)
        popen.assert_not_called()

    def test_dispatch_reruns_once_the_decision_is_settled(self):
        self.seed_cycle([{"seq": 1, "class": "decision", "ended_at": 1}])
        self.file_decision("s1_c01_d01")
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 0, err)
        self.assertIn("cycle=1", out)

    def test_fix_dispatch_follows_a_debug_retry(self):
        self.write_xml(on_error="debug")
        self.seed_cycle([{"seq": 1, "class": "guardrail", "ended_at": 1,
                          "debug": "RETRY"}])
        code, out, err, popen = self.dispatch(extra=["--fix", "do it differently"])
        self.assertEqual(code, 0, err)
        popen.assert_called_once()

    def test_plain_dispatch_after_a_debug_retry_names_the_fix(self):
        self.write_xml(on_error="debug")
        self.seed_cycle([{"seq": 1, "class": "behavioral", "ended_at": 1,
                          "debug": "RETRY"}])
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 1, "after a debug RETRY the next attempt carries the fix")
        self.assertIn("--fix", err)
        self.assertIn("s1_fix.md", err)
        popen.assert_not_called()

    def test_fix_dispatch_without_a_debug_retry_is_refused(self):
        self.write_xml(retry="1", on_error="debug")
        cases = (
            ([], "without --fix"),
            ([{"seq": 1, "class": "timeout", "ended_at": 1}], "without --fix"),
            ([{"seq": 1, "class": "guardrail", "ended_at": 1, "debug": "FAIL"}],
             "stop"),
            ([{"seq": 1, "class": "guardrail", "ended_at": 1, "debug": "RETRY"},
              {"seq": 2, "class": "guardrail", "ended_at": 2}], "stop"),
        )
        for ledger, action in cases:
            with self.subTest(ledger=ledger):
                self.seed_cycle(ledger)
                code, out, err, popen = self.dispatch(extra=["--fix", "stale fix"])
                self.assertEqual(code, 1, "--fix applies only the fix debug just granted")
                self.assertIn(action, err)
                popen.assert_not_called()

    def test_ignored_failure_points_at_new_cycle(self):
        self.write_xml(on_error="ignore")
        self.seed_cycle([{"seq": 1, "class": "env", "ended_at": 1}])
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 1)
        self.assertIn("--new-cycle", err)
        popen.assert_not_called()


class WrapperTests(DispatchWaitTestCase):
    def setUp(self):
        super().setUp()
        self.p = self.paths()
        self.p["system"].parent.mkdir(parents=True)
        self.p["system"].write_text("<role>w</role>", encoding="utf-8")
        self.p["prompt"].write_text("do the task", encoding="utf-8")
        self.token = "tok"
        self.p["handle"].write_text(json.dumps({
            "wrapper_pid": 0, "attempt": 1, "cycle": 1,
            "started_at": time.time(), "timeout": 600, "token": self.token,
            "xml": self.xml, "step_id": "s1", "run_dir": str(self.run_dir),
            "exit_path": str(self.p["exit"]), "result_path": str(self.p["result"]),
            "system_path": str(self.p["system"]), "prompt_path": str(self.p["prompt"]),
            "wait_path": str(self.p["wait"]),
        }), encoding="utf-8")

    def run_wrapper(self, extra=None):
        argv = ["_wrapper", "--system-file", str(self.p["system"]),
               "--prompt-file", str(self.p["prompt"]),
               "--exit-file", str(self.p["exit"]),
               "--result-file", str(self.p["result"]),
               "--attempts-file", str(self.p["attempts"]),
               "--seq", "1", "--cwd", str(self.dir), "--timeout", "600",
               "--handle-file", str(self.p["handle"]), "--token", self.token]
        if extra:
            argv += extra
        return self.run_cli(argv)

    def test_wrapper_writes_exit_result_attempts_on_success(self):
        stdout_json = json.dumps({"result": "42", "is_error": False,
                                  "permission_denials": []})
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
        self.assertEqual(code, 0,
                         "the wrapper exits 0 whenever it recorded a verdict; "
                         "the step's own failure rides in exit.json")
        exit_data = json.loads(self.p["exit"].read_text(encoding="utf-8"))
        self.assertEqual(exit_data["early_class"], "env")
        self.assertEqual(self.p["result"].read_text(encoding="utf-8"), "")
        attempts = json.loads(self.p["attempts"].read_text(encoding="utf-8"))
        self.assertEqual(attempts[0]["class"], "env")

    def test_wrapper_records_early_class_for_timeout(self):
        with mock.patch.object(
                claude_cli, "_launch",
                return_value=claude_cli.CliResult(
                    ok=False, exit_code=-1, error_class="timeout",
                    error="timeout after 600s")):
            self.run_wrapper()
        exit_data = json.loads(self.p["exit"].read_text(encoding="utf-8"))
        self.assertEqual(exit_data["early_class"], "timeout",
                         "an empty result.json alone reads as env; "
                         "early_class is what keeps the true reason")

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

    def write_handle(self, started_at=None, timeout=600, attempt=1):
        handle = {
            "wrapper_pid": 1, "attempt": attempt, "cycle": 1,
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
        self.p["result"].write_text(json.dumps({"result": "42", "is_error": False,
                                                "permission_denials": []}),
                                    encoding="utf-8")
        code, out, err = self.wait()
        self.assertEqual(code, 0)
        self.assertEqual(out, "ok (set answer; no value line)",
                         "a missing VALUE: line is reported, not failed: "
                         "the value line rule is fail-open")
        variables = json.loads(Path(self.vars_path).read_text(encoding="utf-8"))
        self.assertEqual(variables["answer"], "42")

    def test_wait_done_ok_with_value_line(self):
        self.write_handle()
        self.p["exit"].write_text(json.dumps({"returncode": 0, "stderr": ""}),
                                  encoding="utf-8")
        self.p["result"].write_text(
            json.dumps({"result": "Counted the rows.\nVALUE: 42",
                        "is_error": False, "permission_denials": []}),
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

    def test_wait_denied_hides_the_refused_command(self):
        self.write_handle()
        self.p["exit"].write_text(json.dumps({"returncode": 0, "stderr": ""}),
                                  encoding="utf-8")
        self.p["result"].write_text(json.dumps({
            "result": "done", "is_error": False,
            "permission_denials": [{"tool_name": "Bash", "tool_use_id": "t1",
                                    "tool_input": {"command": "secret-task-cmd"}}],
        }), encoding="utf-8")
        code, out, err = self.wait()
        self.assertEqual(code, 1)
        self.assertEqual(out, f"error: denied (details: {self.p['result']})")

    def test_wait_running_within_max(self):
        self.write_handle()
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
        self.write_handle()
        self.p["exit"].write_text(json.dumps({"returncode": 0, "stderr": ""}),
                                  encoding="utf-8")
        self.p["result"].write_text(json.dumps({"result": "42", "is_error": False,
                                                "permission_denials": []}),
                                    encoding="utf-8")
        code1, out1, _ = self.wait()
        code2, out2, _ = self.wait()
        self.assertEqual((code1, code2), (0, 0))
        self.assertEqual(out1, out2)
        log = self.run_dir / "steps.log"
        lines = [l for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(lines), 1,
                         "a second wait replays the verdict instead of "
                         "redoing it, so it logs nothing new")

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
        fake_diag.assert_called_once()

    def test_wait_re_dispatch_clears_wait_record(self):
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
        self.write_handle()
        self.p["exit"].write_text('{"returncode": 0, "std', encoding="utf-8")
        code, out, err = self.wait(max_=0.2)
        self.assertEqual(code, 10)
        self.assertEqual(out, "running")

    def test_wait_expect_file_resolves_against_xml_dir_not_cwd(self):
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
        p2["result"].write_text(json.dumps({"result": "wrote it", "is_error": False,
                                            "permission_denials": []}),
                                encoding="utf-8")
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

    def write_behavioral_exit(self, ledger):
        self.p["exit"].write_text(json.dumps({"returncode": 1, "stderr": ""}),
                                  encoding="utf-8")
        self.p["result"].write_text(json.dumps({
            "result": "x", "is_error": True, "terminal_reason": "max_turns",
            "permission_denials": [],
        }), encoding="utf-8")
        self.p["attempts"].write_text(json.dumps(ledger), encoding="utf-8")

    def ledger(self):
        return json.loads(self.p["attempts"].read_text(encoding="utf-8"))

    def test_wait_leaves_a_failure_to_retry_while_retries_remain(self):
        self.write_xml(retry="1", on_error="debug")
        self.write_handle()
        self.write_behavioral_exit([{"seq": 1, "class": "behavioral", "ended_at": 1}])
        fake_diag = mock.Mock(return_value=Diagnosis("RETRY", "x", fix_instruction="f"))
        with mock.patch("wfrun.__main__.adp.diagnose", fake_diag):
            code, out, err = self.wait()
        self.assertEqual(code, 1)
        fake_diag.assert_not_called()

    def test_wait_diagnoses_once_retries_are_spent_and_records_retry(self):
        self.write_xml(retry="1", on_error="debug")
        self.write_handle(attempt=2)
        self.write_behavioral_exit([{"seq": 1, "class": "behavioral", "ended_at": 1},
                                    {"seq": 2, "class": "behavioral", "ended_at": 2}])
        fake_diag = mock.Mock(return_value=Diagnosis(
            "RETRY", "x", fix_instruction="add --force"))
        with mock.patch("wfrun.__main__.adp.diagnose", fake_diag):
            code, out, err = self.wait()
        self.assertEqual(code, 1)
        fake_diag.assert_called_once()
        self.assertEqual([entry.get("debug") for entry in self.ledger()],
                         [None, "RETRY"])

    def test_wait_records_a_debug_fail(self):
        self.write_xml(on_error="debug")
        self.write_handle()
        self.write_behavioral_exit([{"seq": 1, "class": "behavioral", "ended_at": 1}])
        fake_diag = mock.Mock(return_value=Diagnosis("FAIL", "no fix"))
        with mock.patch("wfrun.__main__.adp.diagnose", fake_diag):
            self.wait()
        self.assertEqual(self.ledger()[0].get("debug"), "FAIL")

    def test_wait_does_not_diagnose_after_the_fixed_rerun(self):
        self.write_xml(on_error="debug")
        self.write_handle(attempt=2)
        self.write_behavioral_exit([
            {"seq": 1, "class": "guardrail", "ended_at": 1, "debug": "RETRY"},
            {"seq": 2, "class": "behavioral", "ended_at": 2}])
        fake_diag = mock.Mock(return_value=Diagnosis("RETRY", "x", fix_instruction="f"))
        with mock.patch("wfrun.__main__.adp.diagnose", fake_diag):
            code, out, err = self.wait()
        self.assertEqual(code, 1)
        fake_diag.assert_not_called()

    def test_wait_records_an_expect_file_miss_as_behavioral(self):
        Path(self.xml).write_text(
            self.XML.format(max="10", retry="0", on_error="fail").replace(
                '<step id="s2" role="w"><task>write it</task></step>',
                '<step id="s2" role="w" expect-file="made.txt">'
                '<task>write it</task></step>'),
            encoding="utf-8")
        p2 = self.paths("s2")
        p2["handle"].write_text(json.dumps({
            "wrapper_pid": 1, "attempt": 1, "cycle": 1, "started_at": time.time(),
            "timeout": 600, "xml": self.xml, "step_id": "s2",
            "run_dir": str(self.run_dir), "exit_path": str(p2["exit"]),
            "result_path": str(p2["result"]), "wait_path": str(p2["wait"]),
        }), encoding="utf-8")
        p2["exit"].write_text(json.dumps({"returncode": 0, "stderr": ""}),
                              encoding="utf-8")
        p2["result"].write_text(json.dumps({"result": "wrote it", "is_error": False,
                                            "permission_denials": []}),
                                encoding="utf-8")
        p2["attempts"].write_text(json.dumps([{"seq": 1, "class": "ok", "ended_at": 1}]),
                                  encoding="utf-8")
        code, out, err = self.run_cli(
            ["wait", str(p2["handle"]), "--max", "5", "--vars", self.vars_path])
        self.assertEqual(code, 1, out)
        ledger = json.loads(p2["attempts"].read_text(encoding="utf-8"))
        self.assertEqual(ledger[0]["class"], "behavioral",
                         "an ok left in the ledger would open a new cycle")
        fake_proc = mock.Mock()
        fake_proc.pid = 99
        with mock.patch("wfrun.__main__.subprocess.Popen", return_value=fake_proc):
            code, out, err = self.run_cli(
                ["dispatch", self.xml, "s2", "--vars", self.vars_path,
                 "--run-dir", str(self.run_dir)])
        self.assertEqual(code, 1, "retry=0, judged inside cycle 1")
        self.assertIn("cycle 1", err)
        self.assertFalse(self.paths("s2", cycle=2)["handle"].exists())


class DispatchLivenessTests(DispatchWaitTestCase):
    def lock_paths(self, step_id="s1"):
        return _lock_paths(self.run_dir, step_id)

    def write_marker(self, token, cycle=1, attempt=1, pid=None, step_id="s1"):
        _write_marker(self.lock_paths(step_id)["marker"], token,
                      cycle=cycle, attempt=attempt, pid=pid)

    def touch_run_lock(self, step_id="s1"):
        path = self.lock_paths(step_id)["run_lock"]
        path.touch()
        return path

    def write_running_handle(self, step_id="s1", cycle=1, token="tok",
                             attempt=1, started_at=None, timeout=600,
                             wrapper_pid=4242):
        p = self.paths(step_id, cycle)
        p["handle"].parent.mkdir(parents=True, exist_ok=True)
        p["handle"].write_text(json.dumps({
            "wrapper_pid": wrapper_pid, "attempt": attempt, "cycle": cycle,
            "started_at": started_at if started_at is not None else time.time(),
            "timeout": timeout, "token": token,
            "xml": self.xml, "step_id": step_id, "run_dir": str(self.run_dir),
            "exit_path": str(p["exit"]), "result_path": str(p["result"]),
            "system_path": str(p["system"]), "prompt_path": str(p["prompt"]),
            "wait_path": str(p["wait"]),
        }), encoding="utf-8")
        return p

    def ledger_for(self, step_id="s1", cycle=1):
        return stepio.load_attempts(self.paths(step_id, cycle)["attempts"])

    def dispatch(self, step_id="s1", extra=None, fake_pid=4242, side_effect=None):
        fake_proc = mock.Mock()
        fake_proc.pid = fake_pid
        argv = ["dispatch", self.xml, step_id, "--vars", self.vars_path,
               "--run-dir", str(self.run_dir)]
        if extra:
            argv += extra
        if side_effect:
            patch_kwargs = {"side_effect": side_effect}
        else:
            patch_kwargs = {"return_value": fake_proc}
        with mock.patch("wfrun.__main__.subprocess.Popen",
                        **patch_kwargs) as popen:
            code, out, err = self.run_cli(argv)
        return code, out, err, popen

    def test_dispatch_refuses_when_marker_token_matches_and_run_lock_is_held(self):
        self.seed_cycle([], cycle=1)
        self.write_running_handle(token="tok", attempt=1)
        self.write_marker(token="tok", attempt=1)
        handle_before = self.paths()["handle"].read_bytes()
        ledger_before = self.ledger_for()
        lock = _AttemptLock(self.lock_paths()["run_lock"])
        lock.acquire()
        try:
            code, out, err, popen = self.dispatch()
            self.assertEqual(code, 1,
                "a marker token matching the handle's, with the run lock "
                "HELD, means attempt 1's wrapper is still alive; dispatch "
                "must refuse instead of launching a second one")
            popen.assert_not_called()
            self.assertIn("wait", err,
                "the refusal must name wait as the next move")
            self.assertIn(str(self.paths()["handle"]), err,
                "the refusal must name the handle to pass to wait")
            self.assertEqual(self.paths()["handle"].read_bytes(),
                             handle_before,
                "a refused dispatch must leave the handle untouched")
            self.assertEqual(self.ledger_for(), ledger_before,
                "a refused dispatch must leave the ledger untouched")
        finally:
            lock.release()
            lock.close()

    def test_dispatch_falls_back_to_time_running_when_marker_is_absent(self):
        self.seed_cycle([], cycle=1)
        self.write_running_handle(token="tok", attempt=1,
                                  started_at=time.time())
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 1,
            "no marker for this token, with a deadline not yet reached, "
            "must fall back to the time-based running judgment and refuse")
        popen.assert_not_called()

    def test_dispatch_refuses_when_run_lock_stays_held_past_the_deadline(self):
        self.seed_cycle([], cycle=1)
        self.write_running_handle(token="tok", attempt=1,
                                  started_at=time.time() - 700, timeout=1)
        self.write_marker(token="tok", attempt=1)
        lock = _AttemptLock(self.lock_paths()["run_lock"])
        lock.acquire()
        try:
            code, out, err, popen = self.dispatch()
            self.assertEqual(code, 1,
                "the run lock stays held past the handle's own deadline, "
                "which is overdue rather than gone; dispatch must still "
                "refuse and record nothing")
            popen.assert_not_called()
            self.assertEqual(self.ledger_for(), [])
            self.assertIn("stop and report", err,
                "an overdue holder cannot be waited out or replaced; the "
                "refusal must tell the orchestrator to stop and report")
        finally:
            lock.release()
            lock.close()

    def test_dispatch_settles_gone_run_lock_evidence_despite_a_live_pid_in_handle(self):
        self.seed_cycle([], cycle=1)
        self.write_running_handle(token="tok", attempt=1,
                                  started_at=time.time(), timeout=600,
                                  wrapper_pid=os.getpid())
        self.write_marker(token="tok", attempt=1)
        self.touch_run_lock()
        code, out, err, popen = self.dispatch()
        self.assertEqual(len(self.ledger_for()), 1,
            "a free run lock with a marker matching the handle's token is "
            "gone evidence: settle must record aborted(1) before this "
            "cycle's next attempt is dispatched, even though the handle's "
            "own pid (this test's own, alive) is still running -- pid "
            "liveness is not the signal")

    def test_dispatch_settles_gone_deadline_evidence_when_marker_is_absent(self):
        self.seed_cycle([], cycle=1)
        self.write_running_handle(token="tok", attempt=1,
                                  started_at=time.time() - 99999, timeout=1)
        code, out, err, popen = self.dispatch()
        self.assertEqual(len(self.ledger_for()), 1,
            "no marker and a deadline long past is gone(deadline) "
            "evidence: settle must record aborted(1) before dispatch "
            "proceeds")

    def test_dispatch_spends_the_single_abort_redispatch_per_cycle(self):
        self.seed_cycle([{"seq": 1, "class": "aborted", "ended_at": 1,
                          "evidence": "deadline", "detected_by": "dispatch"}],
                        cycle=1)
        self.write_running_handle(token="tok", attempt=2,
                                  started_at=time.time() - 99999, timeout=1)
        code, out, err, popen = self.dispatch()
        self.assertEqual(len(self.ledger_for()), 2,
            "a second gone attempt in the same cycle must settle as "
            "aborted(2) before the redispatch cap (one abort per cycle) "
            "refuses the call")
        popen.assert_not_called()
        self.assertEqual(code, 1,
            "the one re-dispatch after an abort is spent, so the call "
            "must refuse")
        self.assertIn("stop and report", err,
            "a spent abort re-dispatch leaves nothing to retry; the "
            "refusal must tell the orchestrator to stop and report")

    def test_dispatch_excludes_aborted_attempts_from_the_retry_count(self):
        self.write_xml(retry="1")
        self.seed_cycle([{"seq": 1, "class": "aborted", "ended_at": 1,
                          "evidence": "deadline", "detected_by": "dispatch"},
                         {"seq": 2, "class": "timeout", "ended_at": 2}],
                        cycle=1)
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 0,
            "an aborted attempt must not count against the retry budget: "
            "with retry=1 and one real timeout failure besides the "
            "aborted one, a retry is still owed")
        self.assertIn("seq=3", out)

    def test_dispatch_fix_admission_follows_the_ladder_view_after_an_abort(self):
        self.write_xml(on_error="debug")
        for extra in (None, ["--fix", "do it differently"]):
            with self.subTest(extra=extra):
                self.seed_cycle([{"seq": 1, "class": "behavioral",
                                  "ended_at": 1, "debug": "RETRY"}], cycle=1)
                self.write_running_handle(
                    token="tok", attempt=2,
                    started_at=time.time() - 99999, timeout=1)
                code, out, err, popen = self.dispatch(extra=extra)
                if extra is None:
                    self.assertEqual(len(self.ledger_for()), 2,
                        "the unfinished attempt 2 is gone and must settle "
                        "as aborted(2) even on the refused half, before "
                        "--fix is named as the only way forward")
                    self.assertEqual(code, 1,
                        "a debug RETRY is still owed its fixed re-run, so a "
                        "plain dispatch must be refused")
                    self.assertIn("--fix", err,
                        "the refusal must name --fix as the way forward")
                else:
                    self.assertIn("seq=3", out,
                        "with --fix admitted from the RETRY still in the "
                        "ladder view, the settled aborted(2) makes this "
                        "seq 3, not seq 2")

    def test_dispatch_abort_cap_message_omits_the_new_cycle_hint(self):
        self.write_xml(retry="0", on_error="debug")
        self.seed_cycle([
            {"seq": 1, "class": "behavioral", "ended_at": 1, "debug": "RETRY"},
            {"seq": 2, "class": "aborted", "ended_at": 2,
             "evidence": "deadline", "detected_by": "dispatch"},
            {"seq": 3, "class": "aborted", "ended_at": 3,
             "evidence": "deadline", "detected_by": "wait"},
        ], cycle=1)
        code, out, err, popen = self.dispatch()
        self.assertNotIn("--new-cycle", err,
            "the abort-redispatch cap is judged before the total-attempt "
            "cap, so a run already spent on aborts must be told to stop, "
            "not to pass --new-cycle")
        self.assertIn("stop", err,
            "the refusal must tell the orchestrator to stop")

    def test_dispatch_new_cycle_flag_does_not_skip_the_liveness_check(self):
        with self.subTest(case="held"):
            self.seed_cycle([], cycle=1)
            self.write_running_handle(token="tok", attempt=1)
            self.write_marker(token="tok", attempt=1)
            lock = _AttemptLock(self.lock_paths()["run_lock"])
            lock.acquire()
            try:
                code, out, err, popen = self.dispatch(extra=["--new-cycle"])
                self.assertEqual(code, 1,
                    "--new-cycle must not bypass the running-wrapper "
                    "check: the run lock is still held, so dispatch must "
                    "still refuse")
            finally:
                lock.release()
                lock.close()
        with self.subTest(case="gone"):
            self.seed_cycle([], cycle=1)
            self.write_running_handle(token="tok", attempt=1,
                                      started_at=time.time() - 99999,
                                      timeout=1)
            code, out, err, popen = self.dispatch(extra=["--new-cycle"])
            self.assertEqual(len(self.ledger_for(cycle=1)), 1,
                "--new-cycle settles the outgoing cycle's gone attempt as "
                "aborted before opening the new cycle, so cycle 1's own "
                "ledger must gain the entry even though dispatch moves on "
                "to cycle 2")

    def test_dispatch_does_not_double_record_an_already_settled_abort(self):
        self.write_xml(retry="0")
        self.seed_cycle([{"seq": 1, "class": "aborted", "ended_at": 1,
                          "evidence": "deadline", "detected_by": "dispatch"}],
                        cycle=1)
        self.write_running_handle(token="tok", attempt=1,
                                  started_at=time.time() - 99999, timeout=1)
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 0,
            "attempt 1 is already settled as aborted in the ledger, so a "
            "ledger match on seq makes it done, not gone again; retry=0 "
            "must not block this dispatch since the ladder view excludes "
            "the aborted attempt from the retry count")
        self.assertIn("seq=2", out)

    def test_dispatch_refuses_when_the_state_lock_is_contended(self):
        self.seed_cycle([], cycle=1)
        lock = _AttemptLock(self.lock_paths()["state_lock"])
        lock.acquire()
        try:
            code, out, err, popen = self.dispatch()
            self.assertEqual(code, 1,
                "another dispatch or wait already holds the per-step "
                "state lock; this call must refuse rather than proceed "
                "concurrently")
            popen.assert_not_called()
            self.assertIn("another dispatch or wait is working on it", err)
        finally:
            lock.release()
            lock.close()

    def test_dispatch_wrapper_argv_carries_the_new_admission_flags(self):
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 0, err)
        wrapper_argv = popen.call_args[0][0]
        self.assertIn("--handle-file", wrapper_argv,
            "the wrapper's admission step needs the handle path and this "
            "attempt's token on its own argv, so dispatch must pass both")
        self.assertIn("--token", wrapper_argv)

    def test_dispatch_restores_the_handle_when_popen_raises(self):
        def raise_oserror(*a, **k):
            raise OSError("launch failed")
        try:
            code, out, err, popen = self.dispatch(side_effect=raise_oserror)
        except OSError as e:
            self.fail(
                "dispatch must catch a Popen launch failure, restore the "
                "prior handle bytes (or remove it for a new cycle), and "
                f"exit 2; it let the OSError propagate instead: {e!r}")
        else:
            self.assertEqual(code, 2)
            self.assertFalse(self.paths()["handle"].exists(),
                "a launch that never happened must not leave the new "
                "attempt's handle behind: an unlaunched token would read "
                "as a running attempt until its deadline")

    def test_dispatch_after_a_failed_launch_reruns_the_recorded_attempt(self):
        self.write_xml(retry="1")
        self.seed_cycle([{"seq": 1, "class": "timeout", "ended_at": 1}],
                        cycle=1)
        p = self.write_running_handle(token="tok", attempt=1)
        self.write_marker(token="tok", attempt=1)
        self.touch_run_lock()
        p["exit"].write_text(json.dumps({"returncode": 1, "stderr": ""}),
                             encoding="utf-8")
        p["wait"].write_text(json.dumps(
            {"attempt": 1, "status": "error", "message": "timeout"}),
            encoding="utf-8")

        def raise_oserror(*a, **k):
            raise OSError("launch failed")

        code, out, err, popen = self.dispatch(side_effect=raise_oserror)
        self.assertEqual(code, 2, err)
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 0,
            "a failed launch restores the earlier attempt's handle while "
            "its exit.json is already gone; a recorded non-ok class in "
            "that state must still be judged by the retry ladder, not "
            "refused for good")
        self.assertIn("seq=2", out)

    def test_dispatch_counts_a_settled_abort_toward_the_workflow_max(self):
        self.write_xml(max_="1")
        self.seed_cycle([], cycle=1)
        self.write_running_handle(token="tok", attempt=1,
                                  started_at=time.time() - 99999, timeout=1)
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 1,
            "settling the gone attempt appends an aborted line to "
            "steps.log, which counts toward wf.max; with max=1 that "
            "spends the whole budget before this call could ever launch "
            "a second attempt")
        popen.assert_not_called()

    def test_dispatch_rereads_the_ledger_after_a_free_probe_before_recording(self):
        from wfrun import runlock
        self.seed_cycle([], cycle=1)
        self.write_running_handle(token="tok", attempt=1)
        self.write_marker(token="tok", attempt=1)

        def sneaky_probe(path):
            stepio.append_attempt(self.paths()["attempts"], 1, "ok")
            return runlock.FREE

        with mock.patch.object(runlock, "probe", side_effect=sneaky_probe):
            code, out, err, popen = self.dispatch()
        self.assertEqual(len(self.ledger_for()), 1)
        self.assertEqual(self.ledger_for()[0].get("class"), "ok",
            "a ledger entry that appears between the probe and the record "
            "must win: dispatch has to re-read the ledger after a free "
            "probe before settling, or it will overwrite a real result "
            "with a false abort")

    def test_dispatch_checks_the_marker_before_probing_the_run_lock(self):
        from wfrun import runlock
        self.seed_cycle([], cycle=1)
        self.write_running_handle(token="tok", attempt=1,
                                  started_at=time.time())

        def sneaky_probe(path):
            self.write_marker(token="tok", attempt=1)
            return runlock.FREE

        with mock.patch.object(runlock, "probe", side_effect=sneaky_probe):
            code, out, err, popen = self.dispatch()
        self.assertEqual(code, 1,
            "the marker must be read before the probe so a wrapper that "
            "wins the run lock during the probe's own call is still seen "
            "as the current token's holder; reading the marker after the "
            "probe would miss it and launch a second wrapper")
        popen.assert_not_called()

    def test_dispatch_refuses_when_run_lock_is_held_despite_a_done_ledger(self):
        self.seed_cycle([{"seq": 1, "class": "ok", "ended_at": 1}], cycle=1)
        lock = _AttemptLock(self.lock_paths()["run_lock"])
        lock.acquire()
        try:
            code, out, err, popen = self.dispatch()
            self.assertEqual(code, 1,
                "a successful ledger normally opens a new cycle "
                "automatically, but a still-held run lock means some "
                "wrapper for this step is alive regardless of what the "
                "ledger says; dispatch must refuse")
            popen.assert_not_called()
        finally:
            lock.release()
            lock.close()

    def test_dispatch_refuses_an_unrecorded_completed_attempt(self):
        self.seed_cycle([{"seq": 1, "class": "ok", "ended_at": 1}], cycle=1)
        p = self.write_running_handle(token="tok", attempt=1)
        p["exit"].write_text(json.dumps({"returncode": 0, "stderr": ""}),
                             encoding="utf-8")
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 1,
            "attempt 1 finished (exit.json exists and the ledger already "
            "carries seq 1) but wait never ran, so nothing has been "
            "checked against vars.json or expect-file; dispatch must "
            "refuse until wait records it, instead of trusting the "
            "ledger alone and opening a new cycle")
        popen.assert_not_called()

    def test_dispatch_refuses_an_ok_ledger_entry_whose_exit_json_is_missing(self):
        self.seed_cycle([{"seq": 1, "class": "ok", "ended_at": 1}], cycle=1)
        self.write_running_handle(token="tok", attempt=1)
        self.write_marker(token="tok", attempt=1)
        self.touch_run_lock()
        code, out, err, popen = self.dispatch()
        self.assertEqual(code, 1,
            "the ledger holds ok for attempt 1 but exit.json never "
            "appeared and the wrapper is gone, so wait can never apply "
            "its result; a dispatch would open a new cycle and discard "
            "the finished attempt")
        popen.assert_not_called()
        self.assertFalse(self.paths(cycle=2)["handle"].exists(),
            "a refused dispatch must not open a new cycle")
        self.assertIn("stop and report", err,
            "nothing can recover this state from the command line; the "
            "refusal must tell the orchestrator to stop and report")

    def test_dispatch_refuses_when_the_handle_is_unreadable_and_the_run_lock_is_held(self):
        p = self.paths()
        p["handle"].parent.mkdir(parents=True, exist_ok=True)
        p["handle"].write_text("{not json", encoding="utf-8")
        with self.subTest(case="held"):
            lock = _AttemptLock(self.lock_paths()["run_lock"])
            lock.acquire()
            try:
                code, out, err, popen = self.dispatch()
                self.assertEqual(code, 1,
                    "an unreadable handle says nothing about the wrapper, "
                    "and a held run lock says one is alive; dispatch must "
                    "refuse instead of launching a second one")
                popen.assert_not_called()
            finally:
                lock.release()
                lock.close()
        with self.subTest(case="free"):
            code, out, err, popen = self.dispatch()
            self.assertEqual(code, 0,
                "with the run lock free, an unreadable handle alone must "
                f"not stop a dispatch: {err}")

    def assert_dispatch_falls_open(self, err_no):
        from wfrun import runlock
        error = OSError(err_no, "lock call failed")
        with mock.patch.object(runlock, "try_acquire", side_effect=error):
            code, out, err, popen = self.dispatch()
        self.assertEqual(code, 0, err)
        self.assertIn("warning", err,
            "proceeding without the state lock must be announced on stderr")
        popen.assert_called_once()

    def test_dispatch_falls_open_when_locking_is_unsupported(self):
        import errno
        self.assert_dispatch_falls_open(errno.ENOLCK)

    def test_dispatch_falls_open_on_any_other_lock_error(self):
        import errno
        self.assert_dispatch_falls_open(errno.EIO)


class WrapperRunLockTests(DispatchWaitTestCase):
    def setUp(self):
        super().setUp()
        self.p = self.paths()
        self.p["system"].parent.mkdir(parents=True)
        self.p["system"].write_text("<role>w</role>", encoding="utf-8")
        self.p["prompt"].write_text("do the task", encoding="utf-8")
        self.handle_path = self.p["handle"]

    def write_handle(self, token="tok", attempt=1):
        self.handle_path.write_text(json.dumps({
            "wrapper_pid": 0, "attempt": attempt, "cycle": 1,
            "started_at": time.time(), "timeout": 600, "token": token,
            "xml": self.xml, "step_id": "s1", "run_dir": str(self.run_dir),
            "exit_path": str(self.p["exit"]),
            "result_path": str(self.p["result"]),
            "system_path": str(self.p["system"]),
            "prompt_path": str(self.p["prompt"]),
            "wait_path": str(self.p["wait"]),
        }), encoding="utf-8")

    def run_wrapper(self, extra):
        argv = ["_wrapper", "--system-file", str(self.p["system"]),
               "--prompt-file", str(self.p["prompt"]),
               "--exit-file", str(self.p["exit"]),
               "--result-file", str(self.p["result"]),
               "--attempts-file", str(self.p["attempts"]),
               "--seq", "1", "--cwd", str(self.dir), "--timeout", "600"]
        argv += extra
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(argv)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue().strip(), err.getvalue().strip()

    def assert_flags_accepted(self, code, err, why):
        self.assertNotEqual(code, 2, why)
        self.assertNotIn("unrecognized arguments", err, why)

    def test_wrapper_admission_observes_its_own_held_lock_and_marker(self):
        from wfrun import runlock
        self.write_handle(token="tok", attempt=1)
        lock_paths = _lock_paths(self.run_dir)
        seen = {}

        def launch(*a, **k):
            seen["probe"] = runlock.probe(str(lock_paths["run_lock"]))
            seen["marker"] = json.loads(
                lock_paths["marker"].read_text(encoding="utf-8"))
            return (0, json.dumps({"result": "x", "is_error": False}), "")

        with mock.patch.object(claude_cli, "_launch", side_effect=launch):
            code, out, err = self.run_wrapper([
                "--handle-file", str(self.handle_path), "--token", "tok"])
        self.assert_flags_accepted(code, err,
            "the wrapper must accept --handle-file/--token so it can take "
            "the run lock, write a marker carrying its own pid and this "
            "attempt's token, and only then launch claude")
        self.assertTrue(lock_paths["marker"].is_file())
        self.assertEqual(seen.get("probe"), runlock.HELD,
            "the run lock must already be held while claude runs")
        marker = seen.get("marker", {})
        self.assertEqual(
            (marker.get("token"), marker.get("cycle"),
             marker.get("attempt"), marker.get("pid")),
            ("tok", 1, 1, os.getpid()),
            "the marker written before the launch must carry this "
            "attempt's token, cycle, seq and the wrapper's own pid")

    def test_wrapper_releases_the_run_lock_after_writing_exit_and_ledger(self):
        from wfrun import runlock
        self.write_handle(token="tok", attempt=1)
        with mock.patch.object(claude_cli, "_launch",
                               return_value=(0, json.dumps(
                                   {"result": "x", "is_error": False}), "")):
            code, out, err = self.run_wrapper([
                "--handle-file", str(self.handle_path), "--token", "tok"])
        self.assert_flags_accepted(code, err,
            "once the wrapper has written exit.json and the ledger entry, "
            "it must release the run lock so the observer's probe reads "
            "free")
        self.assertTrue(self.p["exit"].is_file())
        self.assertEqual(
            [a.get("seq") for a in stepio.load_attempts(self.p["attempts"])],
            [1], "the finished attempt must be in the ledger")
        self.assertEqual(
            runlock.probe(str(_lock_paths(self.run_dir)["run_lock"])),
            runlock.FREE, "the run lock must be free once the wrapper is done")

    def test_wrapper_admission_rejects_a_token_mismatch(self):
        self.write_handle(token="handle-token", attempt=1)
        launch = mock.Mock()
        with mock.patch.object(claude_cli, "_launch", launch):
            code, out, err = self.run_wrapper([
                "--handle-file", str(self.handle_path),
                "--token", "argv-token"])
        self.assert_flags_accepted(code, err,
            "a token on argv that does not match the handle's own token "
            "means a newer dispatch has replaced this attempt; the "
            "wrapper must accept the flags, then refuse admission and "
            "touch nothing, rather than reject the call at the parser")
        launch.assert_not_called()
        written = [name for name in ("exit", "result", "attempts")
                   if self.p[name].exists()]
        if _lock_paths(self.run_dir)["marker"].exists():
            written.append("marker")
        self.assertEqual(written, [],
            "a refused admission must leave no marker, exit, result or "
            "ledger entry")

    def test_wrapper_admission_rejects_a_seq_already_in_the_ledger(self):
        self.write_handle(token="tok", attempt=1)
        self.p["attempts"].write_text(
            json.dumps([{"seq": 1, "class": "ok", "ended_at": 1}]),
            encoding="utf-8")
        launch = mock.Mock()
        with mock.patch.object(claude_cli, "_launch", launch):
            code, out, err = self.run_wrapper([
                "--handle-file", str(self.handle_path), "--token", "tok"])
        self.assert_flags_accepted(code, err,
            "seq 1 is already in the ledger, so this admission is a "
            "duplicate of an already-recorded attempt; the wrapper must "
            "accept the flags, then refuse admission and touch nothing")
        launch.assert_not_called()

    def test_wrapper_admission_rejects_when_run_lock_stays_held(self):
        self.write_handle(token="tok", attempt=1)
        lock = _AttemptLock(_lock_paths(self.run_dir)["run_lock"])
        lock.acquire()
        launch = mock.Mock()
        try:
            with mock.patch.object(claude_cli, "_launch", launch):
                code, out, err = self.run_wrapper([
                    "--handle-file", str(self.handle_path),
                    "--token", "tok"])
            self.assert_flags_accepted(code, err,
                "the run lock is already held past the retry window, so "
                "this wrapper must give up admission rather than run "
                "alongside whoever holds it")
            launch.assert_not_called()
        finally:
            lock.release()
            lock.close()

    def test_wrapper_admission_retries_the_run_lock_briefly(self):
        self.write_handle(token="tok", attempt=1)
        lock = _AttemptLock(_lock_paths(self.run_dir)["run_lock"])
        lock.acquire()

        def release_soon():
            lock.release()
            lock.close()

        timer = threading.Timer(0.05, release_soon)
        timer.start()
        self.addCleanup(timer.cancel)
        try:
            with mock.patch.object(claude_cli, "_launch",
                                   return_value=(0, json.dumps(
                                       {"result": "x",
                                        "is_error": False}), "")) as launch:
                code, out, err = self.run_wrapper([
                    "--handle-file", str(self.handle_path),
                    "--token", "tok"])
            self.assert_flags_accepted(code, err,
                "a run lock released about 50 ms into admission must "
                "still be picked up by a short retry, so the wrapper "
                "launches instead of giving up immediately")
            launch.assert_called_once()
        finally:
            timer.join()

    def wrapper_with_lock_error(self, err_no, lock_suffix):
        from wfrun import runlock
        self.write_handle(token="tok", attempt=1)
        real_try_acquire = runlock.try_acquire

        def try_acquire(path):
            if str(path).endswith(lock_suffix):
                raise OSError(err_no, "lock call failed")
            return real_try_acquire(path)

        launch = mock.Mock(return_value=(0, json.dumps(
            {"result": "x", "is_error": False}), ""))
        with mock.patch.object(runlock, "try_acquire",
                               side_effect=try_acquire), \
                mock.patch.object(claude_cli, "_launch", launch):
            self.run_wrapper([
                "--handle-file", str(self.handle_path), "--token", "tok"])
        return launch

    def test_wrapper_launches_when_the_state_lock_is_unsupported(self):
        import errno
        self.wrapper_with_lock_error(
            errno.ENOLCK, "_state.lock").assert_called_once()

    def test_wrapper_launches_when_the_run_lock_is_unsupported(self):
        import errno
        self.wrapper_with_lock_error(
            errno.ENOLCK, "_run.lock").assert_called_once()

    def test_wrapper_does_not_launch_on_another_state_lock_error(self):
        import errno
        launch = self.wrapper_with_lock_error(errno.EIO, "_state.lock")
        launch.assert_not_called()

    def test_wrapper_does_not_launch_on_another_run_lock_error(self):
        import errno
        launch = self.wrapper_with_lock_error(errno.EIO, "_run.lock")
        launch.assert_not_called()

    def test_wrapper_retries_a_handle_read_that_fails_once(self):
        from wfrun.__main__ import _read_json_tolerant
        self.write_handle(token="tok", attempt=1)
        reads = []

        def read_unreadable_once(path):
            reads.append(path)
            if len(reads) == 1:
                return None
            return _read_json_tolerant(path)

        launch = mock.Mock(return_value=(0, json.dumps(
            {"result": "x", "is_error": False}), ""))
        with mock.patch("wfrun.__main__._read_json_tolerant",
                        side_effect=read_unreadable_once), \
                mock.patch.object(claude_cli, "_launch", launch):
            self.run_wrapper([
                "--handle-file", str(self.handle_path), "--token", "tok"])
        launch.assert_called_once()


class WaitLivenessTests(DispatchWaitTestCase):
    def setUp(self):
        super().setUp()
        self.p = self.paths()
        self.p["exit"].parent.mkdir(parents=True)
        self.p["system"].write_text("<role>w</role>", encoding="utf-8")
        self.p["prompt"].write_text("answer it", encoding="utf-8")

    def write_handle(self, token="tok", started_at=None, timeout=600,
                     attempt=1, with_token=True):
        handle = {
            "wrapper_pid": 1, "attempt": attempt, "cycle": 1,
            "started_at": started_at if started_at is not None else time.time(),
            "timeout": timeout, "xml": self.xml, "step_id": "s1",
            "run_dir": str(self.run_dir), "exit_path": str(self.p["exit"]),
            "result_path": str(self.p["result"]),
            "system_path": str(self.p["system"]),
            "prompt_path": str(self.p["prompt"]),
            "wait_path": str(self.p["wait"]),
        }
        if with_token:
            handle["token"] = token
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

    def ledger(self):
        return stepio.load_attempts(self.p["attempts"])

    def touch_run_lock(self):
        path = _lock_paths(self.run_dir)["run_lock"]
        path.touch()
        return path

    def write_marker(self, token, cycle=1, attempt=1, pid=None):
        _write_marker(_lock_paths(self.run_dir)["marker"], token,
                      cycle=cycle, attempt=attempt, pid=pid)

    def test_wait_settles_gone_run_lock_immediately_without_waiting_out_max(self):
        self.write_handle(token="tok", started_at=time.time(), timeout=600)
        self.write_marker(token="tok")
        self.touch_run_lock()
        code, out, err = self.wait(max_=0.2)
        self.assertEqual(code, 3,
            "a marker matching the handle's token with a free run lock is "
            "gone evidence, valid immediately -- wait must settle and "
            "return 3 without spending its --max budget waiting on a "
            "wrapper whose own timeout is nowhere near reached")
        self.assertEqual(len(self.ledger()), 1)
        self.assertEqual(
            [e.get("status") for e in self.log_entries()], ["aborted"],
            "the settled abort must also be logged, or the workflow's "
            "max never counts it")

    def test_wait_reports_running_with_a_note_while_within_overdue_grace(self):
        self.write_handle(token="tok", started_at=time.time() - 40,
                          timeout=1)
        self.write_marker(token="tok")
        lock = _AttemptLock(_lock_paths(self.run_dir)["run_lock"])
        lock.acquire()
        try:
            code, out, err = self.wait(max_=0.2)
            self.assertEqual(code, 10,
                "the run lock is still held past the handle's own "
                "deadline, which is only overdue rather than gone while "
                "within the grace window; wait must keep reporting "
                "running (with a stderr note) instead of aborting a "
                "wrapper that is merely late")
            self.assertIn("still holds the run lock", err,
                "the running report must say why the attempt is past its "
                "own deadline yet not aborted")
        finally:
            lock.release()
            lock.close()

    def test_wait_reports_aborted_with_pid_and_stop_past_overdue_grace(self):
        self.write_handle(token="tok", started_at=time.time() - 731,
                          timeout=1)
        self.write_marker(token="tok")
        lock = _AttemptLock(_lock_paths(self.run_dir)["run_lock"])
        lock.acquire()
        try:
            code, out, err = self.wait(max_=0.2)
            self.assertEqual(code, 3,
                "a holder still running after the grace window is "
                "reported as aborted")
            self.assertIn("stop", err,
                "once the run lock has stayed held past the deadline for "
                "longer than the grace window, wait must give up and "
                "name the holder's pid and say to stop, even though old "
                "code's own no-exit-json message never says either")
            self.assertIn(str(os.getpid()), err,
                "the report must name the holder's pid, taken from the "
                "marker")
        finally:
            lock.release()
            lock.close()

    def test_wait_is_idempotent_after_settling_an_abort(self):
        self.write_handle(token="tok", started_at=time.time(), timeout=600)
        self.write_marker(token="tok")
        self.touch_run_lock()
        self.wait(max_=0.2)
        self.wait(max_=0.2)
        self.assertEqual(len(self.ledger()), 1,
            "the first wait must settle the gone evidence as one ledger "
            "entry, and a second wait call after the abort was already "
            "settled must not add another")

    def test_wait_records_gone_deadline_for_a_legacy_token_less_handle(self):
        self.write_handle(started_at=time.time() - 99999, timeout=1,
                          with_token=False)
        code, out, err = self.wait(max_=0.2)
        self.assertEqual(len(self.ledger()), 1,
            "a legacy handle with no token still falls back to the time "
            "deadline for its aborted verdict, and that verdict must be "
            "recorded to the ledger and the log, not only returned as an "
            "exit code")

    def test_wait_rereads_before_recording_after_a_free_probe(self):
        from wfrun import runlock
        self.write_handle(token="tok", started_at=time.time(), timeout=600)
        self.write_marker(token="tok")

        def sneaky_probe(path):
            self.p["exit"].write_text(
                json.dumps({"returncode": 0, "stderr": ""}),
                encoding="utf-8")
            self.p["result"].write_text(
                json.dumps({"result": "x", "is_error": False, "permission_denials": []}),
                encoding="utf-8")
            stepio.append_attempt(self.p["attempts"], 1, "ok")
            return runlock.FREE

        with mock.patch.object(runlock, "probe", side_effect=sneaky_probe):
            code, out, err = self.wait(max_=0.2)
        self.assertEqual(code, 0,
            "a wrapper that finishes normally between the probe and the "
            "record must win: wait has to re-read exit.json and the "
            "ledger after a free probe before settling an abort, or it "
            "will discard a real success")

    def test_wait_stops_instead_of_spinning_when_done_but_exit_json_is_missing(self):
        self.write_handle(token="tok", started_at=time.time(), timeout=600)
        self.write_marker(token="tok")
        self.touch_run_lock()
        self.p["attempts"].write_text(
            json.dumps([{"seq": 1, "class": "ok", "ended_at": 1}]),
            encoding="utf-8")
        code, out, err = self.wait(max_=0.5)
        self.assertEqual(code, 3,
            "the ledger already carries seq 1 but exit.json was never "
            "written, so this attempt is done-but-unreadable; wait must "
            "give up within its own --max instead of looping forever on "
            "a file that will never appear, rather than keep reporting "
            "running")
        self.assertEqual(len(self.ledger()), 1)

    def test_wait_gives_up_on_an_unprovable_ledger_entry_past_the_overdue_grace(self):
        self.write_handle(started_at=time.time() - 99999, timeout=1,
                          with_token=False)
        self.p["attempts"].write_text(
            json.dumps([{"seq": 1, "class": "ok", "ended_at": 1}]),
            encoding="utf-8")
        code, out, err = self.wait(max_=0.2)
        self.assertEqual(code, 3,
            "a ledger entry with no exit.json and no lock evidence must "
            "still hit the overdue grace cap instead of reporting running "
            "forever")
        self.assertIn("overdue grace", err,
            "the report must say the grace window ran out")

    def test_wait_settles_an_abort_to_the_run_steps_log_whatever_log_is_passed(self):
        self.write_handle(token="tok", started_at=time.time(), timeout=600)
        self.write_marker(token="tok")
        self.touch_run_lock()
        other_log = self.dir / "other.log"
        code, out, err = self.run_cli(
            ["wait", str(self.p["handle"]), "--max", "0.2",
             "--vars", self.vars_path, "--log", str(other_log)])
        self.assertEqual(code, 3, err)
        self.assertEqual(
            [e.get("status") for e in self.log_entries()], ["aborted"],
            "the dispatch cap counts the run's own steps.log, so an abort "
            "settled by wait must land there even when --log names "
            "another file")

    def test_wait_settles_a_gone_attempt_while_holding_the_state_lock(self):
        from wfrun import runlock
        self.write_handle(token="tok", started_at=time.time(), timeout=600)
        self.write_marker(token="tok")
        self.touch_run_lock()
        state_lock = str(_lock_paths(self.run_dir)["state_lock"])
        real_record = stepio.record_aborted_attempt
        seen = {}

        def record(*args, **kwargs):
            contender = runlock.try_acquire(state_lock)
            seen["contended"] = contender is None
            if contender is not None:
                runlock.release(contender)
            return real_record(*args, **kwargs)

        with mock.patch.object(stepio, "record_aborted_attempt",
                               side_effect=record):
            code, out, err = self.wait(max_=0.2)
        self.assertEqual(code, 3, err)
        self.assertEqual(seen.get("contended"), True,
            "the abort must be recorded under the same state lock hold as "
            "the liveness evaluation, or a late wrapper can be admitted "
            "and run an attempt already reported as aborted")

    def test_wait_stops_after_grace_when_the_state_lock_stays_contended(self):
        self.write_handle(token="tok", started_at=time.time() - 99999,
                          timeout=1)
        lock = _AttemptLock(_lock_paths(self.run_dir)["state_lock"])
        lock.acquire()
        try:
            code, out, err = self.wait(max_=0.5)
            self.assertIn("state lock", err,
                "a state lock that stays contended on every poll must "
                "still hit a time upper bound and give up, and its message "
                "must name the contention")
        finally:
            lock.release()
            lock.close()


class KillTreePlumbingTests(unittest.TestCase):
    def setUp(self):
        claude_cli._resolution_cache.clear()
        self.which_patch = mock.patch.object(
            claude_cli.shutil, "which", return_value="/usr/local/bin/claude")
        self.which_patch.start()

    def tearDown(self):
        self.which_patch.stop()
        claude_cli._resolution_cache.clear()

    def test_kill_tree_false_uses_the_plain_bound_launch(self):
        stdout = json.dumps({"result": "x", "is_error": False})
        with mock.patch.object(claude_cli, "_run_bound") as run, \
             mock.patch.object(claude_cli, "_run_with_tree_kill") as tree_kill:
            run.return_value = mock.Mock(returncode=0, stdout=stdout, stderr="")
            claude_cli.run_claude("hi")
        run.assert_called_once()
        tree_kill.assert_not_called()

    def test_kill_tree_true_uses_tree_kill_path(self):
        stdout = json.dumps({"result": "x", "is_error": False})
        with mock.patch.object(claude_cli, "_run_bound") as run, \
             mock.patch.object(claude_cli, "_run_with_tree_kill") as tree_kill:
            tree_kill.return_value = mock.Mock(returncode=0, stdout=stdout, stderr="")
            claude_cli.run_claude("hi", kill_tree=True)
        tree_kill.assert_called_once()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
