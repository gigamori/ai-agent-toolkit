#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
file="$root/references/execution-profiles.md"
expected='| low | mechanical, unambiguous | haiku | gpt-5.6-luna |
| middle | normal; default if unsure | sonnet | gpt-5.6-terra |
| high | design, diagnosis, critical review, replanning | opus | gpt-5.6-sol |'
actual=$(awk '/^\| (low|middle|high) \|/ { print }' "$file")

if [[ "$actual" != "$expected" ]]; then
  printf '%s\n' "FAIL profile table differs from the required three mappings"
  exit 1
fi
if grep -Eiq 'provider|vendor|thinking|candidate' "$file"; then
  printf '%s\n' "FAIL profile contains unsupported fields"
  exit 1
fi
if ! grep -Fxq 'No cross-effort fallback.' "$file"; then
  printf '%s\n' "FAIL profile does not forbid cross-effort fallback"
  exit 1
fi
printf '%s\n' "passed execution profile table"
