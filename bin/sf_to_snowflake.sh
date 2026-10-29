#!/usr/bin/env bash
# Run a SOQL query against a Salesforce org and load the results into a
# Snowflake table. Uses the sf CLI (Bulk API 2.0) and the snow CLI.
#
# Usage:
#   sf_to_snowflake.sh -o <sf-org> -t <TABLE> (-q "<SOQL>" | -f <query.soql>) [options]
#
# Options:
#   -o  Salesforce org alias/username (e.g. odbm-npc-prod)
#   -t  Target Snowflake table name (created in $SNOW_DB.$SNOW_SCHEMA)
#   -q  SOQL query string
#   -f  File containing the SOQL query
#   -s  Column type map (schema/<Object>.types.tsv from sf_schema.sh); without
#       it, column types are inferred from the data
#   -d  Delta: export only records with SystemModstamp at or after the table's
#       MAX(SYSTEMMODSTAMP) and MERGE them on ID. Falls back to a full load
#       when the table is missing, empty, or has no SYSTEMMODSTAMP column.
#   -D  Deleted records: off (default), flag (keep the row with ISDELETED =
#       TRUE), or remove (delete the row). Deletes are read from the Salesforce
#       recycle bin, which keeps them for about 15 days; use -R for older ones.
#   -R  Reconcile: export the Ids that currently exist in Salesforce and flag
#       or remove (per -D) every table row whose Id is gone. Catches deletes
#       that have left the recycle bin. Doesn't load any other changes.
#   -r  Replace the table if it already exists (default: fail if it exists)
#   -x  Create every column as VARCHAR instead of inferring types
#   -k  Keep the local CSV after loading
#
# Id and SystemModstamp (plus IsDeleted with -D) are added to the query if it
# doesn't select them.
#
# Overridable via environment:
#   SNOW_CONNECTION (default ODBM), SNOW_DB (default CRM_DB),
#   SNOW_SCHEMA (default CMILLER_SF_PLAYGROUND), SNOW_ROLE, SNOW_WAREHOUSE

set -euo pipefail

SNOW_CONNECTION="${SNOW_CONNECTION:-ODBM}"
SNOW_DB="${SNOW_DB:-CRM_DB}"
SNOW_SCHEMA="${SNOW_SCHEMA:-CMILLER_SF_PLAYGROUND}"
SNOW_ROLE="${SNOW_ROLE:-DS_DATA_ENGINEERING_ROLE}"
SNOW_WAREHOUSE="${SNOW_WAREHOUSE:-DS_DATA_ENGINNNERING_WH}"

usage() { awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"; exit 1; }

org="" table="" query="" query_file="" types_file="" delete_mode=off
delta=0 reconcile=0 replace=0 all_text=0 keep=0
while getopts "o:t:q:f:s:D:dRrxkh" opt; do
  case "$opt" in
    o) org="$OPTARG" ;;
    t) table="$OPTARG" ;;
    q) query="$OPTARG" ;;
    f) query_file="$OPTARG" ;;
    s) types_file="$OPTARG" ;;
    D) delete_mode="$OPTARG" ;;
    d) delta=1 ;;
    R) reconcile=1 ;;
    r) replace=1 ;;
    x) all_text=1 ;;
    k) keep=1 ;;
    *) usage ;;
  esac
done

[[ -n "$org" && -n "$table" ]] || usage
[[ -n "$query" || -n "$query_file" ]] || usage
[[ -n "$query" && -n "$query_file" ]] && { echo "Use -q or -f, not both." >&2; exit 1; }
[[ "$table" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || { echo "Invalid table name: $table" >&2; exit 1; }
[[ -z "$query_file" || -r "$query_file" ]] || { echo "Cannot read query file: $query_file" >&2; exit 1; }
[[ -z "$types_file" || -r "$types_file" ]] || { echo "Cannot read type map: $types_file" >&2; exit 1; }
[[ "$delete_mode" =~ ^(off|flag|remove)$ ]] || { echo "-D must be off, flag, or remove." >&2; exit 1; }
if (( reconcile )); then
  (( delta || replace )) && { echo "-R can't be combined with -d or -r." >&2; exit 1; }
  [[ "$delete_mode" != off ]] || { echo "-R needs -D flag or -D remove." >&2; exit 1; }
fi
if [[ "$delete_mode" != off && -n "$types_file" ]] && ! grep -q "^ISDELETED$(printf '\t')" "$types_file"; then
  echo "This object has no IsDeleted field, so deletes can't be tracked. Use -D off." >&2
  exit 1
fi
if [[ -n "$query_file" ]]; then query="$(cat "$query_file")"; fi

table="$(echo "$table" | tr '[:lower:]' '[:upper:]')"

# Snowflake reserved words can't be unquoted table names; the project
# convention is to prefix them with SFORCE_.
reserved=" ACCOUNT ALL ALTER AND ANY AS BETWEEN BY CASE CAST CHECK COLUMN CONNECT
  CONNECTION CONSTRAINT CREATE CROSS CURRENT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP
  CURRENT_USER DATABASE DELETE DISTINCT DROP ELSE EXISTS FALSE FOLLOWING FOR FROM FULL
  GRANT GROUP GSCLUSTER HAVING ILIKE IN INCREMENT INNER INSERT INTERSECT INTO IS ISSUE
  JOIN LATERAL LEFT LIKE LOCALTIME LOCALTIMESTAMP MINUS NATURAL NOT NULL OF ON OR ORDER
  ORGANIZATION QUALIFY REGEXP REVOKE RIGHT RLIKE ROW ROWS SAMPLE SCHEMA SELECT SET SOME
  START TABLE TABLESAMPLE THEN TO TRIGGER TRUE TRY_CAST UNION UNIQUE UPDATE USING VALUES
  VIEW WHEN WHENEVER WHERE WITH "
if [[ "$(echo $reserved)" =~ (^| )$table( |$) ]]; then
  echo "$table is a reserved word in Snowflake; use SFORCE_$table instead." >&2
  exit 1
fi

fq_table="$SNOW_DB.$SNOW_SCHEMA.$table"
stage_path="@~/sf_to_snowflake/$table"

workdir="$(mktemp -d)"
csv="$workdir/${table}.csv"
sql="$workdir/load.sql"
cleanup() {
  if (( keep )); then echo "CSV kept at: $csv"; else rm -rf "$workdir"; fi
}
trap cleanup EXIT

# Session settings shared by every Snowflake call. UTC makes TIMESTAMP_NTZ
# values (Salesforce UTC times) compare and format correctly.
session_sql="ALTER SESSION SET TIMEZONE = 'UTC';
USE ROLE $SNOW_ROLE;
USE WAREHOUSE $SNOW_WAREHOUSE;
USE SCHEMA $SNOW_DB.$SNOW_SCHEMA;"

# Flatten the query to one line and make sure it selects Id, SystemModstamp,
# and any extra fields. Given a timestamp, restrict it to records modified at
# or after it. In "ids" mode, select only Id (for reconciling).
build_query() {
  perl -e '
    my ($q, $since, $extra, $ids_only) = @ARGV;
    $q =~ s/\s+/ /g; $q =~ s/^ | $//g; $q =~ s/;$//;
    $q =~ /^select (.*?) from \w+/i
      or die "Could not parse query; expected SELECT <fields> FROM <Object>\n";
    if ($ids_only) {
      $q =~ /\b(limit|offset)\b/i
        and die "Can not reconcile a query that uses LIMIT or OFFSET\n";
      $q =~ s/^select .*? from /SELECT Id FROM /i;
      print $q;
      exit;
    }
    my %have = map { lc(s/^\s+|\s+$//gr) => 1 } split /,/, $1;
    my @add = grep { !$have{lc $_} } ("Id", "SystemModstamp", split / /, $extra);
    $q =~ s/^select /"SELECT " . join("", map { "$_, " } @add)/ie if @add;
    if ($since) {
      my $cond = "SystemModstamp >= $since";
      if ($q =~ /\bwhere\b/i) {
        $q =~ s/\bwhere (.*?)((?: group by| order by| limit| offset)\b.*)?$/WHERE $cond AND ($1)$2/is;
      } else {
        $q =~ s/(\bfrom \w+)/$1 WHERE $cond/i;
      }
    }
    print $q;
  ' "$1" "${2:-}" "${3:-}" "${4:-}"
}

# 1. In delta mode, find the newest SystemModstamp already loaded
since=""
if (( delta )); then
  echo "==> Reading SystemModstamp watermark from $fq_table..."
  wm_sql="$(cat <<EOF
$session_sql
EXECUTE IMMEDIATE \$\$
DECLARE
  has_table INTEGER;
  has_col INTEGER;
  wm VARCHAR;
BEGIN
  SELECT COUNT(*), COUNT_IF(COLUMN_NAME = 'SYSTEMMODSTAMP') INTO :has_table, :has_col
    FROM $SNOW_DB.INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = '$SNOW_SCHEMA' AND TABLE_NAME = '$table';
  IF (has_table = 0) THEN RETURN 'NO_TABLE'; END IF;
  IF (has_col = 0) THEN RETURN 'NO_COLUMN'; END IF;
  SELECT TO_VARCHAR(CONVERT_TIMEZONE('UTC', MAX(SYSTEMMODSTAMP)::TIMESTAMP_TZ), 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
    INTO :wm FROM $fq_table;
  RETURN COALESCE(wm, 'EMPTY');
END;
\$\$;
EOF
)"
  wm_out="$(snow sql -c "$SNOW_CONNECTION" --format json -q "$wm_sql")"
  wm="$(jq -r '[.. | objects | select(has("anonymous block")) | .["anonymous block"]] | last // empty' <<< "$wm_out" 2>/dev/null || true)"

  case "$wm" in
    NO_TABLE)  echo "    Table doesn't exist yet; doing a full load."; delta=0 ;;
    NO_COLUMN) echo "    Table has no SYSTEMMODSTAMP column; rebuilding it with a full load."; delta=0; replace=1 ;;
    EMPTY)     echo "    Table is empty; doing a full load."; delta=0; replace=1 ;;
    *)
      if [[ ! "$wm" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]]; then
        echo "Could not read the watermark. snow output:" >&2
        echo "$wm_out" >&2
        exit 1
      fi
      since="$wm"
      echo "    Fetching records with SystemModstamp >= $since"
      ;;
  esac
fi

# Deleted records come from the recycle bin (--all-rows). A full load in
# remove mode skips them, since they'd only be deleted again.
extra_fields="" all_rows=0
if [[ "$delete_mode" != off ]]; then
  extra_fields="IsDeleted"
  if (( delta )) || [[ "$delete_mode" == flag ]]; then all_rows=1; fi
fi
(( reconcile )) && all_rows=0

final_query="$(build_query "$query" "$since" "$extra_fields" "$reconcile")"

# 2. Export from Salesforce
if (( reconcile )); then
  echo "==> Exporting current Ids from Salesforce org '$org'..."
else
  echo "==> Exporting from Salesforce org '$org'..."
fi
sf_args=(data export bulk --target-org "$org" --query "$final_query" --output-file "$csv"
  --result-format csv --wait 60 --line-ending LF)
(( all_rows )) && sf_args+=(--all-rows)
sf "${sf_args[@]}"

rows=0
if [[ -s "$csv" ]]; then rows=$(( $(wc -l < "$csv") - 1 )); fi
if (( rows < 1 )); then
  if (( delta )); then echo "==> No changes since $since; $fq_table is up to date."; exit 0; fi
  if (( reconcile )); then
    echo "Salesforce returned no Ids; refusing to reconcile (it would flag or remove every row)." >&2
    exit 1
  fi
  echo "Query returned no rows; nothing to load." >&2
  exit 1
fi
echo "    $rows rows exported (approximate if fields contain line breaks)."

# 3. Normalize the header: strip quotes, uppercase, and turn relationship
#    fields like Account.Name into ACCOUNT_NAME so Snowflake columns are clean.
awk 'NR==1 { gsub(/"/, ""); $0 = toupper($0); gsub(/\./, "_") } { print }' "$csv" > "$csv.tmp" && mv "$csv.tmp" "$csv"

# 4. Work out each column's type: from the type map, all VARCHAR with -x, or
#    (for a new table without either) inferred by Snowflake from the file.
IFS=',' read -r -a cols < <(head -1 "$csv" | tr -d '\r')
col_type() {
  if (( all_text )) || [[ -z "$types_file" ]]; then echo VARCHAR; return; fi
  awk -F'\t' -v c="$1" '$1 == c { print $2; found = 1; exit } END { if (!found) print "VARCHAR" }' "$types_file"
}
col_defs=()
for c in "${cols[@]}"; do col_defs+=("$c $(col_type "$c")"); done

is_deleted="COALESCE(s.ISDELETED::BOOLEAN, FALSE)"

# 5. Build the Snowflake script
if (( reconcile )); then
  ids_table="${table}__IDS"
  if [[ "$delete_mode" == remove ]]; then
    apply_sql="DELETE FROM $fq_table WHERE ID NOT IN (SELECT ID FROM $ids_table);"
  else
    apply_sql="ALTER TABLE $fq_table ADD COLUMN IF NOT EXISTS ISDELETED BOOLEAN;
UPDATE $fq_table SET ISDELETED = TRUE
  WHERE ID NOT IN (SELECT ID FROM $ids_table)
    AND NOT COALESCE(ISDELETED::BOOLEAN, FALSE);"
  fi

  load_sql="CREATE OR REPLACE TEMPORARY TABLE $ids_table (ID VARCHAR);

COPY INTO $ids_table
  FROM $stage_path/
  FILE_FORMAT = (FORMAT_NAME = 'SF_TO_SNOWFLAKE_CSV')
  MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE
  ON_ERROR = 'ABORT_STATEMENT'
  PURGE = TRUE;

$apply_sql"
elif (( delta )); then
  delta_table="${table}__DELTA"
  add_cols="" set_list="" ins_cols="" ins_vals=""
  for i in "${!cols[@]}"; do
    c="${cols[$i]}"
    add_cols+="${add_cols:+,
  }IF NOT EXISTS ${col_defs[$i]}"
    ins_cols+="${ins_cols:+, }$c"
    ins_vals+="${ins_vals:+, }s.$c"
    [[ "$c" == "ID" ]] || set_list+="${set_list:+, }$c = s.$c"
  done

  if [[ "$delete_mode" == remove ]]; then
    merge_actions="WHEN MATCHED AND $is_deleted THEN DELETE
WHEN MATCHED THEN UPDATE SET $set_list
WHEN NOT MATCHED AND NOT $is_deleted THEN INSERT ($ins_cols) VALUES ($ins_vals);"
  else
    merge_actions="WHEN MATCHED THEN UPDATE SET $set_list
WHEN NOT MATCHED THEN INSERT ($ins_cols) VALUES ($ins_vals);"
  fi

  # Add any fields that are new to the query. IF NOT EXISTS has to be repeated
  # for each column: Snowflake applies a single one only to the first column.
  load_sql="ALTER TABLE $fq_table ADD COLUMN
  $add_cols;

CREATE OR REPLACE TEMPORARY TABLE $delta_table LIKE $fq_table;

COPY INTO $delta_table
  FROM $stage_path/
  FILE_FORMAT = (FORMAT_NAME = 'SF_TO_SNOWFLAKE_CSV')
  MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE
  ON_ERROR = 'ABORT_STATEMENT'
  PURGE = TRUE;

MERGE INTO $fq_table t
USING (
  SELECT * FROM $delta_table
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ID ORDER BY SYSTEMMODSTAMP DESC) = 1
) s
ON t.ID = s.ID
$merge_actions"
else
  create_kw="CREATE TABLE"
  (( replace )) && create_kw="CREATE OR REPLACE TABLE"

  if [[ -n "$types_file" ]] || (( all_text )); then
    defs="$(printf '  %s,\n' "${col_defs[@]}")"
    create_sql="$create_kw $fq_table (
${defs%,}
);"
  else
    create_sql="$create_kw $fq_table
  USING TEMPLATE (
    SELECT ARRAY_AGG(OBJECT_CONSTRUCT(*)) WITHIN GROUP (ORDER BY ORDER_ID)
    FROM TABLE(INFER_SCHEMA(LOCATION => '$stage_path/', FILE_FORMAT => '$SNOW_DB.$SNOW_SCHEMA.SF_TO_SNOWFLAKE_CSV'))
  );"
  fi

  load_sql="$create_sql

COPY INTO $fq_table
  FROM $stage_path/
  FILE_FORMAT = (FORMAT_NAME = 'SF_TO_SNOWFLAKE_CSV')
  MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE
  ON_ERROR = 'ABORT_STATEMENT'
  PURGE = TRUE;"
fi

summary_sql="SELECT COUNT(*) AS TOTAL_ROWS, MAX(SYSTEMMODSTAMP) AS LATEST_SYSTEMMODSTAMP FROM $fq_table;"
if [[ "$delete_mode" == flag ]]; then
  summary_sql="SELECT COUNT(*) AS TOTAL_ROWS, COUNT_IF(ISDELETED::BOOLEAN) AS DELETED_ROWS,
  MAX(SYSTEMMODSTAMP) AS LATEST_SYSTEMMODSTAMP FROM $fq_table;"
fi

cat > "$sql" <<EOF
$session_sql

CREATE OR REPLACE TEMPORARY FILE FORMAT SF_TO_SNOWFLAKE_CSV
  TYPE = 'CSV'
  PARSE_HEADER = TRUE
  FIELD_OPTIONALLY_ENCLOSED_BY = '"'
  NULL_IF = ('', 'NULL')
  EMPTY_FIELD_AS_NULL = TRUE;

REMOVE $stage_path/;
PUT 'file://$csv' $stage_path/ AUTO_COMPRESS = TRUE OVERWRITE = TRUE;

$load_sql

$summary_sql
EOF

# 6. Load into Snowflake (one session, so one SSO browser prompt)
if (( reconcile )); then
  echo "==> Reconciling deletes ($delete_mode) in $fq_table via connection '$SNOW_CONNECTION'..."
elif (( delta )); then
  echo "==> Merging into $fq_table via connection '$SNOW_CONNECTION'..."
else
  echo "==> Loading into $fq_table via connection '$SNOW_CONNECTION'..."
fi
snow sql -c "$SNOW_CONNECTION" -f "$sql"

echo "==> Done: $fq_table"
