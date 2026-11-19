# Snowforce engine (Snowflake-native)

Phase 1 of [docs/streamlit-app-design.md](../docs/streamlit-app-design.md): the sync engine as
Snowflake tables, procedures, and tasks. The Streamlit UI comes next; until then, drive it with SQL
([sql/examples.sql](sql/examples.sql)).

## Layout

| Path | What it is |
| --- | --- |
| `app.env` | Where the app lives (`CRM_DB.CMILLER_SF_PLAYGROUND`), its role, warehouse, and integration name |
| `deploy.sh` | Creates or updates everything in one Snowflake session |
| `sql/10_objects.sql` | Config tables, stages, file format, network rule |
| `sql/20_admin_integration.sql` | One-time admin SQL: the external access integration, plus per-run-as-user grants |
| `sql/30_procedures.sql` | `SNOWFORCE_RUN_JOB`, `SNOWFORCE_SAVE_JOB_TASK`, `SNOWFORCE_REFRESH_CONNECTIONS` |
| `sql/examples.sql` | Add a connection and a job, run it, read the history |
| `src/snowforce_core.py` | Pure logic: names, types, SOQL, load SQL, DDL |
| `src/sforce_api.py` | Salesforce client: JWT bearer login, describe, Bulk API 2.0 |
| `src/snowforce_procs.py` | Procedure handlers |
| `tests/` | Unit tests for the pure logic and the Salesforce client |

## Commands

```
app/deploy.sh             # create or update the app
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

## History

Set `TRACK_HISTORY = TRUE` on a job to keep type 2 history in `<TARGET_TABLE>__HISTORY`, the
same as `history` in the CLI's `objects.conf` (see the [main README](../README.md#history)).
The job's role needs to be able to create and write that table.
