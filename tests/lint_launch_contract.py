#!/usr/bin/env python3
"""Lint: the Pi CLI launch contract is the same in both skills that use it.

mode-orchestrator (references/harness-pi.md) and xml-wf (references/run-llm.md)
each launch a fresh `pi -p` process from one bash call. Two things must not
drift apart, and no single file shows it when they do:

1. The shared contract text, kept word for word between the markers
   CONTRACT_BEGIN and CONTRACT_END in both documents.
2. The launch line each document actually tells its orchestrator to copy (the
   fenced block right after LINE_MARKER): it must carry every common element,
   in the order the contract needs. A byte-identical contract block cannot
   show that one skill's real command dropped `< /dev/null`.

Run:   uv run --no-project python tests/lint_launch_contract.py
Check: uv run --no-project python tests/lint_launch_contract.py --self-test
       (one-edge controls: a document missing exactly one element must fail)

Exit 0 = holds, 1 = violation, 2 = scanned nothing.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DOCS = (
    "skills/mode-orchestrator/references/harness-pi.md",
    "skills/xml-wf/references/run-llm.md",
)

CONTRACT_BEGIN = "<!-- pi-cli-launch-contract:begin -->"
CONTRACT_END = "<!-- pi-cli-launch-contract:end -->"
LINE_MARKER = "<!-- pi-cli-launch-line -->"
FENCE = "```"

EXIT_HOLDS = 0
EXIT_VIOLATION = 1
EXIT_SCANNED_NOTHING = 2

# (name, pattern) pairs a contract block must contain. The patterns name the
# entry resolution, which only the shared prose can carry.
CONTRACT_ELEMENTS = (
    ("entry resolved through npm prefix", r"npm prefix -g"),
    ("entry path", r"node_modules/@earendil-works/pi-coding-agent/dist/cli\.js"),
    ("launch through node", r"node <pi-cli-entry>"),
    ("stdin closed", r"< /dev/null"),
    ("no skills", r"--no-skills"),
    ("session kept", r"--session-dir"),
    ("MSYS path conversion off", r"MSYS_NO_PATHCONV=1"),
    ("processes recorded", r"/proc/<pid>/winpid"),
)

# (name, pattern) pairs every launch line must contain, listed in the order
# they must appear: the message is in a variable before the CLI starts, the
# processes are recorded after it is running and before the call waits for it.
LINE_ELEMENTS = (
    ("message assigned to a variable", r"^PROMPT="),
    ("MSYS path conversion off", r"MSYS_NO_PATHCONV=1"),
    ("launch through node", r"node <pi-cli-entry>"),
    ("non-interactive", r"(?<![\w-])-p(?![\w-])"),
    ("no skills", r"--no-skills"),
    ("session kept", r"--session-dir\s+\S+"),
    ("message passed through the variable", r'"\$PROMPT"(?=\s*(?:\\|<|&|$))'),
    ("stdin closed", r"<\s*/dev/null"),
    ("background launch captured", r"child=\$!"),
    ("CLI process translated", r"/proc/\$child/winpid"),
    ("shell process translated", r"/proc/\$\$/winpid"),
    ("call waits for the CLI", r"wait \$child"),
)

MO_PREFLIGHT_ELEMENTS = (
    ("attempt message file selected", r'^PROMPT_FILE="<run-dir>/<turn-key>-<attempt>-prompt\.md"$'),
    ("message read failure stops before launch",
     r'^PROMPT=\$\(< "\$PROMPT_FILE"\) \|\| \{\n'
     r'[^\n]*mode-orchestrator: prompt preflight failed:[^\n]*\n\s*exit 64\n\}'),
    ("empty message stops before launch",
     r'^\[\[ -n "\$PROMPT" \]\] \|\| \{\n'
     r'[^\n]*mode-orchestrator: prompt preflight failed:[^\n]*\n\s*exit 64\n\}'),
)


def line_elements(name: str):
    if name == DOCS[0]:
        return MO_PREFLIGHT_ELEMENTS + LINE_ELEMENTS[1:]
    return LINE_ELEMENTS


NOT_SEEN = (
    "not seen by this gate: a launch line copied outside the marked blocks; "
    "wording around the blocks; a skill's own options (--mode, --tools, "
    "MODE_ORCH_DEPTH); prompt-file creation or overwrite safety; "
    "shell execution and the real CLI."
)


def normalize(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))


def contract_block(text: str) -> str | None:
    """The text between the markers; None unless each occurs exactly once."""
    if text.count(CONTRACT_BEGIN) != 1 or text.count(CONTRACT_END) != 1:
        return None
    begin = text.index(CONTRACT_BEGIN) + len(CONTRACT_BEGIN)
    end = text.index(CONTRACT_END)
    if end < begin:
        return None
    return text[begin:end].strip()


def launch_lines(text: str) -> list[str]:
    """The body of the fenced block that follows each LINE_MARKER."""
    found = []
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.strip() != LINE_MARKER:
            continue
        body, inside = [], False
        for follower in lines[index + 1:]:
            if not inside:
                if follower.strip() == "":
                    continue
                if not follower.startswith(FENCE):
                    break
                inside = True
                continue
            if follower.startswith(FENCE):
                found.append("\n".join(body))
                break
            body.append(follower)
    return found


def missing_elements(text: str, elements) -> list[str]:
    return [name for name, pattern in elements
            if re.search(pattern, text, re.MULTILINE) is None]


def out_of_order(text: str, elements) -> list[str]:
    """Elements present but earlier in the text than the one before them."""
    problems, floor, floor_name = [], -1, ""
    for name, pattern in elements:
        match = re.search(pattern, text, re.MULTILINE)
        if match is None:
            continue
        if match.start() < floor:
            problems.append(f"{name} comes before {floor_name}")
            continue
        floor, floor_name = match.start(), name
    return problems


def check_docs(texts: dict[str, str]) -> list[str]:
    failures: list[str] = []
    blocks: dict[str, str] = {}
    for name, raw in texts.items():
        text = normalize(raw)
        block = contract_block(text)
        if block is None:
            failures.append(f"{name}: the contract block is missing, or its "
                            "markers do not occur exactly once each")
        else:
            blocks[name] = block
            failures += [f"{name}: contract block lacks: {missing}"
                         for missing in missing_elements(block, CONTRACT_ELEMENTS)]
        lines = launch_lines(text)
        if not lines:
            failures.append(f"{name}: no launch line is marked")
        elements = line_elements(name)
        for number, body in enumerate(lines, 1):
            failures += [f"{name}: launch line {number} lacks: {missing}"
                         for missing in missing_elements(body, elements)]
            failures += [f"{name}: launch line {number}: {problem}"
                         for problem in out_of_order(body, elements)]
    if len(set(blocks.values())) > 1:
        failures.append("the contract blocks differ between: " + ", ".join(blocks))
    return failures


def read_docs() -> dict[str, str]:
    return {name: (ROOT / name).read_text(encoding="utf-8")
            for name in DOCS if (ROOT / name).is_file()}


def drop_first(text: str, pattern: str) -> str:
    return re.sub(pattern, "", text, count=1, flags=re.MULTILINE)


def one_edge_edits(text: str, elements):
    """(label, edited text) pairs, each differing from `text` by one edge."""
    begin = text.index(CONTRACT_BEGIN)
    end = text.index(CONTRACT_END)
    for label, pattern in CONTRACT_ELEMENTS:
        yield (f"removing contract element '{label}'",
               text[:begin] + drop_first(text[begin:end], pattern) + text[end:])
    start = text.index(LINE_MARKER)
    for label, pattern in elements:
        yield (f"removing launch element '{label}'",
               text[:start] + drop_first(text[start:], pattern))
    waited = drop_first(text[start:], r"wait \$child")
    recorded = waited.index("child=$!")
    yield ("moving the wait before the processes are recorded",
           text[:start] + waited[:recorded] + "wait $child\n" + waited[recorded:])
    yield ("changing one word of the contract block",
           text.replace("Never `pi`", "Never `pix`", 1))
    yield ("removing the contract begin marker",
           text.replace(CONTRACT_BEGIN, "", 1))
    yield ("removing the line marker", text.replace(LINE_MARKER, "", 1))


def self_test(texts: dict[str, str]) -> list[str]:
    """One-edge controls. Each edit must make check_docs fail; the unedited
    documents must pass first, or the controls prove nothing."""
    problems = [f"unedited documents fail: {f}" for f in check_docs(texts)]
    if len(texts) < len(DOCS):
        problems.append("a document is missing: the controls cannot run")
    if problems:
        return problems
    for target, raw in texts.items():
        text = normalize(raw)
        for label, edited in one_edge_edits(text, line_elements(target)):
            if edited == text or not check_docs({**texts, target: edited}):
                problems.append(f"{target}: {label} was not detected")
    return problems


def main(argv: list[str]) -> int:
    texts = read_docs()
    print(f"scanned {len(texts)} of {len(DOCS)} documents")
    print(NOT_SEEN)
    if not texts:
        print("GATE FAILED: scanned nothing. A gate that sees nothing is not a "
              "clean tree.")
        return EXIT_SCANNED_NOTHING
    if "--self-test" in argv:
        problems = self_test(texts)
        for problem in problems:
            print(f"  self-test: {problem}")
        print("self-test: " + ("FAILED" if problems else "every control detected"))
        return EXIT_VIOLATION if problems else EXIT_HOLDS
    failures = check_docs(texts)
    if len(texts) < len(DOCS):
        missing = [name for name in DOCS if name not in texts]
        failures.append("documents not found: " + ", ".join(missing))
    for failure in failures:
        print(f"  {failure}")
    if failures:
        print(f"{len(failures)} violation(s)")
        return EXIT_VIOLATION
    print("gate holds: the contract text is shared and every launch line "
          "carries the common elements.")
    return EXIT_HOLDS


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
