---
title: How syncing works
nav_order: 5
---

# How syncing works
{: .no_toc }

The local CLI and the Snowflake engine load tables the same way. Where they're configured
differently, both are shown.

1. TOC
{:toc}

## Types

Column types come from the Salesforce schema (`sf sobject describe` locally, `GLACIER_FIELDS`
in Snowflake). Text fields such as postal codes stay `VARCHAR`, and datetimes load as
`TIMESTAMP_NTZ` in UTC.

## Delta sync

A sync reads `MAX(SYSTEMMODSTAMP)` from the table, exports records with
`SystemModstamp >= <that time>`, and `MERGE`s them on `ID`. Records at the boundary second are
fetched again and merged harmlessly. `Id` and `SystemModstamp` are always exported, even if the
field list leaves them out.

A sync does a **full load** instead when the table doesn't exist, is empty, or has no
`SYSTEMMODSTAMP` column.

## New fields

Add the field to the job and sync; missing columns are added to the table. If the field is new
in Salesforce, refresh the schema first so it gets the right type:

| | |
| --- | --- |
| Local | Add it to `queries/<Object>.soql`; run `./run.sh schema <Object>` if it's new |
| Snowflake | Add it to the job's `FIELDS`; `CALL GLACIER_SALESFORCE_DESCRIBE('<connection>', '<Object>')` if it's new |

## Deletes

The delete mode controls what happens to records deleted in Salesforce:

| Mode | Effect |
| --- | --- |
| `flag` (default) | The row stays, with `ISDELETED = TRUE`. Filter with `WHERE NOT ISDELETED`. |
| `remove` | The row is deleted from the Snowflake table. |
| `off` | Deletes are ignored. |

Set it in `project.env` or `objects.conf` locally, or in the job's `DELETE_MODE` in Snowflake.

A sync sees deletes through the Salesforce **recycle bin**: deleting a record bumps its
`SystemModstamp`, so it comes through the delta with `IsDeleted = true`. Undeleted records come
back the same way.

## Reconcile

The recycle bin keeps records for about **15 days**, and less if it fills up or someone empties
it. Hard deletes skip it entirely. To catch those, a **reconcile** exports every current Id (a
cheap, Id-only query) and flags or removes rows whose Id is gone.

A sync runs a reconcile automatically when the object's last successful sync was more than
`RECONCILE_AFTER_DAYS` (default 14) ago, or was never recorded. Syncing at least every couple of
weeks keeps deletes cheap. Run one yourself any time, for example after a mass delete:

| | |
| --- | --- |
| Local | `./run.sh reconcile [Object...]` |
| Snowflake | `CALL GLACIER_RUN_JOB('<job>', 'reconcile')` |

Notes:

- Locally, if the query file has a `WHERE` clause, reconcile uses it too, so rows that stop
  matching the filter are treated as deleted. Queries with `LIMIT`/`OFFSET` can't be reconciled.
  (Snowflake jobs always load the whole object.)
- Reconcile refuses to run if Salesforce returns no Ids, so a permissions problem can't wipe the
  table.
- In `flag` mode, a full reload rebuilds the table from live records plus the recycle bin, so
  rows flagged longer ago than that drop out.

## History

Turn on history to keep a type 2 history table, `<TABLE>__HISTORY`, alongside the table:

| | |
| --- | --- |
| Local | Add `history` to the object's line in `objects.conf` |
| Snowflake | Set the job's `TRACK_HISTORY = TRUE` |

The table itself doesn't change. The history table has one row per version of each record: the
same columns, plus `VALID_FROM`, `VALID_TO`, and `IS_CURRENT`.

- `VALID_FROM` is the version's `SystemModstamp`. `VALID_TO` is the next version's `VALID_FROM`,
  or `NULL` for the current version. A record as of a point in time is
  `WHERE VALID_FROM <= :t AND (VALID_TO > :t OR VALID_TO IS NULL)`.
- A sync adds a version only when a field value changed. `SystemModstamp`, `LastViewedDate`, and
  `LastReferencedDate` don't count, so boundary records fetched again and system-only updates
  don't make new versions.
- History records each record's state at each sync. Several edits between two syncs become one
  version.
- Deletes seen in the recycle bin become a version with `ISDELETED = TRUE`, in `remove` mode
  too, so history keeps records the table drops. Records gone from Salesforce entirely (found by
  reconcile or a full reload) get their current version closed with no new one.
- A full reload replaces the table but not its history: changed records get new versions and
  missing ones are closed.
- Turning history on for an existing table starts history from the table's current rows on the
  next sync that has changes.
- History and the table's merge commit together, so a failed load can't leave them out of step.

## Table names

Locally, you name each table in `objects.conf`. In Snowflake, a job's `TARGET_TABLE` defaults to
the object's API name uppercased (`GiftTransaction` → `GIFTTRANSACTION`, `Subscription__c` →
`SUBSCRIPTION__C`). To keep loading a table the CLI created under another name, such as
`SUBSCRIPTION`, set it as the job's `TARGET_TABLE`; otherwise the first run does a full load into
the new name.

Reserved words keep their name and are double-quoted in SQL (`Order` → `"ORDER"`), so queries
against those tables need the quotes too.
