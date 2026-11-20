-- Copies Snowforce's connections, jobs, and captured fields from
-- CRM_DB.CMILLER_SF_PLAYGROUND into Glacier. Run once, after deploy.sh. It runs as
-- {{APP_ROLE}}, which needs SELECT on the old SNOWFORCE_ tables. Rows already in
-- Glacier are left alone, so it's safe to run again. Run history isn't copied.
-- Print a filled-in copy with:  app/deploy.sh migrate-sql
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

-- Jobs load the new default destination, so each one's first run is a full
-- load. The old app role, DS_DATA_ENGINEERING_ROLE, is dropped as a run-as role
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

COMMIT;
