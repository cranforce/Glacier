"""Pure logic behind the app's forms: validating connections and jobs, and the
SQL that saves them.

Nothing here imports Streamlit or talks to Snowflake, so it can be unit tested
locally. Values are bound as one JSON object (PARSE_JSON(?)), because Snowpark
binds a Python None as the text 'None', but a JSON null becomes SQL NULL.
"""
import json
import re

import glacier_core as core

MY_DOMAIN_URL = re.compile(r"^https://[A-Za-z0-9-]+(\.sandbox)?\.my\.salesforce\.com/?$")
API_VERSION = re.compile(r"^\d{2,3}\.0$")

# Common schedules; "" is manual runs only
SCHEDULES = {
    "Manual only": "",
    "Every 15 minutes": "*/15 * * * * UTC",
    "Every hour": "0 * * * * UTC",
    "Every 6 hours": "0 */6 * * * UTC",
    "Daily, 6 a.m. Eastern": "0 6 * * * America/Detroit",
}

# GLACIER_JOBS columns the app edits, and the type each is cast to from JSON
JOB_COLUMNS = (
    ("JOB_NAME", "VARCHAR"),
    ("CONNECTION_NAME", "VARCHAR"),
    ("SOBJECT", "VARCHAR"),
    ("FIELDS", "ARRAY"),
    ("TARGET_DATABASE", "VARCHAR"),
    ("TARGET_SCHEMA", "VARCHAR"),
    ("TARGET_TABLE", "VARCHAR"),
    ("DELETE_MODE", "VARCHAR"),
    ("RECONCILE_AFTER_DAYS", "NUMBER"),
    ("TRACK_HISTORY", "BOOLEAN"),
    ("RUN_AS_USER", "VARCHAR"),
    ("RUN_AS_ROLE", "VARCHAR"),
    ("WAREHOUSE", "VARCHAR"),
    ("SCHEDULE", "VARCHAR"),
    ("ENABLED", "BOOLEAN"),
)

CONNECTION_COLUMNS = (
    ("CONNECTION_NAME", "VARCHAR"),
    ("MY_DOMAIN_URL", "VARCHAR"),
    ("CLIENT_ID", "VARCHAR"),
    ("USERNAME", "VARCHAR"),
    ("API_VERSION", "VARCHAR"),
    ("SECRET_NAME", "VARCHAR"),
    ("DESCRIPTION", "VARCHAR"),
)


def _blank(value):
    return value is None or str(value).strip() == ""


def _text(value):
    return None if _blank(value) else str(value).strip()


# --- Connections ----------------------------------------------------------------

def secret_name(app, connection_name):
    """The secret the app creates for a connection's private key."""
    return f"{app}.GLACIER_SECRET_{core.identifier(connection_name, 'connection name')}"


def validate_private_key(pem):
    pem = (pem or "").strip()
    if not (pem.startswith("-----BEGIN") and pem.endswith("PRIVATE KEY-----")):
        raise core.ConfigError("Paste the whole PEM private key, BEGIN and END lines included.")
    return pem


def validate_connection(values, app):
    """A GLACIER_CONNECTIONS row from the form's values."""
    name = core.identifier(_text(values.get("CONNECTION_NAME")), "connection name")
    url = _text(values.get("MY_DOMAIN_URL")) or ""
    if not MY_DOMAIN_URL.match(url):
        raise core.ConfigError(
            "My Domain URL must look like https://<domain>.my.salesforce.com "
            "or https://<domain>.sandbox.my.salesforce.com")
    api_version = _text(values.get("API_VERSION")) or "64.0"
    if not API_VERSION.match(api_version):
        raise core.ConfigError(f"Invalid API version: {api_version!r} (use e.g. 64.0)")
    for column in ("CLIENT_ID", "USERNAME"):
        if _blank(values.get(column)):
            raise core.ConfigError(f"{column.replace('_', ' ').title()} is required")
    return {
        "CONNECTION_NAME": name,
        "MY_DOMAIN_URL": url.rstrip("/"),
        "CLIENT_ID": _text(values["CLIENT_ID"]),
        "USERNAME": _text(values["USERNAME"]),
        "API_VERSION": api_version,
        "SECRET_NAME": _text(values.get("SECRET_NAME")) or secret_name(app, name),
        "DESCRIPTION": _text(values.get("DESCRIPTION")),
    }


def create_secret_sql(secret, pem):
    """CREATE SECRET for a private key. Snowflake masks the value in query history."""
    return (f"CREATE OR REPLACE SECRET {secret}\n"
            "  TYPE = GENERIC_STRING\n"
            f"  SECRET_STRING = {core.sql_string(validate_private_key(pem))}")


# --- Jobs -------------------------------------------------------------------------

def validate_job(values, app, owner_role):
    """A GLACIER_JOBS row from the form's values, checked the way a run and its task will."""
    sobject = core.sf_name(_text(values.get("SOBJECT")), "object name")
    fields = values.get("FIELDS")
    if fields is not None:
        fields = [core.sf_name(f, "field name") for f in fields]
        if not fields:
            raise core.ConfigError("Choose at least one field, or use every exportable field.")
    table = _text(values.get("TARGET_TABLE"))
    delete_mode = (_text(values.get("DELETE_MODE")) or "flag").lower()
    if delete_mode not in core.DELETE_MODES:
        raise core.ConfigError(f"Unknown delete mode {delete_mode!r}")
    try:
        reconcile_days = values.get("RECONCILE_AFTER_DAYS")
        reconcile_days = 14 if _blank(reconcile_days) else int(reconcile_days)
    except (TypeError, ValueError):
        raise core.ConfigError("Reconcile after days must be a whole number") from None
    if reconcile_days < 1:
        raise core.ConfigError("Reconcile after days must be at least 1")
    user = _text(values.get("RUN_AS_USER"))
    role = _text(values.get("RUN_AS_ROLE"))

    job = {
        "JOB_NAME": core.identifier(_text(values.get("JOB_NAME")), "job name"),
        "CONNECTION_NAME": core.identifier(_text(values.get("CONNECTION_NAME")), "connection name"),
        "SOBJECT": sobject,
        "FIELDS": fields,
        "TARGET_DATABASE": core.identifier(_text(values.get("TARGET_DATABASE")), "target database"),
        "TARGET_SCHEMA": core.identifier(_text(values.get("TARGET_SCHEMA")), "target schema"),
        "TARGET_TABLE": core.table_identifier(table) if table else None,
        "DELETE_MODE": delete_mode,
        "RECONCILE_AFTER_DAYS": reconcile_days,
        "TRACK_HISTORY": bool(values.get("TRACK_HISTORY")),
        "RUN_AS_USER": user.upper() if user else None,
        "RUN_AS_ROLE": core.identifier(role, "role") if role else None,
        "WAREHOUSE": core.identifier(_text(values.get("WAREHOUSE")), "warehouse"),
        "SCHEDULE": _text(values.get("SCHEDULE")),
        "ENABLED": bool(values.get("ENABLED", True)),
    }
    # The task DDL checks the schedule, run-as user, and role exactly as saving the task will
    core.task_ddl(app, owner_role, job)
    return job


def target_table(job):
    """The table a job loads, as DB.SCHEMA.TABLE."""
    return core.table_ref(job["TARGET_DATABASE"], job["TARGET_SCHEMA"],
                          job.get("TARGET_TABLE") or core.default_table_name(job["SOBJECT"]))


# --- Saving -------------------------------------------------------------------------

def _cast(column, sql_type):
    if sql_type == "ARRAY":
        return f"AS_ARRAY(s.v:{column})"  # NULL for a JSON null
    return f"s.v:{column}::{sql_type}"


def merge_sql(table, key, columns):
    """MERGE one row, given as PARSE_JSON(?), into table on its key column."""
    names = [c for c, _ in columns]
    values = [_cast(c, t) for c, t in columns]
    updates = ",\n    ".join(f"{c} = {v}" for c, v in zip(names, values) if c != key)
    return (f"MERGE INTO {table} t\n"
            "USING (SELECT PARSE_JSON(?) AS v) s\n"
            f"ON t.{key} = s.v:{key}::VARCHAR\n"
            "WHEN MATCHED THEN UPDATE SET\n"
            f"    {updates},\n"
            "    UPDATED_AT = CURRENT_TIMESTAMP()\n"
            f"WHEN NOT MATCHED THEN INSERT ({', '.join(names)})\n"
            f"  VALUES ({', '.join(values)})")


def save_job_sql(app):
    return merge_sql(f"{app}.GLACIER_JOBS", "JOB_NAME", JOB_COLUMNS)


def save_connection_sql(app):
    return merge_sql(f"{app}.GLACIER_CONNECTIONS", "CONNECTION_NAME", CONNECTION_COLUMNS)


def row_param(row):
    """The single bind parameter for merge_sql."""
    return [json.dumps(row)]
