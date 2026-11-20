---
title: Jobs in Snowflake
nav_order: 4
---

# Jobs in Snowflake
{: .no_toc }

A job syncs one Salesforce object into one Snowflake table. It's a row in `SNOWFORCE_JOBS`, and
when it's scheduled, a task named `SNOWFORCE_JOB_<JOB_NAME>`. You can manage jobs in the
`SNOWFORCE_APP` Streamlit app (see `app/README.md`), or with the SQL below; both use the same
tables and procedures.

This assumes [Snowflake setup](snowflake-setup.md) is done and you have a working connection.
The examples use the current app schema and role:

```sql
USE ROLE DS_DATA_ENGINEERING_ROLE;
USE WAREHOUSE SNOWFORCE_WH;
USE SCHEMA CRM_DB.CMILLER_SF_PLAYGROUND;
```

1. TOC
{:toc}

## 1. Find the object and its fields

```sql
CALL SNOWFORCE_LIST_OBJECTS('ODBM_PROD');                 -- every queryable object
CALL SNOWFORCE_DESCRIBE('ODBM_PROD', 'Subscription__c');  -- capture its fields

SELECT FIELD_NAME, SF_TYPE, SNOWFLAKE_TYPE, EXPORTABLE
FROM SNOWFORCE_FIELDS
WHERE CONNECTION_NAME = 'ODBM_PROD' AND SOBJECT = 'Subscription__c'
ORDER BY POSITION;
```

`SNOWFLAKE_TYPE` is the column type the load will use. Fields that aren't `EXPORTABLE` (address,
location, and base64 fields) can't come through the Bulk API; their component fields, such as
`BillingCity`, can. A job's first run
describes the object itself if you haven't.

## 2. Create the job

```sql
INSERT INTO SNOWFORCE_JOBS
  (JOB_NAME, CONNECTION_NAME, SOBJECT, FIELDS, TARGET_DATABASE, TARGET_SCHEMA, TARGET_TABLE,
   DELETE_MODE, WAREHOUSE, SCHEDULE)
SELECT 'SUBSCRIPTION', 'ODBM_PROD', 'Subscription__c',
       ARRAY_CONSTRUCT('Account__c', 'Status__c', 'Start_Date__c', 'End_Date__c'),
       'CRM_DB', 'CMILLER_SF_PLAYGROUND', NULL,
       'flag', 'SNOWFORCE_WH', '0 * * * * UTC';
```

Use `INSERT ... SELECT`, not `VALUES`: Snowflake doesn't allow `ARRAY_CONSTRUCT` in a `VALUES`
list.

| Column | Required | Notes |
| --- | --- | --- |
| `JOB_NAME` | yes | A plain identifier; also names the task |
| `CONNECTION_NAME` | yes | A row in `SNOWFORCE_CONNECTIONS` |
| `SOBJECT` | yes | Salesforce API name, e.g. `Subscription__c` |
| `FIELDS` | | Array of field API names. `NULL` for every exportable field. `Id` and `SystemModstamp` (and `IsDeleted` unless deletes are off) are always added |
| `TARGET_DATABASE`, `TARGET_SCHEMA` | yes | Where the table goes |
| `TARGET_TABLE` | | `NULL` for the object name uppercased (`SUBSCRIPTION__C`). Set it to keep loading an existing table; see [Table names](how-syncing-works.md#table-names) |
| `DELETE_MODE` | | `flag` (default), `remove`, or `off`; see [Deletes](how-syncing-works.md#deletes) |
| `RECONCILE_AFTER_DAYS` | | Default 14; see [Reconcile](how-syncing-works.md#reconcile) |
| `TRACK_HISTORY` | | `TRUE` to keep `<TARGET_TABLE>__HISTORY`; see [History](how-syncing-works.md#history) |
| `WAREHOUSE` | yes | Warehouse the task runs on |
| `SCHEDULE` | | Cron with a time zone, e.g. `0 * * * * UTC`. `NULL` for manual runs only |
| `RUN_AS_USER`, `RUN_AS_ROLE` | | Who the task runs as; see [below](#run-as-a-user-and-role). `NULL` for the app role |
| `ENABLED` | | Default `TRUE`. A disabled job's task is suspended |
| `NEXT_RUN_MODE` | | `full` or `reconcile` for the next run only; see [One-off runs](#one-off-full-reloads-and-reconciles) |

The job's role needs to be able to create and write the target table, and its `__DELTA` and
`__IDS` staging tables (and `__HISTORY` with history on) in the target schema.

## 3. Run it once by hand

`SNOWFORCE_RUN_JOB` runs as your current role and returns a summary of the run.

```sql
CALL SNOWFORCE_RUN_JOB('SUBSCRIPTION');               -- sync (a full load the first time)
CALL SNOWFORCE_RUN_JOB('SUBSCRIPTION', 'full');       -- full reload
CALL SNOWFORCE_RUN_JOB('SUBSCRIPTION', 'reconcile');  -- check every Id for deletes
```

| Mode | Does |
| --- | --- |
| `auto` (default) | The job's `NEXT_RUN_MODE` if set (then clears it), else `sync` |
| `sync` | Delta since the table's `MAX(SYSTEMMODSTAMP)`, merged on Id. Full load if the table is new or empty. Reconciles when due |
| `full` | Rebuild the table from every record |
| `reconcile` | Flag or remove rows whose Id is gone from Salesforce; loads no other changes |

A manual run is a good first test, but it runs as **you**, not as the job's run-as user and role.
To test exactly what the schedule will do, run the task ([step 4](#4-schedule-it)).

## 4. Schedule it

Create the job's task from its row:

```sql
CALL SNOWFORCE_SAVE_JOB_TASK('SUBSCRIPTION');
-- {"task": "CRM_DB.CMILLER_SF_PLAYGROUND.SNOWFORCE_JOB_SUBSCRIPTION", "state": "started"}
```

The task is started when the job is `ENABLED` and has a `SCHEDULE`, and suspended otherwise. Each
run calls `SNOWFORCE_RUN_JOB('<job>')` in `auto` mode.

Run the task now, with the job's user, role, and warehouse:

```sql
EXECUTE TASK SNOWFORCE_JOB_SUBSCRIPTION;
```

The app role needs the account-level `EXECUTE TASK` privilege to start or run tasks. If
`SNOWFORCE_SAVE_JOB_TASK` fails to resume the task, ask a Snowflake admin for
`GRANT EXECUTE TASK ON ACCOUNT TO ROLE DS_DATA_ENGINEERING_ROLE`.

### Schedules

`SCHEDULE` is a cron expression (minute, hour, day of month, month, day of week) followed by a
time zone:

| Schedule | Runs |
| --- | --- |
| `0 * * * * UTC` | Every hour, on the hour |
| `*/15 * * * * UTC` | Every 15 minutes |
| `0 6 * * * America/Detroit` | 6 a.m. Eastern, daily |
| `0 2 * * 1-5 UTC` | 2 a.m. UTC on weekdays |

### Run as a user and role

With `RUN_AS_USER` and `RUN_AS_ROLE` both `NULL`, the task runs as Snowflake's system service in
the app role. That's the simplest setup, and the job can write wherever the app role can.

To run as a specific user instead, set `RUN_AS_USER` (a service user is recommended over a
person), and optionally `RUN_AS_ROLE`. The task runs `EXECUTE AS USER`, starts in the app role,
and switches with `USE ROLE` if a run-as role is set. A run-as role other than the app role needs
a run-as user.

A Snowflake admin grants, once per run-as user (also in `app/deploy.sh admin-sql`):

```sql
GRANT IMPERSONATE ON USER <run_as_user> TO ROLE DS_DATA_ENGINEERING_ROLE;
GRANT ROLE DS_DATA_ENGINEERING_ROLE TO USER <run_as_user>;
GRANT ROLE <job_role> TO USER <run_as_user>;   -- only if the job switches roles
```

A run-as role other than the app role also needs access to the app's objects, since
`SNOWFORCE_RUN_JOB` runs with its rights. Granting this isn't automated yet. It needs `USAGE` on
the app database, schema, file format, and the `SNOWFORCE_RUN_JOB`, `SNOWFORCE_EXTRACT`, and
`SNOWFORCE_DESCRIBE` procedures; `READ` and `WRITE` on the `SNOWFORCE_EXPORTS` stage; `SELECT`
and `UPDATE` on `SNOWFORCE_JOBS`; `SELECT` on `SNOWFORCE_FIELDS`; and `INSERT` and `UPDATE` on
`SNOWFORCE_RUNS`.

## Changing a job

Update the row. The field list, target, delete mode, and history are read at the start of every
run, so changes to those take effect on the next run. The schedule, warehouse, run-as user and
role, and enabled flag are part of the task, so save it again after changing those:

```sql
UPDATE SNOWFORCE_JOBS SET SCHEDULE = '*/30 * * * * UTC', UPDATED_AT = CURRENT_TIMESTAMP()
WHERE JOB_NAME = 'SUBSCRIPTION';
CALL SNOWFORCE_SAVE_JOB_TASK('SUBSCRIPTION');
```

To add a field, add it to `FIELDS` (or leave `FIELDS` `NULL`). If it's new in Salesforce, call
`SNOWFORCE_DESCRIBE` first so the job knows its type. The column is added to the table on the
next run.

## One-off full reloads and reconciles

To make the next scheduled run a full reload or reconcile, set `NEXT_RUN_MODE`. It's cleared once
a run picks it up.

```sql
UPDATE SNOWFORCE_JOBS SET NEXT_RUN_MODE = 'full' WHERE JOB_NAME = 'SUBSCRIPTION';
EXECUTE TASK SNOWFORCE_JOB_SUBSCRIPTION;   -- or wait for the schedule
```

## Pausing and removing a job

Pause:

```sql
UPDATE SNOWFORCE_JOBS SET ENABLED = FALSE WHERE JOB_NAME = 'SUBSCRIPTION';
CALL SNOWFORCE_SAVE_JOB_TASK('SUBSCRIPTION');   -- suspends the task
```

Remove the task and the job. The target table and its history stay:

```sql
DROP TASK IF EXISTS SNOWFORCE_JOB_SUBSCRIPTION;
DELETE FROM SNOWFORCE_JOBS WHERE JOB_NAME = 'SUBSCRIPTION';
```

## Checking runs

Every run, manual or scheduled, is a row in `SNOWFORCE_RUNS`:

```sql
SELECT JOB_NAME, MODE, STATUS, STARTED_AT, FINISHED_AT, ROWS_EXPORTED,
       ROWS_INSERTED, ROWS_UPDATED, ROWS_DELETED, MESSAGE
FROM SNOWFORCE_RUNS
ORDER BY STARTED_AT DESC
LIMIT 20;

-- Failures in the last week
SELECT JOB_NAME, STARTED_AT, MESSAGE
FROM SNOWFORCE_RUNS
WHERE STATUS = 'FAILED' AND STARTED_AT > DATEADD(day, -7, CURRENT_TIMESTAMP())
ORDER BY STARTED_AT DESC;
```

`MODE` is what the run actually did: a `sync` of a new table is recorded as `full`. `MESSAGE`
explains anything unusual, such as a full load of an empty table or how many rows a reconcile
flagged. `RUN_USER` and `RUN_ROLE` show who it ran as, and `SOQL` and `WATERMARK` show what it
asked Salesforce for.

If a scheduled run doesn't appear in `SNOWFORCE_RUNS` at all, the task failed before it started
(a missing grant, for example). Check the task's own history:

```sql
SELECT NAME, STATE, SCHEDULED_TIME, ERROR_MESSAGE
FROM TABLE(INFORMATION_SCHEMA.TASK_HISTORY(TASK_NAME => 'SNOWFORCE_JOB_SUBSCRIPTION'))
ORDER BY SCHEDULED_TIME DESC
LIMIT 10;
```

With `TRACK_HISTORY`, every version of a record:

```sql
SELECT * FROM CRM_DB.CMILLER_SF_PLAYGROUND.SUBSCRIPTION__C__HISTORY
WHERE ID = '<record id>' ORDER BY VALID_FROM;
```

## Troubleshooting

| Message | Fix |
| --- | --- |
| `No job named 'X'` | Check `JOB_NAME` in `SNOWFORCE_JOBS` |
| `Fields not on <Object>: ...` | Fix the field names, or `CALL SNOWFORCE_DESCRIBE` if they're new in Salesforce |
| `<Object> has no IsDeleted field` | Set the job's `DELETE_MODE` to `off` |
| `Salesforce returned no Ids; refusing to reconcile` | The integration user probably can't see the object's records; check its permissions |
| `A run-as role other than the app's own role needs a run-as user.` | Set `RUN_AS_USER`, or clear `RUN_AS_ROLE` |
| `Invalid schedule` | Use cron plus a time zone, e.g. `0 * * * * UTC` |
| Insufficient privileges on the target | The job's role can't create or write the target table; grant it, or change the role |
