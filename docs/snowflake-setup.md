---
title: Snowflake setup
nav_order: 3
---

# Snowflake setup
{: .no_toc }

Deploy the Snowforce engine into Snowflake and connect it to a Salesforce org. Once this is done,
[create jobs](jobs.md) to sync objects.

1. TOC
{:toc}

## What gets set up, and by whom

```mermaid
flowchart LR
  TASK["Task per job"] -- "CALL" --> RUN["SNOWFORCE_RUN_JOB<br/>caller's rights"]
  RUN -- "CALL" --> EXT["SNOWFORCE_EXTRACT<br/>owner's rights"]
  SEC["Secret per connection"] -.-> EXT
  EXT -- "Bulk API 2.0<br/>via the integration" --> SF["Salesforce"]
  EXT -- "CSV parts" --> STG[("SNOWFORCE_EXPORTS stage")]
  STG -- "COPY + MERGE" --> RUN
  RUN -- "as the job's role" --> DEST[("Target table")]
  RUN -- "log" --> RUNS[("SNOWFORCE_RUNS")]
```

Only the owner's-rights procedures can read the Salesforce secrets. `SNOWFORCE_RUN_JOB` runs as
the job's role, so a job can only write where that role can write.

| Step | Who | How often |
| --- | --- | --- |
| [Deploy the app](#2-deploy-the-app) | You, as the app role | Once, then after every code change |
| [Create the external access integration](#3-create-the-external-access-integration) | Snowflake admin | Once |
| [Make a key pair](#4-make-a-key-pair) | You | Per connection |
| [Create an External Client App](#5-create-an-external-client-app-in-salesforce) | Salesforce admin | Per org |
| [Add the connection](#6-add-the-connection) | You | Per connection |

## Prerequisites

- The **Snowflake CLI** with a working connection; see [Local setup, step 3](local-setup.md#3-add-a-snowflake-connection).
- An **app role** that can create tables, stages, file formats, network rules, procedures, and
  tasks in the app schema, and can use its warehouse.
- To run the unit tests: Python 3.11 with `requests` and `cryptography`.

## 1. Configure the app

`app/app.env` says where the app lives:

| Setting | Current value | What it is |
| --- | --- | --- |
| `APP_DB`, `APP_SCHEMA` | `CRM_DB`, `CMILLER_SF_PLAYGROUND` | Schema for every app object (all prefixed `SNOWFORCE_`) |
| `APP_ROLE` | `DS_DATA_ENGINEERING_ROLE` | Owns the app objects and tasks |
| `APP_WAREHOUSE` | `SNOWFORCE_WH` | Warehouse used to deploy |
| `APP_INTEGRATION` | `SNOWFORCE_SALESFORCE_EAI` | External access integration the admin creates |
| `SNOW_CONNECTION` | `ODBM` | `snow` connection used to deploy |

Moving the app to a dedicated schema and role later means changing these, re-running
`deploy.sh`, and adding the connections again. See
[Where the app lives](streamlit-app-design.md#where-the-app-lives).

## 2. Deploy the app

```sh
app/deploy.sh          # create or update everything, in one Snowflake session
app/deploy.sh render   # print the SQL instead of running it
```

This creates the config tables (`SNOWFORCE_CONNECTIONS`, `SNOWFORCE_JOBS`, `SNOWFORCE_RUNS`,
`SNOWFORCE_FIELDS`), the code and exports stages, the CSV file format, and the
`SNOWFORCE_SALESFORCE_EGRESS` network rule. It uploads the Python code in `app/src/` and creates
the procedures.

Tables are only created if missing, so re-deploying keeps your connections, jobs, and run
history. Re-run it whenever `app/src/` or `app/sql/` changes.

Run the unit tests before deploying a code change:

```sh
python3 -m unittest discover -s app/tests
```

## 3. Create the external access integration

This needs `ACCOUNTADMIN` or a role with `CREATE INTEGRATION`, and the network rule from step 2.
Print the SQL and send it to a Snowflake admin:

```sh
app/deploy.sh admin-sql
```

It creates `SNOWFORCE_SALESFORCE_EAI`, which lets the procedures reach `login.salesforce.com`,
`test.salesforce.com`, `*.my.salesforce.com`, and `*.sandbox.my.salesforce.com`, and grants its
use to the app role. It allows every secret, so adding a connection later needs no admin.

Until it exists, `SNOWFORCE_REFRESH_CONNECTIONS` reports that the integration is missing, and the
procedures that talk to Salesforce aren't created.

The same file has the per-user grants needed for jobs that [run as a user](jobs.md#run-as-a-user-and-role).

## 4. Make a key pair

Snowforce logs in to Salesforce with the OAuth 2.0 JWT bearer flow: Salesforce holds a
certificate, and Snowflake holds the matching private key. Make one pair per connection:

```sh
openssl req -x509 -newkey rsa:2048 -nodes -days 730 -subj "/CN=Snowforce" \
  -keyout snowforce.key -out snowforce.crt
```

`snowforce.crt` goes to the Salesforce admin (step 5). `snowforce.key` goes into a Snowflake
secret (step 6). Never commit either; `.gitignore` excludes `*.key` and `*.crt`. Delete the local
key once the secret exists.

The certificate expires after `-days` (two years here). Note the date; see
[Rotating the key](#rotating-the-key).

## 5. Create an External Client App in Salesforce

A Salesforce admin does this once per org:

1. Create an **External Client App** with OAuth enabled and the `api` and `refresh_token`
   scopes.
2. Enable the **JWT bearer flow** and upload `snowforce.crt`.
3. Under the app's policies, set **Admin approved users are pre-authorized**.
4. Give the **integration user** access to the app through a permission set. That user needs
   **API Enabled** and read access to every object you'll sync.
5. Send back the app's **consumer key** and the integration user's **username**.

## 6. Add the connection

As the app role, create a secret with the private key, then a row in `SNOWFORCE_CONNECTIONS`.
Paste the whole key, `BEGIN` and `END` lines included. Snowflake masks secret values in query
history.

```sql
USE ROLE DS_DATA_ENGINEERING_ROLE;
USE WAREHOUSE SNOWFORCE_WH;
USE SCHEMA CRM_DB.CMILLER_SF_PLAYGROUND;

CREATE OR REPLACE SECRET SNOWFORCE_SECRET_ODBM_PROD
  TYPE = GENERIC_STRING
  SECRET_STRING = '-----BEGIN PRIVATE KEY-----
...
-----END PRIVATE KEY-----';

INSERT INTO SNOWFORCE_CONNECTIONS
  (CONNECTION_NAME, MY_DOMAIN_URL, CLIENT_ID, USERNAME, SECRET_NAME, DESCRIPTION)
VALUES ('ODBM_PROD', 'https://ourdailybreadministries.my.salesforce.com',
        '<consumer key>', '<integration user username>',
        'CRM_DB.CMILLER_SF_PLAYGROUND.SNOWFORCE_SECRET_ODBM_PROD', 'Production org');
```

| Column | Notes |
| --- | --- |
| `CONNECTION_NAME` | A plain identifier (letters, digits, `_`); jobs refer to it |
| `MY_DOMAIN_URL` | `https://<domain>.my.salesforce.com`. A `.sandbox.my.salesforce.com` URL logs in through `test.salesforce.com` |
| `CLIENT_ID` | The External Client App's consumer key |
| `USERNAME` | The integration user the jobs run as in Salesforce |
| `API_VERSION` | Defaults to `64.0` |
| `SECRET_NAME` | The secret, fully qualified. The app role must own it or have `USAGE` on it |

Then re-create the Salesforce procedures so they can read the new secret, and test the login:

```sql
CALL SNOWFORCE_REFRESH_CONNECTIONS();
CALL SNOWFORCE_TEST_CONNECTION('ODBM_PROD');   -- returns the instance URL and a count of objects
```

Call `SNOWFORCE_REFRESH_CONNECTIONS()` after every connection you add or remove: a procedure's
secrets are fixed when it's created. `deploy.sh` calls it too.

## Procedures

| Procedure | Runs as | Does |
| --- | --- | --- |
| `SNOWFORCE_RUN_JOB(job, [mode])` | caller | Sync, full reload, or reconcile one job; logs to `SNOWFORCE_RUNS` |
| `SNOWFORCE_SAVE_JOB_TASK(job)` | owner | Create or replace the job's task from its `SNOWFORCE_JOBS` row |
| `SNOWFORCE_REFRESH_CONNECTIONS()` | owner | Re-create the Salesforce procedures with every connection's secret |
| `SNOWFORCE_TEST_CONNECTION(conn)` | owner | Log in and count queryable objects |
| `SNOWFORCE_LIST_OBJECTS(conn)` | owner | List queryable objects |
| `SNOWFORCE_DESCRIBE(conn, object)` | owner | Capture an object's fields into `SNOWFORCE_FIELDS` |
| `SNOWFORCE_EXTRACT(conn, soql, all_rows, run_id)` | owner | Bulk API export to the `SNOWFORCE_EXPORTS` stage |

The last four exist only once the integration does.

## Rotating the key

Before the certificate expires:

1. Make a new key pair (step 4).
2. Have the Salesforce admin upload the new certificate to the External Client App.
3. Replace the secret with the new key: `CREATE OR REPLACE SECRET SNOWFORCE_SECRET_ODBM_PROD ...`.
4. `CALL SNOWFORCE_REFRESH_CONNECTIONS();` then `CALL SNOWFORCE_TEST_CONNECTION('ODBM_PROD');`

## Troubleshooting

| Message | Fix |
| --- | --- |
| `Integration SNOWFORCE_SALESFORCE_EAI doesn't exist yet, or this role can't use it` | Step 3 hasn't been done, or the grant to the app role is missing |
| `Connection X has no usable secret` | Call `SNOWFORCE_REFRESH_CONNECTIONS()`; check `SECRET_NAME` and that the app role can use the secret |
| `Connection X has no CLIENT_ID` (or `USERNAME`) | Fill in the column on the connection row |
| Unknown procedure `SNOWFORCE_TEST_CONNECTION` | The integration is missing (step 3), or `SNOWFORCE_REFRESH_CONNECTIONS()` hasn't run since |
| Salesforce login errors | Check the certificate is uploaded, the user is pre-authorized through the permission set, and the consumer key and username match |
