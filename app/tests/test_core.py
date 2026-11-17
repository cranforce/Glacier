import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, SRC)

import snowforce_core as core  # noqa: E402


class NamesTest(unittest.TestCase):
    def test_default_table_name_is_uppercased_object(self):
        self.assertEqual(core.default_table_name("GiftTransaction"), "GIFTTRANSACTION")
        self.assertEqual(core.default_table_name("Subscription__c"), "SUBSCRIPTION__C")

    def test_reserved_object_names_get_prefix(self):
        self.assertEqual(core.default_table_name("Order"), "SFORCE_ORDER")
        self.assertEqual(core.default_table_name("Account"), "SFORCE_ACCOUNT")

    def test_reserved_table_name_rejected_with_suggestion(self):
        with self.assertRaisesRegex(core.ConfigError, "use SFORCE_ORDER instead"):
            core.table_identifier("order")
        self.assertEqual(core.table_identifier("sforce_order"), "SFORCE_ORDER")

    def test_identifier_rejects_injection(self):
        for bad in ("A B", "A;DROP", "1ABC", "", None, 'A"B'):
            with self.assertRaises(core.ConfigError):
                core.identifier(bad)

    def test_user_identifier_is_quoted_uppercase(self):
        self.assertEqual(core.user_identifier("chris.miller@odbm.org"), '"CHRIS.MILLER@ODBM.ORG"')
        with self.assertRaises(core.ConfigError):
            core.user_identifier('x"; DROP')

    def test_sql_string_escapes(self):
        self.assertEqual(core.sql_string("it's"), "'it''s'")
        self.assertEqual(core.sql_string("a\\b"), "'a\\\\b'")


class FieldsTest(unittest.TestCase):
    def test_types(self):
        cases = {
            ("boolean", 0): "BOOLEAN", ("int", 0): "NUMBER(38,0)", ("currency", 2): "NUMBER(38,2)",
            ("date", 0): "DATE", ("datetime", 0): "TIMESTAMP_NTZ", ("time", 0): "VARCHAR",
            ("string", 0): "VARCHAR", ("reference", 0): "VARCHAR",
        }
        for (sf_type, scale), expected in cases.items():
            self.assertEqual(core.snowflake_type({"type": sf_type, "scale": scale}), expected, sf_type)

    def test_compound_fields_not_exportable(self):
        self.assertFalse(core.exportable({"type": "address"}))
        self.assertFalse(core.exportable({"type": "base64"}))
        self.assertTrue(core.exportable({"type": "string"}))

    def test_normalize_header(self):
        self.assertEqual(core.normalize_header('"Id","SystemModstamp","Account.Name"\r\n'),
                         ["ID", "SYSTEMMODSTAMP", "ACCOUNT_NAME"])


class SoqlTest(unittest.TestCase):
    def test_adds_required_fields_once(self):
        self.assertEqual(core.build_soql("Subscription__c", ["Status__c", "id"]),
                         "SELECT SystemModstamp, Status__c, id FROM Subscription__c")

    def test_delta_and_extras(self):
        self.assertEqual(
            core.build_soql("Order", ["Name"], "2026-10-01T12:00:00Z", ("IsDeleted",)),
            "SELECT Id, SystemModstamp, IsDeleted, Name FROM Order "
            "WHERE SystemModstamp >= 2026-10-01T12:00:00Z")

    def test_rejects_bad_watermark_and_names(self):
        with self.assertRaises(core.ConfigError):
            core.build_soql("Order", ["Name"], "2026-10-01 12:00:00")
        with self.assertRaises(core.ConfigError):
            core.build_soql("Order", ["Name FROM User"])

    def test_reconcile_due(self):
        now = datetime(2026, 10, 30, tzinfo=timezone.utc)
        self.assertTrue(core.reconcile_due(None, now, 14))
        self.assertTrue(core.reconcile_due(now - timedelta(days=15), now, 14))
        self.assertFalse(core.reconcile_due(now - timedelta(days=3), now, 14))


class LoadSqlTest(unittest.TestCase):
    cols = ["ID", "SYSTEMMODSTAMP", "ISDELETED", "NAME"]
    types = {"SYSTEMMODSTAMP": "TIMESTAMP_NTZ", "ISDELETED": "BOOLEAN"}

    def test_add_columns_repeats_if_not_exists(self):
        sql = core.add_columns_sql("D.S.T", self.cols, self.types)
        self.assertEqual(sql.count("IF NOT EXISTS"), 4)
        self.assertIn("IF NOT EXISTS SYSTEMMODSTAMP TIMESTAMP_NTZ", sql)
        self.assertIn("IF NOT EXISTS NAME VARCHAR", sql)
        self.assertTrue(sql.startswith("ALTER TABLE D.S.T ADD COLUMN"))

    def test_merge_flag_vs_remove(self):
        flag = core.merge_sql("D.S.T", "D.S.T__DELTA", self.cols, "flag")
        self.assertNotIn("THEN DELETE", flag)
        self.assertNotIn("ID = s.ID,", flag)  # ID isn't updated
        remove = core.merge_sql("D.S.T", "D.S.T__DELTA", self.cols, "remove")
        self.assertIn("WHEN MATCHED AND COALESCE(s.ISDELETED::BOOLEAN, FALSE) THEN DELETE", remove)
        self.assertIn("WHEN NOT MATCHED AND NOT COALESCE", remove)

    def test_delta_table_has_only_exported_columns(self):
        stmts = core.delta_statements("D.S.T", "D.S.T__DELTA", self.cols, self.types, "@x/", "F", "flag")
        self.assertEqual(stmts[1], "CREATE OR REPLACE TEMPORARY TABLE D.S.T__DELTA (\n  ID VARCHAR,\n  "
                                   "SYSTEMMODSTAMP TIMESTAMP_NTZ,\n  ISDELETED BOOLEAN,\n  NAME VARCHAR\n)")
        self.assertNotIn("LIKE", stmts[1])

    def test_full_load_without_rows_only_creates(self):
        self.assertEqual(len(core.full_load_statements("D.S.T", self.cols, self.types, "@x/", "F", False)), 1)
        both = core.full_load_statements("D.S.T", self.cols, self.types, "@x/", "F", True)
        self.assertTrue(both[0].startswith("CREATE OR REPLACE TABLE D.S.T"))
        self.assertTrue(both[1].startswith("COPY INTO D.S.T"))

    def test_reconcile_modes(self):
        remove = core.reconcile_statements("D.S.T", "D.S.T__IDS", "@x/", "F", "remove")
        self.assertIn("DELETE FROM D.S.T WHERE ID NOT IN", remove[-1])
        flag = core.reconcile_statements("D.S.T", "D.S.T__IDS", "@x/", "F", "flag")
        self.assertIn("UPDATE D.S.T SET ISDELETED = TRUE", flag[-1])

    def test_watermark_by_type(self):
        self.assertIn("TO_VARCHAR(MAX(SYSTEMMODSTAMP)", core.watermark_sql("T", "TIMESTAMP_NTZ"))
        self.assertIn("CONVERT_TIMEZONE('UTC', MAX(SYSTEMMODSTAMP))", core.watermark_sql("T", "TIMESTAMP_TZ"))
        self.assertIn("TRY_TO_TIMESTAMP_TZ", core.watermark_sql("T", "TEXT"))


class DdlTest(unittest.TestCase):
    def test_secret_procedures(self):
        ddl = core.secret_procedure_ddl("DB.SC", "EAI", [("ODBM_PROD", "DB.SC.SEC1"), ("EU", "DB.SC.SEC2")])
        self.assertEqual(len(ddl), len(core.SECRET_PROCEDURES))
        self.assertIn("SECRETS = ('odbm_prod' = DB.SC.SEC1, 'eu' = DB.SC.SEC2)", ddl[0])
        self.assertIn("EXTERNAL_ACCESS_INTEGRATIONS = (EAI)", ddl[0])
        self.assertIn("APP_SCHEMA VARCHAR DEFAULT 'DB.SC'", ddl[0])
        self.assertNotIn("SECRETS", core.secret_procedure_ddl("DB.SC", "EAI", [])[0])

    def test_code_files_match_src(self):
        self.assertEqual(sorted(core.CODE_FILES),
                         sorted(f for f in os.listdir(SRC) if f.endswith(".py")))

    def job(self, **overrides):
        job = {"JOB_NAME": "SUBSCRIPTION", "WAREHOUSE": "WH", "SCHEDULE": "0 * * * * UTC",
               "RUN_AS_USER": None, "RUN_AS_ROLE": None}
        job.update(overrides)
        return job

    def test_task_without_user(self):
        ddl = core.task_ddl("DB.SC", "APP_ROLE", self.job())
        self.assertIn("CREATE OR REPLACE TASK DB.SC.SNOWFORCE_JOB_SUBSCRIPTION", ddl)
        self.assertIn("SCHEDULE = 'USING CRON 0 * * * * UTC'", ddl)
        self.assertNotIn("EXECUTE AS USER", ddl)
        self.assertNotIn("USE ROLE", ddl)
        self.assertIn("CALL DB.SC.SNOWFORCE_RUN_JOB('SUBSCRIPTION');", ddl)

    def test_task_with_user_and_role(self):
        ddl = core.task_ddl("DB.SC", "APP_ROLE",
                            self.job(RUN_AS_USER="svc.snowforce", RUN_AS_ROLE="loader_role", SCHEDULE=None))
        self.assertIn('EXECUTE AS USER "SVC.SNOWFORCE"', ddl)
        self.assertIn("USE ROLE LOADER_ROLE;", ddl)
        self.assertNotIn("SCHEDULE", ddl)

    def test_task_role_without_user_rejected(self):
        with self.assertRaisesRegex(core.ConfigError, "needs a run-as user"):
            core.task_ddl("DB.SC", "APP_ROLE", self.job(RUN_AS_ROLE="OTHER"))
        core.task_ddl("DB.SC", "app_role", self.job(RUN_AS_ROLE="APP_ROLE"))  # its own role is fine

    def test_task_rejects_bad_schedule(self):
        with self.assertRaises(core.ConfigError):
            core.task_ddl("DB.SC", "R", self.job(SCHEDULE="0 * * * * UTC'; DROP TABLE X; --"))


if __name__ == "__main__":
    unittest.main()
