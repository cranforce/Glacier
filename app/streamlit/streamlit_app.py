"""Glacier: manage connections to source systems and sync jobs.

The pages are in pages/. Everything they do goes through the app's tables and
procedures (see app/README.md), so a job behaves the same whether it was set
up here or in SQL.
"""
import streamlit as st

import glacier_ui as ui

st.set_page_config(page_title="Glacier", layout="wide")
st.title("Glacier")
st.caption("Sync enterprise systems into Snowflake")

ctx = ui.context()
st.write(f"App schema `{ctx['app']}`, running as role `{ctx['role']}`.")

connections = ui.connections()
for name in sorted({c["CONNECTOR"] for c in connections}):
    if not ui.connector_ready(name):
        st.warning(
            f"The {ui.connectors.get(name).LABEL} procedures don't exist yet, so its connections can't "
            "be tested and its jobs can't run. A Snowflake admin needs to create the external access "
            f"integration (`app/deploy.sh admin-sql integration {name}`); then save a connection on "
            "the **Connections** page.")
if not connections:
    st.info("Add a connection on the **Connections** page to get started.")

jobs = ui.jobs()
recent = ui.runs(limit=50)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Connections", len(connections))
c2.metric("Jobs", len(jobs))
c3.metric("Scheduled", sum(1 for j in jobs if j["ENABLED"] and j["SCHEDULE"]))
c4.metric("Failed (last 50 runs)", sum(1 for r in recent if r["STATUS"] == "FAILED"))

st.subheader("Recent runs")
if recent:
    st.dataframe(
        [{k: r[k] for k in ("JOB_NAME", "MODE", "STATUS", "STARTED_AT", "FINISHED_AT",
                            "ROWS_EXPORTED", "MESSAGE")} for r in recent[:10]],
        width="stretch", hide_index=True)
else:
    st.info("No runs yet. Create a job on the **Jobs** page.")
