# Salesforce → Snowflake

Exports Salesforce objects with the `sf` CLI (Bulk API 2.0) and loads them into
Snowflake with the `snow` CLI.

```
./run.sh                      # sync every object in objects.conf (delta)
./run.sh Subscription__c      # sync one object
./run.sh full [Object...]     # full reload (CREATE OR REPLACE TABLE)
./run.sh schema [Object...]   # refresh schema/ from Salesforce
```

## Layout

| Path | What it is |
| --- | --- |
| `project.env` | Salesforce org and Snowflake connection, database, schema, role, warehouse |
| `objects.conf` | Objects to sync and their Snowflake table names |
| `queries/<Object>.soql` | Fields to export. Generated with every field if missing; edit freely |
| `schema/<Object>.*` | `sf sobject describe` output: raw JSON, readable field list, Snowflake type map |
| `bin/sf_schema.sh` | Captures schema for one or more objects |
| `bin/sf_to_snowflake.sh` | Exports one query and loads it into one table (full or delta) |
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
- **Deletes are not synced.** Records deleted in Salesforce stay in Snowflake
  until the next `./run.sh full`.

## SSO prompts

A delta sync connects to Snowflake twice (watermark, then load), so with
browser SSO you may see two prompts per object. To cache the token, add
`client_store_temporary_credential = true` to the connection in
`~/.snowflake/connections.toml`.
