import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_decision import DecisionExecutorTestCase, fake_decider, ruling  # noqa: E402
from test_dispatch_wait import DispatchWaitTestCase  # noqa: E402
from test_executor import ExecutorTestCase, FakeClaude  # noqa: E402

from wfrun import adjudicate as adjudicate_mod  # noqa: E402
from wfrun import adp, claude_cli, decision as decision_mod, parser, pi_cli  # noqa: E402
from wfrun.__main__ import main  # noqa: E402
from wfrun.adp import Diagnosis  # noqa: E402
from wfrun.claude_cli import CliResult  # noqa: E402
from wfrun.executor import WorkflowFailure  # noqa: E402

REPO_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1])


def _fake_completed(returncode=0, stdout="", stderr=""):
    proc = mock.Mock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


CHILD_SCRIPT = """
import json
import os
import sys
import time
from pathlib import Path

target = sys.argv[1]
scratch_temp = sys.argv[2]
marker_path = sys.argv[3]
repo_scripts = sys.argv[4]
base_dir = sys.argv[5]

os.environ["TEMP"] = scratch_temp
os.environ["TMP"] = scratch_temp
os.environ["TMPDIR"] = scratch_temp

sys.path.insert(0, repo_scripts)

from wfrun import claude_cli, pi_cli, parser
from wfrun.executor import Executor
from wfrun import __main__ as wfrun_main


def _hang(*a, **k):
    Path(marker_path).write_text("hit", encoding="utf-8")
    time.sleep(120)


def _no_call(*a, **k):
    raise AssertionError("unexpected call")


def _fake_ask(question, **kwargs):
    return True, "because", 0.0


claude_cli._resolve_claude_bin = lambda: ("fake-claude-bin", False)
claude_cli._supports_system_prompt_file = lambda *a, **k: True
pi_cli.resolve_pi_launcher = lambda: ["fake-pi-bin"]

if target in ("executor_cc", "executor_pi"):
    agents_dir = Path(base_dir) / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "w.md").write_text(
        "---\\nname: w\\ndescription: t\\n---\\nROLE-BODY-w", encoding="utf-8")
    xml = ('<workflow name="t" version="2" max="5">'
           '<step id="s1" role="w"><task>hang</task></step></workflow>')
    wf = parser.parse_string(xml, base_dir=base_dir)
    run_dir = Path(base_dir) / "run"
    if target == "executor_cc":
        claude_cli._run_bound = _hang
        runner = claude_cli.run_claude
        model_runner = "cc"
        backend = "cc"
    else:
        claude_cli._run_with_tree_kill = _hang
        runner = pi_cli.run_pi
        model_runner = "llm"
        backend = "pi"
    executor = Executor(wf, {}, run_dir, base_dir=base_dir,
                        run_claude=runner, ask_llm=_fake_ask,
                        diagnose=_no_call, adjudicate=_no_call,
                        model_runner=model_runner, backend=backend)
    executor.run()
elif target == "wrapper":
    claude_cli._run_with_tree_kill = _hang
    steps_dir = Path(base_dir) / "steps"
    steps_dir.mkdir(parents=True, exist_ok=True)
    system_file = steps_dir / "s1_c01_system.md"
    system_file.write_text("<role>w</role>", encoding="utf-8")
    prompt_file = steps_dir / "s1_c01_prompt.md"
    prompt_file.write_text("do it", encoding="utf-8")
    handle_file = steps_dir / "s1_c01_handle.json"
    handle_file.write_text(json.dumps({
        "token": "tok", "cycle": 1, "run_dir": base_dir, "step_id": "s1"}),
        encoding="utf-8")
    argv = ["_wrapper", "--system-file", str(system_file),
           "--prompt-file", str(prompt_file),
           "--exit-file", str(steps_dir / "s1_c01_exit.json"),
           "--result-file", str(steps_dir / "s1_c01_result.json"),
           "--attempts-file", str(steps_dir / "s1_c01_attempts.json"),
           "--seq", "1", "--cwd", base_dir, "--timeout", "600",
           "--handle-file", str(handle_file), "--token", "tok"]
    wfrun_main.main(argv)
"""


class KillRegressionTests(unittest.TestCase):
    def _run_kill_scenario(self, target):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            scratch_temp = base / "temp"
            scratch_temp.mkdir()
            marker = base / "marker.txt"
            base_dir = base / "base"
            base_dir.mkdir()
            child_path = base / "_child.py"
            child_path.write_text(CHILD_SCRIPT, encoding="utf-8")
            proc = subprocess.Popen(
                [sys.executable, str(child_path), target, str(scratch_temp),
                 str(marker), REPO_SCRIPTS_DIR, str(base_dir)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            deadline = time.time() + 30
            while not marker.exists():
                if proc.poll() is not None:
                    out, err = proc.communicate()
                    self.fail(
                        f"{target}: child exited early with returncode="
                        f"{proc.returncode}; stdout={out!r} stderr={err!r}")
                if time.time() > deadline:
                    proc.kill()
                    proc.wait(timeout=10)
                    proc.stdout.close()
                    proc.stderr.close()
                    self.fail(f"{target}: child never wrote its marker in time")
                time.sleep(0.05)
            proc.kill()
            proc.wait(timeout=10)
            proc.stdout.close()
            proc.stderr.close()
            leftover = sorted(
                p.name for p in list(scratch_temp.glob("wfrun-sysprompt-*"))
                + list(scratch_temp.glob("wfrun-pi-sysprompt-*")))
            self.assertEqual(
                leftover, [],
                f"{target}: kill left temp files behind in scratch TEMP: {leftover}")

    def test_executor_cc_backend_leaves_no_temp_file_on_external_kill(self):
        self._run_kill_scenario("executor_cc")

    def test_executor_pi_backend_leaves_no_temp_file_on_external_kill(self):
        self._run_kill_scenario("executor_pi")

    def test_a_layer_wrapper_leaves_no_temp_file_on_external_kill(self):
        self._run_kill_scenario("wrapper")


class RunnerFileIdentityTests(unittest.TestCase):
    def setUp(self):
        claude_cli._resolution_cache.clear()
        pi_cli._resolution_cache.clear()
        self.which_patch = mock.patch.object(
            claude_cli.shutil, "which", return_value="/usr/local/bin/claude")
        self.which_patch.start()
        self.pi_which_patch = mock.patch.object(
            pi_cli.shutil, "which", return_value="/usr/local/bin/pi")
        self.pi_which_patch.start()

    def tearDown(self):
        self.which_patch.stop()
        self.pi_which_patch.stop()
        claude_cli._resolution_cache.clear()
        pi_cli._resolution_cache.clear()

    def test_claude_argv_carries_the_resolved_absolute_path(self):
        with tempfile.TemporaryDirectory() as d:
            sysfile = Path(d) / "system.md"
            sysfile.write_text("<role>x</role>", encoding="utf-8")
            captured = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = cmd
                return _fake_completed(0, json.dumps(
                    {"result": "ok", "is_error": False, "permission_denials": []}))

            with mock.patch.object(claude_cli, "_supports_system_prompt_file",
                                   return_value=True), \
                 mock.patch.object(claude_cli, "_run_bound", side_effect=fake_run):
                res = claude_cli.run_claude("task", system_prompt_file=str(sysfile))
            self.assertTrue(res.ok, res.error)
            idx = captured["cmd"].index("--append-system-prompt-file")
            self.assertEqual(captured["cmd"][idx + 1], str(sysfile.resolve()))

    def test_claude_resolves_a_relative_path_against_the_callers_cwd(self):
        with tempfile.TemporaryDirectory() as parent_cwd, \
             tempfile.TemporaryDirectory() as child_cwd:
            (Path(parent_cwd) / "sub").mkdir()
            sysfile = Path(parent_cwd) / "sub" / "system.md"
            sysfile.write_text("<role>x</role>", encoding="utf-8")
            captured = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = cmd
                return _fake_completed(0, json.dumps(
                    {"result": "ok", "is_error": False, "permission_denials": []}))

            old_cwd = os.getcwd()
            os.chdir(parent_cwd)
            try:
                with mock.patch.object(claude_cli, "_supports_system_prompt_file",
                                       return_value=True), \
                     mock.patch.object(claude_cli, "_run_bound", side_effect=fake_run):
                    res = claude_cli.run_claude(
                        "task", system_prompt_file="sub/system.md", cwd=child_cwd)
            finally:
                os.chdir(old_cwd)
            self.assertTrue(res.ok, res.error)
            idx = captured["cmd"].index("--append-system-prompt-file")
            self.assertEqual(captured["cmd"][idx + 1], str(sysfile.resolve()))

    def test_pi_argv_carries_the_resolved_absolute_path(self):
        with tempfile.TemporaryDirectory() as d:
            sysfile = Path(d) / "system.md"
            sysfile.write_text("<role>x</role>", encoding="utf-8")
            captured = {}

            def capture(cmd, prompt, timeout, cwd):
                captured["cmd"] = cmd
                stdout = "\n".join(json.dumps(e) for e in (
                    {"type": "turn_end", "message": {
                        "content": [{"type": "text", "text": "done"}],
                        "stopReason": "stop"}},))
                return _fake_completed(0, stdout)

            with mock.patch.object(pi_cli.claude_cli, "_run_with_tree_kill",
                                   side_effect=capture):
                res = pi_cli.run_pi("task", system_prompt_file=str(sysfile))
            self.assertTrue(res.ok, res.error)
            idx = captured["cmd"].index("--append-system-prompt")
            self.assertEqual(captured["cmd"][idx + 1], str(sysfile.resolve()))

    def test_cr_only_content_survives_the_new_route_but_not_the_old_one(self):
        with tempfile.TemporaryDirectory() as d:
            text = "before\rafter"
            sysfile = Path(d) / "system.md"
            sysfile.write_text(text, encoding="utf-8")
            new_bytes = sysfile.read_bytes()
            old_text = sysfile.read_text(encoding="utf-8")
            old_bytes = old_text.replace("\n", os.linesep).encode("utf-8")
            self.assertNotEqual(new_bytes, old_bytes)

    def test_wrapper_forwards_the_system_file_path_without_rereading_it(self):
        with tempfile.TemporaryDirectory() as d:
            system_file = Path(d) / "system.md"
            system_file.write_text("before\rafter", encoding="utf-8")
            prompt_file = Path(d) / "prompt.md"
            prompt_file.write_text("do it", encoding="utf-8")
            exit_file = Path(d) / "exit.json"
            result_file = Path(d) / "result.json"
            attempts_file = Path(d) / "attempts.json"
            handle_file = Path(d) / "handle.json"
            handle_file.write_text(json.dumps({
                "token": "tok", "cycle": 1, "run_dir": d, "step_id": "s1"}),
                encoding="utf-8")
            captured = {}

            def fake_launch(prompt, **kwargs):
                captured.update(kwargs)
                return (0, json.dumps({"result": "ok", "is_error": False,
                                       "permission_denials": []}), "")

            with mock.patch.object(claude_cli, "_launch", side_effect=fake_launch):
                code = main(["_wrapper", "--system-file", str(system_file),
                            "--prompt-file", str(prompt_file),
                            "--exit-file", str(exit_file),
                            "--result-file", str(result_file),
                            "--attempts-file", str(attempts_file),
                            "--seq", "1", "--cwd", d, "--timeout", "600",
                            "--handle-file", str(handle_file), "--token", "tok"])
            self.assertEqual(code, 0)
            self.assertEqual(captured.get("system_prompt_file"), str(system_file))
            self.assertNotIn("system_prompt", captured)
            self.assertEqual(Path(captured["system_prompt_file"]).read_bytes(),
                             system_file.read_bytes())


class ShimHostilePathTests(unittest.TestCase):
    def setUp(self):
        claude_cli._resolution_cache.clear()

    def tearDown(self):
        claude_cli._resolution_cache.clear()

    def test_hostile_metachar_in_the_resolved_path_refuses_without_launching(self):
        with tempfile.TemporaryDirectory() as d:
            shim = Path(d) / "claude.cmd"
            shim.write_text("@echo off")
            hostile_dir = Path(d) / "run & dir"
            hostile_dir.mkdir()
            sysfile = hostile_dir / "system.md"
            sysfile.write_text("<role>x</role>", encoding="utf-8")
            claude_cli._resolution_cache.clear()
            with mock.patch.object(claude_cli.shutil, "which", return_value=str(shim)), \
                 mock.patch.object(claude_cli, "_supports_system_prompt_file",
                                   return_value=True), \
                 mock.patch.object(claude_cli, "_run_bound") as run:
                res = claude_cli.run_claude("task", system_prompt_file=str(sysfile))
            self.assertFalse(res.ok)
            self.assertEqual(res.error_class, "env")
            run.assert_not_called()

    def test_benign_path_via_shim_still_launches_with_the_file_flag(self):
        with tempfile.TemporaryDirectory() as d:
            shim = Path(d) / "claude.cmd"
            shim.write_text("@echo off")
            benign_dir = Path(d) / "rundir"
            benign_dir.mkdir()
            sysfile = benign_dir / "system.md"
            sysfile.write_text("<role>x</role>", encoding="utf-8")
            claude_cli._resolution_cache.clear()
            captured = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = cmd
                return _fake_completed(0, json.dumps(
                    {"result": "ok", "is_error": False, "permission_denials": []}))

            with mock.patch.object(claude_cli.shutil, "which", return_value=str(shim)), \
                 mock.patch.object(claude_cli, "_supports_system_prompt_file",
                                   return_value=True), \
                 mock.patch.object(claude_cli, "_run_bound", side_effect=fake_run):
                res = claude_cli.run_claude("task", system_prompt_file=str(sysfile))
            self.assertTrue(res.ok, res.error)
            self.assertIn("--append-system-prompt-file", captured["cmd"])


class ExecutorSystemPromptFileWiringTests(ExecutorTestCase):
    def test_step_passes_system_prompt_file_matching_the_written_system_md(self):
        ex = self.execute(self.wrap('<step id="s1" role="w"><task>x</task></step>'))
        ex.run()
        call = self.fake.calls[0]
        written = self.run_dir / "steps" / "s1_01" / "system.md"
        self.assertEqual(Path(call["system_prompt_file"]), written)
        self.assertEqual(written.read_text(encoding="utf-8"), call["system_prompt"])

    def test_replan_passes_system_prompt_file_matching_the_written_system_md(self):
        child = ('<workflow name="c" version="2" max="5">'
                 '<step id="c1" role="w"><task>child</task></step></workflow>')
        self.fake.handlers.append(
            (lambda p: "PLAN-ME" in p,
             CliResult(ok=True, text=f"```xml\n{child}\n```", cost_usd=0.02)))
        ex = self.execute(self.wrap('<replan id="r1" role="w"><task>PLAN-ME</task></replan>'))
        ex.run()
        call = self.fake.calls[0]
        written = self.run_dir / "steps" / "r1_01" / "system.md"
        self.assertEqual(Path(call["system_prompt_file"]), written)

    def test_debug_diagnose_receives_role_file_under_the_failed_attempt_dir(self):
        captured = {}

        def fake_diag(step, prompt, failure, **kwargs):
            captured.update(kwargs)
            return Diagnosis("FAIL", "no")

        self.fake.handlers.append(
            (lambda p: "boom" in p,
             CliResult(ok=False, error="ERROR: boom", cost_usd=0.01)))
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" on-error="debug"><task>boom</task></step>'),
            diagnose=fake_diag)
        with self.assertRaises(WorkflowFailure):
            ex.run()
        self.assertEqual(captured.get("role_file"),
                         self.run_dir / "steps" / "s1_01" / "debug_system.md")


class DecisionRoleFileWiringTests(DecisionExecutorTestCase):
    def test_llm_adjudicate_receives_role_file_beside_the_request(self):
        self.artifact()
        self.respond_decision("DO-WORK", then_ok=True, output=None)
        calls = []
        ex = self.execute(self.wrap(
            '<step id="s1" role="w" expect-file="art.txt" output="v">'
            '<task>DO-WORK</task></step>', extra='decider="llm"'),
            adjudicate=fake_decider(ruling(option=1), calls=calls))
        ex.run()
        self.assertEqual(len(calls), 1)
        rid = self.decision_events()[0]["request_id"]
        self.assertEqual(calls[0].get("role_file"),
                         decision_mod.decisions_dir(self.run_dir) / f"{rid}_decide_system.md")


class FinishWaitRoleFileWiringTests(DispatchWaitTestCase):
    def test_finish_wait_passes_role_file_to_diagnose(self):
        self.write_xml(on_error="debug")
        p = self.paths()
        p["handle"].parent.mkdir(parents=True, exist_ok=True)
        p["system"].write_text("<role>w</role>", encoding="utf-8")
        p["prompt"].write_text("answer it", encoding="utf-8")
        handle = {
            "wrapper_pid": 1, "attempt": 1, "cycle": 1, "started_at": time.time(),
            "timeout": 600, "xml": self.xml, "step_id": "s1",
            "run_dir": str(self.run_dir), "exit_path": str(p["exit"]),
            "result_path": str(p["result"]),
            "system_path": str(p["system"]), "prompt_path": str(p["prompt"]),
            "wait_path": str(p["wait"]),
        }
        p["handle"].write_text(json.dumps(handle), encoding="utf-8")
        p["exit"].write_text(json.dumps({"returncode": 0, "stderr": ""}), encoding="utf-8")
        p["result"].write_text(json.dumps({"result": "ERROR: boom", "is_error": False}),
                               encoding="utf-8")
        fake_diag = mock.Mock(return_value=Diagnosis("FAIL", "no"))
        log = self.run_dir / "steps.log"
        with mock.patch("wfrun.__main__.adp.diagnose", fake_diag):
            self.run_cli(["wait", str(p["handle"]), "--max", "5",
                         "--vars", self.vars_path, "--log", str(log)])
        self.assertEqual(
            fake_diag.call_args.kwargs.get("role_file"),
            self.run_dir / "steps" / "s1_c01_debug_system.md")


class DiagnoseAndAdjudicateWriteRoleFileTests(unittest.TestCase):
    def test_diagnose_writes_the_role_text_as_utf8_before_calling_the_runner(self):
        with tempfile.TemporaryDirectory() as d:
            agents_dir = Path(d) / ".claude" / "agents"
            agents_dir.mkdir(parents=True)
            (agents_dir / "debug.md").write_text(
                "---\nname: debug\ndescription: t\n---\nDEBUG-ROLE-BODY",
                encoding="utf-8")
            wf = parser.parse_string(
                '<workflow name="t" version="2" max="5">'
                '<step id="s1" role="w"><task>x</task></step></workflow>')
            step = next(iter(wf.iter_steps()))
            failure = CliResult(ok=False, error="ERROR: boom", exit_code=1,
                                stderr="")
            role_file = Path(d) / "debug_system.md"
            captured = {}

            def fake_run_claude(prompt, **kwargs):
                captured.update(kwargs)
                return CliResult(ok=False, error="stub", cost_usd=0.0)

            with mock.patch.object(adp, "run_claude", fake_run_claude):
                adp.diagnose(step, "task text", failure, role_file=role_file, cwd=d)
            self.assertEqual(role_file.read_text(encoding="utf-8"),
                             "<role>\nDEBUG-ROLE-BODY\n</role>")
            self.assertEqual(captured.get("system_prompt_file"), role_file)

    def test_adjudicate_writes_the_decide_role_as_utf8_before_calling_the_runner(self):
        with tempfile.TemporaryDirectory() as d:
            role_file = Path(d) / "decide_system.md"
            captured = {}

            def fake_runner(prompt, **kwargs):
                captured.update(kwargs)
                return CliResult(ok=False, error="stub", cost_usd=0.0)

            adjudicate_mod.adjudicate("s1", "body", 2, runner=fake_runner,
                                      role_file=role_file)
            self.assertEqual(role_file.read_text(encoding="utf-8"),
                             f"<role>\n{adjudicate_mod.DECIDE_ROLE}\n</role>")
            self.assertEqual(captured.get("system_prompt_file"), role_file)

    def test_adjudicate_text_writes_the_decide_role_as_utf8_before_calling_the_runner(self):
        with tempfile.TemporaryDirectory() as d:
            role_file = Path(d) / "decide_system.md"
            captured = {}

            def fake_runner(prompt, **kwargs):
                captured.update(kwargs)
                return CliResult(ok=True, text="option: 1\nfine", cost_usd=0.0)

            adjudicate_mod.adjudicate_text("s1", "req", 2, runner=fake_runner,
                                           role_file=role_file)
            self.assertEqual(role_file.read_text(encoding="utf-8"),
                             f"<role>\n{adjudicate_mod.DECIDE_ROLE}\n</role>")
            self.assertEqual(captured.get("system_prompt_file"), role_file)


if __name__ == "__main__":
    unittest.main()
