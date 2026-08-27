#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
file="$root/references/execution-profiles.md"

fail() {
  printf 'FAIL %s\n' "$1" >&2
  exit 1
}

check_bare_token() {
  local cell=$1
  [[ -n "$cell" ]] || fail "each effort row's CC and Pi cells must be a single bare token"
  [[ "$cell" =~ [[:space:],] ]] && fail "each effort row's CC and Pi cells must be a single bare token"
  return 0
}

validate_profile() {
  local profile=$1 line lower index
  local -a lines=() table_rows=()
  local fallback_index=-1

  [[ -f "$profile" ]] || fail "profile is missing"
  mapfile -t lines < "$profile"
  for index in "${!lines[@]}"; do
    lines[$index]=${lines[$index]%$'\r'}
  done

  for index in "${!lines[@]}"; do
    line="${lines[$index]}"
    lower=${line,,}
    if [[ "$lower" == *provider* || "$lower" == *vendor* || "$lower" == *thinking* || "$lower" == *candidate* ]]; then
      fail "profile contains an unsupported field"
    fi
    if [[ "$lower" == *fallback* && "$line" != 'No cross-effort fallback.' ]]; then
      fail "profile contains a contradictory fallback rule"
    fi
    if [[ "$line" == 'No cross-effort fallback.' ]]; then
      fallback_index=$index
    fi
    if [[ "$line" == \|* ]]; then
      table_rows+=("$line")
    fi
  done

  (( ${#table_rows[@]} == 5 )) || fail "table must contain a header, divider, and exactly three effort rows"
  [[ "${lines[0]:-}" == '# Execution profiles' ]] || fail "profile title is invalid"
  [[ -z "${lines[1]:-}" && -z "${lines[7]:-}" ]] || fail "profile spacing is invalid"
  [[ "${lines[2]:-}" == '| effort | use | CC | Pi |' ]] || fail "header must be exactly effort, use, CC, Pi"
  [[ "${lines[3]:-}" == '|---|---|---|---|' ]] || fail "table divider is invalid"
  [[ "${lines[4]:-}" == '| basic |'* ]] || fail "effort rows must appear in order basic, pro, ultra"
  [[ "${lines[5]:-}" == '| pro |'* ]] || fail "effort rows must appear in order basic, pro, ultra"
  [[ "${lines[6]:-}" == '| ultra |'* ]] || fail "effort rows must appear in order basic, pro, ultra"

  local idx nf cc pi
  for idx in 4 5 6; do
    nf=$(awk -F'|' '{print NF}' <<< "${lines[$idx]}")
    (( nf == 6 )) || fail "each effort row must have exactly four columns"
    cc=$(awk -F'|' '{gsub(/^[ \t]+|[ \t]+$/, "", $4); print $4}' <<< "${lines[$idx]}")
    pi=$(awk -F'|' '{gsub(/^[ \t]+|[ \t]+$/, "", $5); print $5}' <<< "${lines[$idx]}")
    check_bare_token "$cc"
    check_bare_token "$pi"
  done

  (( fallback_index >= 0 )) || fail "profile must contain the required no-fallback rule"
  (( fallback_index == ${#lines[@]} - 1 )) || fail "no-fallback rule must be the final line"
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

candidate="$tmp/title-invalid.md"
sed '1s/.*/# execution profiles/' "$file" > "$candidate"
expect_rejection "title" "profile title is invalid" "$candidate"

candidate="$tmp/blank-spacing-after-title.md"
sed '2s/^$/NOTE/' "$file" > "$candidate"
expect_rejection "blank spacing after title" "profile spacing is invalid" "$candidate"

candidate="$tmp/blank-spacing-before-fallback.md"
sed '8s/^$/NOTE/' "$file" > "$candidate"
expect_rejection "blank spacing before fallback" "profile spacing is invalid" "$candidate"

candidate="$tmp/header-reorder.md"
sed 's/| effort | use | CC | Pi |/| effort | use | Pi | CC |/' "$file" > "$candidate"
expect_rejection "header reorder" "header must be exactly effort, use, CC, Pi" "$candidate"

candidate="$tmp/divider-invalid.md"
sed '4s/.*/|----|---|---|---|/' "$file" > "$candidate"
expect_rejection "divider" "table divider is invalid" "$candidate"

candidate="$tmp/missing-effort-row.md"
sed '/^| pro |/d' "$file" > "$candidate"
expect_rejection "missing effort row" "table must contain a header, divider, and exactly three effort rows" "$candidate"

candidate="$tmp/extra-effort-row.md"
awk '{ print } /^\| ultra \|/ { print "| extra | unsupported | unsupported | unsupported |" }' "$file" > "$candidate"
expect_rejection "extra effort row" "table must contain a header, divider, and exactly three effort rows" "$candidate"

candidate="$tmp/row-order.md"
awk '{lines[NR]=$0} END{tmp=lines[6]; lines[6]=lines[7]; lines[7]=tmp; for(i=1;i<=NR;i++) print lines[i]}' "$file" > "$candidate"
expect_rejection "row order" "effort rows must appear in order basic, pro, ultra" "$candidate"

candidate="$tmp/extra-column.md"
sed '7s/|$/| extra |/' "$file" > "$candidate"
expect_rejection "extra column" "each effort row must have exactly four columns" "$candidate"

candidate="$tmp/candidate-list.md"
sed '5s/haiku/haiku, sonnet/' "$file" > "$candidate"
expect_rejection "candidate list" "each effort row's CC and Pi cells must be a single bare token" "$candidate"

candidate="$tmp/whitespace-token.md"
sed '6s/gpt-5.6-terra/gpt 5.6 terra/' "$file" > "$candidate"
expect_rejection "whitespace token" "each effort row's CC and Pi cells must be a single bare token" "$candidate"

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

candidate="$tmp/fallback-not-terminal.md"
printf '%s\nExtra trailing line.\n' "$(<"$file")" > "$candidate"
expect_rejection "fallback not terminal" "no-fallback rule must be the final line" "$candidate"

printf '%s\n' "passed execution profile gate"
