"""Stored procedure handlers for Glacier.

Two kinds of procedure, kept apart on purpose:

* Owner's rights (GLACIER_EXTRACT, _DESCRIBE, _LIST_OBJECTS, _TEST_CONNECTION,
  _SAVE_JOB_TASK, _REFRESH_CONNECTIONS) hold the Salesforce secrets and manage
  app objects. They never write destination tables.
* Caller's rights (GLACIER_RUN_JOB) runs as the job's role, so a job can only
  read and write what that role can.

Every handler takes the app schema (DB.SCHEMA) as its last argument; the
procedure DDL passes it as a default.
"""
import io
import json
import uuid
from datetime import datetime, timezone

import glacier_core as core
from sforce_api import Salesforce

FILE_FORMAT = "GLACIER_CSV"


# --- Helpers ------------------------------------------------------------------

def _rows(session, sql, params=None):
    return [r.as_dict() for r in session.sql(sql, params=params).collect()]


def _one(session, sql, params=None):
    rows = _rows(session, sql, params)
    return rows[0] if rows else None


def _variant(value):
    """session.call returns VARIANT results as JSON text."""
    return json.loads(value) if isinstance(value, str) else value


def _connection(session, app, connection_name):
    conn = _one(session, f"SELECT * FROM {app}.GLACIER_CONNECTIONS WHERE CONNECTION_NAME = ?",
                [core.identifier(connection_name, "connection name")])
    if conn is None:
        raise core.ConfigError(f"No connection named {connection_name!r}")
    return conn


def _salesforce(session, app, connection_name):
    import _snowflake  # only available inside Snowflake
    conn = _connection(session, app, connection_name)
    try:
        private_key = _snowflake.get_generic_secret_string(core.secret_alias(conn["CONNECTION_NAME"]))
    except Exception as e:
        raise core.ConfigError(
            f"Connection {conn['CONNECTION_NAME']} has no usable secret; "
            f"run CALL {app}.GLACIER_REFRESH_CONNECTIONS() after adding it ({e})") from e
    for column in ("CLIENT_ID", "USERNAME"):
        if not conn.get(column):
            raise core.ConfigError(f"Connection {conn['CONNECTION_NAME']} has no {column}")
    return Salesforce(conn["MY_DOMAIN_URL"], conn["CLIENT_ID"], conn["USERNAME"], private_key,
                      conn["API_VERSION"])


# --- Owner's rights: Salesforce access ------------------------------------------

def test_connection(session, connection_name, app):
    sf = _salesforce(session, app, connection_name)
    objects = sf.list_objects()
    return {"connection": connection_name.upper(), "instance_url": sf.instance_url,
            "queryable_objects": sum(1 for o in objects if o.get("queryable"))}


def list_objects(session, connection_name, app):
    sf = _salesforce(session, app, connection_name)
    return [{"name": o["name"], "label": o.get("label"), "custom": o.get("custom")}
            for o in sf.list_objects() if o.get("queryable")]


def describe(session, connection_name, sobject, app):
    """Capture an object's fields into GLACIER_FIELDS (replaces schema/<Object>.*)."""
    conn = core.identifier(connection_name, "connection name")
    result = _salesforce(session, app, conn).describe(core.sf_name(sobject, "object name"))
    name = result["name"]  # Salesforce's own casing
    fields = [{
        "position": i,
        "name": f["name"],
        "label": f.get("label"),
        "type": f.get("type"),
        "length": f.get("length"),
        "precision": f.get("precision"),
        "scale": f.get("scale"),
        "nillable": f.get("nillable"),
        "calculated": f.get("calculated"),
        "custom": f.get("custom"),
        "referenceTo": "|".join(f.get("referenceTo") or []),
        "snowflakeType": core.snowflake_type(f),
        "exportable": core.exportable(f),
    } for i, f in enumerate(result["fields"])]

    session.sql(f"DELETE FROM {app}.GLACIER_FIELDS WHERE CONNECTION_NAME = ? AND SOBJECT = ?",
                params=[conn, name]).collect()
    session.sql(f"""
        INSERT INTO {app}.GLACIER_FIELDS
          (CONNECTION_NAME, SOBJECT, POSITION, FIELD_NAME, LABEL, SF_TYPE, LENGTH, PRECISION, SCALE,
           NILLABLE, CALCULATED, CUSTOM, REFERENCE_TO, SNOWFLAKE_TYPE, EXPORTABLE, CAPTURED_AT)
        SELECT ?, ?, f.value:position::INT, f.value:name::VARCHAR, f.value:label::VARCHAR,
               f.value:type::VARCHAR, f.value:length::INT, f.value:precision::INT, f.value:scale::INT,
               f.value:nillable::BOOLEAN, f.value:calculated::BOOLEAN, f.value:custom::BOOLEAN,
               f.value:referenceTo::VARCHAR, f.value:snowflakeType::VARCHAR,
               f.value:exportable::BOOLEAN, CURRENT_TIMESTAMP()
        FROM TABLE(FLATTEN(INPUT => PARSE_JSON(?))) f""",
                params=[conn, name, json.dumps(fields)]).collect()
    return {"sobject": name, "fields": len(fields),
            "exportable": sum(1 for f in fields if f["exportable"])}


def extract(session, connection_name, soql, all_rows, run_id, app):
    """Run a Bulk API query and write each results page to the exports stage."""
    sf = _salesforce(session, app, connection_name)
    stage = f"@{app}.GLACIER_EXPORTS/{core.identifier('R' + run_id.replace('-', ''), 'run id')}"
    columns, rows, files = None, 0, 0
    for page, count in sf.bulk_query(soql, all_rows=bool(all_rows)):
        if count == 0:
            continue
        first, _, rest = page.partition(b"\n")
        header = core.normalize_header(first.decode("utf-8"))
        columns = columns or header
        data = ",".join(header).encode("utf-8") + b"\n" + rest
        session.file.put_stream(io.BytesIO(data), f"{stage}/part_{files:05d}.csv",
                                auto_compress=True, overwrite=True)
        rows += count
        files += 1
    return {"columns": columns or [], "rows": rows, "files": files, "stage_path": stage + "/"}


# --- Owner's rights: app management ---------------------------------------------

def refresh_connections(session, integration, app):
    """Re-create the Salesforce procedures with every connection's secret."""
    integration = core.identifier(integration, "integration")
    if not _rows(session, f"SHOW INTEGRATIONS LIKE {core.sql_string(integration)}"):
        return {"skipped": f"Integration {integration} doesn't exist yet, or this role can't use it. "
                           "A Snowflake admin needs to run app/sql/20_admin_integration.sql "
                           "(print it with: app/deploy.sh admin-sql integration)."}
    connections = [(c["CONNECTION_NAME"], c["SECRET_NAME"]) for c in
                   _rows(session, f"SELECT CONNECTION_NAME, SECRET_NAME FROM {app}.GLACIER_CONNECTIONS "
                                  "ORDER BY CONNECTION_NAME")]
    for statement in core.secret_procedure_ddl(app, integration, connections):
        session.sql(statement).collect()
    return {"connections": [name for name, _ in connections],
            "procedures": [p[0] for p in core.SECRET_PROCEDURES]}


def save_job_task(session, job_name, app):
    """Create or replace a job's task, resumed only when the job is enabled and scheduled."""
    job = _job(session, app, job_name)
    owner_role = _one(session, "SELECT CURRENT_ROLE() AS R")["R"]
    session.sql(core.task_ddl(app, owner_role, job)).collect()
    task = core.task_name(app, job["JOB_NAME"])
    resume = bool(job.get("ENABLED")) and bool(job.get("SCHEDULE"))
    session.sql(f"ALTER TASK {task} {'RESUME' if resume else 'SUSPEND'}").collect()
    return {"task": task, "state": "started" if resume else "suspended"}


# --- Caller's rights: running a job -----------------------------------------------

def _job(session, app, job_name):
    job = _one(session, f"SELECT * FROM {app}.GLACIER_JOBS WHERE JOB_NAME = ?",
               [core.identifier(job_name, "job name")])
    if job is None:
        raise core.ConfigError(f"No job named {job_name!r}")
    return job


class _Run:
    """One row in GLACIER_RUNS."""

    def __init__(self, session, app, job_name, mode):
        self.session, self.app, self.run_id = session, app, str(uuid.uuid4())
        session.sql(f"""
            INSERT INTO {app}.GLACIER_RUNS (RUN_ID, JOB_NAME, MODE, STATUS, STARTED_AT, RUN_USER, RUN_ROLE)
            SELECT ?, ?, ?, 'RUNNING', CURRENT_TIMESTAMP(), CURRENT_USER(), CURRENT_ROLE()""",
                    params=[self.run_id, job_name, mode]).collect()

    def finish(self, status, mode=None, message=None, watermark=None, soql=None,
               exported=None, inserted=None, updated=None, deleted=None):
        # Values go in as one JSON object: Snowpark binds a Python None as the
        # text 'None', but a JSON null becomes SQL NULL.
        values = {"run_id": self.run_id, "status": status, "mode": mode, "message": message,
                  "watermark": watermark, "soql": soql, "exported": exported, "inserted": inserted,
                  "updated": updated, "deleted": deleted}
        self.session.sql(f"""
            UPDATE {self.app}.GLACIER_RUNS r SET
              STATUS = v.j:status::VARCHAR,
              FINISHED_AT = CURRENT_TIMESTAMP(),
              MODE = COALESCE(v.j:mode::VARCHAR, r.MODE),
              MESSAGE = v.j:message::VARCHAR,
              WATERMARK = v.j:watermark::VARCHAR,
              SOQL = v.j:soql::VARCHAR,
              ROWS_EXPORTED = v.j:exported::NUMBER,
              ROWS_INSERTED = v.j:inserted::NUMBER,
              ROWS_UPDATED = v.j:updated::NUMBER,
              ROWS_DELETED = v.j:deleted::NUMBER
            FROM (SELECT PARSE_JSON(?) AS j) v
            WHERE r.RUN_ID = v.j:run_id::VARCHAR""", params=[json.dumps(values)]).collect()


def _fields(session, app, connection_name, sobject):
    """The object's captured fields, describing it first if it never has been."""
    sql = (f"SELECT * FROM {app}.GLACIER_FIELDS WHERE CONNECTION_NAME = ? "
           "AND UPPER(SOBJECT) = UPPER(?) ORDER BY POSITION")
    fields = _rows(session, sql, [connection_name, sobject])
    if not fields:
        session.call(f"{app}.GLACIER_DESCRIBE", connection_name, sobject)
        fields = _rows(session, sql, [connection_name, sobject])
    return fields


def _target_state(session, db, schema, table):
    """None if the table doesn't exist, else SYSTEMMODSTAMP's data type ('' if it has none)."""
    columns = _rows(session, f"SELECT COLUMN_NAME, DATA_TYPE FROM {db}.INFORMATION_SCHEMA.COLUMNS "
                             "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?", [schema, table])
    if not columns:
        return None
    return next((c["DATA_TYPE"] for c in columns if c["COLUMN_NAME"] == "SYSTEMMODSTAMP"), "")


def _execute(session, statements):
    return [_rows(session, s) for s in statements]


def _last_success(session, app, job_name):
    row = _one(session, f"""
        SELECT MAX(STARTED_AT) AS T FROM {app}.GLACIER_RUNS
        WHERE JOB_NAME = ? AND STATUS = 'SUCCEEDED' AND MODE IN ('sync', 'full')""", [job_name])
    t = row and row["T"]
    if t is not None and t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t


def run_job(session, job_name, mode, app):
    """Run a job: a SystemModstamp delta (sync), a full reload (full), or a delete reconcile.

    mode 'auto' uses the job's NEXT_RUN_MODE (set by the app for one-off full
    reloads and reconciles) and falls back to sync.
    """
    job = _job(session, app, job_name)
    name = job["JOB_NAME"]
    mode = (mode or "auto").lower()
    if mode == "auto":
        mode = (job.get("NEXT_RUN_MODE") or "sync").lower()
        if job.get("NEXT_RUN_MODE"):
            session.sql(f"UPDATE {app}.GLACIER_JOBS SET NEXT_RUN_MODE = NULL WHERE JOB_NAME = ?",
                        params=[name]).collect()
    if mode not in core.RUN_MODES:
        raise core.ConfigError(f"Unknown mode {mode!r}; use one of {', '.join(core.RUN_MODES)}")

    run = _Run(session, app, name, mode)
    try:
        result = _run(session, app, job, mode, run.run_id)
    except Exception as e:
        run.finish("FAILED", message=str(e)[:10000])
        raise
    finally:
        # COPY purges loaded files; this clears any left by a failure. Best effort,
        # so it can't hide the real error.
        try:
            session.sql(f"REMOVE @{app}.GLACIER_EXPORTS/R{run.run_id.replace('-', '')}/").collect()
        except Exception:
            pass
    run.finish("SUCCEEDED", **{k: result.get(k) for k in
                               ("mode", "message", "watermark", "soql", "exported", "inserted",
                                "updated", "deleted")})
    return {"run_id": run.run_id, **result}


def _run(session, app, job, mode, run_id):
    conn = core.identifier(job["CONNECTION_NAME"], "connection name")
    sobject = core.sf_name(job["SOBJECT"], "object name")
    db = core.identifier(job["TARGET_DATABASE"], "target database")
    schema = core.identifier(job["TARGET_SCHEMA"], "target schema")
    table = core.table_identifier(job.get("TARGET_TABLE") or core.default_table_name(sobject))
    fq_table = core.table_ref(db, schema, table)
    staging = {suffix: core.table_ref(db, schema, table + suffix) for suffix in ("__DELTA", "__IDS")}
    history = (core.table_ref(db, schema, table + core.HISTORY_SUFFIX)
               if job.get("TRACK_HISTORY") else None)
    delete_mode = (job.get("DELETE_MODE") or "flag").lower()
    if delete_mode not in core.DELETE_MODES:
        raise core.ConfigError(f"Unknown delete mode {delete_mode!r}")
    file_format = f"{app}.{FILE_FORMAT}"

    fields = _fields(session, app, conn, sobject)
    by_name = {f["FIELD_NAME"].lower(): f for f in fields}
    types = {core.column_name(f["FIELD_NAME"]): f["SNOWFLAKE_TYPE"] for f in fields}
    if delete_mode != "off" and "isdeleted" not in by_name:
        raise core.ConfigError(f"{sobject} has no IsDeleted field; set the job's delete mode to off")
    chosen = json.loads(job["FIELDS"]) if job.get("FIELDS") else None
    if chosen:
        unknown = [f for f in chosen if f.lower() not in by_name]
        if unknown:
            raise core.ConfigError(f"Fields not on {sobject}: {', '.join(unknown)}. "
                                   f"If they're new, CALL {app}.GLACIER_DESCRIBE('{conn}', '{sobject}').")
        selected = [by_name[f.lower()]["FIELD_NAME"] for f in chosen]
    else:
        selected = [f["FIELD_NAME"] for f in fields if f["EXPORTABLE"]]

    def export(soql, all_rows):
        return _variant(session.call(f"{app}.GLACIER_EXTRACT", conn, soql, all_rows, run_id))

    if mode == "reconcile":
        return _reconcile(session, sobject, fq_table, staging["__IDS"], file_format, delete_mode, export,
                          history, types)

    # Delta from the table's watermark, or a full load when there's nothing to go on
    since, note = None, None
    if mode == "sync":
        data_type = _target_state(session, db, schema, table)
        if data_type is None:
            note = "Table didn't exist; did a full load."
        elif data_type == "":
            note = "Table had no SYSTEMMODSTAMP column; rebuilt it with a full load."
        else:
            since = _one(session, core.watermark_sql(fq_table, data_type))["WATERMARK"]
            if since is None:
                note = "Table was empty; did a full load."

    extras = ("IsDeleted",) if delete_mode != "off" else ()
    # Deleted records come from the recycle bin (queryAll). A full load in remove
    # mode skips them, since they'd only be deleted again.
    all_rows = delete_mode != "off" and (since is not None or delete_mode == "flag")
    soql = core.build_soql(sobject, selected, since, extras)
    exported = export(soql, all_rows)
    columns = exported["columns"] or core.normalize_header(soql[len("SELECT "):soql.index(" FROM ")])
    result = {"soql": soql, "watermark": since, "exported": exported["rows"], "target": fq_table}

    if since is not None:
        result["mode"] = "sync"
        if exported["rows"] == 0:
            result["message"] = f"No changes since {since}."
        else:
            out = _execute(session, core.delta_statements(
                fq_table, staging["__DELTA"], columns, types, exported["stage_path"], file_format,
                delete_mode, history))
            # With history, the statements end INSERT (versions), MERGE, COMMIT
            merged = out[-2 if history else -1][0]
            result.update(inserted=merged.get("number of rows inserted"),
                          updated=merged.get("number of rows updated"),
                          deleted=merged.get("number of rows deleted"))
            if history:
                result["message"] = _versions_message(out[-3])
    else:
        result["mode"] = "full"
        result["message"] = note
        out = _execute(session, core.full_load_statements(
            fq_table, columns, types, exported["stage_path"], file_format, exported["rows"] > 0, history))
        result["inserted"] = sum(r.get("rows_loaded") or 0 for r in out[1]) if exported["rows"] else 0
        if history and exported["rows"]:
            # The statements end INSERT (versions), COMMIT
            result["message"] = " ".join(filter(None, [note, _versions_message(out[-2])]))

    # Catch deletes that have left the recycle bin since the last successful sync
    if mode == "sync" and delete_mode != "off" and core.reconcile_due(
            _last_success(session, app, job["JOB_NAME"]), datetime.now(timezone.utc),
            int(job.get("RECONCILE_AFTER_DAYS") or 14)):
        reconciled = _reconcile(session, sobject, fq_table, staging["__IDS"], file_format, delete_mode,
                                export, history, types)
        result["message"] = " ".join(filter(None, [result.get("message"), reconciled["message"]]))
        if delete_mode == "remove":
            result["deleted"] = (result.get("deleted") or 0) + (reconciled.get("deleted") or 0)
    return result


def _versions_message(insert_result):
    return f"Recorded {insert_result[0].get('number of rows inserted') or 0} history versions."


def _reconcile(session, sobject, fq_table, ids_table, file_format, delete_mode, export,
               history=None, types=None):
    if delete_mode == "off":
        raise core.ConfigError("Deletes are off for this job; nothing to reconcile.")
    exported = export(core.build_ids_soql(sobject), False)
    if exported["rows"] == 0:
        raise core.ConfigError("Salesforce returned no Ids; refusing to reconcile "
                               "(it would flag or remove every row).")
    out = _execute(session, core.reconcile_statements(
        fq_table, ids_table, exported["stage_path"], file_format, delete_mode,
        history, (types or {}).get("SYSTEMMODSTAMP", "VARCHAR")))
    # With history, the flag or remove is followed by COMMIT
    changed = out[-2 if history else -1][0].get("number of rows deleted" if delete_mode == "remove" else "number of rows updated")
    verb = "Removed" if delete_mode == "remove" else "Flagged"
    return {"mode": "reconcile", "exported": exported["rows"], "deleted": changed,
            "message": f"Reconciled against {exported['rows']} current Ids; {verb.lower()} {changed} rows."}
