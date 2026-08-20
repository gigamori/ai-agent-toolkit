# capture_paths.sh — shared helper for the capture-sidecar E2E suites.
# Source AFTER defining $STATE and $SID (the function reads them at call time):
#   . "$(dirname "$0")/capture_paths.sh"
#
# rcap N -> the per-round sidecar path `{sid}.r{N}.capture` (R-1 D1). This is
# the name the hook hands out and the only delivery path production takes:
# `capture_sidecar_path()` builds it, `build_capture_context` puts it in the
# subagent's `sidecar_path`, and the agent contract forbids constructing any
# other. Write new fixtures per-round, never to the legacy name (F-2).
#
# The un-suffixed `{sid}.capture` is the pre-R-1 legacy name. As of the
# retirement of that compat branch, the hook no longer reads it at all — the
# apply path only ever scans `{sid}.r{N}.capture` (`scan_round_sidecars`) — and
# no test arm writes it either: `test_capture_late_sidecar.sh` T-R1-4 (the
# former compat-apply arm) was deleted, and `test_e2e_capture_bind.sh` Stage 5
# (F7a membership containment) was re-pointed to the per-round name via this
# helper. Verified 2026-08-20 by grep over tests/ after the removal.
rcap() { echo "$STATE/$SID.r$1.capture"; }
