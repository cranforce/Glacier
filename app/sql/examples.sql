-- Using the Glacier engine from SQL, until the Streamlit app exists.
-- Replace GLACIER_DB.APP and GLACIER_APP_ROLE if app.env points somewhere else.

USE ROLE GLACIER_APP_ROLE;
USE WAREHOUSE GLACIER_WH;
USE SCHEMA GLACIER_DB.APP;

-- 1. Add a connection ---------------------------------------------------------
-- Logs in with the JWT bearer flow. Make a key pair once per connection:
--   openssl req -x509 -newkey rsa:2048 -nodes -days 730 -subj "/CN=Glacier" \
--     -keyout glacier.key -out glacier.crt
-- Upload glacier.crt to the External Client App; paste glacier.key below,
-- BEGIN and END lines included. Snowflake masks SECRET values in query history.
-- Create it as GLACIER_APP_ROLE: the app's procedures can only use
-- secrets that role owns or has USAGE on.
CREATE OR REPLACE SECRET GLACIER_SECRET_ODBM_PROD
  TYPE = GENERIC_STRING
  SECRET_STRING = '-----BEGIN PRIVATE KEY-----
...
-----END PRIVATE KEY-----';

INSERT INTO GLACIER_CONNECTIONS
  (CONNECTION_NAME, MY_DOMAIN_URL, CLIENT_ID, USERNAME, SECRET_NAME, DESCRIPTION)
VALUES ('ODBM_PROD', 'https://ourdailybreadministries.my.salesforce.com',
        '<consumer key>', '<integration user username>',
        'GLACIER_DB.APP.GLACIER_SECRET_ODBM_PROD', 'Production org');

-- Re-create the Salesforce procedures so they can read the new secret
CALL GLACIER_REFRESH_CONNECTIONS();
CALL GLACIER_TEST_CONNECTION('ODBM_PROD');

-- 2. Look around ------------------------------------------------------------------
CALL GLACIER_LIST_OBJECTS('ODBM_PROD');
CALL GLACIER_DESCRIBE('ODBM_PROD', 'Subscription__c');
SELECT FIELD_NAME, SF_TYPE, SNOWFLAKE_TYPE, EXPORTABLE
FROM GLACIER_FIELDS
WHERE CONNECTION_NAME = 'ODBM_PROD' AND SOBJECT = 'Subscription__c'
ORDER BY POSITION;

-- 3. Add a job --------------------------------------------------------------------
-- FIELDS NULL means every exportable field. TARGET_TABLE NULL means the object
-- name (SUBSCRIPTION__C here); set it to keep loading an existing table.
INSERT INTO GLACIER_JOBS
  (JOB_NAME, CONNECTION_NAME, SOBJECT, FIELDS, TARGET_DATABASE, TARGET_SCHEMA, TARGET_TABLE,
   DELETE_MODE, WAREHOUSE, SCHEDULE)
SELECT 'SUBSCRIPTION', 'ODBM_PROD', 'Subscription__c',
       ARRAY_CONSTRUCT('Account__c', 'Status__c', 'Start_Date__c', 'End_Date__c'),
       'GLACIER_DB', 'NPC_SALESFORCE_PROD_RAW', NULL,
       'flag', 'GLACIER_WH', '0 * * * * UTC';

-- To keep type 2 history in SUBSCRIPTION__C__HISTORY:
UPDATE GLACIER_JOBS SET TRACK_HISTORY = TRUE WHERE JOB_NAME = 'SUBSCRIPTION';

-- 4. Run it -------------------------------------------------------------------------
-- Directly, as your current role:
CALL GLACIER_RUN_JOB('SUBSCRIPTION');            -- sync (full load the first time)
CALL GLACIER_RUN_JOB('SUBSCRIPTION', 'full');    -- full reload
CALL GLACIER_RUN_JOB('SUBSCRIPTION', 'reconcile');

-- Or as a task, on the job's schedule, user, role, and warehouse:
CALL GLACIER_SAVE_JOB_TASK('SUBSCRIPTION');      -- re-run after changing the job
EXECUTE TASK GLACIER_JOB_SUBSCRIPTION;           -- run it now

-- 5. History --------------------------------------------------------------------------
SELECT * FROM GLACIER_RUNS ORDER BY STARTED_AT DESC LIMIT 20;

-- With TRACK_HISTORY, every version of a record:
SELECT * FROM GLACIER_DB.NPC_SALESFORCE_PROD_RAW.SUBSCRIPTION__C__HISTORY
WHERE ID = '<record id>' ORDER BY VALID_FROM;
