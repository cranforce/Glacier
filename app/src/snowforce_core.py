"""Pure Snowforce logic: names, column types, SOQL, and the load SQL.

Nothing here talks to Snowflake or Salesforce, so it can be unit tested
locally. The stored procedures in snowforce_procs.py run the SQL built here.
"""
import re
from datetime import timedelta

# Snowflake reserved words can't be unquoted table names. The project
# convention is to prefix them with SFORCE_.
RESERVED = frozenset("""
    ACCOUNT ALL ALTER AND ANY AS BETWEEN BY CASE CAST CHECK COLUMN CONNECT
    CONNECTION CONSTRAINT CREATE CROSS CURRENT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP
    CURRENT_USER DATABASE DELETE DISTINCT DROP ELSE EXISTS FALSE FOLLOWING FOR FROM FULL
    GRANT GROUP GSCLUSTER HAVING ILIKE IN INCREMENT INNER INSERT INTERSECT INTO IS ISSUE
    JOIN LATERAL LEFT LIKE LOCALTIME LOCALTIMESTAMP MINUS NATURAL NOT NULL OF ON OR ORDER
    ORGANIZATION QUALIFY REGEXP REVOKE RIGHT RLIKE ROW ROWS SAMPLE SCHEMA SELECT SET SOME
    START TABLE TABLESAMPLE THEN TO TRIGGER TRUE TRY_CAST UNION UNIQUE UPDATE USING VALUES
    VIEW WHEN WHENEVER WHERE WITH
""".split())

DELETE_MODES = ("off", "flag", "remove")
RUN_MODES = ("sync", "full", "reconcile")

# Compound and binary fields the Bulk API can't export
UNEXPORTABLE_TYPES = frozenset({"address", "location", "base64"})

SOQL_TIMESTAMP_FORMAT = 'YYYY-MM-DD"T"HH24:MI:SS"Z"'
SOQL_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
SF_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
USER_NAME = re.compile(r"^[A-Za-z0-9_.@+-]+$")
CRON = re.compile(r"^[0-9A-Za-z*/,_ -]+$")

# Python files every procedure imports from the code stage
CODE_FILES = ("snowforce_core.py", "sforce_api.py", "snowforce_procs.py")
PACKAGES = "('snowflake-snowpark-python', 'requests', 'cryptography')"


class ConfigError(ValueError):
    """A job or connection setting that can't be used."""


# --- Names ------------------------------------------------------------------

def identifier(name, what="name"):
    """Validate an unquoted Snowflake identifier and return it uppercased."""
    if not name or not IDENTIFIER.match(name):
        raise ConfigError(f"Invalid {what}: {name!r}")
    return name.upper()


def table_identifier(name):
    """Validate a table name, rejecting reserved words with the SFORCE_ form suggested."""
    table = identifier(name, "table name")
    if table in RESERVED:
        raise ConfigError(f"{table} is a reserved word in Snowflake; use SFORCE_{table} instead.")
    return table


def default_table_name(sobject):
    """The object's API name uppercased, with SFORCE_ in front of reserved words."""
    table = identifier(sobject, "object name")
    return f"SFORCE_{table}" if table in RESERVED else table


def user_identifier(name):
    """A Snowflake user name, double-quoted because names like a.b@c.org need it."""
    if not name or not USER_NAME.match(name):
        raise ConfigError(f"Invalid user name: {name!r}")
    return f'"{name.upper()}"'


def sf_name(name, what="Salesforce name"):
    if not name or not SF_NAME.match(name):
        raise ConfigError(f"Invalid {what}: {name!r}")
    return name


def secret_alias(connection_name):
    """The name a procedure uses to read a connection's secret."""
    return identifier(connection_name, "connection name").lower()


def sql_string(value):
    """A single-quoted SQL string literal."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


# --- Salesforce fields --------------------------------------------------------

def snowflake_type(field):
    """Snowflake column type for a Salesforce describe field.

    The CSV export writes datetimes in UTC, so TIMESTAMP_NTZ holds UTC wall-clock
    time. Time fields stay VARCHAR because Salesforce writes them with a trailing Z.
    """
    sf_type = field.get("type")
    if sf_type == "boolean":
        return "BOOLEAN"
    if sf_type in ("int", "long"):
        return "NUMBER(38,0)"
    if sf_type in ("double", "currency", "percent"):
        return f"NUMBER(38,{int(field.get('scale') or 0)})"
    if sf_type == "date":
        return "DATE"
    if sf_type == "datetime":
        return "TIMESTAMP_NTZ"
    return "VARCHAR"


def exportable(field):
    return field.get("type") not in UNEXPORTABLE_TYPES


def column_name(field_name):
    """Snowflake column for a Salesforce field (Account.Name -> ACCOUNT_NAME)."""
    return field_name.strip().strip('"').upper().replace(".", "_")


def normalize_header(line):
    """Turn a Bulk API CSV header line into Snowflake column names."""
    return [column_name(f) for f in line.rstrip("\r\n").split(",")]


# --- SOQL ---------------------------------------------------------------------

def build_soql(sobject, fields, since=None, extra_fields=()):
    """SELECT the fields (plus Id, SystemModstamp, and any extras) from an object.

    With since, only records modified at or after that UTC timestamp.
    """
    sf_name(sobject, "object name")
    for f in fields:
        sf_name(f, "field name")
    have = {f.lower() for f in fields}
    required = [f for f in ("Id", "SystemModstamp", *extra_fields) if f.lower() not in have]
    soql = f"SELECT {', '.join(required + list(fields))} FROM {sobject}"
    if since:
        if not SOQL_TIMESTAMP.match(since):
            raise ConfigError(f"Invalid watermark: {since!r}")
        soql += f" WHERE SystemModstamp >= {since}"
    return soql


def build_ids_soql(sobject):
    return f"SELECT Id FROM {sf_name(sobject, 'object name')}"


def reconcile_due(last_success, now, days):
    """Whether deletes may have left the recycle bin since the last successful sync."""
    return last_success is None or now - last_success > timedelta(days=days)


# --- Load SQL -----------------------------------------------------------------

IS_DELETED = "COALESCE(s.ISDELETED::BOOLEAN, FALSE)"


def column_defs(columns, types):
    return [f"{c} {types.get(c, 'VARCHAR')}" for c in columns]


def copy_sql(target, stage_path, file_format):
    return (f"COPY INTO {target}\n"
            f"  FROM {stage_path}\n"
            f"  FILE_FORMAT = (FORMAT_NAME = '{file_format}')\n"
            "  MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE\n"
            "  ON_ERROR = 'ABORT_STATEMENT'\n"
            "  PURGE = TRUE")


def create_table_sql(fq_table, columns, types, replace):
    keyword = "CREATE OR REPLACE TABLE" if replace else "CREATE TABLE"
    defs = ",\n  ".join(column_defs(columns, types))
    return f"{keyword} {fq_table} (\n  {defs}\n)"


def add_columns_sql(fq_table, columns, types):
    """One ALTER adding any missing columns. IF NOT EXISTS has to be repeated for
    each column: Snowflake applies a single one only to the first column."""
    defs = ",\n  ".join(f"IF NOT EXISTS {d}" for d in column_defs(columns, types))
    return f"ALTER TABLE {fq_table} ADD COLUMN\n  {defs}"


def merge_sql(fq_table, delta_table, columns, delete_mode):
    sets = ", ".join(f"{c} = s.{c}" for c in columns if c != "ID")
    insert_cols = ", ".join(columns)
    insert_vals = ", ".join(f"s.{c}" for c in columns)
    if delete_mode == "remove":
        actions = (f"WHEN MATCHED AND {IS_DELETED} THEN DELETE\n"
                   f"WHEN MATCHED THEN UPDATE SET {sets}\n"
                   f"WHEN NOT MATCHED AND NOT {IS_DELETED} THEN INSERT ({insert_cols}) VALUES ({insert_vals})")
    else:
        actions = (f"WHEN MATCHED THEN UPDATE SET {sets}\n"
                   f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})")
    return (f"MERGE INTO {fq_table} t\n"
            "USING (\n"
            f"  SELECT * FROM {delta_table}\n"
            "  QUALIFY ROW_NUMBER() OVER (PARTITION BY ID ORDER BY SYSTEMMODSTAMP DESC) = 1\n"
            ") s\n"
            "ON t.ID = s.ID\n"
            f"{actions}")


def full_load_statements(fq_table, columns, types, stage_path, file_format, has_rows):
    statements = [create_table_sql(fq_table, columns, types, replace=True)]
    if has_rows:
        statements.append(copy_sql(fq_table, stage_path, file_format))
    return statements


def delta_statements(fq_table, delta_table, columns, types, stage_path, file_format, delete_mode):
    # The delta table has exactly the exported columns: COPY rejects files whose
    # column count differs from the table's, and the target can have extra columns.
    defs = ",\n  ".join(column_defs(columns, types))
    return [
        add_columns_sql(fq_table, columns, types),
        f"CREATE OR REPLACE TEMPORARY TABLE {delta_table} (\n  {defs}\n)",
        copy_sql(delta_table, stage_path, file_format),
        merge_sql(fq_table, delta_table, columns, delete_mode),
    ]


def reconcile_statements(fq_table, ids_table, stage_path, file_format, delete_mode):
    """Flag or remove every row whose Id no longer exists in Salesforce."""
    statements = [
        f"CREATE OR REPLACE TEMPORARY TABLE {ids_table} (ID VARCHAR)",
        copy_sql(ids_table, stage_path, file_format),
    ]
    if delete_mode == "remove":
        statements.append(f"DELETE FROM {fq_table} WHERE ID NOT IN (SELECT ID FROM {ids_table})")
    else:
        statements += [
            f"ALTER TABLE {fq_table} ADD COLUMN IF NOT EXISTS ISDELETED BOOLEAN",
            f"UPDATE {fq_table} SET ISDELETED = TRUE\n"
            f"  WHERE ID NOT IN (SELECT ID FROM {ids_table})\n"
            "    AND NOT COALESCE(ISDELETED::BOOLEAN, FALSE)",
        ]
    return statements


def watermark_sql(fq_table, data_type):
    """MAX(SYSTEMMODSTAMP) as a SOQL UTC timestamp, whatever the column's type."""
    if data_type == "TIMESTAMP_NTZ":
        expr = "MAX(SYSTEMMODSTAMP)"  # already UTC
    elif data_type in ("TIMESTAMP_TZ", "TIMESTAMP_LTZ"):
        expr = "CONVERT_TIMEZONE('UTC', MAX(SYSTEMMODSTAMP))"
    else:
        expr = "CONVERT_TIMEZONE('UTC', MAX(TRY_TO_TIMESTAMP_TZ(SYSTEMMODSTAMP::VARCHAR)))"
    return f"SELECT TO_VARCHAR({expr}, '{SOQL_TIMESTAMP_FORMAT}') AS WATERMARK FROM {fq_table}"


# --- Procedure and task DDL ---------------------------------------------------

# Procedures that read connection secrets: (name, arguments, returns, handler)
SECRET_PROCEDURES = (
    ("SNOWFORCE_TEST_CONNECTION", "CONNECTION_NAME VARCHAR", "VARIANT", "test_connection"),
    ("SNOWFORCE_LIST_OBJECTS", "CONNECTION_NAME VARCHAR", "VARIANT", "list_objects"),
    ("SNOWFORCE_DESCRIBE", "CONNECTION_NAME VARCHAR, SOBJECT VARCHAR", "VARIANT", "describe"),
    ("SNOWFORCE_EXTRACT",
     "CONNECTION_NAME VARCHAR, SOQL VARCHAR, ALL_ROWS BOOLEAN, RUN_ID VARCHAR", "VARIANT", "extract"),
)


def imports_clause(app):
    return "(" + ", ".join(f"'@{app}.SNOWFORCE_CODE/{f}'" for f in CODE_FILES) + ")"


def secret_procedure_ddl(app, integration, connections):
    """CREATE statements for the procedures that call Salesforce.

    A procedure's secrets are fixed when it's created, so these are re-created
    whenever a connection is added or removed. connections is a list of
    (connection name, fully qualified secret name).
    """
    secrets = ", ".join(f"'{secret_alias(name)}' = {secret}" for name, secret in connections)
    statements = []
    for name, args, returns, handler in SECRET_PROCEDURES:
        statements.append(
            f"CREATE OR REPLACE PROCEDURE {app}.{name}({args}, APP_SCHEMA VARCHAR DEFAULT '{app}')\n"
            f"  RETURNS {returns}\n"
            "  LANGUAGE PYTHON\n"
            "  RUNTIME_VERSION = '3.11'\n"
            f"  PACKAGES = {PACKAGES}\n"
            f"  IMPORTS = {imports_clause(app)}\n"
            f"  HANDLER = 'snowforce_procs.{handler}'\n"
            f"  EXTERNAL_ACCESS_INTEGRATIONS = ({integration})\n"
            + (f"  SECRETS = ({secrets})\n" if secrets else "")
            + "  EXECUTE AS OWNER")
    return statements


def task_name(app, job_name):
    return f"{app}.SNOWFORCE_JOB_{identifier(job_name, 'job name')}"


def task_ddl(app, owner_role, job):
    """CREATE TASK for a job: its warehouse, schedule, run-as user, and role.

    A task that runs as a user starts in the task owner's role and can switch
    with USE ROLE. A task without a user runs as Snowflake's system service in
    the owner role, so it can't switch roles.
    """
    name = job["JOB_NAME"]
    warehouse = identifier(job.get("WAREHOUSE"), "warehouse")
    user = job.get("RUN_AS_USER")
    role = identifier(job["RUN_AS_ROLE"], "role") if job.get("RUN_AS_ROLE") else None
    schedule = job.get("SCHEDULE")
    if role and not user and role != owner_role.upper():
        raise ConfigError("A run-as role other than the app's own role needs a run-as user.")
    if schedule and not CRON.match(schedule):
        raise ConfigError(f"Invalid schedule: {schedule!r}")

    lines = [f"CREATE OR REPLACE TASK {task_name(app, name)}", f"  WAREHOUSE = {warehouse}"]
    if schedule:
        lines.append(f"  SCHEDULE = {sql_string('USING CRON ' + schedule)}")
    if user:
        lines.append(f"  EXECUTE AS USER {user_identifier(user)}")
    lines.append(f"  COMMENT = {sql_string('Snowforce job ' + name)}")
    body = []
    if user and role:
        body.append(f"  USE ROLE {role};")
    body.append(f"  CALL {app}.SNOWFORCE_RUN_JOB({sql_string(name)});")
    lines.append("AS\nEXECUTE IMMEDIATE $$\nBEGIN\n" + "\n".join(body) + "\nEND;\n$$")
    return "\n".join(lines)
