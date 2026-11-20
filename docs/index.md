---
title: Home
nav_order: 1
---

# Snowforce

Snowforce copies Salesforce objects into Snowflake tables and keeps them up to date. Each sync
exports only the records that changed since the last one (by `SystemModstamp`), merges them on
`Id`, handles records deleted in Salesforce, and can keep a type 2 history of every change.

## Two ways to run it

| | Local CLI | Snowflake-native engine |
| --- | --- | --- |
| Runs on | Your machine | Snowflake (Python procedures and tasks) |
| Talks to Salesforce with | The `sf` CLI, logged in as you | An External Client App, JWT bearer flow |
| Configured in | `project.env` and `objects.conf` | The `SNOWFORCE_CONNECTIONS` and `SNOWFORCE_JOBS` tables |
| Scheduling | Whatever runs `./run.sh` | A Snowflake task per job |
| Run history | `state/` folder | `SNOWFORCE_RUNS` table |
| Status | In use | Phases 1 and 2 of the [Streamlit app design](streamlit-app-design.md): engine built, managed by SQL or a Streamlit app (early) |

Both load tables the same way, so the [sync rules](how-syncing-works.md) apply to either.

## Where to start

- **[Local setup](local-setup.md)**: install the CLIs, log in, and run `./run.sh`.
- **[Snowflake setup](snowflake-setup.md)**: deploy the engine, get the admin pieces in place,
  and add a Salesforce connection.
- **[Jobs in Snowflake](jobs.md)**: create a job, run it, schedule it, and check its runs.
- **[How syncing works](how-syncing-works.md)**: deltas, full loads, deletes, reconcile, and
  history.

## Repository layout

| Path | What it is |
| --- | --- |
| `run.sh`, `project.env`, `objects.conf` | The local CLI and its settings |
| `bin/` | `sf_schema.sh` (capture an object's schema) and `sf_to_snowflake.sh` (export and load one object) |
| `app/` | The Snowflake-native engine: `deploy.sh`, SQL, Python procedure code, and tests |
| `sql/adhoc/` | One-off load scripts |
| `docs/` | This site |
