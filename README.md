# Glacier

Syncs enterprise systems into Snowflake. Salesforce is supported today; ShipHero and others are
planned (see the [roadmap](docs/glacier-roadmap.md)).

Glacier runs inside Snowflake, in `GLACIER_DB.APP`. Each sync job is a Snowflake task that calls
Python procedures: they export the records changed since the last run (Salesforce's Bulk API 2.0),
merge them into the job's table, handle deleted records, and can keep a type 2 history of every
change. A Streamlit app, `GLACIER_APP`, manages the connections and jobs and shows the runs.

Full documentation is in [docs/](docs/index.md), published with GitHub Pages:

- [Snowflake setup](docs/snowflake-setup.md): deploy the app and add a connection
- [Jobs](docs/jobs.md): create, run, and schedule a job
- [How syncing works](docs/how-syncing-works.md): deltas, deletes, reconcile, history, and names

## Commands

```
app/deploy.sh                    # create or update the app in Snowflake
app/deploy.sh streamlit          # update only the Streamlit app
app/deploy.sh admin-sql setup    # one-time admin SQL: role, warehouse, database, schemas
python3 -m unittest discover -s app/tests
```

## Layout

| Path | What it is |
| --- | --- |
| `app/` | The engine and the Streamlit app: `deploy.sh`, SQL, Python procedure code, and tests. See [app/README.md](app/README.md) |
| `docs/` | The documentation site |
| `TODOs.md` | Data checks to build |
