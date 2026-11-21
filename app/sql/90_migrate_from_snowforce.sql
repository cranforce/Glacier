-- Copies Snowforce's connections, jobs, and captured fields from
-- CRM_DB.CMILLER_SF_PLAYGROUND into Glacier, with their run history and synced
-- tables. Run once, after deploy.sh. It runs as {{APP_ROLE}}, which needs SELECT
-- on the old SNOWFORCE_ tables and the jobs' tables. Rows and tables already in
-- Glacier are left alone, so it's safe to run again.
-- Print a filled-in copy with:  app/deploy.sh migrate-sql
--
-- Since run (2026-11-20): the Salesforce connector now names tables and columns
-- GIFT_TRANSACTION / SYSTEM_MODSTAMP rather than uppercasing them, and the cloned
-- tables and their columns were renamed to match (jobs' TARGET_TABLE set to NULL).
-- Running this again would clone the old names back, so don't.
--
-- Afterwards:
-- 1. Re-enter each connection's private key on the Connections page (secret
--    values can't be read back). That re-creates its secret and calls
--    GLACIER_REFRESH_CONNECTIONS.
-- 2. Save each job on the Jobs page, or CALL GLACIER_SAVE_JOB_TASK('<job>'), to
--    create its task.

USE ROLE {{APP_ROLE}};
USE WAREHOUSE {{APP_WAREHOUSE}};

BEGIN;

-- Snowforce connections were all Salesforce; their columns become SETTINGS
INSERT INTO {{APP}}.GLACIER_CONNECTIONS
  (CONNECTION_NAME, CONNECTOR, SETTINGS, SECRET_NAME, DESCRIPTION, CREATED_AT)
SELECT c.CONNECTION_NAME, 'salesforce',
       OBJECT_CONSTRUCT('MY_DOMAIN_URL', c.MY_DOMAIN_URL, 'CLIENT_ID', c.CLIENT_ID,
                        'USERNAME', c.USERNAME, 'API_VERSION', c.API_VERSION),
       '{{APP}}.GLACIER_SECRET_' || c.CONNECTION_NAME, c.DESCRIPTION, c.CREATED_AT
FROM CRM_DB.CMILLER_SF_PLAYGROUND.SNOWFORCE_CONNECTIONS c
WHERE c.CONNECTION_NAME NOT IN (SELECT CONNECTION_NAME FROM {{APP}}.GLACIER_CONNECTIONS);

-- Jobs load the new default destination, where their tables are cloned below.
-- The old app role, DS_DATA_ENGINEERING_ROLE, is dropped as a run-as role
-- without a run-as user (the new app role runs those jobs), and SNOWFORCE_WH
-- becomes the app warehouse.
INSERT INTO {{APP}}.GLACIER_JOBS
  (JOB_NAME, CONNECTION_NAME, SOURCE_OBJECT, FIELDS, TARGET_DATABASE, TARGET_SCHEMA, TARGET_TABLE,
   DELETE_MODE, RECONCILE_AFTER_DAYS, TRACK_HISTORY, RUN_AS_USER, RUN_AS_ROLE, WAREHOUSE, SCHEDULE,
   ENABLED, CREATED_AT)
SELECT j.JOB_NAME, j.CONNECTION_NAME, j.SOBJECT, j.FIELDS,
       '{{APP_DB}}', 'NPC_SALESFORCE_PROD_RAW', j.TARGET_TABLE,
       j.DELETE_MODE, j.RECONCILE_AFTER_DAYS, j.TRACK_HISTORY, j.RUN_AS_USER,
       IFF(j.RUN_AS_USER IS NULL AND j.RUN_AS_ROLE = 'DS_DATA_ENGINEERING_ROLE', NULL, j.RUN_AS_ROLE),
       IFF(j.WAREHOUSE = 'SNOWFORCE_WH', '{{APP_WAREHOUSE}}', j.WAREHOUSE), j.SCHEDULE,
       j.ENABLED, j.CREATED_AT
FROM CRM_DB.CMILLER_SF_PLAYGROUND.SNOWFORCE_JOBS j
WHERE j.JOB_NAME NOT IN (SELECT JOB_NAME FROM {{APP}}.GLACIER_JOBS);

-- Saves describing every object again
INSERT INTO {{APP}}.GLACIER_FIELDS
  (CONNECTION_NAME, SOURCE_OBJECT, POSITION, FIELD_NAME, LABEL, SOURCE_TYPE, LENGTH, PRECISION, SCALE,
   NILLABLE, CALCULATED, CUSTOM, REFERENCE_TO, SNOWFLAKE_TYPE, EXPORTABLE, CAPTURED_AT)
SELECT f.CONNECTION_NAME, f.SOBJECT, f.POSITION, f.FIELD_NAME, f.LABEL, f.SF_TYPE, f.LENGTH, f.PRECISION,
       f.SCALE, f.NILLABLE, f.CALCULATED, f.CUSTOM, f.REFERENCE_TO, f.SNOWFLAKE_TYPE, f.EXPORTABLE,
       f.CAPTURED_AT
FROM CRM_DB.CMILLER_SF_PLAYGROUND.SNOWFORCE_FIELDS f
WHERE NOT EXISTS (SELECT 1 FROM {{APP}}.GLACIER_FIELDS g
                  WHERE g.CONNECTION_NAME = f.CONNECTION_NAME AND g.SOURCE_OBJECT = f.SOBJECT);

-- Successful syncs here keep the first runs from reconciling
INSERT INTO {{APP}}.GLACIER_RUNS
  (RUN_ID, JOB_NAME, MODE, STATUS, STARTED_AT, FINISHED_AT, RUN_USER, RUN_ROLE, WATERMARK, SOURCE_QUERY,
   ROWS_EXPORTED, ROWS_INSERTED, ROWS_UPDATED, ROWS_DELETED, MESSAGE)
SELECT r.RUN_ID, r.JOB_NAME, r.MODE, r.STATUS, r.STARTED_AT, r.FINISHED_AT, r.RUN_USER, r.RUN_ROLE,
       r.WATERMARK, r.SOQL, r.ROWS_EXPORTED, r.ROWS_INSERTED, r.ROWS_UPDATED, r.ROWS_DELETED, r.MESSAGE
FROM CRM_DB.CMILLER_SF_PLAYGROUND.SNOWFORCE_RUNS r
WHERE r.RUN_ID NOT IN (SELECT RUN_ID FROM {{APP}}.GLACIER_RUNS);

COMMIT;

-- Clone each job's table (and its history) so its watermark carries over and
-- its first run is a delta, not a full load. Zero-copy clones use no warehouse.
EXECUTE IMMEDIATE $$
DECLARE
  tables CURSOR FOR
    SELECT j.TARGET_DATABASE || '.' || j.TARGET_SCHEMA AS SOURCE_SCHEMA, j.TARGET_TABLE || s.SUFFIX AS NAME
    FROM CRM_DB.CMILLER_SF_PLAYGROUND.SNOWFORCE_JOBS j
    JOIN (SELECT '' AS SUFFIX UNION ALL SELECT '__HISTORY') s ON s.SUFFIX = '' OR j.TRACK_HISTORY
    WHERE j.TARGET_TABLE IS NOT NULL
      AND j.TARGET_TABLE || s.SUFFIX NOT IN (
        SELECT TABLE_NAME FROM {{APP_DB}}.INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = 'NPC_SALESFORCE_PROD_RAW');
BEGIN
  FOR t IN tables DO
    EXECUTE IMMEDIATE 'CREATE TABLE {{APP_DB}}.NPC_SALESFORCE_PROD_RAW."' || t.NAME || '" CLONE '
      || t.SOURCE_SCHEMA || '."' || t.NAME || '"';
  END FOR;
END;
$$;
