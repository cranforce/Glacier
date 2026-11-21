---
title: Glacier roadmap
nav_order: 7
---

# Glacier roadmap

Glacier started as Snowforce, a Salesforce-to-Snowflake sync. It is becoming a general way to
sync enterprise systems into Snowflake. Salesforce is the first source and ShipHero the next,
each a *connector* behind the same jobs, runs, scheduling, delete handling, and history.

Status: phases 1 to 3 are done. Glacier runs in `GLACIER_DB.APP`, and the Snowforce tasks are
suspended. Phase 3's last step, dropping the old Snowforce objects, is still to do.

1. TOC
{:toc}

## Phases

| Phase | What | Where | Status |
| --- | --- | --- | --- |
| 1 | Rename to Glacier; give it its own database, schema, role, and warehouse | Repo | Done |
| 2 | Connector abstraction, with Salesforce as the first connector and no change in behaviour | Repo | Done |
| 3 | Stand up `GLACIER_DB.APP` in Snowflake, move the connections and jobs, and cut over | Snowflake | Done, except dropping the old objects |
| 4 | ShipHero connector | Repo, then Snowflake | Next |

Phase 2 comes before phase 3 on purpose: the new tables are then created in their final shape,
and the connections and jobs are copied only once.

## Phase 1: rename and new home (done)

- Every `SNOWFORCE_*` object is now `GLACIER_*`: tables, stages, procedures, tasks, secrets, the
  network rule, the integration (`GLACIER_SALESFORCE_EAI`), the Streamlit app (`GLACIER_APP`),
  and the warehouse (`GLACIER_WH`). The Python modules are `glacier_core.py`, `glacier_procs.py`,
  `glacier_ui.py`, and `glacier_forms.py`.
- `app/app.env` points at `GLACIER_DB.APP`, owned by `GLACIER_APP_ROLE`.
- `app/sql/00_admin_setup.sql` (`app/deploy.sh admin-sql setup`) is the admin's one-time SQL for
  the role, warehouse, database, and schema. The integration SQL is now
  `app/deploy.sh admin-sql integration`.
- The `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` upgrades for pre-JWT and pre-history tables are
  gone, since the new tables start fresh.
- Synced data defaults to `GLACIER_DB.NPC_SALESFORCE_PROD_RAW`, which `app/deploy.sh admin-sql setup` creates.

## Phase 2: connectors (done)

The engine assumed Salesforce throughout: SOQL, the Bulk API, `Id` and `SystemModstamp`, the
recycle bin, and Salesforce field types. It's now split in two:

- **The core** (`glacier_core.py`, `glacier_procs.py`) keeps everything that is about Snowflake:
  jobs and runs, tasks, staging, `COPY`, `MERGE`, deletes, reconcile, and type 2 history.
- **Each connector** (`connector_salesforce.py`, later `connector_shiphero.py`) says how to log
  in, list objects, describe their fields, and export changed records to the stage.
  `glacier_connectors.py` lists them and documents what a connector provides.

For Salesforce, the generated load SQL is identical to before; it was compared statement by
statement against phase 1's for every delete mode, with and without history.

### What a connector provides

| Piece | Salesforce | ShipHero (to confirm in phase 4) |
| --- | --- | --- |
| Connection settings | My Domain URL, consumer key, username, API version | None beyond the account |
| Secret | PEM private key (JWT bearer) | Refresh token, exchanged for an access token |
| Hosts for the network rule | `login`/`test.salesforce.com`, `*.my.salesforce.com`, `*.sandbox.my.salesforce.com` | `public-api.shiphero.com` |
| Objects | Every queryable sObject | A fixed list Glacier supports, e.g. orders, products, shipments |
| Fields and types | `describe` | Defined in the connector, per object |
| Key field | `Id` | `id` |
| Change field (watermark) | `SystemModstamp` | `updated_at`, where the query supports `updated_from` |
| Deleted flag | `IsDeleted` (recycle bin), plus a reconcile of every `Id` | Unknown; possibly reconcile only, or none |
| Export | Bulk API 2.0 CSV pages | GraphQL pages, cursor paginated within its credit limit, written as CSV |

The key, change, and deleted fields are the connector's `KEYS`, which the core's load and history
SQL use in place of the `ID`, `SYSTEMMODSTAMP`, and `ISDELETED` it used to hard-code. A connector
offers only the delete modes it supports.

### Table changes

| Table | Change |
| --- | --- |
| `GLACIER_CONNECTIONS` | Added `CONNECTOR` (`salesforce`). `MY_DOMAIN_URL`, `CLIENT_ID`, `USERNAME`, and `API_VERSION` moved into a `SETTINGS` object, which each connector validates |
| `GLACIER_JOBS` | `SOBJECT` became `SOURCE_OBJECT` |
| `GLACIER_FIELDS` | `SOBJECT` became `SOURCE_OBJECT`, and `SF_TYPE` became `SOURCE_TYPE` |
| `GLACIER_RUNS` | `SOQL` became `SOURCE_QUERY` |

### Procedures and integrations

Each connector has its own owner's-rights procedures, `GLACIER_<CONNECTOR>_TEST_CONNECTION`,
`_LIST_OBJECTS`, `_DESCRIBE`, and `_EXTRACT`; its own network rule, `GLACIER_<CONNECTOR>_EGRESS`; and
its own external access integration, `GLACIER_<CONNECTOR>_EAI`. Each procedure set holds only its
own connections' secrets and can reach only its own hosts. `GLACIER_REFRESH_CONNECTIONS` re-creates
every connector's procedures and skips a connector whose integration doesn't exist yet.
`GLACIER_RUN_JOB` stays a single caller's-rights procedure that calls the job's connector's
extract.

The integration name follows the convention, so `app.env` no longer has `APP_INTEGRATION`. Each
connector's integration is a separate one-time admin step:
`app/deploy.sh admin-sql integration <connector>`.

A job's run-as role now also needs `SELECT` on `GLACIER_CONNECTIONS`, to find the job's connector.

### Streamlit

- **Connections:** choose the connector first. The rest of the form comes from that connector's
  settings.
- **Jobs:** objects and fields come from whatever the connector's list and describe return, and
  the labels, placeholders, and delete modes come from the connector.

## Phase 3: stand up Glacier in Snowflake (done 2026-11-20)

1. An admin ran `app/deploy.sh admin-sql setup`: `GLACIER_APP_ROLE`, `GLACIER_WH` (small),
   `GLACIER_DB`, and its `APP` and `NPC_SALESFORCE_PROD_RAW` schemas.
2. `app/deploy.sh` deployed the tables, procedures, and the `GLACIER_APP` Streamlit app.
3. An admin ran `app/deploy.sh admin-sql integration salesforce` (`GLACIER_SALESFORCE_EAI`).
4. A one-off migration script, since removed, copied from `CRM_DB.CMILLER_SF_PLAYGROUND`:
   - the `SNOWFORCE_*` connection, 14 jobs, captured fields, and run history into the
     `GLACIER_*` tables, with the jobs pointed at `GLACIER_DB.NPC_SALESFORCE_PROD_RAW` and
     `GLACIER_WH`;
   - the 28 synced tables (14 current, 14 history) as zero-copy clones, so each job's watermark
     carried over and its first run was a delta, not a full load.
5. The connection's private key was re-entered on the Connections page, and each job's task was
   created. The 14 `SNOWFORCE_JOB_*` tasks were suspended first, so the two never overlapped.
6. Tables and columns were renamed to the Salesforce connector's naming convention
   (`GIFT_TRANSACTION`, `SYSTEM_MODSTAMP`, `FINANCIAL_BATCH_C`; see
   [Column names](how-syncing-works.md#column-names)), and the jobs' `TARGET_TABLE` cleared to
   follow it.
7. The local CLI (`run.sh`, `bin/`, `objects.conf`, `project.env`) was retired: Glacier's tasks
   replace it.

Still to do: once the Glacier jobs have run cleanly for a while, drop the old `SNOWFORCE_*`
objects and synced tables in `CRM_DB.CMILLER_SF_PLAYGROUND`, and `SNOWFORCE_WH` if nothing else
uses it.

## Phase 4: ShipHero

Answer these first:

- Which entities matter: orders, products, inventory, shipments, returns, purchase orders?
- How to load nested data, such as order line items and shipment packages. One option is a
  `VARIANT` column on the parent row, the other is a child table per nested list, keyed to the
  parent.
- Which queries support `updated_from` for deltas, and which need a full reload each time.
- How deletes show up, if at all.
- Whether one ShipHero account is enough, or 3PL child accounts are needed.
- How the GraphQL credit limits constrain a run's size and schedule.

## Open decisions

- **Object prefix.** Objects keep `GLACIER_` even in their own schema (see
  [Where the app lives](streamlit-app-design.md#where-the-app-lives)). Now that the app is
  deployed, dropping the prefix means renaming its tables, procedures, tasks, and secrets.
