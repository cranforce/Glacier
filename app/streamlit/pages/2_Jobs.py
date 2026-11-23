"""Create and edit sync jobs. Saving a job writes its GLACIER_JOBS row and
re-creates its task (GLACIER_SAVE_JOB_TASK)."""
import streamlit as st

import glacier_forms as forms
import glacier_ui as ui
from glacier_ui import core

st.set_page_config(page_title="Jobs · Glacier", layout="wide")
st.title("Jobs")
st.caption("A job syncs one object, such as a Salesforce object, into one Snowflake table. "
           "See docs/jobs.md.")
ui.show_flash()

ctx = ui.context()
app = ctx["app"]
connection_rows = ui.connections()
connections = [c["CONNECTION_NAME"] for c in connection_rows]
sources = {c["CONNECTION_NAME"]: ui.connectors.get(c["CONNECTOR"]) for c in connection_rows}
jobs = ui.jobs()
states = ui.task_states()

if not connections:
    st.info("Add a connection on the **Connections** page first.")
    st.stop()

if jobs:
    st.dataframe(
        [{"JOB_NAME": j["JOB_NAME"], "CONNECTION": j["CONNECTION_NAME"], "OBJECT": j["SOURCE_OBJECT"],
          "API": j.get("SOURCE_API") or sources[j["CONNECTION_NAME"]].APIS[0],
          "TARGET": forms.target_table(j, sources[j["CONNECTION_NAME"]]), "DELETES": j["DELETE_MODE"],
          "HISTORY": j["TRACK_HISTORY"], "SCHEDULE": j["SCHEDULE"], "ENABLED": j["ENABLED"],
          "TASK": states.get(j["JOB_NAME"], "not saved")} for j in jobs],
        width="stretch", hide_index=True)

# --- Editor -------------------------------------------------------------------------

st.subheader("Create or edit")
NEW = "New job"
names = [j["JOB_NAME"] for j in jobs]
if "_select_job" in st.session_state:  # set by a save or remove, before the widget exists
    st.session_state["job_choice"] = st.session_state.pop("_select_job")
if st.session_state.get("job_choice") not in (NEW, *names):
    st.session_state["job_choice"] = NEW
choice = st.selectbox("Job", [NEW, *names], key="job_choice")
is_new = choice == NEW
job = {} if is_new else (ui.job(choice) or {})
key = f"job:{choice}:"  # widget keys, so switching jobs resets the form


def default(column, fallback=None):
    value = job.get(column)
    return fallback if value is None else value


left, right = st.columns(2)

with left:
    st.markdown("**Source**")
    job_name = st.text_input("Job name", value=default("JOB_NAME", ""), disabled=not is_new,
                             key=key + "name", help="A plain identifier; also names the task.")
    conn = st.selectbox("Connection", connections, key=key + "conn",
                        index=connections.index(job["CONNECTION_NAME"])
                        if job.get("CONNECTION_NAME") in connections else 0)

    source = ui.connector(conn)
    objects = None
    if ui.connector_ready(source.NAME):
        try:
            objects = ui.list_objects(conn)
        except Exception as e:
            st.warning(f"Couldn't list objects from {source.LABEL}; type the name instead. ({e})")
    if objects:
        names = [o["name"] for o in objects]
        current = default("SOURCE_OBJECT")
        if current and current not in names:
            names.insert(0, current)
        labels = {o["name"]: f"{o['name']} ({o['label']})" for o in objects}
        source_object = st.selectbox("Object", names, key=key + "obj",
                               index=names.index(current) if current else None,
                               format_func=lambda n: labels.get(n, n),
                               placeholder="Search objects...")
    else:
        source_object = st.text_input("Object name", value=default("SOURCE_OBJECT", ""),
                                      key=key + "obj_text", placeholder=source.OBJECT_PLACEHOLDER)

    fields = None
    if source_object:
        captured = ui.fields(conn, source_object)
        if not captured:
            st.caption(f"{source_object}'s fields haven't been captured yet.")
            if st.button(f"Describe {source_object}", key=key + "describe"):
                try:
                    with st.spinner("Describing..."):
                        ui.call_connector(conn, "DESCRIBE", source_object)
                except Exception as e:
                    ui.show_error(e)
                else:
                    st.rerun()
        all_fields = st.radio("Fields", ["Every exportable field", "Choose fields"], horizontal=True,
                              key=key + "all", index=1 if job.get("FIELDS") else 0,
                              help=f"{source.FIELDS_HELP} New fields are picked up on the next run.")
        if all_fields == "Choose fields":
            exportable = [f for f in captured if f["EXPORTABLE"]]
            by_field = {f["FIELD_NAME"]: f for f in exportable}
            chosen = [f for f in job.get("FIELDS") or [] if f in by_field]
            fields = st.multiselect(
                "Fields to sync", list(by_field), default=chosen, key=key + "fields",
                format_func=lambda n: f"{n} · {by_field[n]['LABEL']} · {by_field[n]['SNOWFLAKE_TYPE']}")
            if captured:
                st.caption(f"{len(exportable)} of {len(captured)} fields are exportable; "
                           f"{source.UNEXPORTABLE_NOTE}")

    apis = list(source.APIS)
    current_api = default("SOURCE_API", apis[0])
    source_api = st.selectbox("API", apis, key=key + "api", disabled=len(apis) == 1,
                              index=apis.index(current_api) if current_api in apis else 0,
                              help=source.API_HELP)

with right:
    st.markdown("**Destination**")
    target_db = st.text_input("Database", value=default("TARGET_DATABASE", app.split(".")[0]),
                              key=key + "db")
    target_schema = st.text_input("Schema", value=default("TARGET_SCHEMA", app.split(".")[1]),
                                  key=key + "schema")
    target_table = st.text_input(
        "Table", value=default("TARGET_TABLE", ""), key=key + "table",
        placeholder=source.table_name(source_object)
        if source_object and core.IDENTIFIER.match(source_object) else "",
        help=f"Blank for {source.TABLE_NAME_HELP}. Set it to keep loading an existing table.")
    delete_modes = list(source.DELETE_MODES)
    current_mode = default("DELETE_MODE", "flag" if "flag" in delete_modes else delete_modes[0])
    delete_mode = st.selectbox("Deletes", delete_modes, key=key + "deletes",
                               index=delete_modes.index(current_mode) if current_mode in delete_modes else 0,
                               help=f"flag sets {source.KEYS.deleted_col or 'a deleted flag'}; remove deletes the row; "
                                    "off ignores deletes.")
    reconcile_days = st.number_input("Reconcile after days", min_value=1, step=1, key=key + "reconcile",
                                     value=int(default("RECONCILE_AFTER_DAYS", 14)),
                                     disabled=delete_mode == "off")
    track_history = st.checkbox("Keep type 2 history (<TABLE>__HISTORY)", key=key + "history",
                                value=bool(default("TRACK_HISTORY", False)))

    st.markdown("**Schedule and identity**")
    presets = {**forms.SCHEDULES}
    current_schedule = default("SCHEDULE", "")
    if current_schedule not in presets.values():
        presets[f"Custom: {current_schedule}"] = current_schedule
    presets["Custom..."] = None
    label = st.selectbox("Schedule", list(presets), key=key + "preset",
                         index=list(presets.values()).index(current_schedule))
    schedule = presets[label]
    if schedule is None:
        schedule = st.text_input("Cron and time zone", key=key + "cron", placeholder="0 2 * * 1-5 UTC")
    warehouse = st.text_input("Warehouse", value=default("WAREHOUSE", ctx["warehouse"] or ""),
                              key=key + "wh")
    run_as_user = st.text_input("Run as user", value=default("RUN_AS_USER", ""), key=key + "user",
                                help="Blank to run as the app role. A service user is recommended; "
                                     "it needs admin grants (docs/jobs.md).")
    run_as_role = st.text_input("Run as role", value=default("RUN_AS_ROLE", ""), key=key + "role",
                                help="Needs a run-as user unless it's the app role, "
                                     f"{ctx['role']}.")
    enabled = st.checkbox("Enabled", value=bool(default("ENABLED", True)), key=key + "enabled")

# --- Save -------------------------------------------------------------------------------

if st.button("Save job", type="primary"):
    try:
        saved = forms.validate_job({
            "JOB_NAME": job_name if is_new else choice, "CONNECTION_NAME": conn,
            "SOURCE_OBJECT": source_object,
            "FIELDS": fields, "SOURCE_API": source_api, "TARGET_DATABASE": target_db, "TARGET_SCHEMA": target_schema,
            "TARGET_TABLE": target_table, "DELETE_MODE": delete_mode,
            "RECONCILE_AFTER_DAYS": reconcile_days, "TRACK_HISTORY": track_history,
            "RUN_AS_USER": run_as_user, "RUN_AS_ROLE": run_as_role, "WAREHOUSE": warehouse,
            "SCHEDULE": schedule, "ENABLED": enabled}, app, ctx["role"], source.NAME)
        if is_new and any(j["JOB_NAME"] == saved["JOB_NAME"] for j in jobs):
            raise core.ConfigError(f"A job named {saved['JOB_NAME']} already exists")
        ui.session().sql(forms.save_job_sql(app), params=forms.row_param(saved)).collect()
        task = ui.call("GLACIER_SAVE_JOB_TASK", saved["JOB_NAME"])
        ui.flash(f"Saved {saved['JOB_NAME']}; task {task['task']} is {task['state']}. "
                 f"It loads {forms.target_table(saved, source)}.")
        st.session_state["_select_job"] = saved["JOB_NAME"]
    except Exception as e:
        ui.show_error(e)
    else:
        st.rerun()

# --- Remove -------------------------------------------------------------------------------

if not is_new:
    with st.expander(f"Remove {choice}"):
        st.write("Drops the job's task and its row. The target table and its history stay.")
        if st.checkbox(f"Yes, remove {choice}", key=key + "confirm") and st.button("Remove job"):
            try:
                ui.session().sql(f"DROP TASK IF EXISTS {core.task_name(app, choice)}").collect()
                ui.session().sql(f"DELETE FROM {app}.GLACIER_JOBS WHERE JOB_NAME = ?",
                                 params=[choice]).collect()
                ui.flash(f"Removed {choice}.")
                st.session_state["_select_job"] = NEW
            except Exception as e:
                ui.show_error(e)
            else:
                st.rerun()
