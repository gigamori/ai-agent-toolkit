import unittest
from pathlib import Path

_SKILL = Path(__file__).resolve().parents[2]
_READ_IN_ONE_CALL = ("SKILL.md", "references/run-llm.md", "references/spec.md")
_MAX_BYTES = 51200
_MAX_LINES = 2000


def _as_crlf(path: Path) -> tuple[int, int]:
    data = path.read_bytes().replace(b"\r\n", b"\n")
    lines = data.count(b"\n") + (0 if data.endswith(b"\n") else 1)
    return len(data) + data.count(b"\n"), lines


class TestReferenceReadSize(unittest.TestCase):
    def test_each_file_fits_one_pi_read_in_a_crlf_checkout(self):
        for rel in _READ_IN_ONE_CALL:
            size, lines = _as_crlf(_SKILL / rel)
            with self.subTest(rel=rel):
                self.assertLessEqual(
                    size, _MAX_BYTES,
                    f"{rel}: {size} bytes with CRLF; it must fit one Pi read, "
                    "see docs/dev/test-constraints.md")
                self.assertLessEqual(
                    lines, _MAX_LINES,
                    f"{rel}: {lines} lines; it must fit one Pi read, "
                    "see docs/dev/test-constraints.md")


if __name__ == "__main__":
    unittest.main()
