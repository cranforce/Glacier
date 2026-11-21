"""Pure Glacier logic: names, the load SQL, history, and procedure and task DDL.

Nothing here talks to Snowflake or a source system, so it can be unit tested
locally. The stored procedures in glacier_procs.py run the SQL built here.
What's specific to a source (Salesforce, ShipHero) lives in its connector
module; see glacier_connectors.py.
"""
import re
from dataclasses import dataclass
from datetime import timedelta

# Snowflake reserved words can only be table names when double-quoted, so
# table_ref() quotes them (ORDER -> "ORDER").
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

# Watermarks are UTC timestamps in ISO 8601, e.g. 2026-10-01T12:00:00Z
WATERMARK_FORMAT = 'YYYY-MM-DD"T"HH24:MI:SS"Z"'
WATERMARK = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
USER_NAME = re.compile(r"^[A-Za-z0-9_.@+-]+$")
CRON = re.compile(r"^[0-9A-Za-z*/,_ -]+$")

# Python files every procedure imports from the code stage
CODE_FILES = ("glacier_core.py", "glacier_connectors.py", "glacier_procs.py",
              "connector_salesforce.py", "sforce_api.py")
PACKAGES = "('snowflake-snowpark-python', 'requests', 'cryptography')"


class ConfigError(ValueError):
    """A job or connection setting that can't be used."""


@dataclass(frozen=True)
class Setting:
    """One of a connector's connection settings, for the form and SETTINGS."""
    key: str
    label: str
    default: str = ""
    placeholder: str = ""
    help: str = None


@dataclass(frozen=True)
class RecordKeys:
    """The source fields the load SQL depends on, as the connector names them.

    key identifies a record. change is its last-modified UTC timestamp, which
    is the delta watermark and a history version's VALID_FROM. deleted is a
    boolean deleted flag, or None if the source doesn't report deletes in its
    data. untracked fields change without the record changing, so history
    ignores them (as it does change). column turns a field into its Snowflake
    column, as the connector names columns.
    """
    key: str
    change: str
    deleted: str = None
    untracked: tuple = ()
    column: object = None

    def col(self, field):
        return (self.column or column_name)(field)

    @property
    def key_col(self):
        return self.col(self.key)

    @property
    def change_col(self):
        return self.col(self.change)

    @property
    def deleted_col(self):
        return self.col(self.deleted) if self.deleted else None

    @property
    def untracked_cols(self):
        return frozenset({self.change_col, *(self.col(f) for f in self.untracked)})


# --- Names ------------------------------------------------------------------

def identifier(name, what="name"):
    """Validate an unquoted Snowflake identifier and return it uppercased."""
    if not name or not IDENTIFIER.match(name):
        raise ConfigError(f"Invalid {what}: {name!r}")
    return name.upper()


def table_identifier(name):
    """Validate a table name and return it uppercased, unquoted (reserved words allowed)."""
    return identifier(name, "table name")


def table_ref(database, schema, table):
    """DB.SCHEMA.TABLE for SQL, double-quoting a table name that's a reserved word."""
    table = table_identifier(table)
    quoted = f'"{table}"' if table in RESERVED else table
    return f"{identifier(database, 'database')}.{identifier(schema, 'schema')}.{quoted}"


def user_identifier(name):
    """A Snowflake user name, double-quoted because names like a.b@c.org need it."""
    if not name or not USER_NAME.match(name):
        raise ConfigError(f"Invalid user name: {name!r}")
    return f'"{name.upper()}"'


def secret_alias(connection_name):
    """The name a procedure uses to read a connection's secret."""
    return identifier(connection_name, "connection name").lower()


def sql_string(value):
    """A single-quoted SQL string literal."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


# --- Columns -------------------------------------------------------------------

def column_name(field_name):
    """Snowflake column for a source field, uppercased (Account.Name -> ACCOUNT_NAME).
    A connector with its own convention passes its column function instead."""
    return field_name.strip().strip('"').upper().replace(".", "_")


def normalize_header(line, column=column_name):
    """Turn an export's CSV header line into Snowflake column names."""
    fields = [f.strip().strip('"') for f in line.rstrip("\r\n").split(",")]
    columns = [column(f) for f in fields]
    seen = {}
    for f, c in zip(fields, columns):
        if c in seen:
            raise ConfigError(f"Fields {seen[c]} and {f} would both be column {c}; "
                              "leave one out of the job's fields")
        seen[c] = f
    return columns


# --- Runs ----------------------------------------------------------------------

def reconcile_due(last_success, now, days):
    """Whether deletes may have been missed since the last successful sync, e.g. by
    leaving Salesforce's recycle bin."""
    return last_success is None or now - last_success > timedelta(days=days)


# --- Load SQL -----------------------------------------------------------------

# Type 2 history: <TABLE>__HISTORY keeps one row per version of each record.
# VALID_TO is the next version's VALID_FROM; the current version has NULL.
HISTORY_SUFFIX = "__HISTORY"
HISTORY_COLUMNS = ("VALID_FROM", "VALID_TO", "IS_CURRENT")
NOW_UTC = "CONVERT_TIMEZONE('UTC', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ"


def is_deleted(keys, alias="s"):
    return f"COALESCE({alias}.{keys.deleted_col}::BOOLEAN, FALSE)"


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


def latest_sql(delta_table, keys):
    """The delta's newest row per record."""
    return (f"SELECT * FROM {delta_table}\n"
            f"  QUALIFY ROW_NUMBER() OVER (PARTITION BY {keys.key_col} "
            f"ORDER BY {keys.change_col} DESC) = 1")


def merge_sql(fq_table, delta_table, columns, delete_mode, keys):
    key = keys.key_col
    sets = ", ".join(f"{c} = s.{c}" for c in columns if c != key)
    insert_cols = ", ".join(columns)
    insert_vals = ", ".join(f"s.{c}" for c in columns)
    if delete_mode == "remove":
        deleted = is_deleted(keys)
        actions = (f"WHEN MATCHED AND {deleted} THEN DELETE\n"
                   f"WHEN MATCHED THEN UPDATE SET {sets}\n"
                   f"WHEN NOT MATCHED AND NOT {deleted} THEN INSERT ({insert_cols}) VALUES ({insert_vals})")
    else:
        actions = (f"WHEN MATCHED THEN UPDATE SET {sets}\n"
                   f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})")
    return (f"MERGE INTO {fq_table} t\n"
            f"USING (\n  {latest_sql(delta_table, keys)}\n) s\n"
            f"ON t.{key} = s.{key}\n"
            f"{actions}")


def transaction(statements):
    return ["BEGIN", *statements, "COMMIT"]


def history_table_sql(history_table, valid_type, keys):
    """Create the history table if it's missing. Its other columns are added
    like the target's, so a reconcile can create it without knowing them."""
    return (f"CREATE TABLE IF NOT EXISTS {history_table} (\n"
            f"  {keys.key_col} VARCHAR,\n  VALID_FROM {valid_type},\n  VALID_TO {valid_type},\n"
            "  IS_CURRENT BOOLEAN\n)")


def history_setup_statements(history_table, columns, types, keys):
    clash = [c for c in columns if c in HISTORY_COLUMNS]
    if clash:
        raise ConfigError(f"Can't track history: {', '.join(clash)} would clash with the history columns")
    return [history_table_sql(history_table, types.get(keys.change_col, "VARCHAR"), keys),
            add_columns_sql(history_table, columns, types)]


def history_seed_sql(history_table, fq_table, columns, keys):
    """Start an empty history from the target's rows, for history turned on for an existing table."""
    cols = ", ".join(columns)
    return (f"INSERT INTO {history_table} ({cols}, VALID_FROM, VALID_TO, IS_CURRENT)\n"
            f"  SELECT {cols}, {keys.change_col}, NULL, TRUE FROM {fq_table}\n"
            f"  WHERE NOT EXISTS (SELECT 1 FROM {history_table})")


def _row_hash(alias, columns, keys):
    untracked = keys.untracked_cols
    return "HASH(" + ", ".join(f"{alias}.{c}" for c in columns if c not in untracked) + ")"


def history_apply_statements(history_table, source, columns, keys, close_missing=False):
    """Close each current version whose values differ from source (a query with one row per record),
    then add a current version for every changed or new record.

    Versions older than what history already has are skipped, so re-fetched
    boundary records and records closed by a reconcile don't come back. With
    close_missing, source is the whole object and current versions it no longer
    has are closed.
    """
    key, change = keys.key_col, keys.change_col
    cols = ", ".join(columns)
    statements = [
        f"UPDATE {history_table} h SET VALID_TO = s.{change}, IS_CURRENT = FALSE\n"
        f"  FROM ({source}) s\n"
        f"  WHERE h.{key} = s.{key} AND h.IS_CURRENT AND s.{change} >= h.VALID_FROM\n"
        f"    AND {_row_hash('h', columns, keys)} <> {_row_hash('s', columns, keys)}"]
    if close_missing:
        statements.append(close_history_sql(history_table, f"SELECT {key} FROM ({source})", keys))
    statements.append(
        f"INSERT INTO {history_table} ({cols}, VALID_FROM, VALID_TO, IS_CURRENT)\n"
        f"  SELECT {', '.join(f's.{c}' for c in columns)}, s.{change}, NULL, TRUE\n"
        f"  FROM ({source}) s\n"
        f"  WHERE NOT EXISTS (SELECT 1 FROM {history_table} h\n"
        f"    WHERE h.{key} = s.{key} AND (h.IS_CURRENT OR h.VALID_TO > s.{change}))")
    return statements


def close_history_sql(history_table, ids_sql, keys):
    """End the current version of every record not in ids_sql: it's gone from the source."""
    return (f"UPDATE {history_table} SET VALID_TO = {NOW_UTC}, IS_CURRENT = FALSE\n"
            f"  WHERE IS_CURRENT AND {keys.key_col} NOT IN ({ids_sql})")


def full_load_statements(fq_table, columns, types, stage_path, file_format, has_rows, keys,
                         history_table=None):
    """With history_table, COPY is still statement 1 and the history work follows.
    An empty load leaves history alone rather than closing every record."""
    statements = [create_table_sql(fq_table, columns, types, replace=True)]
    if has_rows:
        statements.append(copy_sql(fq_table, stage_path, file_format))
        if history_table:
            statements += history_setup_statements(history_table, columns, types, keys)
            statements += transaction(history_apply_statements(
                history_table, f"SELECT * FROM {fq_table}", columns, keys, close_missing=True))
    return statements


def delta_statements(fq_table, delta_table, columns, types, stage_path, file_format, delete_mode, keys,
                     history_table=None):
    """The MERGE is the last statement, or with history_table, the one before
    COMMIT, after the history INSERT."""
    # The delta table has exactly the exported columns: COPY rejects files whose
    # column count differs from the table's, and the target can have extra columns.
    defs = ",\n  ".join(column_defs(columns, types))
    statements = [add_columns_sql(fq_table, columns, types)]
    if history_table:
        statements += history_setup_statements(history_table, columns, types, keys)
        statements.append(history_seed_sql(history_table, fq_table, columns, keys))
    statements += [
        f"CREATE OR REPLACE TEMPORARY TABLE {delta_table} (\n  {defs}\n)",
        copy_sql(delta_table, stage_path, file_format),
    ]
    merge = merge_sql(fq_table, delta_table, columns, delete_mode, keys)
    if not history_table:
        return statements + [merge]
    return statements + transaction(
        history_apply_statements(history_table, latest_sql(delta_table, keys), columns, keys) + [merge])


def reconcile_statements(fq_table, ids_table, stage_path, file_format, delete_mode, keys,
                         history_table=None, valid_type="TIMESTAMP_NTZ"):
    """Flag or remove every row whose key no longer exists in the source, and with
    history_table, end those records' current versions. The flag or remove is the
    last statement, or the one before COMMIT."""
    key, deleted = keys.key_col, keys.deleted_col
    statements = [
        f"CREATE OR REPLACE TEMPORARY TABLE {ids_table} ({key} VARCHAR)",
        copy_sql(ids_table, stage_path, file_format),
    ]
    if delete_mode == "remove":
        apply = f"DELETE FROM {fq_table} WHERE {key} NOT IN (SELECT {key} FROM {ids_table})"
    else:
        statements.append(f"ALTER TABLE {fq_table} ADD COLUMN IF NOT EXISTS {deleted} BOOLEAN")
        apply = (f"UPDATE {fq_table} SET {deleted} = TRUE\n"
                 f"  WHERE {key} NOT IN (SELECT {key} FROM {ids_table})\n"
                 f"    AND NOT COALESCE({deleted}::BOOLEAN, FALSE)")
    if not history_table:
        return statements + [apply]
    return statements + [history_table_sql(history_table, valid_type, keys)] + transaction(
        [close_history_sql(history_table, f"SELECT {key} FROM {ids_table}", keys), apply])


def watermark_sql(fq_table, data_type, keys):
    """MAX of the change column as an ISO 8601 UTC timestamp, whatever the column's type."""
    change = keys.change_col
    if data_type == "TIMESTAMP_NTZ":
        expr = f"MAX({change})"  # already UTC
    elif data_type in ("TIMESTAMP_TZ", "TIMESTAMP_LTZ"):
        expr = f"CONVERT_TIMEZONE('UTC', MAX({change}))"
    else:
        expr = f"CONVERT_TIMEZONE('UTC', MAX(TRY_TO_TIMESTAMP_TZ({change}::VARCHAR)))"
    return f"SELECT TO_VARCHAR({expr}, '{WATERMARK_FORMAT}') AS WATERMARK FROM {fq_table}"


# --- Procedure and task DDL ---------------------------------------------------

# Procedures that read a connector's secrets: (action, arguments, handler). Each
# connector gets its own set, GLACIER_<CONNECTOR>_<ACTION>, which can read only
# that connector's secrets and reach only its hosts.
SECRET_PROCEDURES = (
    ("TEST_CONNECTION", "CONNECTION_NAME VARCHAR", "test_connection"),
    ("LIST_OBJECTS", "CONNECTION_NAME VARCHAR", "list_objects"),
    ("DESCRIBE", "CONNECTION_NAME VARCHAR, SOURCE_OBJECT VARCHAR", "describe"),
    ("EXTRACT",
     "CONNECTION_NAME VARCHAR, SOURCE_QUERY VARCHAR, INCLUDE_DELETED BOOLEAN, RUN_ID VARCHAR", "extract"),
)


def procedure_name(connector, action):
    """A connector's procedure, e.g. GLACIER_SALESFORCE_EXTRACT."""
    return f"GLACIER_{identifier(connector, 'connector')}_{action}"


def integration_name(connector):
    """The external access integration an admin creates for a connector."""
    return f"GLACIER_{identifier(connector, 'connector')}_EAI"


def imports_clause(app):
    return "(" + ", ".join(f"'@{app}.GLACIER_CODE/{f}'" for f in CODE_FILES) + ")"


def secret_procedure_ddl(app, connector, integration, connections):
    """CREATE statements for the procedures that call one connector's source.

    A procedure's secrets are fixed when it's created, so these are re-created
    whenever a connection is added or removed. connections is a list of
    (connection name, fully qualified secret name) for this connector.
    """
    secrets = ", ".join(f"'{secret_alias(name)}' = {secret}" for name, secret in connections)
    statements = []
    for action, args, handler in SECRET_PROCEDURES:
        statements.append(
            f"CREATE OR REPLACE PROCEDURE {app}.{procedure_name(connector, action)}({args}, "
            f"CONNECTOR VARCHAR DEFAULT '{connector.lower()}', APP_SCHEMA VARCHAR DEFAULT '{app}')\n"
            "  RETURNS VARIANT\n"
            "  LANGUAGE PYTHON\n"
            "  RUNTIME_VERSION = '3.11'\n"
            f"  PACKAGES = {PACKAGES}\n"
            f"  IMPORTS = {imports_clause(app)}\n"
            f"  HANDLER = 'glacier_procs.{handler}'\n"
            f"  EXTERNAL_ACCESS_INTEGRATIONS = ({integration})\n"
            + (f"  SECRETS = ({secrets})\n" if secrets else "")
            + "  EXECUTE AS OWNER")
    return statements


def task_name(app, job_name):
    return f"{app}.GLACIER_JOB_{identifier(job_name, 'job name')}"


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
    lines.append(f"  COMMENT = {sql_string('Glacier job ' + name)}")
    body = []
    if user and role:
        body.append(f"  USE ROLE {role};")
    body.append(f"  CALL {app}.GLACIER_RUN_JOB({sql_string(name)});")
    lines.append("AS\nEXECUTE IMMEDIATE $$\nBEGIN\n" + "\n".join(body) + "\nEND;\n$$")
    return "\n".join(lines)
