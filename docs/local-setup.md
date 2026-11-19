---
title: Local setup
nav_order: 2
---

# Local setup
{: .no_toc }

Run Snowforce from your own machine with the `sf` and `snow` CLIs.

1. TOC
{:toc}

## Prerequisites

- **macOS or Linux** with `bash`, `awk`, `perl`, and `jq` (`brew install jq`).
- **Salesforce CLI** (`sf`): `brew install --cask sf`, or `npm install --global @salesforce/cli`.
- **Snowflake CLI** (`snow`): `brew install snowflake-cli`, or `pipx install snowflake-cli`.
- A **Salesforce user** with API access and read access to the objects you want to sync.
- A **Snowflake role** that can create and write tables in the target schema, and a warehouse it
  can use. The defaults are `DS_DATA_ENGINEERING_ROLE`, `SNOWFORCE_WH`, and
  `CRM_DB.CMILLER_SF_PLAYGROUND`.

## 1. Clone the repo

```sh
git clone https://github.com/cranforce/Snowforce.git
cd Snowforce
```

## 2. Log in to Salesforce

Log in once with an alias. `project.env` expects `odbm-npc-prod`; use any alias and set `SF_ORG`
to match.

```sh
sf org login web --alias odbm-npc-prod
# A sandbox:
sf org login web --alias my-sandbox --instance-url https://test.salesforce.com

sf org display --target-org odbm-npc-prod   # check it worked
```

## 3. Add a Snowflake connection

`project.env` expects a `snow` connection named `ODBM`. Add it with `snow connection add`, or in
`~/.snowflake/connections.toml`:

```toml
[ODBM]
account = "<account identifier>"
user = "<you>@<your domain>"
authenticator = "externalbrowser"
role = "DS_DATA_ENGINEERING_ROLE"
warehouse = "SNOWFORCE_WH"
# Caches the SSO token, so a sync doesn't open a browser tab for every connection
client_store_temporary_credential = true
```

Check it:

```sh
snow connection test -c ODBM
```

## 4. Check the settings

`project.env` holds the defaults. Anything already set in your environment wins, so you can
override one setting for one run: `SNOW_WAREHOUSE=OTHER_WH ./run.sh`.

| Setting | Default | What it is |
| --- | --- | --- |
| `SF_ORG` | `odbm-npc-prod` | `sf` org alias |
| `SNOW_CONNECTION` | `ODBM` | `snow` connection name |
| `SNOW_DB`, `SNOW_SCHEMA` | `CRM_DB`, `CMILLER_SF_PLAYGROUND` | Where tables are created |
| `SNOW_ROLE`, `SNOW_WAREHOUSE` | `DS_DATA_ENGINEERING_ROLE`, `SNOWFORCE_WH` | Role and warehouse for loads |
| `DELETE_MODE` | `flag` | `off`, `flag`, or `remove`; see [Deletes](how-syncing-works.md#deletes) |
| `RECONCILE_AFTER_DAYS` | `14` | Reconcile deletes when an object hasn't synced in this many days |

## 5. Choose objects

`objects.conf` lists the objects to sync, one per line:

```
<Salesforce object>   <Snowflake table>   [off|flag|remove]   [history]
Account               ACCOUNT             history
Order                 ORDER               flag   history
Subscription__c       SUBSCRIPTION
```

The delete mode defaults to `DELETE_MODE`. `history` keeps a type 2 history table; see
[History](how-syncing-works.md#history). Reserved words such as `ORDER` work as table names;
they're quoted in SQL, so quote them in your queries too (`SELECT * FROM "ORDER"`).

## 6. First sync

```sh
./run.sh schema Subscription__c   # capture the schema and generate queries/Subscription__c.soql
# Optional: edit queries/Subscription__c.soql to trim the field list
./run.sh Subscription__c          # first run is a full load; later runs are deltas
```

`./run.sh` does the schema step for you if it's missing, so for an object you're happy to load
with every field, `./run.sh <Object>` is enough.

## Commands

| Command | Does |
| --- | --- |
| `./run.sh` | Sync every object in `objects.conf` (delta, deletes, reconcile when due) |
| `./run.sh <Object>...` | Sync just these objects |
| `./run.sh full [Object...]` | Full reload (`CREATE OR REPLACE TABLE`) |
| `./run.sh reconcile [Object...]` | Compare every Id against Salesforce to catch old and hard deletes |
| `./run.sh schema [Object...]` | Refresh `schema/` from Salesforce |
| `./run.sh --help` | Usage |

Run it on a schedule with cron or launchd if you like, but scheduled loads are what the
[Snowflake engine](jobs.md) is for: tasks don't depend on your laptop or an SSO login.

## Local files

These are created as you go and aren't committed:

| Path | What it is |
| --- | --- |
| `queries/<Object>.soql` | Fields to export. Generated with every field if missing; edit freely |
| `schema/<Object>.*` | `sf sobject describe` output: raw JSON, readable field list, Snowflake type map |
| `state/<Object>.last_sync` | Time of the last successful sync, used to decide when to reconcile |

Deleting `state/` is safe: the next sync just reconciles every object.

## Adding a field

Add it to `queries/<Object>.soql` and sync; the column is added to the table. If the field is new
in Salesforce, run `./run.sh schema <Object>` first so it gets the right type. (`schema` doesn't
overwrite existing query files.)

## Troubleshooting

- **Two SSO browser prompts per object.** A delta sync connects to Snowflake twice. Set
  `client_store_temporary_credential = true` on the connection (step 3).
- **`<Object> is not in objects.conf; skipping.`** Add a line for it; the first column is the
  Salesforce API name, matched exactly.
- **A text field such as a postal code loaded as a number.** The schema is missing, so types were
  inferred. Run `./run.sh schema <Object>`, then `./run.sh full <Object>`.
