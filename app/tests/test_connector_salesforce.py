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
        names = ("NAME", "LABEL", "KEYS", "DELETE_MODES", "SETTINGS", "SECRET_LABEL", "SECRET_PLACEHOLDER",
                 "OBJECT_PLACEHOLDER", "FIELDS_HELP", "UNEXPORTABLE_NOTE", "validate_settings",
                 "validate_secret", "object_name", "field_name", "build_query", "build_ids_query",
                 "query_columns", "test", "list_objects", "describe", "export")
        for name, connector in connectors.CONNECTORS.items():
            self.assertEqual(connector.NAME, name)
            for attr in names:
                self.assertTrue(hasattr(connector, attr), f"{name} has no {attr}")


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
                         ["ID", "SYSTEMMODSTAMP", "ACCOUNT_NAME"])


if __name__ == "__main__":
    unittest.main()
