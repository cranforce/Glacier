import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import connector_salesforce as sf  # noqa: E402
import glacier_connectors as connectors  # noqa: E402
import glacier_core as core  # noqa: E402


class RegistryTest(unittest.TestCase):
    def test_lookup(self):
        self.assertIs(connectors.get("Salesforce"), sf)
        with self.assertRaises(core.ConfigError):
            connectors.get("nope")

    def test_every_connector_has_the_interface(self):
        names = ("NAME", "LABEL", "KEYS", "keys", "DELETE_MODES", "APIS", "SETTINGS", "SECRET_LABEL", "SECRET_PLACEHOLDER",
                 "OBJECT_PLACEHOLDER", "FIELDS_HELP", "UNEXPORTABLE_NOTE", "validate_settings",
                 "validate_secret", "object_name", "field_name", "build_query", "build_ids_query",
                 "query_columns", "test", "list_objects", "describe", "export")
        for name, connector in connectors.CONNECTORS.items():
            self.assertEqual(connector.NAME, name)
            for attr in names:
                self.assertTrue(hasattr(connector, attr), f"{name} has no {attr}")


class ExportTest(unittest.TestCase):
    def export(self, api):
        calls = []

        class Client:
            def bulk_query(self, soql, all_rows):
                calls.append(("bulk", soql, all_rows))
                return iter(())

            def rest_query(self, soql, all_rows):
                calls.append(("rest", soql, all_rows))
                return iter(())

        original, sf.client = sf.client, lambda settings, secret: Client()
        try:
            sf.export({}, "pem", "SELECT Id FROM X", 1, api)
        finally:
            sf.client = original
        return calls[0]

    def test_api_picks_the_query(self):
        self.assertEqual(self.export(None), ("bulk", "SELECT Id FROM X", True))
        self.assertEqual(self.export("bulk")[0], "bulk")
        self.assertEqual(self.export(" REST ")[0], "rest")
        with self.assertRaises(core.ConfigError):
            self.export("soap")


class SettingsTest(unittest.TestCase):
    def settings(self, **overrides):
        values = {"MY_DOMAIN_URL": "https://odbm.my.salesforce.com/", "CLIENT_ID": "key",
                  "USERNAME": "int@odbm.org", "API_VERSION": ""}
        values.update(overrides)
        return sf.validate_settings(values)

    def test_defaults(self):
        self.assertEqual(self.settings(), {"MY_DOMAIN_URL": "https://odbm.my.salesforce.com",
                                           "CLIENT_ID": "key", "USERNAME": "int@odbm.org",
                                           "API_VERSION": "64.0"})

    def test_sandbox_url(self):
        self.settings(MY_DOMAIN_URL="https://odbm--dev.sandbox.my.salesforce.com")

    def test_rejects_bad_values(self):
        for bad in (dict(MY_DOMAIN_URL="https://evil.example.com"),
                    dict(MY_DOMAIN_URL="http://odbm.my.salesforce.com"),
                    dict(CLIENT_ID=""), dict(USERNAME=None), dict(API_VERSION="64")):
            with self.subTest(bad=bad), self.assertRaises(core.ConfigError):
                self.settings(**bad)

    def test_secret_must_be_a_pem_key(self):
        self.assertEqual(sf.validate_secret(" -----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n"),
                         "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----")
        with self.assertRaises(core.ConfigError):
            sf.validate_secret("not a key")


class FieldsTest(unittest.TestCase):
    def test_types(self):
        cases = {
            ("boolean", 0): "BOOLEAN", ("int", 0): "NUMBER(38,0)", ("currency", 2): "NUMBER(38,2)",
            ("date", 0): "DATE", ("datetime", 0): "TIMESTAMP_NTZ", ("time", 0): "VARCHAR",
            ("string", 0): "VARCHAR", ("reference", 0): "VARCHAR",
        }
        for (sf_type, scale), expected in cases.items():
            self.assertEqual(sf.snowflake_type({"type": sf_type, "scale": scale}), expected, sf_type)

    def test_compound_fields_not_exportable(self):
        self.assertFalse(sf.exportable({"type": "address"}))
        self.assertFalse(sf.exportable({"type": "base64"}))
        self.assertTrue(sf.exportable({"type": "string"}))

    def test_field_rows(self):
        rows = sf.field_rows({"fields": [
            {"name": "Id", "type": "id"},
            {"name": "AccountId", "type": "reference", "referenceTo": ["Account", "Contact"]},
            {"name": "BillingAddress", "type": "address"}]})
        self.assertEqual([r["position"] for r in rows], [0, 1, 2])
        self.assertEqual(rows[1]["sourceType"], "reference")
        self.assertEqual(rows[1]["referenceTo"], "Account|Contact")
        self.assertFalse(rows[2]["exportable"])


class QueryTest(unittest.TestCase):
    def test_adds_required_fields_once(self):
        self.assertEqual(sf.build_query("Subscription__c", ["Status__c", "id"]),
                         "SELECT SystemModstamp, Status__c, id FROM Subscription__c")

    def test_delta_and_extras(self):
        self.assertEqual(
            sf.build_query("Order", ["Name"], "2026-10-01T12:00:00Z", ("IsDeleted",)),
            "SELECT Id, SystemModstamp, IsDeleted, Name FROM Order "
            "WHERE SystemModstamp >= 2026-10-01T12:00:00Z")

    def test_rejects_bad_watermark_and_names(self):
        with self.assertRaises(core.ConfigError):
            sf.build_query("Order", ["Name"], "2026-10-01 12:00:00")
        with self.assertRaises(core.ConfigError):
            sf.build_query("Order", ["Name FROM User"])

    def test_ids_query_and_columns(self):
        self.assertEqual(sf.build_ids_query("Order"), "SELECT Id FROM Order")
        self.assertEqual(sf.query_columns("SELECT Id, SystemModstamp, Account.Name FROM Order"),
                         ["ID", "SYSTEM_MODSTAMP", "ACCOUNT_NAME"])


class NamesTest(unittest.TestCase):
    def test_pascal_case_splits_at_case_changes(self):
        for name, column in (("GiftTransaction", "GIFT_TRANSACTION"), ("SystemModstamp", "SYSTEM_MODSTAMP"),
                             ("IsDeleted", "IS_DELETED"), ("Id", "ID"), ("AccountId", "ACCOUNT_ID"),
                             ("ExternalID__c", "EXTERNAL_ID_C"), ("HTMLBody", "HTML_BODY"),
                             ("Product2", "PRODUCT2"), ("Address2Line", "ADDRESS2_LINE")):
            self.assertEqual(sf.column_name(name), column)

    def test_custom_suffix_loses_an_underscore(self):
        self.assertEqual(sf.column_name("Financial_Batch__c"), "FINANCIAL_BATCH_C")
        self.assertEqual(sf.column_name("Batch_Status__C"), "BATCH_STATUS_C")
        # A namespace's __ stays
        self.assertEqual(sf.column_name("npsp__Primary_Contact__c"), "NPSP__PRIMARY_CONTACT_C")
        self.assertEqual(sf.column_name("Owner__r"), "OWNER__R")

    def test_relationship_fields(self):
        self.assertEqual(sf.column_name("Account.Name"), "ACCOUNT_NAME")
        self.assertEqual(sf.column_name("CreatedBy.LastName"), "CREATED_BY_LAST_NAME")
        self.assertEqual(sf.column_name("Batch__r.Batch_Name__c"), "BATCH__R_BATCH_NAME_C")

    def test_table_names(self):
        for sobject, table in (("GiftTransaction", "GIFT_TRANSACTION"), ("Subscription__c", "SUBSCRIPTION_C"),
                               ("Financial_Batch__c", "FINANCIAL_BATCH_C"), ("Order", "ORDER"),
                               ("ContactPointAddress", "CONTACT_POINT_ADDRESS")):
            self.assertEqual(sf.table_name(sobject), table)
        with self.assertRaises(core.ConfigError):
            sf.table_name("Order; DROP")

    def test_keys_use_the_convention(self):
        self.assertEqual((sf.KEYS.key_col, sf.KEYS.change_col, sf.KEYS.deleted_col),
                         ("ID", "SYSTEM_MODSTAMP", "IS_DELETED"))
        self.assertEqual(sf.KEYS.untracked_cols,
                         {"SYSTEM_MODSTAMP", "LAST_VIEWED_DATE", "LAST_REFERENCED_DATE"})


if __name__ == "__main__":
    unittest.main()
