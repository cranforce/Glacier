---
title: Snowflake setup
nav_order: 3
---

# Snowflake setup
{: .no_toc }

Deploy the Glacier engine into Snowflake and connect it to a Salesforce org. Once this is done,
[create jobs](jobs.md) to sync objects.

1. TOC
{:toc}

## What gets set up, and by whom

```mermaid
flowchart LR
  TASK["Task per job"] -- "CALL" --> RUN["GLACIER_RUN_JOB<br/>caller's rights"]
  RUN -- "CALL" --> EXT["GLACIER_SALESFORCE_EXTRACT<br/>owner's rights"]
  SEC["Secret per connection"] -.-> EXT
  EXT -- "Bulk API 2.0<br/>via the integration" --> SF["Salesforce"]
  EXT -- "CSV parts" --> STG[("GLACIER_EXPORTS stage")]
  STG -- "COPY + MERGE" --> RUN
  RUN -- "as the job's role" --> DEST[("Target table")]
  RUN -- "log" --> RUNS[("GLACIER_RUNS")]
```

Only the owner's-rights procedures can read the Salesforce secrets. `GLACIER_RUN_JOB` runs as
the job's role, so a job can only write where that role can write.

| Step | Who | How often |
| --- | --- | --- |
| [Create the app's home](#1-configure-the-app) | Snowflake admin | Once |
| [Deploy the app](#2-deploy-the-app) | You, as the app role | Once, then after every code change |
| [Create the external access integration](#3-create-the-external-access-integration) | Snowflake admin | Once |
| [Make a key pair](#4-make-a-key-pair) | You | Per connection |
| [Create an External Client App](#5-create-an-external-client-app-in-salesforce) | Salesforce admin | Per org |
| [Add the connection](#6-add-the-connection) | You | Per connection |

## Prerequisites

- The **Snowflake CLI** with a working connection; see [Local setup, step 3](local-setup.md#3-add-a-snowflake-connection).
- The **app role** granted to you (`GRANT ROLE GLACIER_APP_ROLE TO USER <you>`), once an admin
  has created it in step 1.
- To run the unit tests: Python 3.11 with `requests` and `cryptography`.

## 1. Configure the app

`app/app.env` says where the app lives:

| Setting | Current value | What it is |
| --- | --- | --- |
| `APP_DB`, `APP_SCHEMA` | `GLACIER_DB`, `APP` | The app's own database and schema (every object is prefixed `GLACIER_`) |
| `APP_ROLE` | `GLACIER_APP_ROLE` | Owns the app schema, its objects, and the job tasks |
| `APP_WAREHOUSE` | `GLACIER_WH` | Warehouse for deploys, the Streamlit app, and job runs |
| `SNOW_CONNECTION` | `ODBM` | `snow` connection used to deploy |

The database, schema, role, and warehouse are created once by a Snowflake admin. Print the SQL
and send it to them:

```sh
app/deploy.sh admin-sql setup
```

It creates the role (granted to `SYSADMIN`), a small warehouse, the database, and the
schema, which the app role owns. It also creates `GLACIER_DB.NPC_SALESFORCE_PROD_RAW`, the default
destination for Salesforce data, also owned by the app role, and grants the account-level
`EXECUTE TASK` the app's run buttons need. Other roles that load data there, such as the local
CLI's role, need the commented grants at the end. See
[Where the app lives](streamlit-app-design.md#where-the-app-lives).

## 2. Deploy the app

```sh
app/deploy.sh          # create or update everything, in one Snowflake session
app/deploy.sh render   # print the SQL instead of running it
```

This creates the config tables (`GLACIER_CONNECTIONS`, `GLACIER_JOBS`, `GLACIER_RUNS`,
`GLACIER_FIELDS`), the code and exports stages, the CSV file format, and the
`GLACIER_SALESFORCE_EGRESS` network rule. It uploads the Python code in `app/src/`, creates
the procedures, and creates the `GLACIER_APP` Streamlit app (`app/deploy.sh streamlit` updates
only the app).

Tables are only created if missing, so re-deploying keeps your connections, jobs, and run
history. Re-run it whenever `app/src/` or `app/sql/` changes.

Run the unit tests before deploying a code change:

```sh
python3 -m unittest discover -s app/tests
```

## 3. Create the external access integration

Each connector needs its own integration, `GLACIER_<CONNECTOR>_EAI`. This needs `ACCOUNTADMIN` or
a role with `CREATE INTEGRATION`, and the connector's network rule from step 2. Print the SQL and
send it to a Snowflake admin:

```sh
app/deploy.sh admin-sql integration salesforce
```

It creates `GLACIER_SALESFORCE_EAI`, which lets the procedures reach `login.salesforce.com`,
`test.salesforce.com`, `*.my.salesforce.com`, and `*.sandbox.my.salesforce.com`, and grants its
use to the app role. It allows every secret, so adding a connection later needs no admin.

Until it exists, `GLACIER_REFRESH_CONNECTIONS` reports that it skipped Salesforce, and the
procedures that talk to Salesforce aren't created.

The same file has the per-user grants needed for jobs that [run as a user](jobs.md#run-as-a-user-and-role).

## 4. Make a key pair

Glacier logs in to Salesforce with the OAuth 2.0 JWT bearer flow: Salesforce holds a
certificate, and Snowflake holds the matching private key. Make one pair per connection:

```sh
openssl req -x509 -newkey rsa:2048 -nodes -days 730 -subj "/CN=Glacier" \
  -keyout glacier.key -out glacier.crt
```

`glacier.crt` goes to the Salesforce admin (step 5). `glacier.key` goes into a Snowflake
secret (step 6). Never commit either; `.gitignore` excludes `*.key` and `*.crt`. Delete the local
key once the secret exists.

The certificate expires after `-days` (two years here). Note the date; see
[Rotating the key](#rotating-the-key).

## 5. Create an External Client App in Salesforce

A Salesforce admin does this once per org:

1. Create an **External Client App** with OAuth enabled and the `api` and `refresh_token`
   scopes.
2. Enable the **JWT bearer flow** and upload `glacier.crt`.
3. Under the app's policies, set **Admin approved users are pre-authorized**.
4. Give the **integration user** access to the app through a permission set. That user needs
   **API Enabled** and read access to every object you'll sync.
5. Send back the app's **consumer key** and the integration user's **username**.

## 6. Add the connection

As the app role, create a secret with the private key, then a row in `GLACIER_CONNECTIONS`.
Paste the whole key, `BEGIN` and `END` lines included. Snowflake masks secret values in query
history.

```sql
USE ROLE GLACIER_APP_ROLE;
USE WAREHOUSE GLACIER_WH;
USE SCHEMA GLACIER_DB.APP;

CREATE OR REPLACE SECRET GLACIER_SECRET_ODBM_PROD
  TYPE = GENERIC_STRING
  SECRET_STRING = '-----BEGIN PRIVATE KEY-----
...
-----END PRIVATE KEY-----';

INSERT INTO GLACIER_CONNECTIONS (CONNECTION_NAME, CONNECTOR, SETTINGS, SECRET_NAME, DESCRIPTION)
SELECT 'ODBM_PROD', 'salesforce',
       OBJECT_CONSTRUCT('MY_DOMAIN_URL', 'https://ourdailybreadministries.my.salesforce.com',
                        'CLIENT_ID', '<consumer key>',
                        'USERNAME', '<integration user username>',
                        'API_VERSION', '64.0'),
       'GLACIER_DB.APP.GLACIER_SECRET_ODBM_PROD', 'Production org';
```

| Column | Notes |
| --- | --- |
| `CONNECTION_NAME` | A plain identifier (letters, digits, `_`); jobs refer to it |
| `CONNECTOR` | `salesforce` |
| `SETTINGS` | The connector's settings, below |
| `SECRET_NAME` | The secret, fully qualified. The app role must own it or have `USAGE` on it |

Salesforce's `SETTINGS`:

| Key | Notes |
| --- | --- |
| `MY_DOMAIN_URL` | `https://<domain>.my.salesforce.com`. A `.sandbox.my.salesforce.com` URL logs in through `test.salesforce.com` |
| `CLIENT_ID` | The External Client App's consumer key |
| `USERNAME` | The integration user the jobs run as in Salesforce |
| `API_VERSION` | Defaults to `64.0` |

Then re-create the Salesforce procedures so they can read the new secret, and test the login:

```sql
CALL GLACIER_REFRESH_CONNECTIONS();
CALL GLACIER_SALESFORCE_TEST_CONNECTION('ODBM_PROD');   -- returns the instance URL and a count of objects
```

Call `GLACIER_REFRESH_CONNECTIONS()` after every connection you add or remove: a procedure's
secrets are fixed when it's created. `deploy.sh` calls it too.

## Procedures

| Procedure | Runs as | Does |
| --- | --- | --- |
| `GLACIER_RUN_JOB(job, [mode])` | caller | Sync, full reload, or reconcile one job; logs to `GLACIER_RUNS` |
| `GLACIER_SAVE_JOB_TASK(job)` | owner | Create or replace the job's task from its `GLACIER_JOBS` row |
| `GLACIER_REFRESH_CONNECTIONS()` | owner | Re-create each connector's procedures with its connections' secrets |
| `GLACIER_SALESFORCE_TEST_CONNECTION(conn)` | owner | Log in and count queryable objects |
| `GLACIER_SALESFORCE_LIST_OBJECTS(conn)` | owner | List queryable objects |
| `GLACIER_SALESFORCE_DESCRIBE(conn, object)` | owner | Capture an object's fields into `GLACIER_FIELDS` |
| `GLACIER_SALESFORCE_EXTRACT(conn, query, include_deleted, run_id)` | owner | Bulk API export to the `GLACIER_EXPORTS` stage |

The last four are Salesforce's own; each connector gets its set, `GLACIER_<CONNECTOR>_<ACTION>`, holding only its connections' secrets. They exist only once the connector's integration does.

## Rotating the key

Before the certificate expires:

1. Make a new key pair (step 4).
2. Have the Salesforce admin upload the new certificate to the External Client App.
3. Replace the secret with the new key: `CREATE OR REPLACE SECRET GLACIER_SECRET_ODBM_PROD ...`.
4. `CALL GLACIER_REFRESH_CONNECTIONS();` then `CALL GLACIER_SALESFORCE_TEST_CONNECTION('ODBM_PROD');`

## Troubleshooting

| Message | Fix |
| --- | --- |
| `Skipped salesforce (no GLACIER_SALESFORCE_EAI)` | Step 3 hasn't been done, or the grant to the app role is missing |
| `Connection X has no usable secret` | Call `GLACIER_REFRESH_CONNECTIONS()`; check `SECRET_NAME` and that the app role can use the secret |
| `Consumer key is required` (or `Integration user`) | Fill in the setting on the connection |
| Unknown procedure `GLACIER_SALESFORCE_TEST_CONNECTION` | The integration is missing (step 3), or `GLACIER_REFRESH_CONNECTIONS()` hasn't run since |
| Salesforce login errors | Check the certificate is uploaded, the user is pre-authorized through the permission set, and the consumer key and username match |
