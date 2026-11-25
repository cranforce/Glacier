"""The Shopify connector: one store per connection, exported through GraphQL Admin API bulk queries.

Glacier supports a fixed list of objects (OBJECTS). A root object is a top-level
query such as orders; a child object is a list on each row of its parent, such
as orders.lineItems, and gets its own table keyed to the parent. Fields come
from introspecting the object's GraphQL type: scalars, value objects flattened
into dotted paths (totalPriceSet.shopMoney.amount), and the id of a related
record (customer.id).

The settings, names, objects, queries, and flattening are pure and unit tested
locally. The functions that take a secret call Shopify, and run only inside the
owner's rights procedures (see glacier_procs.py). glacier_connectors.py lists
what every connector provides.
"""
import csv
import io
import json
import re
import tempfile
from dataclasses import dataclass

import glacier_core as core
from shopify_api import Shopify, ShopifyError

NAME = "shopify"
LABEL = "Shopify"


@dataclass(frozen=True)
class SourceObject:
    """An object Glacier can sync.

    name is its path from the top-level query (orders.lineItems.taxLines). A
    root object has the change field its query filters on (change, as the
    search syntax names it in search). A child is a connection or a plain list
    on its parent. singular names an object's id and change field on its
    children's rows (order -> order.id, order.updatedAt). A child without an id
    of its own is keyed by its parent's id and its position in the list.
    """
    name: str
    type: str
    label: str
    singular: str = None
    change: str = None
    search: str = None
    connection: bool = False
    has_id: bool = True

    @property
    def path(self):
        return self.name.split(".")

    @property
    def is_root(self):
        return len(self.path) == 1


def _root(name, type_, label, singular, change="updatedAt", search="updated_at"):
    return SourceObject(name, type_, label, singular=singular, change=change, search=search)


OBJECTS = {o.name.lower(): o for o in (
    _root("orders", "Order", "Orders", "order"),
    SourceObject("orders.lineItems", "LineItem", "Order line items", singular="lineItem", connection=True),
    SourceObject("orders.lineItems.taxLines", "TaxLine", "Order line item tax lines", has_id=False),
    SourceObject("orders.lineItems.discountAllocations", "DiscountAllocation",
                 "Order line item discount allocations", has_id=False),
    SourceObject("orders.shippingLines", "ShippingLine", "Order shipping lines", singular="shippingLine",
                 connection=True),
    SourceObject("orders.shippingLines.taxLines", "TaxLine", "Order shipping line tax lines", has_id=False),
    SourceObject("orders.taxLines", "TaxLine", "Order tax lines", has_id=False),
    SourceObject("orders.fulfillments", "Fulfillment", "Order fulfillments"),
    SourceObject("orders.refunds", "Refund", "Order refunds"),
    SourceObject("orders.transactions", "OrderTransaction", "Order transactions"),
    _root("draftOrders", "DraftOrder", "Draft orders", "draftOrder"),
    SourceObject("draftOrders.lineItems", "DraftOrderLineItem", "Draft order line items", connection=True),
    _root("abandonedCheckouts", "AbandonedCheckout", "Abandoned checkouts", "abandonedCheckout"),
    SourceObject("abandonedCheckouts.lineItems", "AbandonedCheckoutLineItem", "Abandoned checkout line items",
                 connection=True),
    _root("customers", "Customer", "Customers", "customer"),
    SourceObject("customers.addressesV2", "MailingAddress", "Customer addresses", connection=True),
    _root("products", "Product", "Products", "product"),
    SourceObject("products.options", "ProductOption", "Product options"),
    _root("productVariants", "ProductVariant", "Product variants", "productVariant"),
    _root("collections", "Collection", "Collections", "collection"),
    _root("inventoryItems", "InventoryItem", "Inventory items", "inventoryItem"),
    _root("events", "Event", "Events", "event", change="createdAt", search="created_at"),
    _root("tenderTransactions", "TenderTransaction", "Tender transactions", "tenderTransaction",
          change="processedAt", search="processed_at"),
)}

# Fields Glacier adds to a row: the deleted flag (always false; a reconcile sets
# it), the row's id as Shopify returns it (a gid, which id holds only the number
# of), and a list child's position and key when it has no id
DELETED = "isDeleted"
GID_FIELD = "adminGraphqlApiId"
INDEX = "arrayIndex"
ROW_KEY = "rowKey"


def column_name(field):
    """A Shopify name as a Snowflake one: camelCase split with underscores, and dots
    as underscores (totalPriceSet.shopMoney.amount -> TOTAL_PRICE_SET_SHOP_MONEY_AMOUNT,
    orders.lineItems -> ORDERS_LINE_ITEMS)."""
    name = field.strip().strip('"')
    return "_".join(core.CASE_CHANGE.sub("_", part) for part in name.split(".")).upper()


def table_name(source_object):
    """The default table for an object (orders.lineItems -> ORDERS_LINE_ITEMS)."""
    return core.table_identifier(column_name(object_name(source_object)))


KEYS = core.RecordKeys(key="id", change="updatedAt", deleted=DELETED, column=column_name)
DELETE_MODES = core.DELETE_MODES
APIS = ("bulk",)
API_HELP = ("bulk runs a GraphQL bulk query, which doesn't count against the store's API rate limit. "
            "An app can run five at once per store; more wait for a free slot.")

SETTINGS = (
    core.Setting("SHOP_DOMAIN", "Store domain", placeholder="<store>.myshopify.com",
                 help="The store's myshopify.com domain, not its custom domain."),
    core.Setting("API_VERSION", "API version", default="2026-10"),
    core.Setting("CLIENT_ID", "Client ID",
                 help="For a Dev Dashboard app: its client ID, with its client secret as the credential. "
                      "Blank for a custom app made in the store's admin, whose Admin API access token is "
                      "the credential."),
)
SECRET_LABEL = "Admin API access token, or client secret"
SECRET_PLACEHOLDER = "shpat_..."
OBJECT_PLACEHOLDER = "orders.lineItems"
FIELDS_HELP = ("The key, the change field (the parent's, for a child object), the parents' ids, and "
               "isDeleted (unless deletes are off) are always included.")
UNEXPORTABLE_NOTE = "fields the app's access scopes don't cover aren't."
TABLE_NAME_HELP = "the object's path, e.g. orders.lineItems -> ORDERS_LINE_ITEMS"

SHOP_DOMAIN = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")
API_VERSION = re.compile(r"^\d{4}-(01|04|07|10)$")
FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
GID = re.compile(r"^gid://shopify/[A-Za-z]+/(\d+)(\?.*)?$")

# Snowflake types for GraphQL scalars; others, and enums, are VARCHAR. IDs load
# as the number at the end of their gid://shopify/<Type>/<number>.
SCALAR_TYPES = {
    "ID": "NUMBER(38,0)", "Int": "NUMBER(38,0)", "UnsignedInt64": "NUMBER(38,0)", "Float": "FLOAT",
    "Decimal": "NUMBER(38,10)", "Money": "NUMBER(38,10)", "Boolean": "BOOLEAN",
    "DateTime": "TIMESTAMP_NTZ", "Date": "DATE",
}
# How many objects deep a field path can go: totalPriceSet.shopMoney.amount is two
MAX_DEPTH = 2
# Records Glacier expands like value objects; other records become only their id
EXPAND_NODES = frozenset({"MailingAddress"})


# --- Settings and names ---------------------------------------------------------

def validate_settings(values):
    """A connection's SETTINGS from the form's values (or a stored row)."""
    values = {k: (str(v).strip() if v is not None else "") for k, v in (values or {}).items()}
    domain = re.sub(r"^https?://", "", values.get("SHOP_DOMAIN", "")).rstrip("/").lower()
    if not SHOP_DOMAIN.match(domain):
        raise core.ConfigError("Store domain must look like <store>.myshopify.com")
    api_version = values.get("API_VERSION") or "2026-10"
    if not API_VERSION.match(api_version):
        raise core.ConfigError(f"Invalid API version: {api_version!r} (use e.g. 2026-10)")
    return {"SHOP_DOMAIN": domain, "API_VERSION": api_version, "CLIENT_ID": values.get("CLIENT_ID", "")}


def validate_secret(text):
    text = (text or "").strip()
    if not text or any(c.isspace() for c in text):
        raise core.ConfigError("Paste the access token or client secret, with no spaces.")
    return text


def source_object(name):
    obj = OBJECTS.get((name or "").strip().lower())
    if obj is None:
        raise core.ConfigError(f"Glacier doesn't support Shopify object {name!r}; "
                               f"use one of {', '.join(o.name for o in OBJECTS.values())}")
    return obj


def object_name(name):
    return source_object(name).name


def field_name(name):
    if not name or not FIELD_NAME.match(name):
        raise core.ConfigError(f"Invalid field name: {name!r}")
    return name


def _root_of(obj):
    return OBJECTS[obj.path[0].lower()]


def _carrier(obj):
    """The object whose rows are JSONL lines: the object itself, or a list child's parent."""
    return obj if obj.is_root or obj.connection else OBJECTS[".".join(obj.path[:-1]).lower()]


def keys(name):
    """A root is keyed by id and changes with its own change field; a child with the
    root's (orders.lineItems: order.updatedAt)."""
    obj = source_object(name)
    root = _root_of(obj)
    change = root.change if obj.is_root else f"{root.singular}.{root.change}"
    return core.RecordKeys(key="id" if obj.has_id else ROW_KEY, change=change, deleted=DELETED,
                           column=column_name)


def context_fields(obj):
    """(field, GraphQL type) Glacier adds to a row: a child's parents' ids and root
    change field, the row's gid, and a list child's position and key."""
    fields = []
    if not obj.is_root:
        root, carrier = _root_of(obj), _carrier(obj)
        fields += [(f"{root.singular}.id", "ID"), (f"{root.singular}.{root.change}", "DateTime")]
        if carrier is not root and carrier is not obj:
            fields.append((f"{carrier.singular}.id", "ID"))
    if obj.has_id:
        fields.append((GID_FIELD, "String"))
    else:
        fields += [(INDEX, "Int"), (ROW_KEY, "String")]
    return fields


def _synthetic(obj):
    return {f for f, _ in context_fields(obj)} | {DELETED}


# --- Fields -----------------------------------------------------------------------

def snowflake_type(source_type, is_list=False):
    """A list loads as JSON text."""
    return "VARCHAR" if is_list else SCALAR_TYPES.get(source_type, "VARCHAR")


def unwrap(type_ref):
    """(kind, name, is_list, nillable) of an introspected type reference."""
    nillable, is_list = type_ref["kind"] != "NON_NULL", False
    while type_ref["kind"] in ("NON_NULL", "LIST"):
        is_list = is_list or type_ref["kind"] == "LIST"
        type_ref = type_ref["ofType"]
    return type_ref["kind"], type_ref["name"], is_list, nillable


def _is_node(info):
    return any(i["name"] == "Node" for i in info.get("interfaces") or [])


def type_fields(type_info, type_name, prefix="", depth=0):
    """(path, GraphQL type, is_list, nillable) for every field a row of type_name can export.

    type_info(name) returns a type's introspection. Scalars and enums are fields;
    a value object's fields are flattened up to MAX_DEPTH objects deep; a related
    record is its id. Lists of objects, connections, interfaces, and fields with
    required arguments are left out: they'd be child objects or need arguments.
    """
    fields = []
    for f in type_info(type_name).get("fields") or []:
        if any(a["type"]["kind"] == "NON_NULL" and a.get("defaultValue") is None for a in f.get("args") or []):
            continue
        kind, name, is_list, nillable = unwrap(f["type"])
        path = prefix + f["name"]
        if kind in ("SCALAR", "ENUM"):
            fields.append((path, name, is_list, nillable))
        elif kind == "OBJECT" and not is_list and not name.endswith("Connection"):
            nested = type_info(name)
            if _is_node(nested) and name not in EXPAND_NODES:
                fields.append((path + ".id", "ID", False, True))
            elif depth < MAX_DEPTH:
                fields += [(p, t, l, nillable or n)
                           for p, t, l, n in type_fields(type_info, name, path + ".", depth + 1)]
    return fields


def field_rows(obj, fields, denied=()):
    """GLACIER_FIELDS rows: the context fields, the type's fields, then the deleted flag.
    A field under a denied path isn't exportable."""
    rows = [(f, t, False, False) for f, t in context_fields(obj)] + list(fields) + [(DELETED, "Boolean", False, False)]
    return [{
        "position": i,
        "name": path,
        "label": path,
        "sourceType": f"[{source_type}]" if is_list else source_type,
        "nillable": nillable,
        "calculated": False,
        "custom": False,
        "snowflakeType": snowflake_type(source_type, is_list),
        "exportable": not any(path == d or path.startswith(d + ".") for d in denied),
    } for i, (path, source_type, is_list, nillable) in enumerate(rows)]


# --- Queries ------------------------------------------------------------------------

def selection(paths):
    """A GraphQL selection set for dotted paths: a, b.c, b.d -> a b { c d }."""
    tree = {}
    for path in paths:
        node = tree
        for part in path.split("."):
            node = node.setdefault(part, {})

    def render(node):
        return " ".join(k + (f" {{ {render(v)} }}" if v else "") for k, v in node.items())
    return render(tree) or "__typename"


def _graphql(obj, columns, since=None, probe=False):
    """The query for an object's columns. probe limits it to a few rows, for an ordinary query."""
    root, carrier = _root_of(obj), _carrier(obj)
    synthetic = _synthetic(obj)
    rows = selection([c for c in columns if c not in synthetic])
    edges = "edges { node { %s } }"
    if obj.is_root:
        inner = rows
    else:
        child = obj.path[1]
        first = "(first: 5)" if probe and carrier.connection else ""
        if carrier is obj:
            below = f"{child}{first} {{ {edges % rows} }}"
        elif carrier is root:
            below = f"{child} {{ {rows} }}"
        else:
            below = f"{child}{first} {{ {edges % ('id ' + obj.path[2] + ' { ' + rows + ' }')} }}"
        inner = f"id {root.change} {below}"
    args = []
    if probe:
        args.append("first: 1")
    if since:
        if not core.WATERMARK.match(since):
            raise core.ConfigError(f"Invalid watermark: {since!r}")
        args.append(f"query: \"{root.search}:>='{since}'\"")
    return f"{{ {root.name}{'(' + ', '.join(args) + ')' if args else ''} {{ {edges % inner} }} }}"


def build_query(name, fields, since=None, extra_fields=()):
    """The bulk query for an object's fields, after two comment lines naming the
    object and the export's columns. The key, change field, and context fields
    are always included. With since, only records changed at or after that UTC
    timestamp: for a child, the ones whose root record changed."""
    obj = source_object(name)
    for f in fields:
        field_name(f)
    k = keys(obj.name)
    columns = list(dict.fromkeys([k.key, k.change, *(f for f, _ in context_fields(obj)), *extra_fields, *fields]))
    return (f"# Glacier object: {obj.name}\n# Glacier columns: {', '.join(columns)}\n"
            + _graphql(obj, columns, since))


def build_ids_query(name):
    obj = source_object(name)
    k = keys(obj.name)
    return f"# Glacier object: {obj.name}\n# Glacier columns: {k.key}\n" + _graphql(obj, [k.key])


def parse_plan(query):
    """(object, columns) from a query's comment lines."""
    lines = query.split("\n")
    try:
        obj = source_object(lines[0].removeprefix("# Glacier object: "))
        columns = [c.strip() for c in lines[1].removeprefix("# Glacier columns: ").split(",")]
    except (IndexError, core.ConfigError) as e:
        raise core.ConfigError(f"Not a Glacier Shopify query: {query[:200]}") from e
    return obj, columns


def query_columns(query):
    """The columns a query's export will have, for an export with no rows."""
    return core.normalize_header(", ".join(parse_plan(query)[1]), column_name)


# --- Results ------------------------------------------------------------------------

def gid_number(gid):
    match = GID.match(gid or "")
    return match.group(1) if match else gid


def csv_value(value):
    """A JSON value as CSV text: gids as their number, lists as JSON, None as empty."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return gid_number(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)


def value_at(node, path):
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def records(obj, columns, lines):
    """Rows of column values from a bulk query's JSONL lines.

    A connection child's lines carry their parent's id as __parentId; their
    parent's change field comes from its own line, which can come later, so
    those children wait in a temporary file until every line has been read.
    """
    root, carrier = _root_of(obj), _carrier(obj)
    root_change = f"{root.singular}.{root.change}"
    needs_parent = carrier is not root and root_change in columns
    parents = {}

    def value(column, item, context):
        if column == GID_FIELD:
            return item.get("id")  # the gid as is
        return csv_value(context[column] if column in context else value_at(item, column))

    def rows(line):
        context = {DELETED: False}
        if obj.is_root:
            pass
        elif carrier is root:
            context.update({f"{root.singular}.id": line["id"], root_change: line.get(root.change)})
        else:
            context.update({f"{root.singular}.id": line["__parentId"],
                            root_change: parents.get(line["__parentId"]), f"{carrier.singular}.id": line["id"]})
        items = [(None, line)] if carrier is obj else enumerate(line.get(obj.path[-1]) or [])
        for i, item in items:
            if i is not None:
                context.update({INDEX: i, ROW_KEY: f"{gid_number(line['id'])}:{i}"})
            yield [value(c, item, context) for c in columns]

    with tempfile.TemporaryFile("w+") as waiting:
        for line in lines:
            if "__parentId" not in line and needs_parent:
                parents[line["id"]] = line.get(root.change)
            if ("__parentId" in line) != (carrier is not root):
                continue
            if needs_parent and line["__parentId"] not in parents:
                waiting.write(json.dumps(line) + "\n")
                continue
            yield from rows(line)
        waiting.seek(0)
        for text in waiting:
            line = json.loads(text)
            if line["__parentId"] not in parents:
                raise ShopifyError(f"Bulk results have no parent {line['__parentId']} for {line.get('id')}")
            yield from rows(line)


def csv_pages(rows, columns, page_size=50000):
    """Yield (CSV bytes, record count) per page of up to page_size rows, each with a
    header row; no rows yields one page with only the header."""
    page, yielded = [], False

    def write(batch):
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n", quoting=csv.QUOTE_ALL)
        writer.writerow(columns)
        writer.writerows(batch)
        return out.getvalue().encode("utf-8")

    for row in rows:
        page.append(row)
        if len(page) >= page_size:
            yield write(page), len(page)
            page, yielded = [], True
    if page or not yielded:
        yield write(page), len(page)


# --- Calls to Shopify ---------------------------------------------------------------

def client(settings, secret):
    s = validate_settings(settings)
    return Shopify(s["SHOP_DOMAIN"], s["API_VERSION"], secret, s["CLIENT_ID"])


def test(settings, secret):
    data = client(settings, secret).graphql(
        "{ shop { name myshopifyDomain currencyCode } "
        "currentAppInstallation { accessScopes { handle } } }")
    return {"shop": data["shop"]["name"], "domain": data["shop"]["myshopifyDomain"],
            "currency": data["shop"]["currencyCode"],
            "scopes": [s["handle"] for s in data["currentAppInstallation"]["accessScopes"]]}


def list_objects(settings, secret):
    return [{"name": o.name, "label": o.label, "custom": False} for o in OBJECTS.values()]


def denied_paths(obj, errors):
    """The field paths a probe's errors point at. An error without a field path
    (a malformed query, a missing scope for the whole object) is raised."""
    denied = set()
    for e in errors:
        parts = [p for p in e.get("path") or [] if isinstance(p, str) and p not in ("edges", "node")]
        parts = parts[len(obj.path):] if parts[:len(obj.path)] == obj.path else []
        if not parts:
            raise ShopifyError(f"Shopify refused {obj.name}: {e.get('message', e)}")
        denied.add(".".join(parts))
    return denied


def describe(settings, secret, name, attempts=5):
    """(the object's name, its GLACIER_FIELDS rows).

    Introspects the object's type, then queries a row with every field and
    marks the ones Shopify denies (fields needing scopes the app doesn't have)
    as not exportable.
    """
    obj = source_object(name)
    shopify = client(settings, secret)
    fields = type_fields(shopify.type_info, obj.type)
    denied = set()
    for _ in range(attempts):
        allowed = [f[0] for f in fields if not any(f[0] == d or f[0].startswith(d + ".") for d in denied)]
        _, errors = shopify.graphql(_graphql(obj, allowed, probe=True), allow_errors=True)
        if not errors:
            return obj.name, field_rows(obj, fields, denied)
        denied |= denied_paths(obj, errors)
    raise ShopifyError(f"Shopify still refused fields of {obj.name} after {attempts} tries")


def export(settings, secret, query, include_deleted, api=None):
    """Yield (CSV bytes, record count) pages for a query from build_query.

    include_deleted does nothing: Shopify's results have no deleted records,
    so deletes are found by reconcile.
    """
    core.source_api(api, APIS)
    obj, columns = parse_plan(query)
    graphql = "\n".join(line for line in query.split("\n") if not line.startswith("#"))
    lines = client(settings, secret).bulk_query(graphql)
    return csv_pages(records(obj, columns, lines), columns)
