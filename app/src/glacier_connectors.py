"""The connectors Glacier can sync from, by name.

A connector is a module that provides:

  NAME, LABEL         "salesforce", "Salesforce"
  KEYS                glacier_core.RecordKeys: the key, change, and deleted fields
  DELETE_MODES        The delete modes it supports (all of them when it reports deletes)
  APIS, API_HELP      The APIs a job can export through, the default first, and form help
  SETTINGS            glacier_core.Setting per connection setting, stored in SETTINGS
  SECRET_LABEL, SECRET_PLACEHOLDER, OBJECT_PLACEHOLDER, FIELDS_HELP, UNEXPORTABLE_NOTE,
  TABLE_NAME_HELP
                      Text for the Streamlit forms
  validate_settings(values) -> settings
  validate_secret(text) -> secret value
  object_name(name), field_name(name) -> the name, validated
  table_name(object) -> the default Snowflake table for an object
  column_name(field) -> the Snowflake column for a field (core.column_name
                      uppercases; KEYS.column should be the same function)
  build_query(object, fields, since, extra_fields) -> the query an export runs
  build_ids_query(object) -> a query exporting only the key, for reconcile
  query_columns(query) -> columns, for an export with no rows

and, given a connection's settings and secret, run in an owner's rights procedure:

  test(settings, secret) -> dict
  list_objects(settings, secret) -> [{name, label, custom}]
  describe(settings, secret, object) -> (object name, GLACIER_FIELDS rows)
  export(settings, secret, query, include_deleted, api) -> iterator of (CSV bytes, record count)

Each page an export yields is CSV whose header row names the fields.
"""
import connector_salesforce
import glacier_core as core

CONNECTORS = {c.NAME: c for c in (connector_salesforce,)}


def get(name):
    connector = CONNECTORS.get((name or "").lower())
    if connector is None:
        raise core.ConfigError(f"Unknown connector {name!r}; use one of {', '.join(CONNECTORS)}")
    return connector
