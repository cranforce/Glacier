# Glacier engine (Snowflake-native)

The design is in [docs/streamlit-app-design.md](../docs/streamlit-app-design.md): the sync engine
as Snowflake tables, procedures, and tasks, and a Streamlit app to manage it. You can also drive it
with SQL ([sql/examples.sql](sql/examples.sql)).

## Layout

| Path | What it is |
| --- | --- |
| `app.env` | Where the app lives (`GLACIER_DB.APP`), its role, and its warehouse |
| `sql/00_admin_setup.sql` | One-time admin SQL: the app's role, warehouse, database, and schema |
| `deploy.sh` | Creates or updates everything in one Snowflake session |
| `sql/10_objects.sql` | Config tables, stages, file format, network rule |
| `sql/20_admin_integration.sql` | One-time admin SQL per connector: its external access integration, plus per-run-as-user grants |
| `sql/30_procedures.sql` | `GLACIER_RUN_JOB`, `GLACIER_SAVE_JOB_TASK`, `GLACIER_REFRESH_CONNECTIONS` |
| `sql/40_streamlit.sql` | The `GLACIER_APP` Streamlit app |
| `sql/examples.sql` | Add a connection and a job, run it, read the history |
| `src/glacier_core.py` | Pure logic: names, load SQL, history, procedure and task DDL |
| `src/glacier_connectors.py` | The connectors by name, and what each one provides |
| `src/connector_salesforce.py` | The Salesforce connector: settings, types, SOQL, and calls through `sforce_api.py` |
| `src/sforce_api.py` | Salesforce client: JWT bearer login, describe, Bulk API 2.0 and REST queries |
| `src/connector_shopify.py` | The Shopify connector: its objects, introspected fields, bulk queries, and JSONL flattening |
| `src/shopify_api.py` | Shopify client: access token or client credentials, GraphQL, introspection, bulk queries |
| `src/glacier_procs.py` | Procedure handlers; they reach sources through the connectors |
| `streamlit/` | The Streamlit app: a home page, and Connections, Jobs, and Runs pages |
| `streamlit/glacier_forms.py` | Pure logic behind the forms: validation and the save SQL |
| `streamlit/glacier_ui.py` | Session, app schema, and reads and calls shared by the pages |
| `streamlit/environment.yml` | The app's packages, from Snowflake's Anaconda channel |
| `streamlit/run_local.sh` | Run the app on your machine against the deployed engine |
| `tests/` | Unit tests for the pure logic, the connectors, the Salesforce and Shopify clients, and the forms |

## Connectors

Everything specific to a source system is in its connector module, `src/connector_<name>.py`; the
core, the procedures, and the app reach it through `src/glacier_connectors.py`, whose docstring
lists what a connector provides. To add one:

1. Write `src/connector_<name>.py` and add it to `CONNECTORS` in `glacier_connectors.py` and to
   `CODE_FILES` in `glacier_core.py`.
2. Add its network rule, `GLACIER_<NAME>_EGRESS`, to `sql/10_objects.sql`.
3. Deploy, then have an admin run `app/deploy.sh admin-sql integration <name>`.

## Commands

```
app/deploy.sh                        # create or update the app
app/deploy.sh streamlit              # update only the Streamlit app
app/deploy.sh admin-sql setup        # one-time admin SQL, before the first deploy
app/deploy.sh admin-sql integration salesforce  # one-time admin SQL per connector, after the first deploy
app/deploy.sh render                 # print the deploy SQL without running it
app/streamlit/run_local.sh           # run the Streamlit app locally (Python 3.11, streamlit, snowpark)
python3 -m unittest discover -s app/tests
```

## Before it can reach Salesforce

0. **Snowflake admin**, before the first deploy, runs the output of `app/deploy.sh admin-sql setup`
   (creates the `GLACIER_DB` database, its `APP` and `NPC_SALESFORCE_PROD_RAW` schemas, `GLACIER_APP_ROLE`, and `GLACIER_WH`).
1. **Snowflake admin**, after the first deploy, runs the output of `app/deploy.sh admin-sql integration salesforce` (creates
   `GLACIER_SALESFORCE_EAI`). Until then, `GLACIER_REFRESH_CONNECTIONS` reports that the
   integration is missing and the Salesforce procedures don't exist.
2. **Make a key pair** per connection (the command is in [sql/examples.sql](sql/examples.sql)).
   The certificate goes to Salesforce; the private key goes in a Snowflake secret, never in git.
3. **Salesforce admin**, per org: an External Client App with OAuth, the `api` and
   `refresh_token` scopes, and the JWT bearer flow enabled with the certificate uploaded. Under
   its policies, "Admin approved users are pre-authorized", with the integration user given
   access through a permission set. That user needs API Enabled and read access to the synced
   objects. You need the app's consumer key and the user's username.
4. Add the connection ([sql/examples.sql](sql/examples.sql) step 1) and
   `CALL GLACIER_REFRESH_CONNECTIONS()`.

## Before it can reach Shopify

See [docs/shopify.md](../docs/shopify.md#setting-up-a-store): an app per store, then
`app/deploy.sh admin-sql integration shopify`, then a connection per store.

## Procedures

| Procedure | Runs as | Does |
| --- | --- | --- |
| `GLACIER_RUN_JOB(job, [mode])` | caller | Sync (delta), full reload, or reconcile one job; logs to `GLACIER_RUNS` |
| `GLACIER_SAVE_JOB_TASK(job)` | owner | Create or replace the job's task from its `GLACIER_JOBS` row |
| `GLACIER_REFRESH_CONNECTIONS()` | owner | Re-create each connector's procedures with its connections' secrets |
| `GLACIER_SALESFORCE_TEST_CONNECTION(conn)` | owner | Log in and count queryable objects |
| `GLACIER_SALESFORCE_LIST_OBJECTS(conn)` | owner | Queryable objects, for picking one |
| `GLACIER_SALESFORCE_DESCRIBE(conn, object)` | owner | Capture fields into `GLACIER_FIELDS` |
| `GLACIER_SALESFORCE_EXTRACT(conn, query, include_deleted, run_id, api)` | owner | Bulk API or REST export to the `GLACIER_EXPORTS` stage |

The last four are Salesforce's own; each connector gets its set, `GLACIER_<CONNECTOR>_<ACTION>`, holding only its connections' secrets. They exist only once the connector's integration does.

## Streamlit app

`deploy.sh` creates `GLACIER_APP` in the app schema; open it from Snowsight under
**Projects > Streamlit**. It runs with its owner's rights, as the app role, and does everything
through the tables and procedures above:

| Page | Does |
| --- | --- |
| Connections | Add or edit a connection: pick its connector, fill in its settings, and paste its credential, which becomes its secret. Saving calls `GLACIER_REFRESH_CONNECTIONS`. Test it, remove it |
| Jobs | Create or edit a job: pick the object and fields from the connection's source, the destination, deletes, history, schedule, and run-as user and role. Saving writes the `GLACIER_JOBS` row and calls `GLACIER_SAVE_JOB_TASK` |
| Runs | `GLACIER_RUNS` history, and Run now, Full reload, and Reconcile |

The app can't run as another user, so its run buttons start the job's task (`EXECUTE TASK`),
which runs as the job's user, role, and warehouse. A full reload or reconcile sets the job's
`NEXT_RUN_MODE` first. Starting tasks needs the account-level `EXECUTE TASK` privilege.

The app imports `glacier_core.py` and the connectors for validation; `deploy.sh` uploads copies
of `src/` next to the app, and locally they're read from `src/`.

## History

Set `TRACK_HISTORY = TRUE` on a job to keep type 2 history in `<TARGET_TABLE>__HISTORY` (see
[History](../docs/how-syncing-works.md#history)).
The job's role needs to be able to create and write that table.
