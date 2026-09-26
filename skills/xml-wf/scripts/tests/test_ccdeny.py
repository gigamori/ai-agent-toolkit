import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun import ccdeny  # noqa: E402

SESSION = "11111111-2222-3333-4444-555555555555"
OTHER_SESSION = "99999999-8888-7777-6666-555555555555"
DESCRIPTION = "wfrun s1 a1 0badc0de"


def _denial(kind="permission-rule"):
    return {"type": "user", "toolDenialKind": kind,
            "message": {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "toolu_x",
                "is_error": True, "content": "refused"}]}}


def _agent_call(tool_use_id):
    return {"type": "assistant",
            "message": {"role": "assistant", "content": [{
                "type": "tool_use", "id": tool_use_id, "name": "Agent",
                "input": {}}]}}


class CheckDelegationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name) / "config"
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config),
                                           "CLAUDE_CODE_SESSION_ID": SESSION})
        env.start()
        self.addCleanup(env.stop)

    def agent(self, agent_id, lines, session=SESSION, **meta):
        subagents = self.config / "projects" / "proj" / session / "subagents"
        subagents.mkdir(parents=True, exist_ok=True)
        (subagents / f"agent-{agent_id}.meta.json").write_text(
            json.dumps(meta), encoding="utf-8")
        transcript = subagents / f"agent-{agent_id}.jsonl"
        transcript.write_text("".join(json.dumps(line) + "\n" for line in lines),
                              encoding="utf-8")
        return transcript

    def check(self):
        return ccdeny.check_delegation({"delegation": DESCRIPTION})

    def test_denial_in_the_delegated_agent_is_denied(self):
        self.agent("a1", [_denial()], description=DESCRIPTION)
        check = self.check()
        self.assertEqual(check.verdict, ccdeny.DENIED)
        self.assertEqual(check.label, "DENIED 1")
        self.assertEqual(check.kinds, ("permission-rule",))

    def test_clean_transcript_is_clean(self):
        transcript = self.agent("a1", [_agent_call("toolu_1")], description=DESCRIPTION)
        check = self.check()
        self.assertEqual(check.verdict, ccdeny.CLEAN)
        self.assertEqual(check.transcript, str(transcript))

    def test_every_denial_kind_counts(self):
        kinds = ["permission-rule", "user-rejected", "automode-blocked",
                 "automode-unavailable", "cancelled"]
        self.agent("a1", [_denial(kind) for kind in kinds], description=DESCRIPTION)
        self.assertEqual(self.check().label, "DENIED 5")

    def test_another_sessions_agent_is_not_this_step(self):
        self.agent("a1", [_denial()], session=OTHER_SESSION, description=DESCRIPTION)
        self.assertEqual(self.check().verdict, ccdeny.NO_TRANSCRIPT)

    def test_denial_in_an_agent_the_step_started_counts(self):
        self.agent("a1", [_agent_call("toolu_child")], description=DESCRIPTION)
        self.agent("a2", [_denial()], description="child", toolUseId="toolu_child")
        self.assertEqual(self.check().label, "DENIED 1")

    def test_denial_in_a_child_naming_its_parent_agent_counts(self):
        self.agent("a1", [_agent_call("toolu_1")], description=DESCRIPTION)
        self.agent("a2", [_denial()], agentType="fork", parentAgentId="a1")
        self.assertEqual(self.check().label, "DENIED 1")

    def test_denial_two_levels_below_counts(self):
        self.agent("a1", [_agent_call("toolu_l1")], description=DESCRIPTION)
        self.agent("a2", [_agent_call("toolu_l2")], toolUseId="toolu_l1")
        self.agent("a3", [_denial()], toolUseId="toolu_l2")
        self.assertEqual(self.check().label, "DENIED 1")

    def test_an_agent_the_step_did_not_start_is_not_scanned(self):
        self.agent("a1", [_agent_call("toolu_1")], description=DESCRIPTION)
        self.agent("a9", [_denial()], description="unrelated", toolUseId="toolu_9")
        self.assertEqual(self.check().verdict, ccdeny.CLEAN)

    def test_empty_transcript_is_no_transcript(self):
        self.agent("a1", [], description=DESCRIPTION)
        self.assertEqual(self.check().verdict, ccdeny.NO_TRANSCRIPT)

    def test_handle_without_a_delegation_is_no_transcript(self):
        self.agent("a1", [_denial()], description=DESCRIPTION)
        check = ccdeny.check_delegation({"step_id": "s1"})
        self.assertEqual(check.verdict, ccdeny.NO_TRANSCRIPT)

    def test_without_a_session_the_check_does_not_apply(self):
        self.agent("a1", [_denial()], description=DESCRIPTION)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}):
            check = self.check()
        self.assertEqual(check.verdict, ccdeny.NOT_APPLICABLE)
        self.assertEqual(check.label, "n/a")

    def test_no_handle_does_not_apply(self):
        self.assertEqual(ccdeny.check_delegation(None).verdict, ccdeny.NOT_APPLICABLE)

    def test_nul_byte_does_not_hide_a_denial(self):
        transcript = self.agent("a1", [_denial()], description=DESCRIPTION)
        transcript.write_bytes(b"\x00garbage\n" + transcript.read_bytes())
        self.assertEqual(self.check().label, "DENIED 1")

    def test_denial_text_quoted_in_tool_output_is_not_a_denial(self):
        quoted = {"type": "user", "message": {"role": "user", "content": [{
            "type": "tool_result", "is_error": True,
            "content": '"toolDenialKind":"permission-rule"'}]}}
        self.agent("a1", [quoted], description=DESCRIPTION)
        self.assertEqual(self.check().verdict, ccdeny.CLEAN)

    def test_newest_agent_wins_when_a_description_repeats(self):
        older = self.agent("a1", [_denial()], description=DESCRIPTION)
        stale = time.time() - 100
        os.utime(older.with_name("agent-a1.meta.json"), (stale, stale))
        newer = self.agent("a2", [_agent_call("toolu_1")], description=DESCRIPTION)
        check = self.check()
        self.assertEqual(check.verdict, ccdeny.CLEAN)
        self.assertEqual(check.transcript, str(newer))


if __name__ == "__main__":
    unittest.main()
