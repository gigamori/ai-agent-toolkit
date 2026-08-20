# capture_paths.sh — shared helper for the capture-sidecar E2E suites.
# Source AFTER defining $STATE and $SID (the function reads them at call time):
#   . "$(dirname "$0")/capture_paths.sh"
#
# rcap N -> the per-round sidecar path `{sid}.r{N}.capture` (R-1 D1). This is
# the name the hook hands out and the only delivery path production takes;
# the un-suffixed `{sid}.capture` is the pre-R-1 compat branch and is pinned
# by exactly ONE arm (test_capture_late_sidecar.sh T-R1-4) — write new
# fixtures per-round, never to the legacy name (F-2).
rcap() { echo "$STATE/$SID.r$1.capture"; }
