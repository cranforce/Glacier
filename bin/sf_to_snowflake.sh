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
#   -H  Keep type 2 history in <TABLE>__HISTORY: one row per version of each
#       record, with VALID_FROM, VALID_TO, and IS_CURRENT. Turning it on for an
#       existing table starts history from the table's current rows. A full
#       reload (-r) keeps the history table; reconcile (-R) ends the current
#       version of records gone from Salesforce.
#   -r  Replace the table if it already exists (default: fail if it exists)
#   -x  Create every column as VARCHAR instead of inferring types
#   -k  Keep the local CSV after loading
#
# Id and SystemModstamp (plus IsDeleted with -D) are added to the query if it
# doesn't select them.
#
# Overridable via environment:
#   SNOW_CONNECTION (default ODBM), SNOW_DB (default GLACIER_DB),
#   SNOW_SCHEMA (default NPC_SALESFORCE_PROD_RAW), SNOW_ROLE, SNOW_WAREHOUSE

set -euo pipefail

SNOW_CONNECTION="${SNOW_CONNECTION:-ODBM}"
SNOW_DB="${SNOW_DB:-GLACIER_DB}"
SNOW_SCHEMA="${SNOW_SCHEMA:-NPC_SALESFORCE_PROD_RAW}"
SNOW_ROLE="${SNOW_ROLE:-DS_DATA_ENGINEERING_ROLE}"
SNOW_WAREHOUSE="${SNOW_WAREHOUSE:-GLACIER_WH}"

usage() { awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"; exit 1; }

org="" table="" query="" query_file="" types_file="" delete_mode=off
delta=0 reconcile=0 replace=0 all_text=0 keep=0 history=0
while getopts "o:t:q:f:s:D:dRHrxkh" opt; do
  case "$opt" in
    o) org="$OPTARG" ;;
    t) table="$OPTARG" ;;
    q) query="$OPTARG" ;;
    f) query_file="$OPTARG" ;;
    s) types_file="$OPTARG" ;;
    D) delete_mode="$OPTARG" ;;
    d) delta=1 ;;
    R) reconcile=1 ;;
    H) history=1 ;;
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

# Snowflake reserved words can only be table names when double-quoted
# (ORDER -> "ORDER"), so quote them wherever SQL names the table.
reserved=" ACCOUNT ALL ALTER AND ANY AS BETWEEN BY CASE CAST CHECK COLUMN CONNECT
  CONNECTION CONSTRAINT CREATE CROSS CURRENT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP
  CURRENT_USER DATABASE DELETE DISTINCT DROP ELSE EXISTS FALSE FOLLOWING FOR FROM FULL
  GRANT GROUP GSCLUSTER HAVING ILIKE IN INCREMENT INNER INSERT INTERSECT INTO IS ISSUE
  JOIN LATERAL LEFT LIKE LOCALTIME LOCALTIMESTAMP MINUS NATURAL NOT NULL OF ON OR ORDER
  ORGANIZATION QUALIFY REGEXP REVOKE RIGHT RLIKE ROW ROWS SAMPLE SCHEMA SELECT SET SOME
  START TABLE TABLESAMPLE THEN TO TRIGGER TRUE TRY_CAST UNION UNIQUE UPDATE USING VALUES
  VIEW WHEN WHENEVER WHERE WITH "
table_sql="$table"
if [[ "$(echo $reserved)" =~ (^| )$table( |$) ]]; then
  table_sql="\"$table\""
fi

fq_table="$SNOW_DB.$SNOW_SCHEMA.$table_sql"
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

add_cols="" delta_cols="" set_list="" ins_cols="" ins_vals="" h_hash="" s_hash=""
for i in "${!cols[@]}"; do
  c="${cols[$i]}"
  add_cols+="${add_cols:+,
  }IF NOT EXISTS ${col_defs[$i]}"
  delta_cols+="${delta_cols:+,
  }${col_defs[$i]}"
  ins_cols+="${ins_cols:+, }$c"
  ins_vals+="${ins_vals:+, }s.$c"
  [[ "$c" == "ID" ]] || set_list+="${set_list:+, }$c = s.$c"
  # Fields that change without the record changing: SystemModstamp moves on
  # system updates, and the Last*Date fields when someone views the record.
  case "$c" in
    SYSTEMMODSTAMP|LASTVIEWEDDATE|LASTREFERENCEDDATE) ;;
    *) h_hash+="${h_hash:+, }h.$c"; s_hash+="${s_hash:+, }s.$c" ;;
  esac
  if (( history && !reconcile )) && [[ "$c" =~ ^(VALID_FROM|VALID_TO|IS_CURRENT)$ ]]; then
    echo "Can't track history: $c would clash with the history columns." >&2
    exit 1
  fi
done

# Type 2 history (-H). VALID_TO is the next version's VALID_FROM; the current
# version has NULL. The table starts with only ID and the history columns so a
# reconcile can create it; the other columns are added like the target's.
hist_table="$SNOW_DB.$SNOW_SCHEMA.${table}__HISTORY"
valid_type="$(col_type SYSTEMMODSTAMP)"
hist_setup="CREATE TABLE IF NOT EXISTS $hist_table (
  ID VARCHAR,
  VALID_FROM $valid_type,
  VALID_TO $valid_type,
  IS_CURRENT BOOLEAN
);"
(( reconcile )) || hist_setup+="

ALTER TABLE $hist_table ADD COLUMN
  $add_cols;"

# End the current version of every record not in the given query: it's gone
# from Salesforce.
hist_close_missing() {
  echo "UPDATE $hist_table SET VALID_TO = CONVERT_TIMEZONE('UTC', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ, IS_CURRENT = FALSE
  WHERE IS_CURRENT AND ID NOT IN ($1);"
}

# Close each current version whose values differ from the source (one row per
# Id), then add a current version for every changed or new record. Versions
# older than what history already has are skipped, so re-fetched boundary
# records and records closed by a reconcile don't come back.
hist_apply() {
  echo "UPDATE $hist_table h SET VALID_TO = s.SYSTEMMODSTAMP, IS_CURRENT = FALSE
  FROM ($1) s
  WHERE h.ID = s.ID AND h.IS_CURRENT AND s.SYSTEMMODSTAMP >= h.VALID_FROM
    AND HASH($h_hash) <> HASH($s_hash);
${2:-}
INSERT INTO $hist_table ($ins_cols, VALID_FROM, VALID_TO, IS_CURRENT)
  SELECT $ins_vals, s.SYSTEMMODSTAMP, NULL, TRUE
  FROM ($1) s
  WHERE NOT EXISTS (SELECT 1 FROM $hist_table h
    WHERE h.ID = s.ID AND (h.IS_CURRENT OR h.VALID_TO > s.SYSTEMMODSTAMP));"
}

# 5. Build the Snowflake script
if (( reconcile )); then
  ids_table="${table}__IDS"
  prepare_sql=""
  if [[ "$delete_mode" == remove ]]; then
    apply_sql="DELETE FROM $fq_table WHERE ID NOT IN (SELECT ID FROM $ids_table);"
  else
    prepare_sql="ALTER TABLE $fq_table ADD COLUMN IF NOT EXISTS ISDELETED BOOLEAN;"
    apply_sql="UPDATE $fq_table SET ISDELETED = TRUE
  WHERE ID NOT IN (SELECT ID FROM $ids_table)
    AND NOT COALESCE(ISDELETED::BOOLEAN, FALSE);"
  fi
  if (( history )); then
    prepare_sql+="${prepare_sql:+

}$hist_setup"
    apply_sql="BEGIN;

$(hist_close_missing "SELECT ID FROM $ids_table")

$apply_sql

COMMIT;"
  fi

  load_sql="CREATE OR REPLACE TEMPORARY TABLE $ids_table (ID VARCHAR);

COPY INTO $ids_table
  FROM $stage_path/
  FILE_FORMAT = (FORMAT_NAME = 'SF_TO_SNOWFLAKE_CSV')
  MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE
  ON_ERROR = 'ABORT_STATEMENT'
  PURGE = TRUE;

${prepare_sql:+$prepare_sql

}$apply_sql"
elif (( delta )); then
  delta_table="${table}__DELTA"
  latest="SELECT * FROM $delta_table
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ID ORDER BY SYSTEMMODSTAMP DESC) = 1"

  if [[ "$delete_mode" == remove ]]; then
    merge_actions="WHEN MATCHED AND $is_deleted THEN DELETE
WHEN MATCHED THEN UPDATE SET $set_list
WHEN NOT MATCHED AND NOT $is_deleted THEN INSERT ($ins_cols) VALUES ($ins_vals);"
  else
    merge_actions="WHEN MATCHED THEN UPDATE SET $set_list
WHEN NOT MATCHED THEN INSERT ($ins_cols) VALUES ($ins_vals);"
  fi

  merge_sql="MERGE INTO $fq_table t
USING (
  $latest
) s
ON t.ID = s.ID
$merge_actions"

  # History and the merge commit together, so a failure can't leave history
  # behind the table (whose watermark decides what the next sync fetches).
  # An empty history starts from the table's rows, before the merge.
  hist_seed=""
  if (( history )); then
    hist_seed="$hist_setup

INSERT INTO $hist_table ($ins_cols, VALID_FROM, VALID_TO, IS_CURRENT)
  SELECT $ins_cols, SYSTEMMODSTAMP, NULL, TRUE FROM $fq_table
  WHERE NOT EXISTS (SELECT 1 FROM $hist_table);"
    merge_sql="BEGIN;

$(hist_apply "$latest")

$merge_sql

COMMIT;"
  fi

  # Add any fields that are new to the query. IF NOT EXISTS has to be repeated
  # for each column: Snowflake applies a single one only to the first column.
  load_sql="ALTER TABLE $fq_table ADD COLUMN
  $add_cols;

${hist_seed:+$hist_seed

}-- Exactly the exported columns: COPY rejects files whose column count differs
-- from the table's, and the target can have extra columns.
CREATE OR REPLACE TEMPORARY TABLE $delta_table (
  $delta_cols
);

COPY INTO $delta_table
  FROM $stage_path/
  FILE_FORMAT = (FORMAT_NAME = 'SF_TO_SNOWFLAKE_CSV')
  MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE
  ON_ERROR = 'ABORT_STATEMENT'
  PURGE = TRUE;

$merge_sql"
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

  # The history table outlives full reloads: records whose values changed get
  # a new version, and records no longer in Salesforce are closed.
  if (( history )); then
    load_sql+="

$hist_setup

BEGIN;

$(hist_apply "SELECT * FROM $fq_table" "
$(hist_close_missing "SELECT ID FROM $fq_table")
")

COMMIT;"
  fi
fi

summary_sql="SELECT COUNT(*) AS TOTAL_ROWS, MAX(SYSTEMMODSTAMP) AS LATEST_SYSTEMMODSTAMP FROM $fq_table;"
if [[ "$delete_mode" == flag ]]; then
  summary_sql="SELECT COUNT(*) AS TOTAL_ROWS, COUNT_IF(ISDELETED::BOOLEAN) AS DELETED_ROWS,
  MAX(SYSTEMMODSTAMP) AS LATEST_SYSTEMMODSTAMP FROM $fq_table;"
fi
if (( history )); then
  summary_sql+="
SELECT COUNT(*) AS HISTORY_VERSIONS, COUNT_IF(IS_CURRENT) AS CURRENT_VERSIONS FROM $hist_table;"
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
