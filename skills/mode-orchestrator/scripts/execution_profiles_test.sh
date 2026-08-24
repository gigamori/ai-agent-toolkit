#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
file="$root/references/execution-profiles.md"
expected_profile=$(cat <<'EOF'
# Execution profiles

| effort | use | CC | Pi |
|---|---|---|---|
| low | mechanical, unambiguous | haiku | gpt-5.6-luna |
| middle | normal; default if unsure | sonnet | gpt-5.6-terra |
| high | design, diagnosis, critical review, replanning | opus | gpt-5.6-sol |

No cross-effort fallback.
EOF
)

fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

validate_profile() {
  local profile=$1 line lower index
  local -a lines=() table_rows=()

  [[ -f "$profile" ]] || fail "profile is missing"
  mapfile -t lines < "$profile"
  for index in "${!lines[@]}"; do
    lines[$index]=${lines[$index]%$'\r'}
  done

  for line in "${lines[@]}"; do
    lower=${line,,}
    if [[ "$lower" == *provider* || "$lower" == *vendor* || "$lower" == *thinking* || "$lower" == *candidate* ]]; then
      fail "profile contains an unsupported field"
    fi
    if [[ "$lower" == *fallback* && "$line" != 'No cross-effort fallback.' ]]; then
      fail "profile contains a contradictory fallback rule"
    fi
    if [[ "$line" == \|* ]]; then
      table_rows+=("$line")
    fi
  done

  (( ${#table_rows[@]} == 5 )) || fail "table must contain a header, divider, and exactly three effort rows"
  [[ "${lines[*]}" == *'No cross-effort fallback.'* ]] || fail "profile must contain the required no-fallback rule"
  (( ${#lines[@]} == 9 )) || fail "profile must have exactly nine lines"
  [[ "${lines[0]}" == '# Execution profiles' ]] || fail "profile title is invalid"
  [[ -z "${lines[1]}" && -z "${lines[7]}" ]] || fail "profile spacing is invalid"
  [[ "${lines[2]}" == '| effort | use | CC | Pi |' ]] || fail "header must be exactly effort, use, CC, Pi"
  [[ "${lines[3]}" == '|---|---|---|---|' ]] || fail "table divider is invalid"
  [[ "${lines[4]}" == '| low | mechanical, unambiguous | haiku | gpt-5.6-luna |' ]] || fail "low row must match the required mapping"
  [[ "${lines[5]}" == '| middle | normal; default if unsure | sonnet | gpt-5.6-terra |' ]] || fail "middle row must match the required mapping"
  [[ "${lines[6]}" == '| high | design, diagnosis, critical review, replanning | opus | gpt-5.6-sol |' ]] || fail "high row must match the required mapping"
  [[ "${lines[8]}" == 'No cross-effort fallback.' ]] || fail "profile must end with the required no-fallback rule"
  actual=$(tr -d '\r' < "$profile")
  [[ "$actual" == "$expected_profile" ]] || fail "profile content differs from the full required contract"
}

expect_rejection() {
  local label=$1 expected_message=$2 candidate=$3 output

  if output=$(validate_profile "$candidate" 2>&1); then
    printf 'FAIL mutation control accepted: %s\n' "$label" >&2
    exit 1
  fi
  if [[ "$output" != "FAIL $expected_message" ]]; then
    printf 'FAIL mutation control hit the wrong predicate: %s: %s\n' "$label" "$output" >&2
    exit 1
  fi
  printf 'passed mutation control: %s\n' "$label"
}

validate_profile "$file"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

candidate="$tmp/header-reorder.md"
sed 's/| effort | use | CC | Pi |/| effort | use | Pi | CC |/' "$file" > "$candidate"
expect_rejection "header reorder" "header must be exactly effort, use, CC, Pi" "$candidate"

candidate="$tmp/cc-pi-swap.md"
sed \
  -e 's/| low | mechanical, unambiguous | haiku | gpt-5.6-luna |/| low | mechanical, unambiguous | gpt-5.6-luna | haiku |/' \
  -e 's/| middle | normal; default if unsure | sonnet | gpt-5.6-terra |/| middle | normal; default if unsure | gpt-5.6-terra | sonnet |/' \
  -e 's/| high | design, diagnosis, critical review, replanning | opus | gpt-5.6-sol |/| high | design, diagnosis, critical review, replanning | gpt-5.6-sol | opus |/' \
  "$file" > "$candidate"
expect_rejection "CC/Pi cell swap" "low row must match the required mapping" "$candidate"

candidate="$tmp/missing-effort-row.md"
sed '/^| middle |/d' "$file" > "$candidate"
expect_rejection "missing effort row" "table must contain a header, divider, and exactly three effort rows" "$candidate"

candidate="$tmp/extra-effort-row.md"
awk '{ print } /^\| high \|/ { print "| extra | unsupported | unsupported | unsupported |" }' "$file" > "$candidate"
expect_rejection "extra effort row" "table must contain a header, divider, and exactly three effort rows" "$candidate"

candidate="$tmp/extra-column.md"
awk 'NR >= 3 && NR <= 7 { sub(/\r$/, ""); sub(/\|$/, "| extra |") } { print }' "$file" > "$candidate"
expect_rejection "extra column" "header must be exactly effort, use, CC, Pi" "$candidate"

candidate="$tmp/wrong-mapping.md"
sed 's/| low | mechanical, unambiguous | haiku | gpt-5.6-luna |/| low | mechanical, unambiguous | sonnet | gpt-5.6-luna |/' "$file" > "$candidate"
expect_rejection "wrong mapping" "low row must match the required mapping" "$candidate"

candidate="$tmp/candidate-list.md"
sed 's/| low | mechanical, unambiguous | haiku | gpt-5.6-luna |/| low | mechanical, unambiguous | haiku, sonnet | gpt-5.6-luna |/' "$file" > "$candidate"
expect_rejection "candidate list" "low row must match the required mapping" "$candidate"

candidate="$tmp/provider-field.md"
sed 's/| effort | use | CC | Pi |/| effort | use | CC | Pi | provider |/' "$file" > "$candidate"
expect_rejection "provider field" "profile contains an unsupported field" "$candidate"

candidate="$tmp/thinking-field.md"
sed 's/| effort | use | CC | Pi |/| effort | use | CC | Pi | thinking |/' "$file" > "$candidate"
expect_rejection "thinking field" "profile contains an unsupported field" "$candidate"

candidate="$tmp/missing-no-fallback.md"
sed '$d' "$file" > "$candidate"
expect_rejection "missing no-fallback rule" "profile must contain the required no-fallback rule" "$candidate"

candidate="$tmp/contradictory-fallback.md"
printf '%s\nCross-effort fallback is allowed.\n' "$(<"$file")" > "$candidate"
expect_rejection "contradictory fallback text" "profile contains a contradictory fallback rule" "$candidate"

printf '%s\n' "passed execution profile gate"
