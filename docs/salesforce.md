---
title: Salesforce
nav_order: 6
---

# Salesforce
{: .no_toc }

How the Salesforce connector syncs an org, and how to set one up. Each org (production or
sandbox) is one connection. Its jobs load into `GLACIER_DB.NPC_SALESFORCE_PROD_RAW` by default,
and each job can name another schema.

1. TOC
{:toc}

## API

Each job exports through the API named in its `SOURCE_API`. Every run of the job (sync, full
load, or reconcile) uses that API:

| API | How | Counts against |
| --- | --- | --- |
| `bulk` (default) | A Bulk API 2.0 query job, polled until done, then read in 50,000-record pages | The org's Bulk API query limits |
| `rest` | REST `query` (or `queryAll` when deleted records are wanted), 2,000 records per call | The org's daily API request limit |

Use `rest` for frequent syncs with small deltas. Use `bulk` for large objects and full loads,
which would take hundreds of REST calls. Both write the same CSV, so a job can switch APIs between
runs without changing its table. See [APIs](how-syncing-works.md#apis).

Glacier logs in with the OAuth 2.0 **JWT bearer flow**. Salesforce holds a certificate on an
External Client App, and a Snowflake secret holds the matching private key. Glacier signs a
short-lived JWT naming the app's consumer key and the integration user. Salesforce exchanges it
for an access token, and Glacier logs in again if the token expires during a run.

## Objects

Glacier can sync any object the integration user can query.
`CALL GLACIER_SALESFORCE_LIST_OBJECTS('<connection>')` lists them. Each object is one job and one
table. Every object has the same key, change field, and deleted field:

| Field | Column | Role |
| --- | --- | --- |
| `Id` | `ID` | The key that rows are merged on |
| `SystemModstamp` | `SYSTEM_MODSTAMP` | The change field, and the watermark for deltas |
| `IsDeleted` | `IS_DELETED` | Set by deletes in the recycle bin, or by a reconcile |

`Id` and `SystemModstamp` are always exported, and so is `IsDeleted` unless the job's delete mode
is `off`, even if the job's field list leaves them out.

## Fields and names

A describe (`GLACIER_SALESFORCE_DESCRIBE`) captures an object's fields into `GLACIER_FIELDS`, with
Snowflake types:

| Salesforce type | Snowflake type |
| --- | --- |
| `boolean` | `BOOLEAN` |
| `int`, `long` | `NUMBER(38,0)` |
| `double`, `currency`, `percent` | `NUMBER(38,<scale>)` |
| `date` | `DATE` |
| `datetime` | `TIMESTAMP_NTZ`, in UTC |
| Everything else, including `time` | `VARCHAR` |

`address`, `location`, and `base64` fields are marked not exportable, because the Bulk API can't
export compound or binary fields. Export their parts instead, such as `BillingCity` and
`BillingPostalCode`. A job with `FIELDS` `NULL` exports every exportable field and picks up new
ones by itself. Run the describe again when a field is added in Salesforce so it gets the right
type.

**Names** follow Glacier's convention, from the API names:

- PascalCase is split at each case change: `GiftTransaction` → `GIFT_TRANSACTION`.
- A custom name's `__c` becomes `_c`: `Financial_Batch__c` → `FINANCIAL_BATCH_C`. A namespace's
  `__` stays (`npsp__Batch__c` → `NPSP__BATCH_C`).
- A relationship field's dots become underscores: `Account.Name` → `ACCOUNT_NAME`.

Reserved words are double-quoted (`Order` → `"ORDER"`). See
[Table names](how-syncing-works.md#table-names) and
[Column names](how-syncing-works.md#column-names).

## Deltas

A sync exports records with `SystemModstamp >=` the table's latest `SYSTEM_MODSTAMP`, and merges
them on `ID`. Records at the boundary second are exported again and merge with no change. A sync
does a full load instead when the table doesn't exist, is empty, or has no `SYSTEM_MODSTAMP`
column.

## Deletes

Deleting a record bumps its `SystemModstamp`, so a sync sees it in the **recycle bin** (through
`queryAll`) with `IsDeleted = true`. Undeleted records come back the same way. The job's
`DELETE_MODE` flags the row (`flag`, the default), removes it (`remove`), or ignores the delete
(`off`).

The recycle bin keeps records for about 15 days, and hard deletes skip it. A **reconcile**
catches those: it exports every `Id` and flags or removes rows whose `Id` is no longer there. A
sync runs one every `RECONCILE_AFTER_DAYS` (14 by default), and you can run one any time. A
reconcile refuses to run if Salesforce returns no Ids, so a permissions problem can't wipe the
table. See [Deletes](how-syncing-works.md#deletes) and [Reconcile](how-syncing-works.md#reconcile).

## History

With `TRACK_HISTORY = TRUE`, a job keeps a type 2 history table, `<TABLE>__HISTORY`. A version's
`VALID_FROM` is its `SystemModstamp`. A sync adds a version only when a field value changed.
`SystemModstamp`, `LastViewedDate`, and `LastReferencedDate` don't count, since they change when
someone only views a record. See [History](how-syncing-works.md#history).

## Setting up an org

The full steps, with the SQL, are in [Snowflake setup](snowflake-setup.md). In short:

1. **Snowflake admin**, once: run `app/deploy.sh admin-sql integration salesforce`, which creates
   `GLACIER_SALESFORCE_EAI`. It reaches `login.salesforce.com`, `test.salesforce.com`,
   `*.my.salesforce.com`, and `*.sandbox.my.salesforce.com`, so a new org needs no network
   change. The output of `app/deploy.sh admin-sql setup` also creates
   `NPC_SALESFORCE_PROD_RAW`.
2. **Make a key pair** per connection
   ([step 4](snowflake-setup.md#4-make-a-key-pair)):

   ```sh
   openssl req -x509 -newkey rsa:2048 -nodes -days 730 -subj "/CN=Glacier" \
     -keyout glacier.key -out glacier.crt
   ```

3. **Salesforce admin**, per org
   ([step 5](snowflake-setup.md#5-create-an-external-client-app-in-salesforce)): create an
   **External Client App** with OAuth and the `api` and `refresh_token` scopes. Enable the JWT
   bearer flow, upload `glacier.crt`, and set **Admin approved users are pre-authorized**. Give
   the integration user the app through a permission set. That user needs **API Enabled** and
   read access to every object you'll sync. Send back the consumer key and the username.
4. **Add the connection** on the Connections page, or with SQL as in
   [Snowflake setup](snowflake-setup.md#6-add-the-connection):

   | Setting | Value |
   | --- | --- |
   | `MY_DOMAIN_URL` | `https://<domain>.my.salesforce.com`. A `.sandbox.my.salesforce.com` URL logs in through `test.salesforce.com` |
   | `CLIENT_ID` | The External Client App's consumer key |
   | `USERNAME` | The integration user the jobs run as in Salesforce |
   | `API_VERSION` | `64.0` by default |

   The secret is the whole PEM private key from `glacier.key`, `BEGIN` and `END` lines included.
   Delete the local key once the secret exists.

5. `CALL GLACIER_DB.APP.GLACIER_REFRESH_CONNECTIONS()`, then
   `CALL GLACIER_DB.APP.GLACIER_SALESFORCE_TEST_CONNECTION('<connection>')`. The test returns the
   instance URL and the number of queryable objects.

The certificate expires after two years. Before then, follow
[Rotating the key](snowflake-setup.md#rotating-the-key).

| Org | Connection | Schema |
| --- | --- | --- |
| Production | `ODBM_PROD` | `NPC_SALESFORCE_PROD_RAW` |
