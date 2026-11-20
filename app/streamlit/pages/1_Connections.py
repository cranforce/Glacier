"""Add, edit, test, and remove connections to source systems."""
import streamlit as st

import glacier_forms as forms
import glacier_ui as ui
from glacier_ui import connectors, core

st.set_page_config(page_title="Connections · Glacier", layout="wide")
st.title("Connections")
st.caption("One per source system account, such as a Salesforce org. "
           "See docs/snowflake-setup.md for each connector's credentials.")

ui.show_flash()
app = ui.app()
connections = ui.connections()
by_name = {c["CONNECTION_NAME"]: c for c in connections}

if connections:
    st.dataframe(
        [{"CONNECTION_NAME": c["CONNECTION_NAME"], "CONNECTOR": c["CONNECTOR"], **c["SETTINGS"],
          "SECRET_NAME": c["SECRET_NAME"], "DESCRIPTION": c["DESCRIPTION"]} for c in connections],
        width="stretch", hide_index=True)
else:
    st.info("No connections yet.")

# --- Add or edit ----------------------------------------------------------------------

st.subheader("Add or edit")
NEW = "New connection"
choice = st.selectbox("Connection", [NEW, *by_name], key="conn_choice")
current = by_name.get(choice, {})
is_new = choice == NEW

kinds = list(connectors.CONNECTORS)
kind = st.selectbox("Connector", kinds, disabled=not is_new, key=f"connector:{choice}",
                    index=kinds.index(current["CONNECTOR"]) if current else 0,
                    format_func=lambda k: connectors.get(k).LABEL)
source = connectors.get(kind)
settings = current.get("SETTINGS", {})

with st.form(f"connection:{choice}:{kind}"):
    name = st.text_input("Name", value=current.get("CONNECTION_NAME", ""), disabled=not is_new,
                         help="Letters, digits, and _. Jobs refer to it.")
    values = {s.key: st.text_input(s.label, value=settings.get(s.key) or s.default,
                                   placeholder=s.placeholder, help=s.help)
              for s in source.SETTINGS}
    description = st.text_input("Description", value=current.get("DESCRIPTION") or "")
    secret = st.text_area(
        source.SECRET_LABEL,
        help="Snowflake masks secret values in query history." +
             ("" if is_new else " Leave blank to keep the current one."),
        placeholder=source.SECRET_PLACEHOLDER)
    submitted = st.form_submit_button("Save connection", type="primary")

if submitted:
    try:
        conn = forms.validate_connection({
            "CONNECTION_NAME": name if is_new else choice, "CONNECTOR": kind, "SETTINGS": values,
            "DESCRIPTION": description, "SECRET_NAME": current.get("SECRET_NAME")}, app)
        if is_new and conn["CONNECTION_NAME"] in by_name:
            raise core.ConfigError(f"A connection named {conn['CONNECTION_NAME']} already exists")
        if is_new and not secret.strip():
            raise core.ConfigError(f"A new connection needs its {source.SECRET_LABEL.lower()}")
        with st.spinner("Saving..."):
            if secret.strip():
                ui.session().sql(forms.create_secret_sql(conn["SECRET_NAME"], secret, kind)).collect()
            ui.session().sql(forms.save_connection_sql(app),
                             params=forms.row_param(conn)).collect()
            # A procedure's secrets are fixed when it's created
            refreshed = ui.call("GLACIER_REFRESH_CONNECTIONS")
        ui.list_objects.clear()
        ui.flash(f"Saved {conn['CONNECTION_NAME']}.")
        if "skipped" in refreshed:
            ui.flash(refreshed["skipped"], "warning")
    except Exception as e:
        ui.show_error(e)
    else:
        st.rerun()

# --- Test -------------------------------------------------------------------------------

if connections:
    st.subheader("Test")
    test_name = st.selectbox("Connection to test", list(by_name), key="conn_test")
    if st.button("Test connection"):
        try:
            with st.spinner(f"Logging in to {ui.connector(test_name).LABEL}..."):
                st.json(ui.call_connector(test_name, "TEST_CONNECTION"))
        except Exception as e:
            ui.show_error(e)

# --- Remove -------------------------------------------------------------------------------

if connections:
    with st.expander("Remove a connection"):
        remove = st.selectbox("Connection to remove", list(by_name), key="conn_remove")
        users = [j["JOB_NAME"] for j in ui.jobs() if j["CONNECTION_NAME"] == remove]
        if users:
            st.warning(f"Used by {', '.join(users)}. Move or remove those jobs first.")
        else:
            drop_secret = by_name[remove]["SECRET_NAME"] == forms.secret_name(app, remove)
            st.write("Removes the connection" +
                     (f" and its secret `{by_name[remove]['SECRET_NAME']}`." if drop_secret else "."))
            if st.checkbox(f"Yes, remove {remove}") and st.button("Remove", type="primary"):
                try:
                    ui.session().sql(f"DELETE FROM {app}.GLACIER_CONNECTIONS WHERE CONNECTION_NAME = ?",
                                     params=[remove]).collect()
                    ui.call("GLACIER_REFRESH_CONNECTIONS")
                    if drop_secret:
                        ui.session().sql(f"DROP SECRET IF EXISTS {forms.secret_name(app, remove)}").collect()
                    ui.flash(f"Removed {remove}.")
                except Exception as e:
                    ui.show_error(e)
                else:
                    st.rerun()
