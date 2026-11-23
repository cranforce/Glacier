"""The Salesforce connector: JWT bearer login, describe, SOQL, and Bulk API 2.0 or REST exports.

The settings, names, types, and queries are pure and unit tested locally. The
functions that take a secret call Salesforce, and run only inside the owner's
rights procedures (see glacier_procs.py). glacier_connectors.py lists what
every connector provides.
"""
import re

import glacier_core as core
from sforce_api import Salesforce

NAME = "salesforce"
LABEL = "Salesforce"

# Splits PascalCase at each case change: GiftTransaction -> Gift_Transaction,
# ExternalID -> External_ID, HTMLBody -> HTML_Body
CASE_CHANGE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
CUSTOM_SUFFIX = re.compile(r"__c$", re.IGNORECASE)


def snowflake_name(name):
    """A Salesforce name as a Snowflake one: PascalCase split with underscores and
    __c shortened to _c (GiftTransaction -> GIFT_TRANSACTION,
    Financial_Batch__c -> FINANCIAL_BATCH_C). A relationship field's dots become
    underscores (Account.Name -> ACCOUNT_NAME)."""
    name = name.strip().strip('"')
    parts = (CASE_CHANGE.sub("_", CUSTOM_SUFFIX.sub("_c", part)) for part in name.split("."))
    return "_".join(parts).upper()


def table_name(sobject):
    """The default table for an object (Financial_Batch__c -> FINANCIAL_BATCH_C)."""
    return core.table_identifier(snowflake_name(object_name(sobject)))


def column_name(field):
    return snowflake_name(field)


KEYS = core.RecordKeys(key="Id", change="SystemModstamp", deleted="IsDeleted",
                       # Change when someone views the record
                       untracked=("LastViewedDate", "LastReferencedDate"),
                       column=column_name)
DELETE_MODES = core.DELETE_MODES
# The APIs a job can export through; the first is the default
APIS = ("bulk", "rest")
API_HELP = ("bulk runs a Bulk API 2.0 job and counts against the org's Bulk API limits; rest pages "
            "through the REST API 2,000 records per call and counts against its API request limit. "
            "rest suits small deltas; bulk, large loads.")

SETTINGS = (
    core.Setting("MY_DOMAIN_URL", "My Domain URL", placeholder="https://<domain>.my.salesforce.com",
                 help="A .sandbox.my.salesforce.com URL logs in through test.salesforce.com."),
    core.Setting("CLIENT_ID", "Consumer key", help="The External Client App's consumer key."),
    core.Setting("USERNAME", "Integration user", help="The Salesforce user the jobs run as."),
    core.Setting("API_VERSION", "API version", default="64.0"),
)
SECRET_LABEL = "Private key (PEM)"
SECRET_PLACEHOLDER = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----"
OBJECT_PLACEHOLDER = "Subscription__c"
FIELDS_HELP = "Id, SystemModstamp, and IsDeleted (unless deletes are off) are always included."
UNEXPORTABLE_NOTE = "address, location, and base64 fields aren't."
TABLE_NAME_HELP = "the object's name, e.g. Financial_Batch__c -> FINANCIAL_BATCH_C"

MY_DOMAIN_URL = re.compile(r"^https://[A-Za-z0-9-]+(\.sandbox)?\.my\.salesforce\.com/?$")
API_VERSION = re.compile(r"^\d{2,3}\.0$")
SF_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

# Compound and binary fields the Bulk API can't export
UNEXPORTABLE_TYPES = frozenset({"address", "location", "base64"})


# --- Settings and names ---------------------------------------------------------

def validate_settings(values):
    """A connection's SETTINGS from the form's values (or a stored row)."""
    values = {k: (str(v).strip() if v is not None else "") for k, v in (values or {}).items()}
    url = values.get("MY_DOMAIN_URL", "")
    if not MY_DOMAIN_URL.match(url):
        raise core.ConfigError(
            "My Domain URL must look like https://<domain>.my.salesforce.com "
            "or https://<domain>.sandbox.my.salesforce.com")
    api_version = values.get("API_VERSION") or "64.0"
    if not API_VERSION.match(api_version):
        raise core.ConfigError(f"Invalid API version: {api_version!r} (use e.g. 64.0)")
    for key, label in (("CLIENT_ID", "Consumer key"), ("USERNAME", "Integration user")):
        if not values.get(key):
            raise core.ConfigError(f"{label} is required")
    return {"MY_DOMAIN_URL": url.rstrip("/"), "CLIENT_ID": values["CLIENT_ID"],
            "USERNAME": values["USERNAME"], "API_VERSION": api_version}


def validate_secret(pem):
    pem = (pem or "").strip()
    if not (pem.startswith("-----BEGIN") and pem.endswith("PRIVATE KEY-----")):
        raise core.ConfigError("Paste the whole PEM private key, BEGIN and END lines included.")
    return pem


def object_name(name):
    return sf_name(name, "object name")


def field_name(name):
    return sf_name(name, "field name")


def sf_name(name, what="Salesforce name"):
    if not name or not SF_NAME.match(name):
        raise core.ConfigError(f"Invalid {what}: {name!r}")
    return name


# --- Fields -----------------------------------------------------------------------

def snowflake_type(field):
    """Snowflake column type for a Salesforce describe field.

    The CSV export writes datetimes in UTC, so TIMESTAMP_NTZ holds UTC wall-clock
    time. Time fields stay VARCHAR because Salesforce writes them with a trailing Z.
    """
    sf_type = field.get("type")
    if sf_type == "boolean":
        return "BOOLEAN"
    if sf_type in ("int", "long"):
        return "NUMBER(38,0)"
    if sf_type in ("double", "currency", "percent"):
        return f"NUMBER(38,{int(field.get('scale') or 0)})"
    if sf_type == "date":
        return "DATE"
    if sf_type == "datetime":
        return "TIMESTAMP_NTZ"
    return "VARCHAR"


def exportable(field):
    return field.get("type") not in UNEXPORTABLE_TYPES


def field_rows(describe_result):
    """GLACIER_FIELDS rows for a describe result."""
    return [{
        "position": i,
        "name": f["name"],
        "label": f.get("label"),
        "sourceType": f.get("type"),
        "length": f.get("length"),
        "precision": f.get("precision"),
        "scale": f.get("scale"),
        "nillable": f.get("nillable"),
        "calculated": f.get("calculated"),
        "custom": f.get("custom"),
        "referenceTo": "|".join(f.get("referenceTo") or []),
        "snowflakeType": snowflake_type(f),
        "exportable": exportable(f),
    } for i, f in enumerate(describe_result["fields"])]


# --- Queries ------------------------------------------------------------------------

def build_query(sobject, fields, since=None, extra_fields=()):
    """SOQL selecting the fields (plus Id, SystemModstamp, and any extras) from an object.

    With since, only records modified at or after that UTC timestamp.
    """
    sf_name(sobject, "object name")
    for f in fields:
        sf_name(f, "field name")
    have = {f.lower() for f in fields}
    required = [f for f in (KEYS.key, KEYS.change, *extra_fields) if f.lower() not in have]
    soql = f"SELECT {', '.join(required + list(fields))} FROM {sobject}"
    if since:
        if not core.WATERMARK.match(since):
            raise core.ConfigError(f"Invalid watermark: {since!r}")
        soql += f" WHERE {KEYS.change} >= {since}"
    return soql


def build_ids_query(sobject):
    return f"SELECT {KEYS.key} FROM {sf_name(sobject, 'object name')}"


def query_columns(soql):
    """The columns a query's export will have, for an export with no rows."""
    return core.normalize_header(soql[len("SELECT "):soql.index(" FROM ")], column_name)


# --- Calls to Salesforce ---------------------------------------------------------------

def client(settings, secret):
    s = validate_settings(settings)
    return Salesforce(s["MY_DOMAIN_URL"], s["CLIENT_ID"], s["USERNAME"], secret, s["API_VERSION"])


def test(settings, secret):
    sf = client(settings, secret)
    objects = sf.list_objects()
    return {"instance_url": sf.instance_url,
            "queryable_objects": sum(1 for o in objects if o.get("queryable"))}


def list_objects(settings, secret):
    return [{"name": o["name"], "label": o.get("label"), "custom": o.get("custom")}
            for o in client(settings, secret).list_objects() if o.get("queryable")]


def describe(settings, secret, sobject):
    """(the object's name in Salesforce's casing, its GLACIER_FIELDS rows)."""
    result = client(settings, secret).describe(object_name(sobject))
    return result["name"], field_rows(result)


def export(settings, secret, soql, include_deleted, api=None):
    """Yield (CSV bytes, record count) per results page, each with its own header row.

    include_deleted adds records in the recycle bin (queryAll). api is bulk
    (the default) or rest.
    """
    sf = client(settings, secret)
    query = sf.rest_query if core.source_api(api, APIS) == "rest" else sf.bulk_query
    return query(soql, all_rows=bool(include_deleted))
