# Snowforce engine (Snowflake-native)

Phases 1 and 2 of [docs/streamlit-app-design.md](../docs/streamlit-app-design.md): the sync engine
as Snowflake tables, procedures, and tasks, and a Streamlit app to manage it. You can also drive it
with SQL ([sql/examples.sql](sql/examples.sql)).

## Layout

| Path | What it is |
| --- | --- |
| `app.env` | Where the app lives (`CRM_DB.CMILLER_SF_PLAYGROUND`), its role, warehouse, and integration name |
| `deploy.sh` | Creates or updates everything in one Snowflake session |
| `sql/10_objects.sql` | Config tables, stages, file format, network rule |
| `sql/20_admin_integration.sql` | One-time admin SQL: the external access integration, plus per-run-as-user grants |
| `sql/30_procedures.sql` | `SNOWFORCE_RUN_JOB`, `SNOWFORCE_SAVE_JOB_TASK`, `SNOWFORCE_REFRESH_CONNECTIONS` |
| `sql/40_streamlit.sql` | The `SNOWFORCE_APP` Streamlit app |
| `sql/examples.sql` | Add a connection and a job, run it, read the history |
| `src/snowforce_core.py` | Pure logic: names, types, SOQL, load SQL, DDL |
| `src/sforce_api.py` | Salesforce client: JWT bearer login, describe, Bulk API 2.0 |
| `src/snowforce_procs.py` | Procedure handlers |
| `streamlit/` | The Streamlit app: a home page, and Connections, Jobs, and Runs pages |
| `streamlit/snowforce_forms.py` | Pure logic behind the forms: validation and the save SQL |
| `streamlit/snowforce_ui.py` | Session, app schema, and reads and calls shared by the pages |
| `streamlit/environment.yml` | The app's packages, from Snowflake's Anaconda channel |
| `streamlit/run_local.sh` | Run the app on your machine against the deployed engine |
| `tests/` | Unit tests for the pure logic, the Salesforce client, and the forms |

## Commands

```
app/deploy.sh             # create or update the app
app/deploy.sh streamlit   # update only the Streamlit app
app/streamlit/run_local.sh  # run the Streamlit app locally (Python 3.11, streamlit, snowpark)
app/deploy.sh admin-sql   # print the one-time SQL for a Snowflake admin
app/deploy.sh render      # print the deploy SQL without running it
python3 -m unittest discover -s app/tests
```

## Before it can reach Salesforce

1. **Snowflake admin** runs the output of `app/deploy.sh admin-sql` (creates
   `SNOWFORCE_SALESFORCE_EAI`). Until then, `SNOWFORCE_REFRESH_CONNECTIONS` reports that the
   integration is missing and the Salesforce procedures don't exist.
2. **Make a key pair** per connection (the command is in [sql/examples.sql](sql/examples.sql)).
   The certificate goes to Salesforce; the private key goes in a Snowflake secret, never in git.
3. **Salesforce admin**, per org: an External Client App with OAuth, the `api` and
   `refresh_token` scopes, and the JWT bearer flow enabled with the certificate uploaded. Under
   its policies, "Admin approved users are pre-authorized", with the integration user given
   access through a permission set. That user needs API Enabled and read access to the synced
   objects. You need the app's consumer key and the user's username.
4. Add the connection ([sql/examples.sql](sql/examples.sql) step 1) and
   `CALL SNOWFORCE_REFRESH_CONNECTIONS()`.

## Procedures

| Procedure | Runs as | Does |
| --- | --- | --- |
| `SNOWFORCE_RUN_JOB(job, [mode])` | caller | Sync (delta), full reload, or reconcile one job; logs to `SNOWFORCE_RUNS` |
| `SNOWFORCE_SAVE_JOB_TASK(job)` | owner | Create or replace the job's task from its `SNOWFORCE_JOBS` row |
| `SNOWFORCE_REFRESH_CONNECTIONS()` | owner | Re-create the Salesforce procedures with every connection's secret |
| `SNOWFORCE_TEST_CONNECTION(conn)` | owner | Log in and count queryable objects |
| `SNOWFORCE_LIST_OBJECTS(conn)` | owner | Queryable objects, for picking one |
| `SNOWFORCE_DESCRIBE(conn, object)` | owner | Capture fields into `SNOWFORCE_FIELDS` |
| `SNOWFORCE_EXTRACT(conn, soql, all_rows, run_id)` | owner | Bulk API export to the `SNOWFORCE_EXPORTS` stage |

The last four exist only once the integration does.

## Streamlit app

`deploy.sh` creates `SNOWFORCE_APP` in the app schema; open it from Snowsight under
**Projects > Streamlit**. It runs with its owner's rights, as the app role, and does everything
through the tables and procedures above:

| Page | Does |
| --- | --- |
| Connections | Add or edit a connection (creates its secret from the pasted private key, then calls `SNOWFORCE_REFRESH_CONNECTIONS`), test it, remove it |
| Jobs | Create or edit a job: pick the object and fields from Salesforce, the destination, deletes, history, schedule, and run-as user and role. Saving writes the `SNOWFORCE_JOBS` row and calls `SNOWFORCE_SAVE_JOB_TASK` |
| Runs | `SNOWFORCE_RUNS` history, and Run now, Full reload, and Reconcile |

The app can't run as another user, so its run buttons start the job's task (`EXECUTE TASK`),
which runs as the job's user, role, and warehouse. A full reload or reconcile sets the job's
`NEXT_RUN_MODE` first. Starting tasks needs the account-level `EXECUTE TASK` privilege.

The app imports `snowforce_core.py` for validation; `deploy.sh` uploads a copy next to the app,
and locally it's read from `src/`.

## History

Set `TRACK_HISTORY = TRUE` on a job to keep type 2 history in `<TARGET_TABLE>__HISTORY`, the
same as `history` in the CLI's `objects.conf` (see the [main README](../README.md#history)).
The job's role needs to be able to create and write that table.
