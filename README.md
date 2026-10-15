# Salesforce → Snowflake

Exports Salesforce objects with the `sf` CLI (Bulk API 2.0) and loads them into
Snowflake with the `snow` CLI.

```
./run.sh                        # sync every object in objects.conf (delta + deletes)
./run.sh Subscription__c        # sync one object
./run.sh full [Object...]       # full reload (CREATE OR REPLACE TABLE)
./run.sh reconcile [Object...]  # catch deletes older than the recycle bin
./run.sh schema [Object...]     # refresh schema/ from Salesforce
```

## Layout

| Path | What it is |
| --- | --- |
| `project.env` | Salesforce org and Snowflake connection, database, schema, role, warehouse |
| `objects.conf` | Objects to sync, their Snowflake table names, and optional per-object delete mode |
| `queries/<Object>.soql` | Fields to export. Generated with every field if missing; edit freely |
| `schema/<Object>.*` | `sf sobject describe` output: raw JSON, readable field list, Snowflake type map |
| `bin/sf_schema.sh` | Captures schema for one or more objects |
| `bin/sf_to_snowflake.sh` | Exports one query and loads it into one table (full or delta) |
| `state/` | Last successful sync time per object (local only, not committed) |
| `sql/adhoc/` | One-off load scripts |

## How syncing works

- **Types** come from the Salesforce schema (`schema/<Object>.types.tsv`), so
  text fields such as postal codes stay `VARCHAR`, and datetimes load as
  `TIMESTAMP_NTZ` in UTC.
- **Delta**: reads `MAX(SYSTEMMODSTAMP)` from the table, exports records with
  `SystemModstamp >= <that time>`, and `MERGE`s them on `ID`. Records at the
  boundary second are re-fetched and merged harmlessly. `Id` and
  `SystemModstamp` are added to every query automatically.
- **Full load** happens automatically when the table doesn't exist, is empty,
  or has no `SYSTEMMODSTAMP` column.
- **New fields**: add them to the query file and sync; missing columns are
  added to the table. Run `./run.sh schema` first if the field is new in
  Salesforce, so it gets the right type.

## Deletes

`DELETE_MODE` in `project.env` (or a third column in `objects.conf`) controls
what happens to records deleted in Salesforce:

| Mode | Effect |
| --- | --- |
| `flag` (default) | Row stays, with `ISDELETED = TRUE`. Filter with `WHERE NOT ISDELETED`. |
| `remove` | Row is deleted from the Snowflake table. |
| `off` | Deletes are ignored. |

A sync sees deletes through the Salesforce **recycle bin** (`--all-rows`):
deleting a record bumps its `SystemModstamp`, so it comes through the delta
with `IsDeleted = true`. Undeleted records come back the same way.

The recycle bin only keeps records for about **15 days**, and less if it fills
up or someone empties it. Hard deletes skip it entirely. To catch those, a
**reconcile** exports every current Id (a cheap, Id-only query) and flags or
removes rows whose Id is gone. Sync runs one automatically when an object's
last successful sync (`state/<Object>.last_sync`) is more than
`RECONCILE_AFTER_DAYS` (default 14) ago, or was never recorded. Run
`./run.sh reconcile` yourself any time, e.g. after a mass delete.

So, syncing at least every couple of weeks keeps deletes cheap. Syncing less
often still works, but each run then also does the Id comparison.

Notes:
- If the query has a `WHERE` clause, reconcile uses it too, so rows that stop
  matching the filter are treated as deleted. Queries with `LIMIT`/`OFFSET`
  can't be reconciled.
- Reconcile refuses to run if Salesforce returns no Ids, so a permissions
  problem can't wipe the table.
- In `flag` mode, `./run.sh full` rebuilds the table from live records plus
  the recycle bin, so rows flagged longer ago than that drop out.

## SSO prompts

A delta sync connects to Snowflake twice (watermark, then load), so with
browser SSO you may see two prompts per object. To cache the token, add
`client_store_temporary_credential = true` to the connection in
`~/.snowflake/connections.toml`.
