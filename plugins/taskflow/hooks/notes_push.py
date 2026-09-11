"""notes_push.py — per-turn project-notes pointer push (imported by session_init.py).

Builds the block session_init.py injects each turn:

    project-notes (index rows):
    <verbatim project-notes/index.md rows, K at most>
    project-notes matched: M of N rows (K shown)

Three retrieval layers, composed in this order (design: notes-retrieval-locality-qmd.md §4):

    L1  location, exact file   note `anchors:` path == a path this turn touches
    L2  location, directory    same leaf directory; at most ceil(K/2) rows
    T   topic                  outer RRF of (qmd structured query rank, in-process
                               lexical rank); qmd only when project-notes/.qmd/ exists

"A path this turn touches" = paths named in the prompt ∪ the tail of the session's
`.reads` ledger (read_capture.py) ∪ the tail of its `.touched` ledger
(touched_capture.py). `_projects/` and `tmp/` paths are never location signals.

Contract kept from the router: rows are index.md lines verbatim, `_archive/` rows are
excluded, note bodies are never read for output (anchors are read from frontmatter
only). Degrade: when qmd is expected (`.qmd/` present, mode auto) but cannot run
(binary unresolved, spawn failure, non-zero exit, timeout, bad JSON), the topic layer
is the lexical rank alone and the caller shows one notice per session.

Config (env): TASKFLOW_NOTES_PUSH = off | lexical | auto (default);
TASKFLOW_QMD_BIN = path of qmd's `bin/qmd` (default: resolved next to the `qmd` on
PATH); TASKFLOW_QMD_TIMEOUT = seconds the qmd query may take (default 6). qmd is
spawned as `node <bin/qmd>` — never through a `.cmd` shim.

Cost, accepted explicitly (user ruling 2026-09-12): the query runs synchronously on
the prompt-injection path every turn of an onboarded project, measured 2.7-2.9 s on the
reference machine (GPU embedding, 79-210 docs). The timeout is a shared contract
constant with the Pi extension; change it on both sides or not at all.

stdlib only: hooks run under `uv run --no-project`.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess

K_ROWS = 8
L2_CAP = math.ceil(K_ROWS / 2)
LEDGER_TAIL = 40
QMD_TIMEOUT_S = 6.0
TIMEOUT_ENV = 'TASKFLOW_QMD_TIMEOUT'
QMD_MARGIN = 8
RRF_K = 60
VEC_MAX_CHARS = 1000
LEX_TERMS_MAX = 12
IDF_CUTOFF = 0.30
VECTOR_TAU = 0.3
QMD_DIR = '.qmd'
MODE_ENV = 'TASKFLOW_NOTES_PUSH'
BIN_ENV = 'TASKFLOW_QMD_BIN'
MODES = ('off', 'lexical', 'auto')
QMD_REL_BIN = os.path.join('node_modules', '@tobilu', 'qmd', 'bin', 'qmd')
QMD_URI_PREFIX = 'qmd://'
EXCLUDED_PATH_PREFIXES = ('_projects/', 'tmp/')
ARCHIVE_PREFIX = '_archive/'
HEADER = 'project-notes (index rows):'

FM_RE = re.compile(r'^---\r?\n(.*?)\r?\n---\r?\n', re.DOTALL)
ANCHORS_KEY_RE = re.compile(r'^anchors:\s*(.*)$')
ANCHORS_ITEM_RE = re.compile(r'^\s+-\s*(.+?)\s*$')
PATH_RE = re.compile(
    r'(?<![\w/.-])((?:[A-Za-z]:)?/?(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,6})'
    r'(?::\d+)?'
)
CJK_RUN_RE = re.compile(r'[぀-ヿ㐀-鿿豈-﫿]+')
HIRAGANA_RE = re.compile(r'[぀-ゟ]+')
ASCII_TOKEN_RE = re.compile(r'[a-z0-9]{2,}')
LEX_WORD_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{2,}')
VEC_NEGATION_RE = re.compile(r'(^|\s)-(?=[\w"])')

CONTROL_SLUG_RE = re.compile(r'^(?:pj|mode|role):\S*$', re.IGNORECASE)
BARE_CONTROL = {'norouter'}
COMMAND_RE = re.compile(r'^/[a-z0-9_-]+(?::[a-z0-9_-]+)*$', re.IGNORECASE)
FLAG_RE = re.compile(r'^--?[a-z][a-z0-9-]*$', re.IGNORECASE)

STOPWORDS = {
    'the', 'and', 'for', 'are', 'with', 'this', 'that', 'from', 'into', 'your', 'you',
    'all', 'can', 'has', 'have', 'had', 'was', 'were', 'been', 'being', 'but', 'not',
    'out', 'get', 'set', 'its', 'will', 'may', 'one', 'two', 'more', 'than', 'then',
    'when', 'what', 'how', 'why', 'who', 'which', 'such', 'run', 'dev', 'use', 'add',
    'fix', 'new',
}


class QmdError(Exception):
    pass


# ---------------------------------------------------------------------------
# subject text (port of the Pi extension's deriveSubjectText)
# ---------------------------------------------------------------------------

def _is_mechanism(token: str) -> bool:
    if token.lower() in BARE_CONTROL:
        return True
    return bool(CONTROL_SLUG_RE.match(token) or COMMAND_RE.match(token) or FLAG_RE.match(token))


def derive_subject_text(prompt: str) -> str:
    """The prompt minus mechanism tokens (pj:/mode:/role: slugs, slash commands,
    CLI flags, `norouter`). Line structure kept. All-mechanism prompt → ''."""
    out = []
    for line in prompt.split('\n'):
        out.append(' '.join(t for t in line.split() if t and not _is_mechanism(t)))
    return '\n'.join(out).strip()


# ---------------------------------------------------------------------------
# location signals
# ---------------------------------------------------------------------------

def _normalize(path: str, repo_root: str) -> str:
    p = path.replace('\\', '/')
    root = repo_root.replace('\\', '/').rstrip('/')
    if p.lower().startswith(root.lower() + '/'):
        p = p[len(root) + 1:]
    while p.startswith('./'):
        p = p[2:]
    return p


def _is_signal_path(rel: str) -> bool:
    if not rel or rel.startswith('/') or re.match(r'^[A-Za-z]:', rel):
        return False
    return not rel.lower().startswith(EXCLUDED_PATH_PREFIXES)


def prompt_paths(text: str, repo_root: str) -> list[str]:
    """Repo-relative paths named in the prompt that exist on disk; `:line`
    suffixes dropped; `_projects/` and `tmp/` never count."""
    out: list[str] = []
    for m in PATH_RE.finditer(text):
        rel = _normalize(m.group(1), repo_root)
        if not _is_signal_path(rel) or rel in out:
            continue
        if os.path.isfile(os.path.join(repo_root, rel)):
            out.append(rel)
    return out


def ledger_tail(path: str, n: int = LEDGER_TAIL) -> list[str]:
    """Last n non-empty lines of a ledger file ('' lines and a torn tail dropped)."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            lines = [ln.strip() for ln in f.read().split('\n')]
    except OSError:
        return []
    lines = [ln for ln in lines if ln]
    return lines[-n:]


def turn_paths(prompt: str, repo_root: str, state_dir: str, session_id: str) -> list[str]:
    seen: list[str] = []
    for rel in prompt_paths(prompt, repo_root):
        if rel not in seen:
            seen.append(rel)
    for suffix in ('.reads', '.touched'):
        for rel in ledger_tail(os.path.join(state_dir, f'{session_id}{suffix}')):
            rel = _normalize(rel, repo_root)
            if _is_signal_path(rel) and rel not in seen:
                seen.append(rel)
    return seen


# ---------------------------------------------------------------------------
# index rows and anchors
# ---------------------------------------------------------------------------

def parse_index_rows(text: str) -> list[dict]:
    """Data rows of project-notes/index.md. Same split rule as
    check_progress.parse_notes_index_rows (unescaped `|` only, `\\|` restored)."""
    rows: list[dict] = []
    for line in text.splitlines():
        if not line.startswith('|'):
            continue
        cells = [c.strip().replace('\\|', '|') for c in re.split(r'(?<!\\)\|', line.strip('|'))]
        if not cells or not cells[0]:
            continue
        if cells[0].lower() == 'file' or set(cells[0]) <= {'-', ' '}:
            continue
        tags_cell = cells[2] if len(cells) > 2 else ''
        rows.append({
            'file': cells[0],
            'description': (cells[1] if len(cells) > 1 else '').lower(),
            'tags': [t.strip().lower() for t in tags_cell.split(',') if t.strip()],
            'updated': cells[3] if len(cells) > 3 else '',
            'raw': line.rstrip(),
        })
    return rows


def active_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if not r['file'].startswith(ARCHIVE_PREFIX)]


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in '"\'':
        s = s[1:-1]
    return s.strip()


def parse_anchors_block(block: str) -> list[str]:
    """`anchors:` entries from a frontmatter body: flow list `[a, b]` or block list."""
    lines = block.splitlines()
    out: list[str] = []
    for i, line in enumerate(lines):
        m = ANCHORS_KEY_RE.match(line)
        if not m:
            continue
        rest = m.group(1).strip()
        if rest.startswith('['):
            out = [_unquote(x) for x in rest.strip('[]').split(',')]
        else:
            for nxt in lines[i + 1:]:
                mm = ANCHORS_ITEM_RE.match(nxt)
                if not mm:
                    break
                out.append(_unquote(mm.group(1)))
        break
    return [a.replace('\\', '/') for a in out if a]


def read_anchors(note_path: str) -> list[str]:
    """Frontmatter `anchors:` of one note; [] when absent or unreadable. Only
    the file head is read — note bodies stay unread on the push path."""
    try:
        with open(note_path, 'r', encoding='utf-8', errors='replace') as f:
            head = f.read(8192)
    except OSError:
        return []
    m = FM_RE.match(head)
    if not m:
        return []
    return parse_anchors_block(m.group(1))


def _by_updated_desc(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: r['updated'], reverse=True)


def location_matches(rows: list[dict], notes_dir: str, paths: list[str]) -> tuple[list[dict], list[dict]]:
    """(L1, L2): rows whose anchor path equals a turn path / shares its leaf
    directory. Each list is Updated-desc, ties in index order."""
    turn = {p.lower() for p in paths}
    turn_dirs = {os.path.dirname(p).lower() for p in paths if os.path.dirname(p)}
    l1: list[dict] = []
    l2: list[dict] = []
    for row in rows:
        anchors = read_anchors(os.path.join(notes_dir, row['file']))
        apaths = {a.split('::', 1)[0].lower() for a in anchors}
        if not apaths:
            continue
        if apaths & turn:
            l1.append(row)
        elif any(os.path.dirname(p) in turn_dirs for p in apaths if os.path.dirname(p)):
            l2.append(row)
    return _by_updated_desc(l1), _by_updated_desc(l2)


# ---------------------------------------------------------------------------
# lexical layer (Pi router-deterministic.ts ranker with the §4.1.1 changes:
# tags as text by substring, simple IDF cut, CJK runs first with bigram fallback)
# ---------------------------------------------------------------------------

def _boundary_re(token: str) -> re.Pattern:
    return re.compile(r'(?<![a-z0-9])' + re.escape(token) + r'(?![a-z0-9])')


def _hiragana_only(s: str) -> bool:
    return bool(s) and HIRAGANA_RE.fullmatch(s) is not None


def subject_tokens(subject: str) -> list[tuple[str, str]]:
    """Distinct (kind, text) tokens: ('ascii', word), ('run', cjk run)."""
    lower = subject.lower()
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for m in ASCII_TOKEN_RE.finditer(lower):
        if m.group(0) not in seen:
            seen.add(m.group(0))
            out.append(('ascii', m.group(0)))
    for run in CJK_RUN_RE.findall(lower):
        if len(run) >= 2 and run not in seen and not _hiragana_only(run):
            seen.add(run)
            out.append(('run', run))
    return out


def _token_weight(kind: str, text: str, row: dict, boundary: re.Pattern | None) -> int:
    desc = row['description']
    if kind == 'ascii':
        if len(text) < 3 or text in STOPWORDS:
            return 0
        return 2 if boundary is not None and boundary.search(desc) else 0
    if text in desc:
        return 2
    hits = 0
    for i in range(len(text) - 1):
        bigram = text[i:i + 2]
        if not _hiragana_only(bigram) and bigram in desc:
            hits += 1
    return hits


def rank_lexical(rows: list[dict], subject: str) -> list[dict]:
    """Rows with score > 0, score desc → Updated desc → index order.

    The IDF cut is per TOKEN: a CJK run and its bigram fallback form one
    weight column and are cut together, never bigram by bigram. The cutoff
    is IDF_CUTOFF * len(rows), so on a corpus of 3 or fewer active rows any
    single hit already reaches it and this layer returns nothing; the
    location and qmd layers are unaffected."""
    if not rows:
        return []
    tokens = subject_tokens(subject)
    lower = subject.lower()
    cutoff = IDF_CUTOFF * len(rows)
    boundaries = {t: _boundary_re(t) for k, t in tokens if k == 'ascii'}

    token_weights: list[list[int]] = []
    for kind, text in tokens:
        col = [_token_weight(kind, text, r, boundaries.get(text)) for r in rows]
        if sum(1 for w in col if w) >= cutoff:
            col = [0] * len(rows)
        token_weights.append(col)

    tag_df: dict[str, int] = {}
    for r in rows:
        for t in set(r['tags']):
            tag_df[t] = tag_df.get(t, 0) + 1

    scored: list[tuple[int, dict]] = []
    for i, row in enumerate(rows):
        score = sum(col[i] for col in token_weights)
        for t in set(row['tags']):
            if len(t) >= 2 and t in lower and tag_df[t] < cutoff:
                score += 2
        if score > 0:
            scored.append((score, row))
    ordered = _by_updated_desc([r for _, r in scored])
    score_of = {id(r): s for s, r in scored}
    return sorted(ordered, key=lambda r: -score_of[id(r)])


# ---------------------------------------------------------------------------
# topic layer via qmd
# ---------------------------------------------------------------------------

def resolve_qmd_bin(env: dict | None = None) -> str | None:
    env = os.environ if env is None else env
    explicit = env.get(BIN_ENV, '').strip()
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    which = shutil.which('qmd', path=env.get('PATH'))
    if not which:
        return None
    cand = os.path.join(os.path.dirname(which), QMD_REL_BIN)
    return cand if os.path.isfile(cand) else None


def resolve_qmd_timeout(env: dict | None = None) -> float:
    """Seconds allowed for one qmd query: TASKFLOW_QMD_TIMEOUT when it parses
    as a positive number, else QMD_TIMEOUT_S."""
    env = os.environ if env is None else env
    raw = env.get(TIMEOUT_ENV, '').strip()
    try:
        value = float(raw)
    except ValueError:
        return QMD_TIMEOUT_S
    return value if value > 0 else QMD_TIMEOUT_S


def sanitize_vec(text: str) -> str:
    """One line, no `"`, and no `-` right after whitespace: qmd rejects the
    latter in a vec: line as an unsupported negation (exit 1)."""
    s = text.replace('\r', ' ').replace('\n', ' ').replace('"', '')
    s = VEC_NEGATION_RE.sub(r'\1', s)
    return ' '.join(s.split())


def lex_terms(subject: str) -> list[str]:
    """One lex: line per term: ASCII words (>= 3 chars, hyphen/underscore kept,
    stopwords out) and CJK fragments of >= 2 chars after splitting runs on
    hiragana. qmd ANDs the terms inside one lex: line, so terms are emitted
    one per line and combine by RRF instead."""
    out: list[str] = []
    for w in LEX_WORD_RE.findall(subject):
        t = w.lower().strip('-_')
        if len(t) >= 3 and t not in STOPWORDS and t not in out:
            out.append(t)
    for run in CJK_RUN_RE.findall(subject):
        for frag in HIRAGANA_RE.split(run):
            if len(frag) >= 2 and frag not in out:
                out.append(frag)
    return [t.replace('"', '') for t in out][:LEX_TERMS_MAX]


def build_query_doc(subject: str) -> str:
    lines = ['vec: ' + sanitize_vec(subject[:VEC_MAX_CHARS])]
    lines += ['lex: ' + t for t in lex_terms(subject)]
    return '\n'.join(lines)


def _extract_json_array(stdout: str) -> list:
    start = stdout.find('[')
    end = stdout.rfind(']')
    if start < 0 or end < start:
        raise QmdError('no JSON array in qmd output')
    data = json.loads(stdout[start:end + 1])
    if not isinstance(data, list):
        raise QmdError('qmd output is not a list')
    return data


def run_qmd(bin_path: str, notes_dir: str, doc: str, n: int,
            timeout: float = QMD_TIMEOUT_S, runner=subprocess.run) -> list[dict]:
    """`node <bin/qmd> query <doc> -n <n> --no-rerank --explain --json` in
    notes_dir. Raises QmdError / OSError / subprocess.TimeoutExpired."""
    cmd = ['node', bin_path, 'query', doc, '-n', str(n), '--no-rerank', '--explain', '--json']
    cp = runner(cmd, cwd=notes_dir, capture_output=True, timeout=timeout,
                encoding='utf-8', errors='replace')
    if cp.returncode != 0:
        raise QmdError(f'qmd exit {cp.returncode}')
    return [r for r in _extract_json_array(cp.stdout) if isinstance(r, dict)]


def _result_file(result: dict) -> str:
    f = str(result.get('file', ''))
    if f.startswith(QMD_URI_PREFIX):
        f = f[len(QMD_URI_PREFIX):]
        f = f.split('/', 1)[1] if '/' in f else ''
    return f.replace('\\', '/')


def _result_is_hit(result: dict) -> bool:
    """With --explain, a hit is a non-empty BM25 score set or a vector score
    >= VECTOR_TAU; without those keys every returned file counts."""
    fts = [v for k, v in result.items() if 'fts' in k.lower()]
    vec = [v for k, v in result.items() if 'vector' in k.lower()]
    if not fts and not vec:
        return True
    if any(v for v in fts):
        return True
    for v in vec:
        vals = v.values() if isinstance(v, dict) else (v if isinstance(v, list) else [v])
        try:
            if any(float(x) >= VECTOR_TAU for x in vals):
                return True
        except (TypeError, ValueError):
            continue
    return False


def qmd_rank(results: list[dict]) -> tuple[list[str], set[str]]:
    """(ordered distinct files, hit files) from qmd chunk results."""
    order: list[str] = []
    hits: set[str] = set()
    for r in results:
        f = _result_file(r)
        if not f:
            continue
        if f not in order:
            order.append(f)
        if _result_is_hit(r):
            hits.add(f)
    return order, hits


# ---------------------------------------------------------------------------
# composition
# ---------------------------------------------------------------------------

def rrf_merge(list_a: list[str], list_b: list[str], updated_of: dict[str, str]) -> list[str]:
    """Outer RRF with equal weights, 1/(RRF_K + rank); ties Updated desc, then
    first-seen order. An empty list contributes nothing."""
    scores: dict[str, float] = {}
    for lst in (list_a, list_b):
        for rank, f in enumerate(lst):
            scores[f] = scores.get(f, 0.0) + 1.0 / (RRF_K + rank + 1)
    files = list(scores)
    files.sort(key=lambda f: updated_of.get(f, ''), reverse=True)
    files.sort(key=lambda f: -scores[f])
    return files


def compose(rows: list[dict], l1: list[dict], l2: list[dict], topic: list[str],
            hits: set[str], k: int = K_ROWS) -> tuple[list[dict], int]:
    """(shown rows, M). Order L1 → L2 (≤ L2_CAP) → topic; a row appears once."""
    by_file = {r['file']: r for r in rows}
    shown: list[dict] = []
    seen: set[str] = set()

    def add(row: dict) -> None:
        if row['file'] not in seen and len(shown) < k:
            seen.add(row['file'])
            shown.append(row)

    for row in l1:
        add(row)
    for row in l2[:L2_CAP]:
        add(row)
    for f in topic:
        if f in by_file:
            add(by_file[f])
    matched = {r['file'] for r in l1} | {r['file'] for r in l2} | (hits & set(by_file))
    return shown, len(matched)


def render(shown: list[dict], matched: int, total: int) -> str:
    lines = [HEADER] + [r['raw'] for r in shown]
    lines.append(f'project-notes matched: {matched} of {total} rows ({len(shown)} shown)')
    return '\n'.join(lines)


def build_notes_push(project_dir: str, repo_root: str, prompt: str, session_id: str,
                     state_dir: str, env: dict | None = None,
                     runner=subprocess.run) -> tuple[str, str | None]:
    """(block text, degrade reason). '' block when the project has no
    project-notes/index.md. The reason is non-None only when qmd was expected
    (mode auto and `.qmd/` present) but did not deliver; the caller decides
    whether to surface it (once per session)."""
    env = os.environ if env is None else env
    mode = env.get(MODE_ENV, 'auto').strip().lower()
    if mode not in MODES:
        mode = 'auto'

    notes_dir = os.path.join(project_dir, 'project-notes')
    try:
        with open(os.path.join(notes_dir, 'index.md'), 'r', encoding='utf-8', errors='replace') as f:
            rows = active_rows(parse_index_rows(f.read()))
    except OSError:
        return '', None

    paths = turn_paths(prompt, repo_root, state_dir, session_id)
    l1, l2 = location_matches(rows, notes_dir, paths)

    subject = derive_subject_text(prompt)
    lexical: list[dict] = []
    if mode != 'off' and subject:
        lexical = rank_lexical(rows, subject)
    lexical_files = [r['file'] for r in lexical]

    qmd_files: list[str] = []
    qmd_hits: set[str] = set()
    reason: str | None = None
    if mode == 'auto' and subject and os.path.isdir(os.path.join(notes_dir, QMD_DIR)):
        bin_path = resolve_qmd_bin(env)
        if not bin_path:
            reason = f'qmd binary not found (set {BIN_ENV})'
        else:
            timeout = resolve_qmd_timeout(env)
            try:
                results = run_qmd(bin_path, notes_dir, build_query_doc(subject),
                                  K_ROWS + QMD_MARGIN, timeout=timeout, runner=runner)
                qmd_files, qmd_hits = qmd_rank(results)
            except subprocess.TimeoutExpired:
                reason = f'qmd timeout after {timeout:g}s'
            except (QmdError, OSError, ValueError) as e:
                reason = str(e) or e.__class__.__name__

    updated_of = {r['file']: r['updated'] for r in rows}
    topic = rrf_merge(qmd_files, lexical_files, updated_of)
    shown, matched = compose(rows, l1, l2, topic, qmd_hits | set(lexical_files))
    return render(shown, matched, len(rows)), reason
