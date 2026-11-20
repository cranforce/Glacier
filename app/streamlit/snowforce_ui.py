"""Shared helpers for the Snowforce app pages: the Snowflake session, the app
schema, and reads and calls against the app's tables and procedures.

In Streamlit in Snowflake the app runs with its owner's rights (the app role),
in the schema it was created in. Locally, run_local.sh sets the SNOWFORCE_*
environment variables from app.env.
"""
import json
import os
import sys

import streamlit as st

# Locally, snowforce_core comes from app/src; deploy.sh uploads a copy next to this file.
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if os.path.isdir(_SRC):
    sys.path.insert(0, _SRC)

import snowforce_core as core  # noqa: E402


@st.cache_resource
def session():
    if "SNOWFORCE_CONNECTION" not in os.environ:  # in Snowflake
        try:
            from snowflake.snowpark.context import get_active_session
            return get_active_session()
        except Exception:
            return st.connection("snowflake").session()
    from snowflake.snowpark import Session
    s = Session.builder.config("connection_name", os.environ["SNOWFORCE_CONNECTION"]).create()
    if os.environ.get("SNOWFORCE_ROLE"):
        s.use_role(os.environ["SNOWFORCE_ROLE"])
    if os.environ.get("SNOWFORCE_WAREHOUSE"):
        s.use_warehouse(os.environ["SNOWFORCE_WAREHOUSE"])
    return s


def rows(sql, params=None):
    return [r.as_dict() for r in session().sql(sql, params=params).collect()]


def one(sql, params=None):
    result = rows(sql, params)
    return result[0] if result else None


@st.cache_resource
def context():
    """The app schema (DB.SCHEMA), the role the app runs as, and its warehouse."""
    row = one("SELECT CURRENT_DATABASE() AS DB, CURRENT_SCHEMA() AS SCH, "
              "CURRENT_ROLE() AS ROLE, CURRENT_WAREHOUSE() AS WH")
    app = os.environ.get("SNOWFORCE_APP") or f"{row['DB']}.{row['SCH']}"
    return {"app": app.upper(), "role": row["ROLE"], "warehouse": row["WH"]}


def app():
    return context()["app"]


def call(procedure, *args):
    """CALL an app procedure; VARIANT results come back parsed."""
    result = session().call(f"{app()}.{procedure}", *args)
    return json.loads(result) if isinstance(result, str) else result


def show_error(e):
    st.error(getattr(e, "message", None) or str(e))


def flash(message, kind="success"):
    """Show a message after the next st.rerun(), e.g. once a save has refreshed the page."""
    st.session_state.setdefault("_flash", []).append((kind, message))


def show_flash():
    for kind, message in st.session_state.pop("_flash", []):
        getattr(st, kind)(message)


# --- Reads ------------------------------------------------------------------------

def connections():
    return rows(f"SELECT * FROM {app()}.SNOWFORCE_CONNECTIONS ORDER BY CONNECTION_NAME")


def jobs():
    return rows(f"SELECT * FROM {app()}.SNOWFORCE_JOBS ORDER BY JOB_NAME")


def job(job_name):
    row = one(f"SELECT * FROM {app()}.SNOWFORCE_JOBS WHERE JOB_NAME = ?", [job_name])
    if row and row.get("FIELDS"):
        row["FIELDS"] = json.loads(row["FIELDS"])
    return row


def fields(connection_name, sobject):
    """Captured describe output for an object; empty until SNOWFORCE_DESCRIBE has run."""
    return rows(f"SELECT * FROM {app()}.SNOWFORCE_FIELDS WHERE CONNECTION_NAME = ? "
                "AND UPPER(SOBJECT) = UPPER(?) ORDER BY POSITION", [connection_name, sobject])


def runs(job_name=None, limit=100):
    where = "WHERE JOB_NAME = ?" if job_name else ""
    return rows(f"SELECT * FROM {app()}.SNOWFORCE_RUNS {where} "
                f"ORDER BY STARTED_AT DESC LIMIT {int(limit)}", [job_name] if job_name else None)


def task_states():
    """{job name: task state} for every job task that exists."""
    prefix = "SNOWFORCE_JOB_"
    tasks = rows(f"SHOW TASKS LIKE '{prefix}%' IN SCHEMA {app()}")
    return {t["name"][len(prefix):]: t["state"] for t in tasks}


def salesforce_ready():
    """Whether SNOWFORCE_REFRESH_CONNECTIONS has created the Salesforce procedures."""
    return bool(rows(f"SHOW PROCEDURES LIKE 'SNOWFORCE_EXTRACT' IN SCHEMA {app()}"))


@st.cache_data(ttl=3600, show_spinner="Loading objects from Salesforce...")
def list_objects(connection_name):
    return sorted(call("SNOWFORCE_LIST_OBJECTS", connection_name), key=lambda o: o["name"].lower())


# --- Running jobs -----------------------------------------------------------------

def start_job(job_name, mode="sync"):
    """Run a job's task now, so it runs as the job's user, role, and warehouse.

    The app can't run as another user itself; a full reload or reconcile is
    queued with NEXT_RUN_MODE, which the run picks up and clears.
    """
    name = core.identifier(job_name, "job name")
    if mode != "sync":
        session().sql(f"UPDATE {app()}.SNOWFORCE_JOBS SET NEXT_RUN_MODE = ? WHERE JOB_NAME = ?",
                      params=[mode, name]).collect()
    session().sql(f"EXECUTE TASK {core.task_name(app(), name)}").collect()
