#!/usr/bin/env bash
# Sync Salesforce objects into Snowflake.
#
# Usage:
#   ./run.sh [sync] [Object...]   Load changes since the last run (SystemModstamp
#                                 delta, merged on Id); full load if the table is new
#   ./run.sh full [Object...]     Full reload (CREATE OR REPLACE TABLE)
#   ./run.sh schema [Object...]   Refresh schema/ from Salesforce (sf sobject describe)
#
# With no objects, runs every object in objects.conf. Settings are in project.env.

set -euo pipefail
cd "$(dirname "$0")"
source ./project.env

usage() { awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"; exit 1; }

cmd=sync
case "${1:-}" in
  sync|full|schema) cmd="$1"; shift ;;
  -h|--help|help) usage ;;
esac

configured() { awk '!/^[[:space:]]*(#|$)/ { print $1 }' objects.conf; }
table_for() { awk -v o="$1" '!/^[[:space:]]*(#|$)/ && $1 == o { print $2; exit }' objects.conf; }

if (( $# )); then objects=("$@"); else objects=($(configured)); fi
(( ${#objects[@]} )) || { echo "No objects configured in objects.conf." >&2; exit 1; }

if [[ "$cmd" == schema ]]; then
  exec bin/sf_schema.sh -o "$SF_ORG" "${objects[@]}"
fi

failed=()
for obj in "${objects[@]}"; do
  table="$(table_for "$obj")"
  if [[ -z "$table" ]]; then
    echo "!! $obj is not in objects.conf; skipping." >&2
    failed+=("$obj")
    continue
  fi

  echo
  echo "######## $obj -> $table ($cmd)"

  # Capture the schema the first time an object is synced
  if [[ ! -e "schema/$obj.types.tsv" || ! -e "queries/$obj.soql" ]]; then
    bin/sf_schema.sh -o "$SF_ORG" "$obj" || { failed+=("$obj"); continue; }
  fi

  mode=-d
  [[ "$cmd" == full ]] && mode=-r
  bin/sf_to_snowflake.sh -o "$SF_ORG" -t "$table" -f "queries/$obj.soql" -s "schema/$obj.types.tsv" "$mode" \
    || failed+=("$obj")
done

echo
if (( ${#failed[@]} )); then
  echo "Failed: ${failed[*]}" >&2
  exit 1
fi
echo "All objects done."
