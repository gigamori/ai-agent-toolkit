import re
import unittest
from pathlib import Path

_SKILL_MD = Path(__file__).resolve().parents[2] / "SKILL.md"
_CLOSED_DOUBLE_QUOTED = re.compile(r'[\w-]+: "(?:[^"\\]|\\.)*"')


def _double_quoted_fields(text: str) -> list[str]:
    lines = text.splitlines()
    end = lines.index("---", 1)
    return [line for line in lines[1:end] if re.match(r'[\w-]+: "', line)]


class TestSkillFrontmatter(unittest.TestCase):
    def test_every_double_quoted_field_is_one_closed_yaml_scalar(self):
        fields = _double_quoted_fields(_SKILL_MD.read_text(encoding="utf-8"))
        self.assertTrue(fields, "SKILL.md has no double-quoted frontmatter field")
        for line in fields:
            self.assertIsNotNone(
                _CLOSED_DOUBLE_QUOTED.fullmatch(line),
                f"{line.split(':')[0]}: a bare double quote inside the value "
                "ends the YAML scalar; see docs/dev/test-constraints.md")


if __name__ == "__main__":
    unittest.main()
