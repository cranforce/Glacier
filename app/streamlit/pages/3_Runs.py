"""Run history from GLACIER_RUNS, and starting runs.

Runs start through the job's task (EXECUTE TASK), so they use the job's user,
role, and warehouse, not the app's.
"""
import streamlit as st

import glacier_ui as ui
from glacier_ui import core

st.set_page_config(page_title="Runs · Glacier", layout="wide")
st.title("Runs")
ui.show_flash()

app = ui.app()
jobs = [j["JOB_NAME"] for j in ui.jobs()]
states = ui.task_states()

ALL = "All jobs"
left, mid, right = st.columns([2, 1, 1])
job_name = left.selectbox("Job", [ALL, *jobs])
status = mid.selectbox("Status", ["Any", "RUNNING", "SUCCEEDED", "FAILED"])
limit = right.number_input("Show", min_value=10, max_value=1000, value=100, step=50)

# --- Start a run --------------------------------------------------------------------

if job_name != ALL:
    state = states.get(job_name)
    if state is None:
        st.warning(f"{job_name} has no task yet; save it on the **Jobs** page first.")
    else:
        st.caption(f"Task {core.task_name(app, job_name)} is {state}. Runs start in the background; "
                   "refresh to see them.")
        actions = st.columns(4)
        for column, (label, mode) in zip(actions, (("Run now", "sync"), ("Full reload", "full"),
                                                   ("Reconcile deletes", "reconcile"))):
            if column.button(label, key=f"run:{mode}"):
                try:
                    ui.start_job(job_name, mode)
                    ui.flash(f"Started {job_name} ({mode}).")
                except Exception as e:
                    ui.show_error(e)
                else:
                    st.rerun()
        if actions[3].button("Refresh"):
            st.rerun()

# --- History --------------------------------------------------------------------------

history = ui.runs(None if job_name == ALL else job_name, limit)
if status != "Any":
    history = [r for r in history if r["STATUS"] == status]

if not history:
    st.info("No runs to show.")
    st.stop()

st.dataframe(
    [{k: r[k] for k in ("JOB_NAME", "MODE", "STATUS", "STARTED_AT", "FINISHED_AT", "ROWS_EXPORTED",
                        "ROWS_INSERTED", "ROWS_UPDATED", "ROWS_DELETED", "RUN_USER", "RUN_ROLE",
                        "MESSAGE")} for r in history],
    width="stretch", hide_index=True)

failed = [r for r in history if r["STATUS"] == "FAILED"]
if failed:
    with st.expander(f"Latest failure: {failed[0]['JOB_NAME']} at {failed[0]['STARTED_AT']}"):
        st.code(failed[0]["MESSAGE"] or "(no message)", language=None)
        if failed[0]["SOURCE_QUERY"]:
            st.code(failed[0]["SOURCE_QUERY"], language="sql")

st.caption("A scheduled run that never appears here failed before it started, e.g. a missing grant. "
           "Check the task's history; see docs/jobs.md.")
