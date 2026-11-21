---
title: Home
nav_order: 1
---

# Glacier

Glacier syncs enterprise systems into Snowflake. Salesforce is supported today; ShipHero and
others are planned (see the [roadmap](glacier-roadmap.md)).

For Salesforce, Glacier copies objects into Snowflake tables and keeps them up to date. Each sync
exports only the records that changed since the last one (by `SystemModstamp`), merges them on
`Id`, handles records deleted in Salesforce, and can keep a type 2 history of every change.

## How it runs

| | |
| --- | --- |
| Runs on | Snowflake: Python procedures and tasks in `GLACIER_DB.APP` |
| Talks to Salesforce with | An External Client App, JWT bearer flow |
| Configured in | The `GLACIER_CONNECTIONS` and `GLACIER_JOBS` tables, through the `GLACIER_APP` Streamlit app or SQL |
| Scheduling | A Snowflake task per job |
| Run history | The `GLACIER_RUNS` table |

## Where to start

- **[Snowflake setup](snowflake-setup.md)**: deploy the engine, get the admin pieces in place,
  and add a Salesforce connection.
- **[Jobs in Snowflake](jobs.md)**: create a job, run it, schedule it, and check its runs.
- **[How syncing works](how-syncing-works.md)**: deltas, full loads, deletes, reconcile, and
  history.
- **[Roadmap](glacier-roadmap.md)**: the move to `GLACIER_DB.APP` and connectors beyond Salesforce.

## Repository layout

| Path | What it is |
| --- | --- |
| `app/` | The engine and the Streamlit app: `deploy.sh`, SQL, Python procedure code, and tests |
| `docs/` | This site |
