import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, SRC)

import glacier_core as core  # noqa: E402

# Salesforce's fields, as most of these tests' SQL expects
KEYS = core.RecordKeys(key="Id", change="SystemModstamp", deleted="IsDeleted",
                       untracked=("LastViewedDate", "LastReferencedDate"))


class NamesTest(unittest.TestCase):
    def test_table_ref_quotes_reserved_words(self):
        self.assertEqual(core.table_ref("crm_db", "s", "order"), 'CRM_DB.S."ORDER"')
        self.assertEqual(core.table_ref("crm_db", "s", "ORDER__DELTA"), "CRM_DB.S.ORDER__DELTA")
        self.assertEqual(core.table_ref("crm_db", "s", "Gift_Transaction"), "CRM_DB.S.GIFT_TRANSACTION")
        with self.assertRaises(core.ConfigError):
            core.table_ref("crm_db", "s", 'ORDER"; DROP')

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


class ColumnsTest(unittest.TestCase):
    def test_normalize_header(self):
        self.assertEqual(core.normalize_header('"Id","SystemModstamp","Account.Name"\r\n'),
                         ["ID", "SYSTEMMODSTAMP", "ACCOUNT_NAME"])

    def test_record_keys_columns(self):
        self.assertEqual((KEYS.key_col, KEYS.change_col, KEYS.deleted_col), ("ID", "SYSTEMMODSTAMP", "ISDELETED"))
        self.assertEqual(KEYS.untracked_cols, {"SYSTEMMODSTAMP", "LASTVIEWEDDATE", "LASTREFERENCEDDATE"})
        self.assertIsNone(core.RecordKeys("id", "updated_at").deleted_col)

    def test_record_keys_use_the_connectors_columns(self):
        keys = core.RecordKeys("id", "updatedAt", column=lambda f: f"X_{f.upper()}")
        self.assertEqual((keys.key_col, keys.change_col), ("X_ID", "X_UPDATEDAT"))

    def test_normalize_header_rejects_colliding_columns(self):
        self.assertEqual(core.normalize_header("a,b", str.upper), ["A", "B"])
        with self.assertRaises(core.ConfigError):
            core.normalize_header("Foo_Bar,FOO_BAR")

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
        flag = core.merge_sql("D.S.T", "D.S.T__DELTA", self.cols, "flag", KEYS)
        self.assertNotIn("THEN DELETE", flag)
        self.assertNotIn("ID = s.ID,", flag)  # ID isn't updated
        remove = core.merge_sql("D.S.T", "D.S.T__DELTA", self.cols, "remove", KEYS)
        self.assertIn("WHEN MATCHED AND COALESCE(s.ISDELETED::BOOLEAN, FALSE) THEN DELETE", remove)
        self.assertIn("WHEN NOT MATCHED AND NOT COALESCE", remove)

    def test_delta_table_has_only_exported_columns(self):
        stmts = core.delta_statements("D.S.T", "D.S.T__DELTA", self.cols, self.types, "@x/", "F", "flag", KEYS)
        self.assertEqual(stmts[1], "CREATE OR REPLACE TEMPORARY TABLE D.S.T__DELTA (\n  ID VARCHAR,\n  "
                                   "SYSTEMMODSTAMP TIMESTAMP_NTZ,\n  ISDELETED BOOLEAN,\n  NAME VARCHAR\n)")
        self.assertNotIn("LIKE", stmts[1])

    def test_full_load_without_rows_only_creates(self):
        self.assertEqual(len(core.full_load_statements("D.S.T", self.cols, self.types, "@x/", "F", False, KEYS)), 1)
        both = core.full_load_statements("D.S.T", self.cols, self.types, "@x/", "F", True, KEYS)
        self.assertTrue(both[0].startswith("CREATE OR REPLACE TABLE D.S.T"))
        self.assertTrue(both[1].startswith("COPY INTO D.S.T"))

    def test_reconcile_modes(self):
        remove = core.reconcile_statements("D.S.T", "D.S.T__IDS", "@x/", "F", "remove", KEYS)
        self.assertIn("DELETE FROM D.S.T WHERE ID NOT IN", remove[-1])
        flag = core.reconcile_statements("D.S.T", "D.S.T__IDS", "@x/", "F", "flag", KEYS)
        self.assertIn("UPDATE D.S.T SET ISDELETED = TRUE", flag[-1])

    def test_delta_with_history(self):
        stmts = core.delta_statements("D.S.T", "D.S.T__DELTA", self.cols, self.types, "@x/", "F", "remove",
                                      KEYS, "D.S.T__HISTORY")
        self.assertTrue(stmts[1].startswith("CREATE TABLE IF NOT EXISTS D.S.T__HISTORY"))
        self.assertIn("VALID_FROM TIMESTAMP_NTZ", stmts[1])
        self.assertTrue(stmts[2].startswith("ALTER TABLE D.S.T__HISTORY ADD COLUMN"))
        self.assertIn("WHERE NOT EXISTS (SELECT 1 FROM D.S.T__HISTORY)", stmts[3])  # seed
        self.assertEqual(stmts[-1], "COMMIT")
        self.assertTrue(stmts[-2].startswith("MERGE INTO D.S.T"))
        self.assertTrue(stmts[-3].startswith("INSERT INTO D.S.T__HISTORY"))
        close = stmts[stmts.index("BEGIN") + 1]
        self.assertTrue(close.startswith("UPDATE D.S.T__HISTORY h SET VALID_TO = s.SYSTEMMODSTAMP"))
        # SystemModstamp alone doesn't make a new version
        self.assertIn("HASH(h.ID, h.ISDELETED, h.NAME) <> HASH(s.ID, s.ISDELETED, s.NAME)", close)
        self.assertIn("QUALIFY ROW_NUMBER()", close)

    def test_delta_without_history_unchanged(self):
        stmts = core.delta_statements("D.S.T", "D.S.T__DELTA", self.cols, self.types, "@x/", "F", "flag", KEYS)
        self.assertEqual(len(stmts), 4)
        self.assertNotIn("BEGIN", stmts)

    def test_history_rejects_clashing_columns(self):
        with self.assertRaisesRegex(core.ConfigError, "IS_CURRENT"):
            core.delta_statements("D.S.T", "D.S.T__DELTA", self.cols + ["IS_CURRENT"], self.types, "@x/",
                                  "F", "flag", KEYS, "D.S.T__HISTORY")

    def test_full_load_with_history(self):
        stmts = core.full_load_statements("D.S.T", self.cols, self.types, "@x/", "F", True, KEYS, "D.S.T__HISTORY")
        self.assertTrue(stmts[1].startswith("COPY INTO D.S.T"))
        self.assertEqual(stmts[-1], "COMMIT")
        self.assertTrue(stmts[-2].startswith("INSERT INTO D.S.T__HISTORY"))
        self.assertTrue(any("ID NOT IN (SELECT ID FROM (SELECT * FROM D.S.T))" in s for s in stmts))
        self.assertIn("FROM (SELECT * FROM D.S.T) s", stmts[-2])
        # An empty load doesn't close every record
        self.assertEqual(len(core.full_load_statements("D.S.T", self.cols, self.types, "@x/", "F", False,
                                                       KEYS, "D.S.T__HISTORY")), 1)

    def test_reconcile_with_history(self):
        stmts = core.reconcile_statements("D.S.T", "D.S.T__IDS", "@x/", "F", "remove", KEYS, "D.S.T__HISTORY")
        self.assertEqual(stmts[-1], "COMMIT")
        self.assertTrue(stmts[-2].startswith("DELETE FROM D.S.T WHERE"))
        self.assertIn("UPDATE D.S.T__HISTORY SET VALID_TO = CONVERT_TIMEZONE", stmts[-3])
        self.assertTrue(stmts[-5].startswith("CREATE TABLE IF NOT EXISTS D.S.T__HISTORY"))

    def test_other_sources_keys(self):
        """A source with its own key and change fields, and no deleted flag."""
        keys = core.RecordKeys(key="id", change="updated_at")
        cols = ["ID", "UPDATED_AT", "STATUS"]
        merge = core.merge_sql("D.S.T", "D.S.T__DELTA", cols, "off", keys)
        self.assertIn("PARTITION BY ID ORDER BY UPDATED_AT DESC", merge)
        self.assertNotIn("SYSTEMMODSTAMP", merge)
        stmts = core.delta_statements("D.S.T", "D.S.T__DELTA", cols, {"UPDATED_AT": "TIMESTAMP_NTZ"}, "@x/",
                                      "F", "off", keys, "D.S.T__HISTORY")
        close = stmts[stmts.index("BEGIN") + 1]
        self.assertIn("SET VALID_TO = s.UPDATED_AT", close)
        self.assertIn("HASH(h.ID, h.STATUS) <> HASH(s.ID, s.STATUS)", close)
        self.assertIn("VALID_FROM TIMESTAMP_NTZ", stmts[1])
        self.assertIn("MAX(UPDATED_AT)", core.watermark_sql("T", "TIMESTAMP_NTZ", keys))

    def test_watermark_by_type(self):
        self.assertIn("TO_VARCHAR(MAX(SYSTEMMODSTAMP)", core.watermark_sql("T", "TIMESTAMP_NTZ", KEYS))
        self.assertIn("CONVERT_TIMEZONE('UTC', MAX(SYSTEMMODSTAMP))", core.watermark_sql("T", "TIMESTAMP_TZ", KEYS))
        self.assertIn("TRY_TO_TIMESTAMP_TZ", core.watermark_sql("T", "TEXT", KEYS))


class DdlTest(unittest.TestCase):
    def test_secret_procedures(self):
        ddl = core.secret_procedure_ddl("DB.SC", "salesforce", "EAI",
                                        [("ODBM_PROD", "DB.SC.SEC1"), ("EU", "DB.SC.SEC2")])
        self.assertEqual(len(ddl), len(core.SECRET_PROCEDURES))
        self.assertIn("PROCEDURE DB.SC.GLACIER_SALESFORCE_TEST_CONNECTION(", ddl[0])
        self.assertIn("PROCEDURE DB.SC.GLACIER_SALESFORCE_EXTRACT(", ddl[-1])
        self.assertIn("SECRETS = ('odbm_prod' = DB.SC.SEC1, 'eu' = DB.SC.SEC2)", ddl[0])
        self.assertIn("EXTERNAL_ACCESS_INTEGRATIONS = (EAI)", ddl[0])
        self.assertIn("CONNECTOR VARCHAR DEFAULT 'salesforce', APP_SCHEMA VARCHAR DEFAULT 'DB.SC'", ddl[0])
        self.assertNotIn("SECRETS", core.secret_procedure_ddl("DB.SC", "salesforce", "EAI", [])[0])

    def test_connector_names(self):
        self.assertEqual(core.procedure_name("shiphero", "DESCRIBE"), "GLACIER_SHIPHERO_DESCRIBE")
        self.assertEqual(core.integration_name("salesforce"), "GLACIER_SALESFORCE_EAI")
        with self.assertRaises(core.ConfigError):
            core.procedure_name("x; DROP", "EXTRACT")

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
        self.assertIn("CREATE OR REPLACE TASK DB.SC.GLACIER_JOB_SUBSCRIPTION", ddl)
        self.assertIn("SCHEDULE = 'USING CRON 0 * * * * UTC'", ddl)
        self.assertNotIn("EXECUTE AS USER", ddl)
        self.assertNotIn("USE ROLE", ddl)
        self.assertIn("CALL DB.SC.GLACIER_RUN_JOB('SUBSCRIPTION');", ddl)

    def test_task_with_user_and_role(self):
        ddl = core.task_ddl("DB.SC", "APP_ROLE",
                            self.job(RUN_AS_USER="svc.glacier", RUN_AS_ROLE="loader_role", SCHEDULE=None))
        self.assertIn('EXECUTE AS USER "SVC.GLACIER"', ddl)
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
