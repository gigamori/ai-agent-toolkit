import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun.__main__ import main  # noqa: E402

XML = """
<workflow name="t" version="2" max="10">
  <step id="s1" output="report_path" retry="1" timeout="100"><role>W</role><task>write it</task></step>
  <step id="s2" on-error="debug" timeout="100"><role>W</role><task>try it</task></step>
  <replan id="r1" max-steps="5" retry="1"><role>W</role><task>plan it</task></replan>
</workflow>
"""

SESSION = "11111111-2222-3333-4444-555555555555"

DECISION_BODY = ("DECISION: which join\nfork: two readings of merge\noptions:\n"
                 "  1. A -- loses rows\n  2. B -- loses columns\n"
                 "recommendation: 1\nwork-state: stopped")

CHILD_XML = ('<workflow name="c" version="2" max="2">'
             '<step id="x"><task>t</task></step></workflow>')


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def end_marker(manifest):
    prompt = Path(manifest["judge"]["prompt_path"]).read_text(encoding="utf-8")
    return re.search(r"\[\[WFRUN-END [^\]]*\]\]", prompt).group(0)


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.on_sleep = None
        self.sleeps = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.sleeps += 1
        if self.on_sleep is not None:
            self.on_sleep(self.sleeps)


class RunLlmCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.run_dir = self.root / "run"
        self.steps = self.run_dir / "steps"
        self.steps.mkdir(parents=True)
        self.xml = str(self.root / "wf.xml")
        Path(self.xml).write_text(XML, encoding="utf-8")
        self.vars = str(self.run_dir / "vars.json")
        Path(self.vars).write_text("{}", encoding="utf-8")
        self.log = str(self.run_dir / "steps.log")
        env = mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""})
        env.start()
        self.addCleanup(env.stop)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue().strip(), err.getvalue().strip()

    def base(self, step_id="s1"):
        return str(self.steps / f"{step_id}_result.md")

    def pointer(self, step_id="s1"):
        return self.steps / f"{step_id}_handle.json"

    def prompt(self, step_id="s1", *extra):
        return self.cli("prompt", self.xml, step_id, "--vars", self.vars,
                        "--out", str(self.steps / f"{step_id}_prompt.md"),
                        "--result", self.base(step_id), *extra)

    def handle(self, step_id="s1"):
        return json.loads(self.pointer(step_id).read_text(encoding="utf-8"))

    def finish(self, body="done", step_id="s1", sentinel=None):
        handle = self.handle(step_id)
        Path(handle["result_path"]).write_text(
            f"{body}\n{sentinel or handle['sentinel']}\n", encoding="utf-8")

    def record(self, step_id="s1", *extra):
        return self.cli("record", self.xml, step_id, "--result", self.base(step_id),
                        "--vars", self.vars, "--log", self.log, *extra)

    def ledger(self, step_id="s1", cycle=1):
        path = self.steps / f"{step_id}_c{cycle:02d}_attempts.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []

    def poll(self, *extra):
        return self.cli("poll", str(self.pointer()), *extra)

    def stop(self, facet="cc", *extra):
        return self.cli("stop", str(self.pointer()), "--facet", facet, *extra)

    def judge(self):
        return self.cli("judge", self.xml, "s1", "--result", self.base())


class ClockCase(RunLlmCase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        for target, value in (("wfrun.llmgate._monotonic", self.clock.monotonic),
                              ("wfrun.llmgate._sleep", self.clock.sleep)):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class CcCase(ClockCase):
    def setUp(self):
        super().setUp()
        config = self.root / "config"
        self.subagents = config / "projects" / "proj" / SESSION / "subagents"
        self.subagents.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": SESSION,
                                           "CLAUDE_CONFIG_DIR": str(config)})
        env.start()
        self.addCleanup(env.stop)

    def transcript(self, lines, agent="a1"):
        meta = {"agentType": "general-purpose",
                "description": self.handle()["delegation"]}
        (self.subagents / f"agent-{agent}.meta.json").write_text(
            json.dumps(meta), encoding="utf-8")
        path = self.subagents / f"agent-{agent}.jsonl"
        path.write_text("".join(json.dumps(line) + "\n" for line in lines),
                        encoding="utf-8")
        return path

    def work_lines(self, at=None, tool="Write", tool_input=None, answered=True):
        if at is None:
            at = (self.handle()["dispatched_at"] if self.pointer().is_file()
                  else time.time()) + 5
        stamp = iso(at)
        lines = [{"type": "assistant", "timestamp": stamp, "message": {
            "role": "assistant", "content": [{
                "type": "tool_use", "id": "toolu_w", "name": tool,
                "input": tool_input if tool_input is not None
                else {"file_path": "out.md"}}]}}]
        if answered:
            lines.append({"type": "user", "timestamp": stamp, "message": {
                "role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": "toolu_w",
                    "content": "ok"}]}})
        return lines

    def aborted_and_stopped(self, lines=None, task_stop="stopped"):
        self.prompt()
        path = self.transcript(self.work_lines() if lines is None else lines)
        stop = self.stop("cc", "--task-stop", task_stop)
        record = self.record()
        return path, stop, record

    def evidence(self):
        handle = self.handle()
        stem = f"s1_c{handle['cycle']:02d}_a{handle['attempt']:02d}"
        return json.loads((self.steps / f"{stem}_evidence.json")
                          .read_text(encoding="utf-8"))

    def write_record(self, refs, verdict="allow", method="restart",
                     next_timeout=None):
        manifest = self.evidence()
        record = Path(manifest["judge"]["record_path"])
        lines = [f"verdict: {verdict}", f"method: {method}"]
        if next_timeout is not None:
            lines.append(f"next-timeout: {next_timeout}")
        lines.append("evidence:")
        lines += [f"- {path}#L{first}-L{last}: the attempt wrote its output"
                  for path, first, last in refs]
        lines += ["limits: none", end_marker(manifest)]
        record.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return record

    def judged(self, **record_args):
        path, _, _ = self.aborted_and_stopped()
        code, _, err = self.judge()
        self.assertEqual(code, 0, err)
        return path, self.write_record([(str(path), 1, 2)], **record_args)


class TestAttemptFiles(RunLlmCase):
    def test_first_attempt_gets_its_own_result_and_keyed_sentinel(self):
        code, out, err = self.prompt()
        self.assertEqual(code, 0, err)
        handle = self.handle()
        self.assertEqual((handle["cycle"], handle["attempt"]), (1, 1))
        self.assertNotEqual(Path(handle["result_path"]), Path(self.base()))
        self.assertEqual(Path(handle["result_path"]).parent, self.steps)
        self.assertTrue(handle["sentinel"].startswith("[[WFRUN-END step=s1 "),
                        handle["sentinel"])
        self.assertIn(handle["token"], handle["sentinel"])
        text = (self.steps / "s1_prompt.md").read_text(encoding="utf-8")
        self.assertIn(handle["result_path"], text)
        self.assertIn(handle["sentinel"], text)
        self.assertEqual(Path(handle["prompt_path"]).read_text(encoding="utf-8"), text)
        self.assertIn(f"result={handle['result_path']}", out)
        self.assertIn(f"description={handle['delegation']}", out)
        self.assertIn("attempt=c01a01", out)

    def test_a_retry_keeps_the_failed_attempts_result(self):
        self.prompt()
        self.finish("ERROR: boom")
        code, _, _ = self.record()
        self.assertEqual(code, 1)
        first = self.handle()
        code, _, err = self.prompt()
        self.assertEqual(code, 0, err)
        second = self.handle()
        self.assertEqual((second["cycle"], second["attempt"]), (1, 2))
        self.assertNotEqual(second["result_path"], first["result_path"])
        self.assertNotEqual(second["token"], first["token"])
        self.assertTrue(Path(first["result_path"]).read_text(encoding="utf-8")
                        .startswith("ERROR: boom"),
                        "an earlier attempt's result is evidence and is never deleted")
        self.assertTrue(Path(first["prompt_path"]).is_file())

    def test_success_opens_the_next_cycle(self):
        self.prompt()
        self.finish()
        code, _, _ = self.record("s1", "--reply", "OK s1")
        self.assertEqual(code, 0)
        code, _, err = self.prompt()
        self.assertEqual(code, 0, err)
        self.assertEqual((self.handle()["cycle"], self.handle()["attempt"]), (2, 1))
        self.assertEqual(self.ledger(cycle=1)[-1]["class"], "ok")

    def test_the_ledger_entry_names_its_attempt(self):
        self.prompt()
        self.finish()
        self.record()
        entry = self.ledger()[-1]
        self.assertEqual((entry["seq"], entry["class"], entry["token"]),
                         (1, "ok", self.handle()["token"]))

    def test_a_recorded_attempt_is_not_recorded_twice(self):
        self.prompt()
        self.finish()
        self.record()
        code, out, _ = self.record()
        self.assertEqual(code, 2)
        self.assertIn("already recorded", out)
        self.assertEqual(len(self.ledger()), 1)


class TestKeyedSentinel(RunLlmCase):
    def test_another_attempts_sentinel_does_not_complete_this_one(self):
        self.prompt()
        first = self.handle()
        self.finish("ERROR: boom")
        self.record()
        self.prompt()
        self.finish("looks finished", sentinel=first["sentinel"])
        self.assertEqual(self.poll()[:2], (10, "running"))
        code, out, _ = self.record()
        self.assertEqual((code, out), (3, "aborted: incomplete (no end marker)"))

    def test_a_step_only_sentinel_does_not_complete_an_attempt(self):
        self.prompt()
        self.finish("looks finished", sentinel="[[WFRUN-END step=s1]]")
        self.assertEqual(self.poll()[:2], (10, "running"))


class TestAdmission(RunLlmCase):
    def test_an_unrecorded_attempt_blocks_every_new_attempt(self):
        self.prompt()
        for extra in ((), ("--new-cycle",)):
            code, _, err = self.prompt("s1", *extra)
            self.assertEqual(code, 1, extra)
            self.assertIn("no recorded verdict", err)
        self.assertEqual(self.handle()["attempt"], 1)

    def test_a_refusal_writes_nothing(self):
        self.prompt()
        names = sorted(p.name for p in self.steps.iterdir())
        prompt_bytes = (self.steps / "s1_prompt.md").read_bytes()
        pointer_bytes = self.pointer().read_bytes()
        self.prompt()
        self.assertEqual(sorted(p.name for p in self.steps.iterdir()), names)
        self.assertEqual((self.steps / "s1_prompt.md").read_bytes(), prompt_bytes)
        self.assertEqual(self.pointer().read_bytes(), pointer_bytes)

    def test_spent_retries_follow_on_error(self):
        for _ in range(2):
            self.prompt()
            self.finish("ERROR: boom")
            self.record()
        code, _, err = self.prompt()
        self.assertEqual(code, 1)
        self.assertIn("retries are spent", err)

    def test_a_pending_decision_blocks_the_step_in_every_cycle(self):
        self.prompt()
        self.finish(DECISION_BODY)
        code, _, _ = self.record()
        self.assertEqual(code, 4)
        for extra in ((), ("--new-cycle",)):
            code, _, err = self.prompt("s1", *extra)
            self.assertEqual(code, 1, extra)
            self.assertIn("no ruling yet", err)

    def test_the_workflow_max_is_enforced(self):
        Path(self.log).write_text("".join(
            json.dumps({"step": "x", "status": "success"}) + "\n"
            for _ in range(10)), encoding="utf-8")
        code, _, err = self.prompt()
        self.assertEqual(code, 1)
        self.assertIn("workflow max=10", err)

    def test_fix_without_a_recorded_debug_retry_is_refused(self):
        self.prompt()
        self.finish("ERROR: boom")
        self.record()
        code, _, err = self.prompt("s1", "--fix", "do better")
        self.assertEqual(code, 1)
        self.assertIn("no debug RETRY", err)

    def test_an_aborted_attempt_needs_a_stop_record(self):
        self.prompt()
        code, _, _ = self.record()
        self.assertEqual(code, 3)
        for extra in ((), ("--new-cycle",)):
            code, _, err = self.prompt("s1", *extra)
            self.assertEqual(code, 1, extra)
            self.assertIn("wfrun stop", err)

    def test_redispatch_without_an_aborted_attempt_is_refused(self):
        code, _, _ = self.prompt("s1", "--redispatch", "allow x")
        self.assertEqual(code, 1)
        self.prompt()
        self.finish("ERROR: boom")
        self.record()
        code, _, err = self.prompt("s1", "--redispatch", "allow x")
        self.assertEqual(code, 1)
        self.assertIn("aborted", err)

    def test_redispatch_over_a_completed_attempt_is_refused(self):
        self.prompt()
        self.finish()
        self.assertEqual(self.record()[0], 0)
        code, _, err = self.prompt("s1", "--redispatch", "allow x")
        self.assertEqual(code, 1)
        self.assertIn("aborted", err)
        self.assertEqual((self.handle()["cycle"], self.handle()["attempt"]), (1, 1))


class TestLateCompletion(RunLlmCase):
    def test_a_result_completed_after_the_abort_was_recorded_is_not_adopted(self):
        self.prompt()
        self.assertEqual(self.record()[0], 3)
        self.finish()
        code, out, _ = self.record()
        self.assertEqual(code, 2)
        self.assertIn("not adopted", out)
        entry = self.ledger()[-1]
        self.assertEqual((len(self.ledger()), entry["class"]), (1, "aborted"))
        self.assertTrue(entry.get("late_completion"))
        self.assertFalse(entry.get("stop_contradiction"))
        self.assertEqual(json.loads(Path(self.vars).read_text(encoding="utf-8")), {})
        code, _, err = self.prompt("s1", "--new-cycle")
        self.assertEqual(code, 1)
        self.assertIn("wfrun stop", err)


class TestPiStop(ClockCase):
    def test_a_pi_stop_without_recorded_pids_is_unconfirmed(self):
        self.prompt()
        code, out, _ = self.stop("pi-cli")
        self.assertEqual(code, 13, out)
        self.assertTrue(out.startswith("unconfirmed:"), out)
        self.record()
        code, _, err = self.prompt("s1", "--new-cycle")
        self.assertEqual(code, 1)
        self.assertIn("unconfirmed", err)
        self.assertEqual(self.judge()[0], 1)


class TestPoll(RunLlmCase):
    def craft(self, elapsed, timeout):
        handle = self.handle()
        handle["dispatched_at"] = time.time() - elapsed
        handle["timeout"] = timeout
        self.pointer().write_text(json.dumps(handle), encoding="utf-8")

    def test_the_deadline_boundary(self):
        self.prompt()
        self.craft(50, 100)
        self.assertEqual(self.poll()[:2], (10, "running"))
        self.craft(150, 100)
        self.assertEqual(self.poll()[:2], (11, "deadline-exceeded"))

    def test_wait_returns_at_the_deadline_and_writes_nothing(self):
        self.prompt()
        self.craft(150, 100)
        before = {p: p.read_bytes() for p in self.run_dir.rglob("*") if p.is_file()}
        self.assertEqual(self.poll("--wait")[:2], (11, "deadline-exceeded"))
        after = {p: p.read_bytes() for p in self.run_dir.rglob("*") if p.is_file()}
        self.assertEqual(after, before)

    def test_wait_returns_once_the_attempt_completes(self):
        self.prompt()
        calls = []

        def complete(seconds):
            calls.append(seconds)
            self.finish()

        with mock.patch("wfrun.__main__.time.sleep", complete):
            code, out, _ = self.poll("--wait")
        self.assertEqual((code, out), (0, "done"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.ledger(), [], "poll never records a verdict")


class TestStopAndJudge(CcCase):
    def test_a_confirmed_stop_and_an_allowing_record_admit_one_restart(self):
        path, stop, record = self.aborted_and_stopped()
        self.assertEqual(stop[0], 12, stop)
        self.assertEqual(record[0], 3, record)
        code, out, err = self.judge()
        self.assertEqual(code, 0, err)
        judge = self.evidence()["judge"]
        self.assertIn(f"record={judge['record_path']}", out)
        self.assertIn(f"description={judge['description']}", out)
        record_path = self.write_record([(str(path), 1, 2)])
        code, _, err = self.prompt("s1", "--redispatch", f"allow {record_path}")
        self.assertEqual(code, 0, err)
        handle = self.handle()
        self.assertEqual((handle["cycle"], handle["attempt"]), (1, 2))
        self.assertEqual(handle["supersedes"], {"cycle": 1, "attempt": 1})
        self.assertEqual(handle["timeout"], 100)

    def test_the_judge_prompt_carries_paths_not_content(self):
        self.aborted_and_stopped()
        self.judge()
        judge = self.evidence()["judge"]
        text = Path(judge["prompt_path"]).read_text(encoding="utf-8")
        self.assertIn(judge["record_path"], text)
        self.assertNotIn("write it", text)

    def test_a_failed_task_stop_is_unconfirmed(self):
        _, stop, _ = self.aborted_and_stopped(task_stop="failed")
        self.assertEqual(stop[0], 13, stop)
        code, _, err = self.judge()
        self.assertEqual(code, 1)
        self.assertIn("not confirmed", err)

    def test_an_unanswered_tool_call_is_unconfirmed(self):
        _, stop, _ = self.aborted_and_stopped(lines=self.work_lines(
            tool="Bash", tool_input={"command": "x"}, answered=False))
        self.assertEqual(stop[0], 13, stop)
        self.assertIn("Bash", stop[1])

    def test_a_background_shell_is_unconfirmed(self):
        _, stop, _ = self.aborted_and_stopped(lines=self.work_lines(
            tool="Bash", tool_input={"command": "x", "run_in_background": True}))
        self.assertEqual(stop[0], 13, stop)
        self.assertIn("background", stop[1])

    def test_an_agent_left_in_the_background_is_unconfirmed(self):
        _, stop, _ = self.aborted_and_stopped(lines=self.work_lines(
            tool="Agent", tool_input={"prompt": "p"}))
        self.assertEqual(stop[0], 13, stop)
        self.assertIn("background", stop[1])

    def test_a_changing_transcript_is_unconfirmed(self):
        self.prompt()
        path = self.transcript(self.work_lines())

        def grow(count):
            if count == 1:
                with path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"type": "assistant",
                                        "message": {"content": []}}) + "\n")

        self.clock.on_sleep = grow
        code, out, _ = self.stop("cc", "--task-stop", "stopped")
        self.assertEqual(code, 13, out)
        self.assertIn("changed during the observation window", out)

    def test_no_transcript_is_unconfirmed(self):
        self.prompt()
        code, out, _ = self.stop("cc", "--task-stop", "stopped")
        self.assertEqual(code, 13, out)

    def test_a_completion_inside_the_window_is_adopted(self):
        self.prompt()
        self.transcript(self.work_lines())
        self.clock.on_sleep = lambda count: count == 2 and self.finish()
        code, out, _ = self.stop("cc", "--task-stop", "stopped")
        self.assertEqual((code, out), (0, "done"))
        code, out, _ = self.record()
        self.assertEqual(code, 0, out)

    def test_a_completion_after_the_recorded_abort_is_not_adopted(self):
        self.aborted_and_stopped()
        self.finish()
        code, out, _ = self.record()
        self.assertEqual(code, 2)
        self.assertIn("not adopted", out)
        self.assertTrue(self.ledger()[-1].get("stop_contradiction"))
        self.assertEqual(self.judge()[0], 1)
        code, _, err = self.prompt("s1", "--new-cycle")
        self.assertEqual(code, 1)
        self.assertIn("cannot be trusted", err)

    def test_a_late_completion_after_an_unconfirmed_stop_is_not_adopted(self):
        _, stop, record = self.aborted_and_stopped(task_stop="failed")
        self.assertEqual((stop[0], record[0]), (13, 3))
        self.finish()
        code, out, _ = self.record()
        self.assertEqual(code, 2)
        self.assertIn("not adopted", out)
        self.assertFalse(self.ledger()[-1].get("stop_contradiction"))
        code, _, err = self.prompt("s1", "--new-cycle")
        self.assertEqual(code, 1)
        self.assertIn("unconfirmed", err)

    def test_one_judge_per_attempt(self):
        self.aborted_and_stopped()
        self.assertEqual(self.judge()[0], 0)
        code, _, err = self.judge()
        self.assertEqual(code, 1)
        self.assertIn("already issued", err)

    def test_a_refused_call_is_never_judged(self):
        self.prompt()
        self.transcript(self.work_lines() + [{
            "type": "user", "toolDenialKind": "permission-rule",
            "message": {"role": "user", "content": []}}])
        self.stop("cc", "--task-stop", "stopped")
        code, out, _ = self.record()
        self.assertEqual(code, 3, "a call a stop cut off is recorded with the stop, "
                                  "never as a denial that follows on-error")
        self.assertTrue(out.startswith("aborted"), out)
        self.assertIn("deny-check: DENIED 1", out)
        self.assertEqual(self.ledger()[-1]["deny_check"], "DENIED 1")
        logged = json.loads(Path(self.log).read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual((logged["status"], logged["deny_check"]), ("aborted", "DENIED 1"))
        self.assertEqual(self.judge()[0], 1)

    def test_an_ok_reply_after_a_stop_is_recorded_aborted(self):
        self.prompt()
        self.transcript(self.work_lines())
        self.assertEqual(self.stop("cc", "--task-stop", "stopped")[0], 12)
        code, out, _ = self.record("s1", "--reply", "OK s1")
        self.assertEqual(code, 3, "after a stop the attempt is settled by its stop, "
                                  "not by a reply claiming OK")
        self.assertTrue(out.startswith("aborted"), out)
        self.assertEqual(self.ledger()[-1]["class"], "aborted")
        code, _, err = self.prompt()
        self.assertEqual(code, 1)
        self.assertIn("wfrun judge", err)

    def test_a_stop_without_a_transcript_is_recorded_aborted_and_says_so(self):
        self.prompt()
        self.assertEqual(self.stop("cc", "--task-stop", "stopped")[0], 13)
        code, out, _ = self.record()
        self.assertEqual(code, 3)
        self.assertIn("deny-check: NO-TRANSCRIPT", out)
        self.assertEqual(self.ledger()[-1]["deny_check"], "NO-TRANSCRIPT")

    def test_a_deny_reply_is_refused(self):
        _, record = self.judged()
        code, _, err = self.prompt("s1", "--redispatch", f"deny {record}")
        self.assertEqual(code, 1)
        self.assertIn("denied", err)

    def test_a_reply_naming_another_file_is_refused(self):
        _, record = self.judged()
        code, _, _ = self.prompt("s1", "--redispatch", f"allow {record}.x")
        self.assertEqual(code, 1)

    def test_a_record_that_proposes_another_method_is_refused(self):
        _, record = self.judged(method="resume")
        code, _, err = self.prompt("s1", "--redispatch", f"allow {record}")
        self.assertEqual(code, 1)
        self.assertIn("resume", err)

    def test_a_record_citing_outside_the_manifest_is_refused(self):
        self.aborted_and_stopped()
        self.judge()
        other = self.root / "other.txt"
        other.write_text("x\ny\n", encoding="utf-8")
        record = self.write_record([(str(other), 1, 1)])
        code, _, err = self.prompt("s1", "--redispatch", f"allow {record}")
        self.assertEqual(code, 1)
        self.assertIn("not in the evidence manifest", err)

    def test_a_citation_past_the_end_is_refused(self):
        path, _, _ = self.aborted_and_stopped()
        self.judge()
        record = self.write_record([(str(path), 1, 99)])
        code, _, _ = self.prompt("s1", "--redispatch", f"allow {record}")
        self.assertEqual(code, 1)

    def test_changed_evidence_is_refused(self):
        path, record = self.judged()
        with path.open("a", encoding="utf-8") as f:
            f.write("{}\n")
        code, _, err = self.prompt("s1", "--redispatch", f"allow {record}")
        self.assertEqual(code, 1)
        self.assertIn("changed after it was listed", err)

    def test_a_record_after_the_judge_deadline_is_refused(self):
        _, record = self.judged()
        judge = self.evidence()["judge"]
        late = judge["issued_at"] + judge["timeout"] + 10
        os.utime(record, (late, late))
        code, _, err = self.prompt("s1", "--redispatch", f"allow {record}")
        self.assertEqual(code, 1)
        self.assertIn("deadline", err)

    def test_the_abort_budget_allows_one_restart_per_cycle(self):
        _, record = self.judged()
        code, _, err = self.prompt("s1", "--redispatch", f"allow {record}")
        self.assertEqual(code, 0, err)
        self.transcript(self.work_lines(), agent="a2")
        self.stop("cc", "--task-stop", "stopped")
        self.assertEqual(self.record()[0], 3)
        code, _, err = self.judge()
        self.assertEqual(code, 1)
        self.assertIn("re-dispatch after an abort is spent", err)

    def test_a_new_cycle_after_an_aborted_attempt_is_refused(self):
        self.aborted_and_stopped()
        pointer = self.handle()
        names = sorted(p.name for p in self.steps.iterdir())
        code, _, err = self.prompt("s1", "--new-cycle")
        self.assertEqual(code, 1, "an aborted attempt goes on only through its judge, "
                                  "never into a new cycle")
        self.assertIn("--new-cycle", err)
        self.assertEqual(self.handle(), pointer)
        self.assertEqual(sorted(p.name for p in self.steps.iterdir()), names)

    def test_a_new_cycle_does_not_pass_a_judge_deny(self):
        self.judged(verdict="deny", method="none")
        code, _, _ = self.prompt("s1", "--new-cycle")
        self.assertEqual(code, 1, "a judge's deny stands: a new cycle is no way "
                                  "around it")


class TestDeadlineRestart(CcCase):
    def stopped_at_deadline(self, at_offset=5):
        self.prompt()
        handle = self.handle()
        path = self.transcript(self.work_lines(at=handle["dispatched_at"] + at_offset))
        late = handle["dispatched_at"] + handle["timeout"] + 1
        with mock.patch("wfrun.llmgate._now", return_value=late):
            stop = self.stop("cc", "--task-stop", "stopped")
        self.assertEqual(stop[0], 12, stop)
        self.record()
        self.assertEqual(self.judge()[0], 0)
        return path

    def test_a_longer_deadline_grounded_in_the_transcript_is_used(self):
        path = self.stopped_at_deadline()
        self.assertIn("next-timeout", Path(self.evidence()["judge"]["prompt_path"])
                      .read_text(encoding="utf-8"))
        record = self.write_record([(str(path), 1, 2)], next_timeout=200)
        code, _, err = self.prompt("s1", "--redispatch", f"allow {record}")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.handle()["timeout"], 200)

    def test_no_longer_deadline_is_refused(self):
        path = self.stopped_at_deadline()
        for proposed in (None, 100):
            with self.subTest(proposed=proposed):
                record = self.write_record([(str(path), 1, 2)], next_timeout=proposed)
                code, _, err = self.prompt("s1", "--redispatch", f"allow {record}")
                self.assertEqual(code, 1)
                self.assertIn("next-timeout", err)

    def test_progress_outside_the_attempts_run_is_refused(self):
        path = self.stopped_at_deadline(at_offset=-50)
        record = self.write_record([(str(path), 1, 2)], next_timeout=200)
        code, _, err = self.prompt("s1", "--redispatch", f"allow {record}")
        self.assertEqual(code, 1)
        self.assertIn("stamped inside", err)


class TestDebugVerdict(RunLlmCase):
    def fail_once(self):
        self.prompt("s2")
        self.finish("ERROR: boom", "s2")
        return self.record("s2")

    def test_spent_retries_point_at_the_debug_delegation(self):
        self.fail_once()
        code, _, err = self.prompt("s2")
        self.assertEqual(code, 1)
        self.assertIn("record --debug-reply", err)

    def test_a_retry_verdict_needs_its_fix_file(self):
        self.fail_once()
        code, out, _ = self.record("s2", "--debug-reply", "RETRY")
        self.assertEqual(code, 2, out)
        self.assertNotIn("debug", self.ledger("s2")[-1])

    def test_a_recorded_retry_admits_one_fixed_rerun(self):
        self.fail_once()
        (self.steps / "s2_fix.md").write_text("do it right", encoding="utf-8")
        code, out, _ = self.record("s2", "--debug-reply", "RETRY")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ledger("s2")[-1]["debug"], "RETRY")
        code, _, err = self.prompt("s2", "--fix", "do it right")
        self.assertEqual(code, 0, err)
        self.assertIn("do it right", (self.steps / "s2_prompt.md").read_text(encoding="utf-8"))
        self.finish("ERROR: again", "s2")
        self.record("s2")
        code, _, _ = self.prompt("s2", "--fix", "do it right")
        self.assertEqual(code, 1)

    def test_a_second_debug_reply_for_one_attempt_is_refused(self):
        self.fail_once()
        self.record("s2", "--debug-reply", "FAIL")
        code, out, _ = self.record("s2", "--debug-reply", "FAIL")
        self.assertEqual(code, 2, out)

    def test_a_fail_verdict_stops(self):
        self.fail_once()
        code, out, _ = self.record("s2", "--debug-reply", "FAIL: no fix")
        self.assertEqual(code, 1, out)
        self.assertIn("stop and report", out)

    def test_an_unreadable_debug_reply_records_nothing(self):
        self.fail_once()
        code, out, _ = self.record("s2", "--debug-reply", "I think it was the network")
        self.assertEqual(code, 2, out)
        self.assertNotIn("debug", self.ledger("s2")[-1])


class TestReplanAttempts(RunLlmCase):
    def replan(self, *extra):
        return self.cli("prompt", self.xml, "r1", "--vars", self.vars,
                        "--out", str(self.steps / "r1_prompt.md"),
                        "--result", str(self.run_dir / "replans" / "r1.xml"), *extra)

    def outputs(self):
        path = self.run_dir / "replans" / "r1_replan.json"
        return [entry["output"] for entry in json.loads(path.read_text(encoding="utf-8"))]

    def test_the_builder_gets_its_own_output_file(self):
        code, out, err = self.replan()
        self.assertEqual(code, 0, err)
        output = self.outputs()[-1]
        self.assertIn(f"result={output}", out)
        self.assertIn(output, (self.steps / "r1_prompt.md").read_text(encoding="utf-8"))

    def test_an_unfinished_builder_is_not_delegated_again(self):
        self.replan()
        Path(self.outputs()[-1]).parent.mkdir(parents=True, exist_ok=True)
        Path(self.outputs()[-1]).write_text("<workflow name=", encoding="utf-8")
        for extra in ((), ("--fix", "errors")):
            code, _, err = self.replan(*extra)
            self.assertEqual(code, 1, extra)
            self.assertIn("not delegated again", err)
        self.assertEqual(len(self.outputs()), 1)

    def test_a_fix_keeps_the_first_output_within_retry(self):
        self.replan()
        first = self.outputs()[-1]
        Path(first).parent.mkdir(parents=True, exist_ok=True)
        Path(first).write_text(CHILD_XML, encoding="utf-8")
        code, _, err = self.replan("--fix", "line 1: bad")
        self.assertEqual(code, 0, err)
        second = self.outputs()[-1]
        self.assertNotEqual(second, first)
        self.assertEqual(Path(first).read_text(encoding="utf-8"), CHILD_XML)
        Path(second).write_text(CHILD_XML, encoding="utf-8")
        code, _, err = self.replan("--fix", "line 1: still bad")
        self.assertEqual(code, 1)
        self.assertIn("retries are spent", err)

    def test_a_finished_builder_without_fix_is_the_next_visit(self):
        self.replan()
        Path(self.outputs()[-1]).parent.mkdir(parents=True, exist_ok=True)
        Path(self.outputs()[-1]).write_text(CHILD_XML, encoding="utf-8")
        code, out, err = self.replan()
        self.assertEqual(code, 0, err)
        self.assertIn("attempt=c02a01", out)


LLM_XML = XML.replace('max="10">', 'max="10" decider="llm">', 1)


class TestDeciderAttempts(RunLlmCase):
    def raise_decision(self):
        self.prompt()
        self.finish(DECISION_BODY)
        self.assertEqual(self.record()[0], 4)

    def decide(self):
        return self.cli("decide", self.xml, "s1", "--result", self.base())

    def decider_records(self):
        return sorted((self.run_dir / "decisions").glob("*_decider.json"))

    def test_a_request_is_delegated_to_the_llm_decider_once(self):
        Path(self.xml).write_text(LLM_XML, encoding="utf-8")
        self.raise_decision()
        code, out, err = self.decide()
        self.assertEqual(code, 0, err)
        record = json.loads(self.decider_records()[0].read_text(encoding="utf-8"))
        self.assertIn(f"answer={record['answer_path']}", out)
        self.assertIn(f"description={record['description']}", out)
        code, _, err = self.decide()
        self.assertEqual(code, 1)
        self.assertIn("not delegated again", err)
        self.assertEqual(len(self.decider_records()), 1)

    def test_a_human_decider_is_never_delegated(self):
        self.raise_decision()
        code, _, err = self.decide()
        self.assertEqual(code, 2)
        self.assertIn("human", err)
        self.assertEqual(self.decider_records(), [])

    def test_no_open_request_means_no_delegation(self):
        Path(self.xml).write_text(LLM_XML, encoding="utf-8")
        self.prompt()
        code, _, err = self.decide()
        self.assertEqual(code, 2)
        self.assertIn("no open decision request", err)


def session_dir_of(out):
    match = re.search(r"session-dir=([^,)]+)", out)
    return None if match is None else Path(match.group(1))


class TestAttemptSession(RunLlmCase):
    def test_each_attempt_names_its_own_session_dir(self):
        code, out, err = self.prompt()
        self.assertEqual(code, 0, err)
        first = session_dir_of(out)
        self.assertIsNotNone(first, out)
        self.assertEqual(first.parent, self.steps)
        self.finish("ERROR: boom")
        self.record()
        code, out, err = self.prompt()
        self.assertEqual(code, 0, err)
        self.assertNotEqual(session_dir_of(out), first)


class TestCcStopObservation(CcCase):
    def test_a_task_that_already_ended_counts_as_stopped(self):
        _, stop, _ = self.aborted_and_stopped(task_stop="completed")
        self.assertEqual(stop[0], 12, stop)

    def test_a_refused_or_interrupted_call_is_unconfirmed(self):
        self.prompt()
        self.transcript(self.work_lines() + [{
            "type": "user", "toolDenialKind": "user-rejected",
            "message": {"role": "user", "content": []}}])
        code, out, _ = self.stop("cc", "--task-stop", "stopped")
        self.assertEqual(code, 13, out)
        self.assertIn("user-rejected", out)

    def test_a_second_stop_replays_the_first(self):
        _, stop, _ = self.aborted_and_stopped(task_stop="failed")
        self.assertEqual(stop[0], 13, stop)
        code, out, _ = self.stop("cc", "--task-stop", "stopped")
        self.assertEqual(code, 13, "an attempt's stop is observed once; asking "
                                   "again cannot confirm it")

    def test_the_observation_window_is_at_most_a_minute(self):
        self.prompt()
        code, _, err = self.stop("cc", "--task-stop", "stopped", "--observe", "61")
        self.assertEqual(code, 2, err)

    def test_a_cc_stop_needs_the_task_stop_result(self):
        self.prompt()
        code, _, err = self.stop("cc")
        self.assertEqual(code, 2, err)

    def test_a_recorded_success_has_nothing_to_stop(self):
        self.prompt()
        self.finish()
        self.assertEqual(self.record()[0], 0)
        code, _, err = self.stop("cc", "--task-stop", "stopped")
        self.assertEqual(code, 2)
        self.assertIn("nothing to stop", err)

    def test_a_completion_after_a_confirmed_stop_is_not_adopted(self):
        self.prompt()
        self.transcript(self.work_lines())
        self.assertEqual(self.stop("cc", "--task-stop", "stopped")[0], 12)
        self.finish()
        code, out, _ = self.record()
        self.assertEqual(code, 2, out)
        self.assertIn("not adopted", out)
        entry = self.ledger()[-1]
        self.assertEqual((len(self.ledger()), entry["class"]), (1, "aborted"))
        self.assertTrue(entry.get("stop_contradiction"))
        self.assertEqual(json.loads(Path(self.vars).read_text(encoding="utf-8")), {})
        code, _, err = self.prompt("s1", "--new-cycle")
        self.assertEqual(code, 1)
        self.assertIn("cannot be trusted", err)

    def test_a_completion_after_an_unconfirmed_stop_is_not_adopted(self):
        self.prompt()
        self.transcript(self.work_lines())
        self.assertEqual(self.stop("cc", "--task-stop", "failed")[0], 13)
        self.finish()
        code, out, _ = self.record()
        self.assertEqual(code, 2, out)
        entry = self.ledger()[-1]
        self.assertTrue(entry.get("late_completion"))
        self.assertFalse(entry.get("stop_contradiction"))

    def test_a_growing_transcript_of_a_started_agent_is_unconfirmed(self):
        self.prompt()
        self.transcript(self.work_lines(tool="Agent", tool_input={
            "prompt": "p", "run_in_background": False}))
        (self.subagents / "agent-a2.meta.json").write_text(
            json.dumps({"toolUseId": "toolu_w"}), encoding="utf-8")
        child = self.subagents / "agent-a2.jsonl"
        child.write_text("", encoding="utf-8")

        def grow(count):
            if count == 1:
                with child.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"type": "assistant",
                                        "message": {"content": []}}) + "\n")

        self.clock.on_sleep = grow
        code, out, _ = self.stop("cc", "--task-stop", "stopped")
        self.assertEqual(code, 13, out)
        self.assertIn("changed during the observation window", out)


class TestJudgeContract(CcCase):
    def test_the_manifest_pins_every_evidence_file(self):
        self.aborted_and_stopped()
        code, out, err = self.judge()
        self.assertEqual(code, 0, err)
        manifest = self.evidence()
        self.assertEqual(sorted(entry["kind"] for entry in manifest["files"]),
                         ["prompt", "stop", "transcript"])
        for entry in manifest["files"]:
            self.assertEqual(entry["sha256"],
                             hashlib.sha256(Path(entry["path"]).read_bytes()).hexdigest(),
                             entry["path"])
        for fact in ("model=", "tools=", f"timeout={manifest['judge']['timeout']}"):
            self.assertIn(fact, out)

    def test_a_record_without_its_limits_is_refused(self):
        path, _, _ = self.aborted_and_stopped()
        self.judge()
        record = Path(self.evidence()["judge"]["record_path"])
        record.write_text(f"verdict: allow\nmethod: restart\nevidence:\n"
                          f"- {path}#L1-L2: it wrote its output\n"
                          f"{end_marker(self.evidence())}\n", encoding="utf-8")
        code, _, err = self.prompt("s1", "--redispatch", f"allow {record}")
        self.assertEqual(code, 1)
        self.assertIn("limits", err)

    def test_a_record_without_the_judges_end_marker_is_refused(self):
        _, record = self.judged()
        complete = record.read_text(encoding="utf-8")
        marker = end_marker(self.evidence())
        for last in ("", self.handle()["sentinel"]):
            with self.subTest(last=last):
                record.write_text(complete.replace(marker, last), encoding="utf-8")
                code, _, err = self.prompt("s1", "--redispatch", f"allow {record}")
                self.assertEqual(code, 1)
                self.assertIn("end marker", err)
        record.write_text(complete, encoding="utf-8")
        code, _, err = self.prompt("s1", "--redispatch", f"allow {record}")
        self.assertEqual(code, 0, err)

    def test_a_record_that_does_not_allow_is_refused(self):
        _, record = self.judged(verdict="deny")
        code, _, err = self.prompt("s1", "--redispatch", f"allow {record}")
        self.assertEqual(code, 1)
        self.assertIn("verdict: allow", err)

    def test_a_record_citing_no_evidence_is_refused(self):
        self.aborted_and_stopped()
        self.judge()
        record = self.write_record([])
        code, _, err = self.prompt("s1", "--redispatch", f"allow {record}")
        self.assertEqual(code, 1)
        self.assertIn("cites no evidence", err)

    def test_a_refusal_in_an_agent_started_after_the_judge_blocks_the_restart(self):
        _, record = self.judged()
        (self.subagents / "agent-a9.meta.json").write_text(
            json.dumps({"parentAgentId": "a1"}), encoding="utf-8")
        (self.subagents / "agent-a9.jsonl").write_text(json.dumps({
            "type": "user", "toolDenialKind": "permission-rule",
            "message": {"role": "user", "content": []}}) + "\n", encoding="utf-8")
        code, _, err = self.prompt("s1", "--redispatch", f"allow {record}")
        self.assertEqual(code, 1)
        self.assertIn("refused", err)

    def test_the_judge_has_a_handle_that_wakes_at_its_deadline(self):
        self.aborted_and_stopped()
        self.judge()
        judge = self.evidence()["judge"]
        handle = json.loads(Path(judge["handle_path"]).read_text(encoding="utf-8"))
        self.assertEqual((handle["dispatched_at"], handle["timeout"]),
                         (judge["issued_at"], judge["timeout"]))
        self.assertEqual(self.cli("poll", judge["handle_path"])[:2], (10, "running"))
        Path(judge["record_path"]).write_text(f"verdict: deny\n{handle['sentinel']}\n",
                                              encoding="utf-8")
        self.assertEqual(self.cli("poll", judge["handle_path"])[:2], (0, "done"))

    def test_a_judge_handle_is_not_an_attempt(self):
        self.aborted_and_stopped()
        self.judge()
        handle_path = self.evidence()["judge"]["handle_path"]
        code, _, err = self.cli("stop", handle_path, "--facet", "cc",
                                "--task-stop", "stopped")
        self.assertEqual(code, 2, err)


def running(image, created):
    from wfrun import procid
    return procid.Probe(procid.STATE_RUNNING, image, created)


def gone():
    from wfrun import procid
    return procid.Probe(procid.STATE_GONE)


def unreadable():
    from wfrun import procid
    return procid.Probe(procid.STATE_UNKNOWN, reason="access denied")


LAUNCHED = {101: ("bash.exe", 5), 102: ("node.exe", 6)}


class PiCase(ClockCase):
    def pids(self, *pids):
        return self.cli("pids", str(self.pointer()), *(str(pid) for pid in pids))

    def probing(self, table):
        return mock.patch("wfrun.procid.probe",
                          lambda pid: table[pid] if pid in table else gone())

    def launched(self):
        code, out, err = self.prompt()
        self.assertEqual(code, 0, err)
        with self.probing({pid: running(*identity)
                           for pid, identity in LAUNCHED.items()}):
            code, _, err = self.pids(*LAUNCHED)
        self.assertEqual(code, 0, err)
        return out

    def stop_with(self, table, *extra):
        with self.probing(table):
            return self.stop("pi-cli", *extra)

    def stem(self):
        handle = self.handle()
        return f"s1_c{handle['cycle']:02d}_a{handle['attempt']:02d}"

    def stop_record(self):
        return json.loads((self.steps / f"{self.stem()}_stop.json")
                          .read_text(encoding="utf-8"))

    def evidence(self):
        return json.loads((self.steps / f"{self.stem()}_evidence.json")
                          .read_text(encoding="utf-8"))

    def session(self, out, *offsets):
        directory = session_dir_of(out)
        directory.mkdir(parents=True, exist_ok=True)
        t0 = self.handle()["dispatched_at"]
        path = directory / "2026-01-01T00-00-00-000Z_s.jsonl"
        path.write_text("".join(
            json.dumps({"type": "message", "timestamp": iso(t0 + offset)}) + "\n"
            for offset in offsets), encoding="utf-8")
        return path

    def write_record(self, path, next_timeout=None):
        manifest = self.evidence()
        record = Path(manifest["judge"]["record_path"])
        lines = ["verdict: allow", "method: restart"]
        if next_timeout is not None:
            lines.append(f"next-timeout: {next_timeout}")
        lines += ["evidence:", f"- {path}#L1-L2: the attempt kept working",
                  "limits: none", end_marker(manifest)]
        record.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return record


class TestPiProcesses(PiCase):
    def test_gone_recorded_processes_confirm_the_stop(self):
        self.launched()
        code, out, _ = self.stop_with({})
        self.assertEqual(code, 12, out)
        record = self.stop_record()
        self.assertEqual(sorted(p["pid"] for p in record["processes"]), [101, 102])
        self.assertIn("descendants", record["not_observed"],
                      "a Pi CLI stop speaks for the recorded processes only")

    def test_a_result_completed_before_the_stop_is_adopted(self):
        self.launched()
        self.finish()
        self.assertEqual(self.stop_with({})[:2], (0, "done"))
        self.assertEqual(self.record()[0], 0)

    def test_an_ok_reply_after_a_confirmed_stop_is_recorded_aborted(self):
        self.launched()
        self.assertEqual(self.stop_with({})[0], 12)
        code, out, _ = self.record("s1", "--reply", "OK s1")
        self.assertEqual(code, 3, "after a stop the attempt is settled by its stop, "
                                  "not by a reply claiming OK")
        self.assertTrue(out.startswith("aborted"), out)
        self.assertEqual(self.ledger()[-1]["class"], "aborted")

    def test_a_recorded_process_still_running_leaves_the_stop_unconfirmed(self):
        self.launched()
        code, out, _ = self.stop_with({102: running("node.exe", 6)})
        self.assertEqual(code, 13, out)
        self.assertIn("102", out)

    def test_a_reused_pid_is_not_the_recorded_process(self):
        self.launched()
        code, out, _ = self.stop_with({101: running("bash.exe", 7),
                                       102: running("svchost.exe", 99)})
        self.assertEqual(code, 12, out)

    def test_a_process_that_cannot_be_read_counts_as_running(self):
        self.launched()
        code, out, _ = self.stop_with({102: unreadable()})
        self.assertEqual(code, 13, out)

    def test_a_process_that_is_not_running_is_not_recorded(self):
        self.prompt()
        with self.probing({102: running("node.exe", 6)}):
            code, _, err = self.pids(101, 102)
        self.assertEqual(code, 1, err)
        self.assertIn("101", err)
        code, out, _ = self.stop_with({})
        self.assertEqual(code, 13, out)
        self.assertIn("no process set", out)

    def test_processes_are_recorded_once_per_attempt(self):
        self.launched()
        with self.probing({103: running("node.exe", 7)}):
            code, _, err = self.pids(103)
        self.assertEqual(code, 1, err)
        self.assertIn("already", err)

    def test_processes_are_recorded_while_the_attempt_runs(self):
        self.prompt()
        self.finish()
        self.record()
        with self.probing({103: running("node.exe", 7)}):
            code, _, err = self.pids(103)
        self.assertEqual(code, 1, err)


class TestPiJudge(PiCase):
    def test_the_attempts_session_is_its_transcript(self):
        out = self.launched()
        path = self.session(out, 1, 5)
        self.assertEqual(self.stop_with({})[0], 12)
        self.record()
        code, _, err = self.judge()
        self.assertEqual(code, 0, err)
        listed = {Path(entry["path"]): entry["kind"] for entry in self.evidence()["files"]}
        self.assertEqual(listed.get(path.resolve()), "transcript")

    def test_an_attempt_without_a_session_is_not_judged(self):
        self.launched()
        self.assertEqual(self.stop_with({})[0], 12)
        self.record()
        code, _, err = self.judge()
        self.assertEqual(code, 1)
        self.assertIn("session", err)

    def test_a_longer_deadline_grounded_in_the_session_is_used(self):
        out = self.launched()
        path = self.session(out, 1, 5)
        handle = self.handle()
        late = handle["dispatched_at"] + handle["timeout"] + 1
        with mock.patch("wfrun.llmgate._now", return_value=late):
            self.assertEqual(self.stop_with({})[0], 12)
        self.record()
        self.assertEqual(self.judge()[0], 0)
        record = self.write_record(path, next_timeout=200)
        code, _, err = self.prompt("s1", "--redispatch", f"allow {record}")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.handle()["timeout"], 200)


SLEEPER = "import os, time; print(os.getpid(), flush=True); time.sleep(60)"

TREE_ROOT = (
    "import os, subprocess, sys, time; "
    f"child = subprocess.Popen([sys.executable, '-c', {SLEEPER!r}], "
    "stdin=subprocess.DEVNULL, stdout=subprocess.PIPE); "
    "print(os.getpid(), child.pid, child.stdout.readline().decode().strip(), "
    "flush=True); time.sleep(60)")

LATE_CHILD_ROOT = (
    "import os, subprocess, sys, time; "
    "print(os.getpid(), flush=True); sys.stdin.readline(); "
    f"child = subprocess.Popen([sys.executable, '-c', {SLEEPER!r}], "
    "stdin=subprocess.DEVNULL, stdout=subprocess.PIPE); "
    "print(child.pid, child.stdout.readline().decode().strip(), flush=True); "
    "time.sleep(60)")


@unittest.skipUnless(os.name == "nt", "the process probe reads the Windows API")
class TestPiProcessTree(RunLlmCase):
    def setUp(self):
        super().setUp()
        self.learned = []

    def spawn(self, script):
        root = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True, encoding="utf-8")
        self.addCleanup(self.reap, root)
        return root

    def learn(self, *pids):
        from wfrun import procid
        for pid in pids:
            self.learned.append((pid, procid.probe(pid).created))

    def reap(self, root):
        if root.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(root.pid)],
                           capture_output=True, check=False)
        root.wait(timeout=10)
        root.stdin.close()
        root.stdout.close()
        from wfrun import procid
        for pid, created in self.learned:
            if created is not None and procid.is_running(pid, created):
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, check=False)

    def stop_pi(self, seconds):
        return self.cli("stop", str(self.pointer()), "--facet", "pi-cli",
                        "--observe", str(seconds))

    def test_a_killed_tree_confirms_the_stop(self):
        self.prompt()
        root = self.spawn(TREE_ROOT)
        shell, child, worker = (int(x) for x in root.stdout.readline().split())
        self.learn(shell, child, worker)
        code, _, err = self.cli("pids", str(self.pointer()), str(shell), str(worker))
        self.assertEqual(code, 0, err)
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(root.pid)],
                       capture_output=True, check=False)
        root.wait(timeout=10)
        code, out, _ = self.stop_pi(5)
        self.assertEqual(code, 12, out)

    def test_a_live_process_leaves_the_stop_unconfirmed(self):
        self.prompt()
        root = self.spawn(TREE_ROOT)
        shell, child, worker = (int(x) for x in root.stdout.readline().split())
        self.learn(shell, child, worker)
        code, _, err = self.cli("pids", str(self.pointer()), str(shell), str(worker))
        self.assertEqual(code, 0, err)
        code, out, _ = self.stop_pi(1)
        self.assertEqual(code, 13, out)
        self.assertIn(str(worker), out)

    def test_a_process_outside_the_recorded_set_is_not_observed(self):
        from wfrun import procid
        self.prompt()
        root = self.spawn(LATE_CHILD_ROOT)
        shell = int(root.stdout.readline())
        self.learn(shell)
        code, _, err = self.cli("pids", str(self.pointer()), str(shell))
        self.assertEqual(code, 0, err)
        root.stdin.write("go\n")
        root.stdin.flush()
        child, worker = (int(x) for x in root.stdout.readline().split())
        self.learn(child, worker)
        subprocess.run(["taskkill", "/F", "/PID", str(shell)],
                       capture_output=True, check=False)
        root.wait(timeout=10)
        code, out, _ = self.stop_pi(5)
        self.assertEqual(code, 12, "the stop speaks for the recorded set only, so a "
                                   "process started after the record is not seen")
        self.assertTrue(procid.is_running(worker, dict(self.learned)[worker]),
                        "the unrecorded process is still running")
        record = json.loads(next(self.steps.glob("s1_c01_a01_stop.json"))
                            .read_text(encoding="utf-8"))
        self.assertIn("descendants", record["not_observed"])


if __name__ == "__main__":
    unittest.main()
