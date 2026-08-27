import io
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wfrun import adjudicate as adjudicate_mod  # noqa: E402
from wfrun import model, parser  # noqa: E402
from wfrun.__main__ import main  # noqa: E402
from wfrun.claude_cli import CliResult  # noqa: E402


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue().strip(), err.getvalue()


WORKFLOW_XML = (
    '<workflow name="t" version="2" max="5" decider="llm">'
    '<step id="s1" tools="Read"><task>x</task></step></workflow>'
)


class DeciderModelDispatchTests(unittest.TestCase):
    def test_plan_printed_decider_model_matches_adjudicate_dispatch(self):
        with tempfile.TemporaryDirectory() as d:
            xml_path = Path(d) / "wf.xml"
            xml_path.write_text(WORKFLOW_XML, encoding="utf-8")
            code, out, err = run_cli(["plan", str(xml_path)])
        self.assertEqual(code, 0, err)
        match = re.search(r"decider:\s+llm \(model=([^)]+)\)", out)
        self.assertIsNotNone(match, out)
        planned_model = match.group(1)

        wf = parser.parse_string(WORKFLOW_XML)
        _, decider_model = model.resolve_decider(wf)
        captured = {}

        def fake_runner(prompt, **kwargs):
            captured["model"] = kwargs.get("model")
            return CliResult(ok=False, error="stub", cost_usd=0.0)

        adjudicate_mod.adjudicate("s1", "body", 2, model=decider_model,
                                  runner=fake_runner)
        self.assertEqual(
            planned_model, captured["model"],
            "wfrun plan resolves decider-model via the 'llm' table while "
            "adjudicate dispatches via the 'cc' table; the two agree here "
            "only because the bundled map binds the same tier to the same "
            "name on both tables -- a red result means either a genuine "
            "plan/dispatch divergence or a deliberate split between the "
            "'cc' and 'llm' tables, not necessarily a regression")


if __name__ == "__main__":
    unittest.main()
