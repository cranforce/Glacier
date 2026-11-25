---
title: Shopify
nav_order: 6
---

# Shopify
{: .no_toc }

How the Shopify connector syncs a store, and how to set one up. Glacier replaces the Precog
loads in `RAW_DB` for the US, Europe, India, and Artos stores. Each store is one connection, and
it loads into its own schema in `GLACIER_DB`.

1. TOC
{:toc}

## API

The connector exports through the **GraphQL Admin API**, using **bulk queries**. Shopify made
the REST Admin API legacy in October 2024, and new features come only to GraphQL.

A bulk query runs on Shopify's side and doesn't count against the store's rate limit. Glacier
starts it, polls until it's done, and downloads the results as JSONL. It then writes them as
CSV pages, like every other export, so loading, history, and deletes work as they do for
Salesforce. An app can run five bulk queries at once per store. A job that finds all five slots
taken waits for one, for up to an hour.

## Objects

Glacier supports a fixed list of objects. A **root** object is a top-level query. A **child**
object is a list on each row of its parent, and gets its own table keyed to that parent.

| Object | Table | Key | Change field |
| --- | --- | --- | --- |
| `orders` | `ORDERS` | `ID` | `UPDATED_AT` |
| `orders.lineItems` | `ORDERS_LINE_ITEMS` | `ID` | `ORDER_UPDATED_AT` |
| `orders.lineItems.taxLines` | `ORDERS_LINE_ITEMS_TAX_LINES` | `ROW_KEY` | `ORDER_UPDATED_AT` |
| `orders.lineItems.discountAllocations` | `ORDERS_LINE_ITEMS_DISCOUNT_ALLOCATIONS` | `ROW_KEY` | `ORDER_UPDATED_AT` |
| `orders.shippingLines` | `ORDERS_SHIPPING_LINES` | `ID` | `ORDER_UPDATED_AT` |
| `orders.shippingLines.taxLines` | `ORDERS_SHIPPING_LINES_TAX_LINES` | `ROW_KEY` | `ORDER_UPDATED_AT` |
| `orders.taxLines` | `ORDERS_TAX_LINES` | `ROW_KEY` | `ORDER_UPDATED_AT` |
| `orders.fulfillments` | `ORDERS_FULFILLMENTS` | `ID` | `ORDER_UPDATED_AT` |
| `orders.refunds` | `ORDERS_REFUNDS` | `ID` | `ORDER_UPDATED_AT` |
| `orders.transactions` | `ORDERS_TRANSACTIONS` | `ID` | `ORDER_UPDATED_AT` |
| `draftOrders` | `DRAFT_ORDERS` | `ID` | `UPDATED_AT` |
| `draftOrders.lineItems` | `DRAFT_ORDERS_LINE_ITEMS` | `ID` | `DRAFT_ORDER_UPDATED_AT` |
| `abandonedCheckouts` | `ABANDONED_CHECKOUTS` | `ID` | `UPDATED_AT` |
| `abandonedCheckouts.lineItems` | `ABANDONED_CHECKOUTS_LINE_ITEMS` | `ID` | `ABANDONED_CHECKOUT_UPDATED_AT` |
| `customers` | `CUSTOMERS` | `ID` | `UPDATED_AT` |
| `customers.addressesV2` | `CUSTOMERS_ADDRESSES_V2` | `ID` | `CUSTOMER_UPDATED_AT` |
| `products` | `PRODUCTS` | `ID` | `UPDATED_AT` |
| `products.options` | `PRODUCTS_OPTIONS` | `ID` | `PRODUCT_UPDATED_AT` |
| `productVariants` | `PRODUCT_VARIANTS` | `ID` | `UPDATED_AT` |
| `collections` | `COLLECTIONS` | `ID` | `UPDATED_AT` |
| `inventoryItems` | `INVENTORY_ITEMS` | `ID` | `UPDATED_AT` |
| `events` | `EVENTS` | `ID` | `CREATED_AT` |
| `tenderTransactions` | `TENDER_TRANSACTIONS` | `ID` | `PROCESSED_AT` |

Each object is one job. A child's job queries its root records and takes each one's child
rows, so `orders` and its eight children are nine jobs, each running its own bulk query.

Not supported yet:
- **Refund and fulfillment line items.** They're connections under a list
  (`orders.refunds.refundLineItems`), which bulk queries may not allow.
- **Collection membership** (Precog's `COLLECTS`). It needs a key made of two ids.
- **Inventory levels.** Their quantities need arguments.
- **Reference data such as the shop, locations, and policies.** These have no change field, and
  every Glacier job needs one for its deltas.

## Fields and names

A describe (`GLACIER_SHOPIFY_DESCRIBE`) reads the object's GraphQL type and captures what a row
can export:

- **Scalars and enums**, as columns.
- **Value objects**, flattened into dotted paths up to two objects deep
  (`totalPriceSet.shopMoney.amount`).
- **Related records**, as their id only (`customer.id`). Addresses are the exception: they're
  expanded like value objects.
- **Lists of scalars** such as `tags`, as JSON text.

Lists of objects and connections aren't columns: they're child objects. So are fields that need
arguments.

The describe then queries one row with every field. It marks any field Shopify refuses as not
exportable, so that one field doesn't fail every bulk query. This usually means the app's access
scopes don't cover the field, such as customer data the app isn't approved for.

**Names** follow Glacier's convention. camelCase is split with underscores, and each dot in a
path becomes an underscore. Child tables use the same names as their parent's columns:

| Field | Column |
| --- | --- |
| `totalPriceSet.shopMoney.amount` | `TOTAL_PRICE_SET_SHOP_MONEY_AMOUNT` |
| `originalUnitPriceSet.shopMoney.amount` (line item) | `ORIGINAL_UNIT_PRICE_SET_SHOP_MONEY_AMOUNT` |
| `customer.id` | `CUSTOMER_ID` |
| `order.id`, `order.updatedAt` (on a child) | `ORDER_ID`, `ORDER_UPDATED_AT` |

GraphQL's field names differ from REST's in places; a line item's price, for example, is under
`originalUnitPriceSet`. Downstream models built on the Precog tables need a column mapping, not
just a new schema name. A describe lists each object's fields.

**Types:** IDs load as the number at the end of their `gid://shopify/<Type>/<number>`, so they
join to Precog's numeric ids. The row's own gid is also kept whole, in `ADMIN_GRAPHQL_API_ID`. `Decimal` and `Money` load as `NUMBER(38,10)`, `DateTime` as
`TIMESTAMP_NTZ` in UTC, and enums and other scalars as `VARCHAR`.

**Glacier's own columns:**

| Column | On | What |
| --- | --- | --- |
| `ADMIN_GRAPHQL_API_ID` | Rows with an id | The row's id as Shopify returns it, such as `gid://shopify/Order/123`, including any query string (`gid://shopify/MailingAddress/5?model_name=CustomerAddress`). It's what the API takes, and it has the same name as Precog's column |
| `<PARENT>_ID`, `<ROOT>_UPDATED_AT` | Child rows | The parent's id, and the root's change field, which is the child's watermark |
| `ARRAY_INDEX`, `ROW_KEY` | List children without an id (tax lines, discount allocations) | The position in the parent's list, and `<parent id>:<position>` as the key |
| `IS_DELETED` | Every row | False when loaded; a reconcile sets it (see below) |

## Deltas

A sync filters the root query on the table's latest change field. For example, orders use
`updated_at:>='<watermark>'`. A child's watermark is its root's change field, so
`ORDERS_LINE_ITEMS` exports the line items of every order changed since its latest
`ORDER_UPDATED_AT`. Events filter on `created_at` and tender transactions on `processed_at`,
since neither has an `updated_at`.

## Deletes

Shopify's results have no deleted records. Deletes are found only by a **reconcile**: an export
of every key, which flags (`IS_DELETED`) or removes rows that are no longer there. A sync runs one
every `RECONCILE_AFTER_DAYS` (14 by default), and you can run one any time. This also catches
child rows that dropped off their parent, such as a removed shipping line.

## History

History works as it does for Salesforce. A version's `VALID_FROM` is the change field (the
root's, for a child). A record whose tracked columns haven't changed gets no new version, even if
its change field moved.

Shopify keeps its own trail for most of an order's life: transactions, refunds, fulfillments, and
events are records that don't change once created. So turn history on only where Shopify keeps
none:

| Object | History |
| --- | --- |
| `products`, `productVariants` | **On.** Shopify keeps no price history |
| `customers` | **On.** Tags, marketing consent, and account state are overwritten |
| `orders` | Off to start. Turn it on if someone needs an order's state as of a date |
| Child objects, events, tender transactions, transactions | Off |

Leave `inventoryQuantity` out of the `productVariants` job's fields. Stock changes make up most
of a variant's updates, and history would record every one.

## Setting up a store

1. **Shopify admin**, per store: create an app with read access to what's synced. That's
   `read_orders`, `read_draft_orders`, `read_customers`, `read_products`, and `read_inventory`.
   Abandoned checkouts also need the app's `manage_abandoned_checkouts` permission. Customer
   fields need protected customer data access.
   - A **Dev Dashboard app** in the same Shopify organization as the store gives a client ID and
     secret. Glacier exchanges them for a 24-hour token on each run.
   - A **custom app** created in the store's admin before 2026 gives an Admin API access token
     (`shpat_...`). Shopify no longer allows new ones.
2. **Snowflake admin**, once: run `app/deploy.sh admin-sql integration shopify`, which creates
   `GLACIER_SHOPIFY_EAI`. The output of `app/deploy.sh admin-sql setup` also creates the four
   store schemas.
3. **Add the connection** on the Connections page, or with SQL as in
   [Snowflake setup](snowflake-setup.md#6-add-the-connection):

   | Setting | Value |
   | --- | --- |
   | `SHOP_DOMAIN` | The store's `<store>.myshopify.com` domain |
   | `API_VERSION` | `2026-10` |
   | `CLIENT_ID` | The Dev Dashboard app's client ID, or blank for an access token |

   The secret is the client secret, or the access token.

4. `CALL GLACIER_DB.APP.GLACIER_REFRESH_CONNECTIONS()`, then
   `CALL GLACIER_DB.APP.GLACIER_SHOPIFY_TEST_CONNECTION('<connection>')`. The test returns the
   store's name and the app's access scopes.

| Store | Connection | Schema | Precog schema it replaces |
| --- | --- | --- | --- |
| US | `SHOPIFY_US_PROD` | `SHOPIFY_US_PROD_RAW` | `SHOPIFY_SHOPIFY_PRIVATE_APP_ODBM_US_PROD` |
| Europe | `SHOPIFY_EUROPE_PROD` | `SHOPIFY_EUROPE_PROD_RAW` | `SHOPIFY_EUROPE_SHOPIFY_ODBM_EUROPE` |
| India | `SHOPIFY_INDIA_PROD` | `SHOPIFY_INDIA_PROD_RAW` | `INDIA_SHOPIFY_DHDINDIA` |
| Artos | `SHOPIFY_ARTOS_PROD` | `SHOPIFY_ARTOS_PROD_RAW` | `SHOPIFY_ARTOS_SHOPIFY_Q2K3YB_8N_MYSHOPIFY_COM` |

## To confirm against a real store

These are built from Shopify's documentation and haven't yet run against a store:

- The bulk results download host. The network rule allows `storage.googleapis.com`; if
  downloads fail with a network error, check the host in a bulk operation's `url`.
- That bulk queries return non-connection lists, such as `taxLines`, inline on the parent's line.
- That `orders.shippingLines` works in a bulk query: Shopify requires a connection's nodes to
  have ids.
- How Shopify reports a field the app can't read. The describe expects field-level errors with a
  `path`.
