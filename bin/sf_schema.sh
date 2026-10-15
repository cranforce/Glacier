#!/usr/bin/env bash
# Capture Salesforce object metadata with `sf sobject describe`.
#
# Usage:
#   sf_schema.sh -o <sf-org> [-d <schema-dir>] [-Q <query-dir>] [-F] <Object>...
#
# Options:
#   -o  Salesforce org alias/username (e.g. odbm-npc-prod)
#   -d  Directory for schema files (default: schema)
#   -Q  Directory for generated queries (default: queries)
#   -F  Overwrite existing query files (default: only create missing ones)
#
# Writes, per object:
#   <schema-dir>/<Object>.json        raw describe output
#   <schema-dir>/<Object>.fields.csv  readable field list (name, label, type, ...)
#   <schema-dir>/<Object>.types.tsv   COLUMN<TAB>Snowflake type, for sf_to_snowflake.sh -s
#   <query-dir>/<Object>.soql         SELECT of every field the Bulk API can export

set -euo pipefail

usage() { awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"; exit 1; }

org="" schema_dir="schema" query_dir="queries" force=0
while getopts "o:d:Q:Fh" opt; do
  case "$opt" in
    o) org="$OPTARG" ;;
    d) schema_dir="$OPTARG" ;;
    Q) query_dir="$OPTARG" ;;
    F) force=1 ;;
    *) usage ;;
  esac
done
shift $((OPTIND - 1))

[[ -n "$org" && $# -gt 0 ]] || usage
mkdir -p "$schema_dir" "$query_dir"

for obj in "$@"; do
  [[ "$obj" =~ ^[A-Za-z][A-Za-z0-9_]*$ ]] || { echo "Invalid object name: $obj" >&2; exit 1; }
  echo "==> Describing $obj in org '$org'..."

  json="$schema_dir/$obj.json"
  sf sobject describe --sobject "$obj" --target-org "$org" --json > "$json.tmp"
  jq '.result' "$json.tmp" > "$json" && rm -f "$json.tmp"

  jq -r '
    ["name", "label", "type", "length", "precision", "scale", "nillable", "calculated", "custom", "referenceTo"],
    (.fields[] | [.name, .label, .type, .length, .precision, .scale, .nillable, .calculated, .custom, (.referenceTo | join("|"))])
    | @csv' "$json" > "$schema_dir/$obj.fields.csv"

  # Salesforce -> Snowflake types. The CSV export writes datetimes in UTC, so
  # TIMESTAMP_NTZ holds UTC wall-clock time. Time fields stay VARCHAR because
  # Salesforce writes them with a trailing Z, which TIME won't parse.
  jq -r '
    def sftype:
      if   .type == "boolean"                              then "BOOLEAN"
      elif .type == "int" or .type == "long"               then "NUMBER(38,0)"
      elif .type == "double" or .type == "currency" or .type == "percent"
                                                           then "NUMBER(38,\(.scale))"
      elif .type == "date"                                 then "DATE"
      elif .type == "datetime"                             then "TIMESTAMP_NTZ"
      else "VARCHAR" end;
    .fields[] | [(.name | ascii_upcase), sftype] | @tsv' "$json" > "$schema_dir/$obj.types.tsv"

  # Compound (address/location) and base64 fields can't be exported via the
  # Bulk API; their component fields (BillingStreet, etc.) are listed separately.
  soql="$query_dir/$obj.soql"
  if [[ ! -e "$soql" ]] || (( force )); then
    jq -r --arg obj "$obj" '
      [.fields[] | select(.type != "address" and .type != "location" and .type != "base64") | .name]
      | "SELECT\n  " + join(",\n  ") + "\nFROM " + $obj' "$json" > "$soql"
    echo "    Wrote $soql"
  fi

  echo "    $(jq '.fields | length' "$json") fields -> $schema_dir/$obj.{json,fields.csv,types.tsv}"
done
