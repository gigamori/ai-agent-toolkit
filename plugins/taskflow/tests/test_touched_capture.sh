#!/usr/bin/env bash
# test_touched_capture.sh — unit tests for the PostToolUse capture hook
# plugins/taskflow/hooks/touched_capture.py (exec-binding.md §3.1; the capture
# half of AC-1). Covers path extraction (Write/Edit/NotebookEdit, Bash
# mv|cp|rm + redirection + tee), normalization, and the orphan guard.
#
# Invokes the hook fresh from disk (subprocess), so no live `claude` is needed.
#
# Usage:  bash plugins/taskflow/tests/test_touched_capture.sh
# Requires: bash (Git-Bash on win32 — primary), uv.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$REPO_ROOT"
HOOK="$REPO_ROOT/plugins/taskflow/hooks/touched_capture.py"
STATE_DIR="$REPO_ROOT/_projects/_state"
SID="touchcap$$-0000-0000-0000-000000000000"
SID2="touchcap2$$-0000-0000-0000-000000000000"
STATE_FILE="$STATE_DIR/$SID.json"
TOUCHED_FILE="$STATE_DIR/$SID.touched"

to_win() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else echo "$1"; fi; }

PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }
cleanup() {
  rm -f "$STATE_FILE" "$TOUCHED_FILE" "$STATE_DIR/$SID2.touched"
  echo ""
  if [ "$FAIL" -eq 0 ]; then echo "All $PASS tests passed."; else echo "$FAIL failed, $PASS passed."; fi
}
trap cleanup EXIT
mkdir -p "$STATE_DIR"

echo "=== Test: touched_capture.py (PostToolUse .touched capture) ==="
echo ""

# --- Unit: path extraction -------------------------------------------------
echo "[extract] tool_input → write paths"
EXTRACT=$(uv run python - "$(to_win "$HOOK")" << 'PY'
import importlib.util, sys, os
hp = sys.argv[1]; sys.path.insert(0, os.path.dirname(hp))
spec = importlib.util.spec_from_file_location("tc", hp)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bp = m.extract_bash_paths
checks = [
    ("write",     m.extract_paths({"file_path": "/r/a.md"}) == ["/r/a.md"]),
    ("notebook",  m.extract_paths({"notebook_path": "/r/n.ipynb"}) == ["/r/n.ipynb"]),
    ("mv",        set(bp("mv a.md b.md")) == {"a.md", "b.md"}),
    ("cp",        "x.md" in bp("cp x.md y.md")),
    ("rm",        bp("rm z.md") == ["z.md"]),
    ("redirect",  "out.txt" in bp("echo hi > out.txt")),
    ("redirect2", "log.md" in bp("echo hi >> log.md")),
    ("fd_redir",  "err.log" in bp("cmd 2> err.log")),
    ("tee",       "t.md" in bp("echo x | tee t.md")),
    ("tee_a",     "t2.md" in bp("echo x | tee -a t2.md")),
    ("skip_dup",  bp("cmd >&2") == []),
    ("chain",     "d.md" in bp("echo x && mv c.md d.md")),
    ("flag_skip", "-r" not in bp("rm -r dir.md")),
]
for name, ok in checks:
    print(f"{name}={ok}")
PY
)
echo "$EXTRACT" | sed 's/^/    /'
for line in $EXTRACT; do
  line="${line%$'\r'}"   # strip CR (python \n→\r\n on win32 stdout)
  val="${line##*=}"
  [ "$val" = "True" ] && pass "extract ${line%%=*}" || fail "extract $line"
done

# --- End-to-end: payload → normalized .touched line ------------------------
echo ""
echo "[main] Write payload appends normalized repo-relative path"
cat > "$STATE_FILE" << EOF
{"session_id":"$SID","project":"_x"}
EOF
rm -f "$TOUCHED_FILE"
WP=$(to_win "$REPO_ROOT/_projects/_x/tasks/0_todo/f.md")
printf '{"session_id":"%s","tool_input":{"file_path":"%s"}}' "$SID" "$WP" \
  | uv run python "$(to_win "$HOOK")"
if [ -f "$TOUCHED_FILE" ] && grep -q "^_projects/_x/tasks/0_todo/f.md$" "$TOUCHED_FILE"; then
  pass "main appends normalized repo-relative path to .touched"
else
  fail "main did not append expected line: $(cat "$TOUCHED_FILE" 2>/dev/null)"
fi

echo ""
echo "[main] Bash redirect payload appends target"
printf '{"session_id":"%s","tool_input":{"command":"echo done >> notes.txt"}}' "$SID" \
  | uv run python "$(to_win "$HOOK")"
grep -q "^notes.txt$" "$TOUCHED_FILE" \
  && pass "bash redirect target captured" || fail "bash redirect not captured"

# --- Orphan guard: no state file → no .touched -----------------------------
echo ""
echo "[guard] no state file → no orphan .touched"
printf '{"session_id":"%s","tool_input":{"file_path":"/r/x.md"}}' "$SID2" \
  | uv run python "$(to_win "$HOOK")"
[ ! -f "$STATE_DIR/$SID2.touched" ] \
  && pass "session without state writes no .touched" \
  || fail "orphan .touched written for stateless session"

echo ""
echo "=== Done ==="
