import json
import os
import sys
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, os.path.join(HERE, "..", "streamlit"))

import glacier_core as core  # noqa: E402
import glacier_forms as forms  # noqa: E402

APP = "CRM_DB.APP"
ROLE = "APP_ROLE"


def job(**overrides):
    values = {"JOB_NAME": "subscription", "CONNECTION_NAME": "odbm_prod", "SOURCE_OBJECT": "Subscription__c",
              "FIELDS": None, "TARGET_DATABASE": "crm_db", "TARGET_SCHEMA": "s", "TARGET_TABLE": "",
              "DELETE_MODE": "flag", "RECONCILE_AFTER_DAYS": 14, "TRACK_HISTORY": False,
              "RUN_AS_USER": "", "RUN_AS_ROLE": "", "WAREHOUSE": "glacier_wh",
              "SCHEDULE": "0 * * * * UTC", "ENABLED": True}
    values.update(overrides)
    return values


class JobTest(unittest.TestCase):
    def test_normalizes_names_and_blanks(self):
        saved = forms.validate_job(job(), APP, ROLE, "salesforce")
        self.assertEqual(saved["JOB_NAME"], "SUBSCRIPTION")
        self.assertEqual(saved["CONNECTION_NAME"], "ODBM_PROD")
        self.assertEqual(saved["TARGET_DATABASE"], "CRM_DB")
        self.assertIsNone(saved["TARGET_TABLE"])
        self.assertIsNone(saved["RUN_AS_USER"])
        self.assertIsNone(saved["FIELDS"])
        self.assertEqual(forms.target_table(saved), "CRM_DB.S.SUBSCRIPTION__C")

    def test_manual_schedule_is_null(self):
        self.assertIsNone(forms.validate_job(job(SCHEDULE=""), APP, ROLE, "salesforce")["SCHEDULE"])

    def test_reserved_table_is_quoted_in_target(self):
        saved = forms.validate_job(job(SOURCE_OBJECT="Order"), APP, ROLE, "salesforce")
        self.assertEqual(forms.target_table(saved), 'CRM_DB.S."ORDER"')

    def test_rejects_bad_values(self):
        for bad in (dict(JOB_NAME="has space"), dict(SOURCE_OBJECT="Bad;Object"), dict(FIELDS=[]),
                    dict(FIELDS=["Name", "x y"]), dict(DELETE_MODE="purge"),
                    dict(RECONCILE_AFTER_DAYS=0), dict(WAREHOUSE=""), dict(TARGET_TABLE='T"; DROP'),
                    dict(SCHEDULE="0 * * * * UTC; DROP"), dict(RUN_AS_USER='x"; DROP')):
            with self.subTest(bad=bad), self.assertRaises(core.ConfigError):
                forms.validate_job(job(**bad), APP, ROLE, "salesforce")

    def test_run_as_role_needs_user_unless_app_role(self):
        with self.assertRaises(core.ConfigError):
            forms.validate_job(job(RUN_AS_ROLE="other_role"), APP, ROLE, "salesforce")
        self.assertEqual(forms.validate_job(job(RUN_AS_ROLE="app_role"), APP, ROLE, "salesforce")["RUN_AS_ROLE"], ROLE)
        saved = forms.validate_job(job(RUN_AS_ROLE="other_role", RUN_AS_USER="svc.user@odbm.org"), APP, ROLE, "salesforce")
        self.assertEqual(saved["RUN_AS_USER"], "SVC.USER@ODBM.ORG")

    def test_presets_are_valid_schedules(self):
        for schedule in forms.SCHEDULES.values():
            forms.validate_job(job(SCHEDULE=schedule), APP, ROLE, "salesforce")


class ConnectionTest(unittest.TestCase):
    def connection(self, settings=None, **overrides):
        values = {"CONNECTION_NAME": "odbm_prod", "CONNECTOR": "salesforce", "DESCRIPTION": " ",
                  "SETTINGS": {"MY_DOMAIN_URL": "https://odbm.my.salesforce.com/", "CLIENT_ID": "key",
                               "USERNAME": "int@odbm.org", "API_VERSION": "", **(settings or {})}}
        values.update(overrides)
        return forms.validate_connection(values, APP)

    def test_defaults(self):
        conn = self.connection()
        self.assertEqual(conn["CONNECTION_NAME"], "ODBM_PROD")
        self.assertEqual(conn["CONNECTOR"], "salesforce")
        self.assertEqual(conn["SETTINGS"]["MY_DOMAIN_URL"], "https://odbm.my.salesforce.com")
        self.assertEqual(conn["SETTINGS"]["API_VERSION"], "64.0")
        self.assertEqual(conn["SECRET_NAME"], "CRM_DB.APP.GLACIER_SECRET_ODBM_PROD")
        self.assertIsNone(conn["DESCRIPTION"])

    def test_keeps_existing_secret_name(self):
        self.assertEqual(self.connection(SECRET_NAME="DB.S.OLD")["SECRET_NAME"], "DB.S.OLD")

    def test_rejects_bad_values(self):
        for bad in (dict(CONNECTION_NAME="a-b"), dict(CONNECTOR="nope"), dict(CONNECTOR=None),
                    dict(settings={"CLIENT_ID": ""})):
            with self.subTest(bad=bad), self.assertRaises(core.ConfigError):
                self.connection(**bad)

    def test_secret_sql_escapes_and_checks_key(self):
        pem = "-----BEGIN PRIVATE KEY-----\nab'c\n-----END PRIVATE KEY-----\n"
        sql = forms.create_secret_sql("DB.S.SEC", pem, "salesforce")
        self.assertIn("SECRET_STRING = '-----BEGIN PRIVATE KEY-----\nab''c\n-----END PRIVATE KEY-----'", sql)
        with self.assertRaises(core.ConfigError):
            forms.create_secret_sql("DB.S.SEC", "not a key", "salesforce")


class MergeTest(unittest.TestCase):
    def test_job_merge(self):
        sql = forms.save_job_sql(APP)
        self.assertTrue(sql.startswith("MERGE INTO CRM_DB.APP.GLACIER_JOBS t"))
        self.assertIn("ON t.JOB_NAME = s.v:JOB_NAME::VARCHAR", sql)
        self.assertIn("FIELDS = AS_ARRAY(s.v:FIELDS)", sql)
        self.assertIn("ENABLED = s.v:ENABLED::BOOLEAN", sql)
        self.assertNotIn("JOB_NAME = s.v:JOB_NAME::VARCHAR,", sql)  # the key isn't updated
        self.assertNotIn("NEXT_RUN_MODE", sql)                       # left for the run to clear
        self.assertEqual(sql.count("?"), 1)

    def test_connection_merge_casts_settings(self):
        sql = forms.save_connection_sql(APP)
        self.assertIn("SETTINGS = s.v:SETTINGS::OBJECT", sql)
        self.assertIn("CONNECTOR = s.v:CONNECTOR::VARCHAR", sql)

    def test_row_param_keeps_nulls(self):
        saved = forms.validate_job(job(), APP, ROLE, "salesforce")
        self.assertIsNone(json.loads(forms.row_param(saved)[0])["TARGET_TABLE"])


if __name__ == "__main__":
    unittest.main()
