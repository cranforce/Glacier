#!/usr/bin/env bash
# Sync Salesforce objects into Snowflake.
#
# Usage:
#   ./run.sh [sync] [Object...]    Load changes since the last run (SystemModstamp
#                                  delta, merged on Id), including deletes;
#                                  full load if the table is new
#   ./run.sh full [Object...]      Full reload (CREATE OR REPLACE TABLE)
#   ./run.sh reconcile [Object...] Catch deletes the sync can't see by comparing
#                                  every Id against Salesforce
#   ./run.sh schema [Object...]    Refresh schema/ from Salesforce (sf sobject describe)
#
# With no objects, runs every object in objects.conf. Settings are in project.env.

set -euo pipefail
cd "$(dirname "$0")"
source ./project.env

usage() { awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"; exit 1; }

cmd=sync
case "${1:-}" in
  sync|full|reconcile|schema) cmd="$1"; shift ;;
  -h|--help|help) usage ;;
esac

configured() { awk '!/^[[:space:]]*(#|$)/ { print $1 }' objects.conf; }
# Prints "<table> <delete mode> <history 0|1>" for an object, or nothing if it
# isn't configured. After the table, a line can have a delete mode and "history".
config_for() {
  awk -v o="$1" -v d="$DELETE_MODE" '!/^[[:space:]]*(#|$)/ && $1 == o {
    mode = d; history = 0
    for (i = 3; i <= NF; i++) if ($i == "history") history = 1; else mode = $i
    print $2, mode, history; exit
  }' objects.conf
}

if (( $# )); then objects=("$@"); else objects=($(configured)); fi
(( ${#objects[@]} )) || { echo "No objects configured in objects.conf." >&2; exit 1; }

if [[ "$cmd" == schema ]]; then
  exec bin/sf_schema.sh -o "$SF_ORG" "${objects[@]}"
fi

mkdir -p state
failed=()
for obj in "${objects[@]}"; do
  read -r table delete_mode history <<< "$(config_for "$obj")" || true
  if [[ -z "${table:-}" ]]; then
    echo "!! $obj is not in objects.conf; skipping." >&2
    failed+=("$obj")
    continue
  fi

  echo
  echo "######## $obj -> $table ($cmd, deletes: $delete_mode$( (( history )) && echo ", history: ${table}__HISTORY"))"

  # Capture the schema the first time an object is synced
  if [[ ! -e "schema/$obj.types.tsv" || ! -e "queries/$obj.soql" ]]; then
    bin/sf_schema.sh -o "$SF_ORG" "$obj" || { failed+=("$obj"); continue; }
  fi

  load=(bin/sf_to_snowflake.sh -o "$SF_ORG" -t "$table" -f "queries/$obj.soql"
    -s "schema/$obj.types.tsv" -D "$delete_mode")
  (( history )) && load+=(-H)
  state_file="state/$obj.last_sync"
  started="$(date +%s)"
  last_sync="$(cat "$state_file" 2>/dev/null || echo 0)"

  case "$cmd" in
    reconcile)
      if [[ "$delete_mode" == off ]]; then
        echo "    Deletes are off for $obj; nothing to reconcile."
        continue
      fi
      "${load[@]}" -R || failed+=("$obj")
      ;;
    full)
      if "${load[@]}" -r; then echo "$started" > "$state_file"; else failed+=("$obj"); fi
      ;;
    sync)
      "${load[@]}" -d || { failed+=("$obj"); continue; }
      if [[ "$delete_mode" != off ]] && (( started - last_sync > RECONCILE_AFTER_DAYS * 86400 )); then
        if (( last_sync == 0 )); then
          echo "    No previous sync recorded for $obj; reconciling deletes."
        else
          echo "    Last sync was $(( (started - last_sync) / 86400 )) days ago; reconciling deletes the recycle bin may have dropped."
        fi
        "${load[@]}" -R || { failed+=("$obj"); continue; }
      fi
      echo "$started" > "$state_file"
      ;;
  esac
done

echo
if (( ${#failed[@]} )); then
  echo "Failed: ${failed[*]}" >&2
  exit 1
fi
echo "All objects done."
