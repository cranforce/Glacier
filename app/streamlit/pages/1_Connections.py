"""Add, edit, test, and remove Salesforce connections."""
import streamlit as st

import snowforce_forms as forms
import snowforce_ui as ui
from snowforce_ui import core

st.set_page_config(page_title="Connections · Snowforce", layout="wide")
st.title("Connections")
st.caption("One per Salesforce org, logging in with the JWT bearer flow. "
           "See docs/snowflake-setup.md for the key pair and External Client App.")

ui.show_flash()
app = ui.app()
connections = ui.connections()
by_name = {c["CONNECTION_NAME"]: c for c in connections}

if connections:
    st.dataframe(
        [{k: c[k] for k in ("CONNECTION_NAME", "MY_DOMAIN_URL", "USERNAME", "API_VERSION",
                            "SECRET_NAME", "DESCRIPTION")} for c in connections],
        use_container_width=True, hide_index=True)
else:
    st.info("No connections yet.")

# --- Add or edit ----------------------------------------------------------------------

st.subheader("Add or edit")
NEW = "New connection"
choice = st.selectbox("Connection", [NEW, *by_name], key="conn_choice")
current = by_name.get(choice, {})
is_new = choice == NEW

with st.form(f"connection:{choice}"):
    name = st.text_input("Name", value=current.get("CONNECTION_NAME", ""), disabled=not is_new,
                         help="Letters, digits, and _. Jobs refer to it.")
    url = st.text_input("My Domain URL", value=current.get("MY_DOMAIN_URL", ""),
                        placeholder="https://<domain>.my.salesforce.com")
    client_id = st.text_input("Consumer key", value=current.get("CLIENT_ID") or "")
    username = st.text_input("Integration user", value=current.get("USERNAME") or "")
    api_version = st.text_input("API version", value=current.get("API_VERSION") or "64.0")
    description = st.text_input("Description", value=current.get("DESCRIPTION") or "")
    private_key = st.text_area(
        "Private key (PEM)",
        help="Snowflake masks secret values in query history." +
             ("" if is_new else " Leave blank to keep the current key."),
        placeholder="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----")
    submitted = st.form_submit_button("Save connection", type="primary")

if submitted:
    try:
        conn = forms.validate_connection({
            "CONNECTION_NAME": name if is_new else choice, "MY_DOMAIN_URL": url,
            "CLIENT_ID": client_id, "USERNAME": username, "API_VERSION": api_version,
            "DESCRIPTION": description, "SECRET_NAME": current.get("SECRET_NAME")}, app)
        if is_new and conn["CONNECTION_NAME"] in by_name:
            raise core.ConfigError(f"A connection named {conn['CONNECTION_NAME']} already exists")
        if is_new and not private_key.strip():
            raise core.ConfigError("A new connection needs its private key")
        with st.spinner("Saving..."):
            if private_key.strip():
                ui.session().sql(forms.create_secret_sql(conn["SECRET_NAME"], private_key)).collect()
            ui.session().sql(forms.save_connection_sql(app),
                             params=forms.row_param(conn)).collect()
            # A procedure's secrets are fixed when it's created
            refreshed = ui.call("SNOWFORCE_REFRESH_CONNECTIONS")
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
            with st.spinner("Logging in to Salesforce..."):
                st.json(ui.call("SNOWFORCE_TEST_CONNECTION", test_name))
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
                    ui.session().sql(f"DELETE FROM {app}.SNOWFORCE_CONNECTIONS WHERE CONNECTION_NAME = ?",
                                     params=[remove]).collect()
                    ui.call("SNOWFORCE_REFRESH_CONNECTIONS")
                    if drop_secret:
                        ui.session().sql(f"DROP SECRET IF EXISTS {forms.secret_name(app, remove)}").collect()
                    ui.flash(f"Removed {remove}.")
                except Exception as e:
                    ui.show_error(e)
                else:
                    st.rerun()
