"""Stored procedure handlers for Glacier.

Two kinds of procedure, kept apart on purpose:

* Owner's rights hold the connection secrets and manage app objects; they never
  write destination tables. Each connector has its own GLACIER_<CONNECTOR>_EXTRACT,
  _DESCRIBE, _LIST_OBJECTS, and _TEST_CONNECTION, with only that connector's
  secrets and integration. GLACIER_SAVE_JOB_TASK and GLACIER_REFRESH_CONNECTIONS
  manage the app.
* Caller's rights (GLACIER_RUN_JOB) runs as the job's role, so a job can only
  read and write what that role can.

Every handler takes the app schema (DB.SCHEMA) as its last argument, and the
connector's handlers take the connector before it; the procedure DDL passes
them as defaults.
"""
import io
import json
import uuid
from datetime import datetime, timezone

import glacier_connectors as connectors
import glacier_core as core

FILE_FORMAT = "GLACIER_CSV"


# --- Helpers ------------------------------------------------------------------

def _rows(session, sql, params=None):
    return [r.as_dict() for r in session.sql(sql, params=params).collect()]


def _one(session, sql, params=None):
    rows = _rows(session, sql, params)
    return rows[0] if rows else None


def _variant(value):
    """session.call and VARIANT columns come back as JSON text."""
    return json.loads(value) if isinstance(value, str) else value


def _connection(session, app, connection_name):
    conn = _one(session, f"SELECT * FROM {app}.GLACIER_CONNECTIONS WHERE CONNECTION_NAME = ?",
                [core.identifier(connection_name, "connection name")])
    if conn is None:
        raise core.ConfigError(f"No connection named {connection_name!r}")
    conn["SETTINGS"] = _variant(conn.get("SETTINGS")) or {}
    return conn


def _source(session, app, connection_name, connector):
    """(connector module, settings, secret) for a connection, inside its connector's procedure."""
    import _snowflake  # only available inside Snowflake
    conn = _connection(session, app, connection_name)
    if conn["CONNECTOR"].lower() != connector.lower():
        raise core.ConfigError(f"Connection {conn['CONNECTION_NAME']} is a {conn['CONNECTOR']} connection, "
                               f"not {connector}")
    try:
        secret = _snowflake.get_generic_secret_string(core.secret_alias(conn["CONNECTION_NAME"]))
    except Exception as e:
        raise core.ConfigError(
            f"Connection {conn['CONNECTION_NAME']} has no usable secret; "
            f"run CALL {app}.GLACIER_REFRESH_CONNECTIONS() after adding it ({e})") from e
    return connectors.get(connector), conn["SETTINGS"], secret


# --- Owner's rights: source access ----------------------------------------------

def test_connection(session, connection_name, connector, app):
    source, settings, secret = _source(session, app, connection_name, connector)
    return {"connection": connection_name.upper(), **source.test(settings, secret)}


def list_objects(session, connection_name, connector, app):
    source, settings, secret = _source(session, app, connection_name, connector)
    return source.list_objects(settings, secret)


def describe(session, connection_name, source_object, connector, app):
    """Capture an object's fields into GLACIER_FIELDS."""
    conn = core.identifier(connection_name, "connection name")
    source, settings, secret = _source(session, app, conn, connector)
    name, fields = source.describe(settings, secret, source.object_name(source_object))

    session.sql(f"DELETE FROM {app}.GLACIER_FIELDS WHERE CONNECTION_NAME = ? AND SOURCE_OBJECT = ?",
                params=[conn, name]).collect()
    session.sql(f"""
        INSERT INTO {app}.GLACIER_FIELDS
          (CONNECTION_NAME, SOURCE_OBJECT, POSITION, FIELD_NAME, LABEL, SOURCE_TYPE, LENGTH, PRECISION,
           SCALE, NILLABLE, CALCULATED, CUSTOM, REFERENCE_TO, SNOWFLAKE_TYPE, EXPORTABLE, CAPTURED_AT)
        SELECT ?, ?, f.value:position::INT, f.value:name::VARCHAR, f.value:label::VARCHAR,
               f.value:sourceType::VARCHAR, f.value:length::INT, f.value:precision::INT, f.value:scale::INT,
               f.value:nillable::BOOLEAN, f.value:calculated::BOOLEAN, f.value:custom::BOOLEAN,
               f.value:referenceTo::VARCHAR, f.value:snowflakeType::VARCHAR,
               f.value:exportable::BOOLEAN, CURRENT_TIMESTAMP()
        FROM TABLE(FLATTEN(INPUT => PARSE_JSON(?))) f""",
                params=[conn, name, json.dumps(fields)]).collect()
    return {"object": name, "fields": len(fields),
            "exportable": sum(1 for f in fields if f["exportable"])}


def extract(session, connection_name, query, include_deleted, run_id, api, connector, app):
    """Run an export through api (NULL for the connector's default) and write each page to the
    exports stage."""
    source, settings, secret = _source(session, app, connection_name, connector)
    stage = f"@{app}.GLACIER_EXPORTS/{core.identifier('R' + run_id.replace('-', ''), 'run id')}"
    columns, rows, files = None, 0, 0
    for page, count in source.export(settings, secret, query, bool(include_deleted), api):
        if count == 0:
            continue
        first, _, rest = page.partition(b"\n")
        header = core.normalize_header(first.decode("utf-8"), source.column_name)
        columns = columns or header
        data = ",".join(header).encode("utf-8") + b"\n" + rest
        session.file.put_stream(io.BytesIO(data), f"{stage}/part_{files:05d}.csv",
                                auto_compress=True, overwrite=True)
        rows += count
        files += 1
    return {"columns": columns or [], "rows": rows, "files": files, "stage_path": stage + "/"}


# --- Owner's rights: app management ---------------------------------------------

def refresh_connections(session, app):
    """Re-create each connector's procedures with its connections' secrets.

    A connector whose integration doesn't exist yet, or that this role can't
    use, is skipped.
    """
    rows = _rows(session, f"SELECT CONNECTION_NAME, CONNECTOR, SECRET_NAME FROM {app}.GLACIER_CONNECTIONS "
                          "ORDER BY CONNECTION_NAME")
    refreshed, skipped = {}, []
    for name in connectors.CONNECTORS:
        integration = core.integration_name(name)
        if not _rows(session, f"SHOW INTEGRATIONS LIKE {core.sql_string(integration)}"):
            skipped.append(f"{name} (no {integration})")
            continue
        connections = [(r["CONNECTION_NAME"], r["SECRET_NAME"]) for r in rows
                       if r["CONNECTOR"].lower() == name]
        for statement in core.secret_procedure_ddl(app, name, integration, connections):
            session.sql(statement).collect()
        refreshed[name] = [c for c, _ in connections]
    result = {"connectors": refreshed}
    if skipped:
        result["skipped"] = (
            f"Skipped {', '.join(skipped)}: the integration doesn't exist yet, or this role can't use it. "
            "A Snowflake admin needs to run the output of app/deploy.sh admin-sql integration <connector>.")
    return result


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

    def finish(self, status, mode=None, message=None, watermark=None, query=None,
               exported=None, inserted=None, updated=None, deleted=None):
        # Values go in as one JSON object: Snowpark binds a Python None as the
        # text 'None', but a JSON null becomes SQL NULL.
        values = {"run_id": self.run_id, "status": status, "mode": mode, "message": message,
                  "watermark": watermark, "query": query, "exported": exported, "inserted": inserted,
                  "updated": updated, "deleted": deleted}
        self.session.sql(f"""
            UPDATE {self.app}.GLACIER_RUNS r SET
              STATUS = v.j:status::VARCHAR,
              FINISHED_AT = CURRENT_TIMESTAMP(),
              MODE = COALESCE(v.j:mode::VARCHAR, r.MODE),
              MESSAGE = v.j:message::VARCHAR,
              WATERMARK = v.j:watermark::VARCHAR,
              SOURCE_QUERY = v.j:query::VARCHAR,
              ROWS_EXPORTED = v.j:exported::NUMBER,
              ROWS_INSERTED = v.j:inserted::NUMBER,
              ROWS_UPDATED = v.j:updated::NUMBER,
              ROWS_DELETED = v.j:deleted::NUMBER
            FROM (SELECT PARSE_JSON(?) AS j) v
            WHERE r.RUN_ID = v.j:run_id::VARCHAR""", params=[json.dumps(values)]).collect()


def _fields(session, app, connector, connection_name, source_object):
    """The object's captured fields, describing it first if it never has been."""
    sql = (f"SELECT * FROM {app}.GLACIER_FIELDS WHERE CONNECTION_NAME = ? "
           "AND UPPER(SOURCE_OBJECT) = UPPER(?) ORDER BY POSITION")
    fields = _rows(session, sql, [connection_name, source_object])
    if not fields:
        session.call(f"{app}.{core.procedure_name(connector, 'DESCRIBE')}", connection_name, source_object)
        fields = _rows(session, sql, [connection_name, source_object])
    return fields


def _target_state(session, db, schema, table, change_col):
    """None if the table doesn't exist, else the change column's data type ('' if it has none)."""
    columns = _rows(session, f"SELECT COLUMN_NAME, DATA_TYPE FROM {db}.INFORMATION_SCHEMA.COLUMNS "
                             "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?", [schema, table])
    if not columns:
        return None
    return next((c["DATA_TYPE"] for c in columns if c["COLUMN_NAME"] == change_col), "")


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
    """Run a job: a delta from the change column (sync), a full reload (full), or a delete reconcile.

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
                               ("mode", "message", "watermark", "query", "exported", "inserted",
                                "updated", "deleted")})
    return {"run_id": run.run_id, **result}


def _run(session, app, job, mode, run_id):
    conn = core.identifier(job["CONNECTION_NAME"], "connection name")
    connector = _connection(session, app, conn)["CONNECTOR"].lower()
    source = connectors.get(connector)
    source_object = source.object_name(job["SOURCE_OBJECT"])
    keys = source.keys(source_object)
    db = core.identifier(job["TARGET_DATABASE"], "target database")
    schema = core.identifier(job["TARGET_SCHEMA"], "target schema")
    table = core.table_identifier(job.get("TARGET_TABLE") or source.table_name(source_object))
    fq_table = core.table_ref(db, schema, table)
    staging = {suffix: core.table_ref(db, schema, table + suffix) for suffix in ("__DELTA", "__IDS")}
    history = (core.table_ref(db, schema, table + core.HISTORY_SUFFIX)
               if job.get("TRACK_HISTORY") else None)
    delete_mode = (job.get("DELETE_MODE") or "flag").lower()
    if delete_mode not in source.DELETE_MODES:
        raise core.ConfigError(f"Unknown delete mode {delete_mode!r} for {source.LABEL}; "
                               f"use one of {', '.join(source.DELETE_MODES)}")
    api = core.source_api(job.get("SOURCE_API"), source.APIS)
    file_format = f"{app}.{FILE_FORMAT}"

    fields = _fields(session, app, connector, conn, source_object)
    by_name = {f["FIELD_NAME"].lower(): f for f in fields}
    types = {source.column_name(f["FIELD_NAME"]): f["SNOWFLAKE_TYPE"] for f in fields}
    if delete_mode != "off" and (keys.deleted or "").lower() not in by_name:
        raise core.ConfigError(f"{source_object} has no {keys.deleted or 'deleted'} field; "
                               "set the job's delete mode to off")
    chosen = json.loads(job["FIELDS"]) if job.get("FIELDS") else None
    if chosen:
        unknown = [f for f in chosen if f.lower() not in by_name]
        if unknown:
            describe_proc = f"{app}.{core.procedure_name(connector, 'DESCRIBE')}"
            raise core.ConfigError(f"Fields not on {source_object}: {', '.join(unknown)}. "
                                   f"If they're new, CALL {describe_proc}('{conn}', '{source_object}').")
        selected = [by_name[f.lower()]["FIELD_NAME"] for f in chosen]
    else:
        selected = [f["FIELD_NAME"] for f in fields if f["EXPORTABLE"]]

    def export(query, include_deleted):
        return _variant(session.call(f"{app}.{core.procedure_name(connector, 'EXTRACT')}",
                                     conn, query, include_deleted, run_id, api))

    def reconcile():
        return _reconcile(session, source, source_object, keys, fq_table, staging["__IDS"], file_format,
                          delete_mode, export, history, types)

    if mode == "reconcile":
        return reconcile()

    # Delta from the table's watermark, or a full load when there's nothing to go on
    since, note = None, None
    if mode == "sync":
        data_type = _target_state(session, db, schema, table, keys.change_col)
        if data_type is None:
            note = "Table didn't exist; did a full load."
        elif data_type == "":
            note = f"Table had no {keys.change_col} column; rebuilt it with a full load."
        else:
            since = _one(session, core.watermark_sql(fq_table, data_type, keys))["WATERMARK"]
            if since is None:
                note = "Table was empty; did a full load."

    extras = (keys.deleted,) if delete_mode != "off" else ()
    # Deleted records come from the source's deleted records (Salesforce's recycle
    # bin). A full load in remove mode skips them, since they'd only be deleted again.
    include_deleted = delete_mode != "off" and (since is not None or delete_mode == "flag")
    query = source.build_query(source_object, selected, since, extras)
    exported = export(query, include_deleted)
    columns = exported["columns"] or source.query_columns(query)
    result = {"query": query, "api": api, "watermark": since, "exported": exported["rows"],
              "target": fq_table}

    if since is not None:
        result["mode"] = "sync"
        if exported["rows"] == 0:
            result["message"] = f"No changes since {since}."
        else:
            out = _execute(session, core.delta_statements(
                fq_table, staging["__DELTA"], columns, types, exported["stage_path"], file_format,
                delete_mode, keys, history))
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
            fq_table, columns, types, exported["stage_path"], file_format, exported["rows"] > 0, keys,
            history))
        result["inserted"] = sum(r.get("rows_loaded") or 0 for r in out[1]) if exported["rows"] else 0
        if history and exported["rows"]:
            # The statements end INSERT (versions), COMMIT
            result["message"] = " ".join(filter(None, [note, _versions_message(out[-2])]))

    # Catch deletes the delta can't see, e.g. ones that have left Salesforce's recycle bin
    if mode == "sync" and delete_mode != "off" and core.reconcile_due(
            _last_success(session, app, job["JOB_NAME"]), datetime.now(timezone.utc),
            int(job.get("RECONCILE_AFTER_DAYS") or 14)):
        reconciled = reconcile()
        result["message"] = " ".join(filter(None, [result.get("message"), reconciled["message"]]))
        if delete_mode == "remove":
            result["deleted"] = (result.get("deleted") or 0) + (reconciled.get("deleted") or 0)
    return result


def _versions_message(insert_result):
    return f"Recorded {insert_result[0].get('number of rows inserted') or 0} history versions."


def _reconcile(session, source, source_object, keys, fq_table, ids_table, file_format, delete_mode, export,
               history=None, types=None):
    if delete_mode == "off":
        raise core.ConfigError("Deletes are off for this job; nothing to reconcile.")
    exported = export(source.build_ids_query(source_object), False)
    if exported["rows"] == 0:
        raise core.ConfigError(f"{source.LABEL} returned no records; refusing to reconcile "
                               "(it would flag or remove every row).")
    out = _execute(session, core.reconcile_statements(
        fq_table, ids_table, exported["stage_path"], file_format, delete_mode, keys,
        history, (types or {}).get(keys.change_col, "VARCHAR"), (types or {}).get(keys.key_col, "VARCHAR")))
    # With history, the flag or remove is followed by COMMIT
    changed = out[-2 if history else -1][0].get("number of rows deleted" if delete_mode == "remove" else "number of rows updated")
    verb = "Removed" if delete_mode == "remove" else "Flagged"
    return {"mode": "reconcile", "exported": exported["rows"], "deleted": changed,
            "message": f"Reconciled against {exported['rows']} current records; {verb.lower()} {changed} rows."}
