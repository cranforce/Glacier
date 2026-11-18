-- Snowforce app objects. deploy.sh fills in the {{...}} settings from app.env.
-- Tables are created only if missing, so re-running this keeps existing config.

USE ROLE {{APP_ROLE}};
USE WAREHOUSE {{APP_WAREHOUSE}};

-- One row per Salesforce org, logging in with the JWT bearer flow. The secret is
-- a GENERIC_STRING secret holding the PEM private key whose certificate is
-- uploaded to the External Client App.
CREATE TABLE IF NOT EXISTS {{APP}}.SNOWFORCE_CONNECTIONS (
  CONNECTION_NAME VARCHAR NOT NULL PRIMARY KEY,
  MY_DOMAIN_URL   VARCHAR NOT NULL COMMENT 'https://<domain>.my.salesforce.com',
  CLIENT_ID       VARCHAR COMMENT 'External Client App consumer key',
  USERNAME        VARCHAR COMMENT 'Salesforce integration user the jobs run as',
  API_VERSION     VARCHAR DEFAULT '64.0' NOT NULL,
  SECRET_NAME     VARCHAR NOT NULL COMMENT 'Fully qualified GENERIC_STRING secret with the private key',
  DESCRIPTION     VARCHAR,
  CREATED_AT      TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP(),
  UPDATED_AT      TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
);
-- Tables created before the switch to JWT bearer
ALTER TABLE {{APP}}.SNOWFORCE_CONNECTIONS ADD COLUMN
  IF NOT EXISTS CLIENT_ID VARCHAR COMMENT 'External Client App consumer key',
  IF NOT EXISTS USERNAME VARCHAR COMMENT 'Salesforce integration user the jobs run as';

-- One row per sync job. Each job gets a task, SNOWFORCE_JOB_<JOB_NAME>.
CREATE TABLE IF NOT EXISTS {{APP}}.SNOWFORCE_JOBS (
  JOB_NAME             VARCHAR NOT NULL PRIMARY KEY,
  CONNECTION_NAME      VARCHAR NOT NULL,
  SOBJECT              VARCHAR NOT NULL COMMENT 'Salesforce object API name',
  FIELDS               ARRAY COMMENT 'Field API names; NULL for every exportable field',
  TARGET_DATABASE      VARCHAR NOT NULL,
  TARGET_SCHEMA        VARCHAR NOT NULL,
  TARGET_TABLE         VARCHAR COMMENT 'NULL for the object name, uppercased; reserved words like ORDER are quoted',
  DELETE_MODE          VARCHAR DEFAULT 'flag' NOT NULL COMMENT 'off, flag, or remove',
  RECONCILE_AFTER_DAYS NUMBER DEFAULT 14 NOT NULL,
  RUN_AS_USER          VARCHAR COMMENT 'NULL to run as the system service in the app role',
  RUN_AS_ROLE          VARCHAR COMMENT 'Needs RUN_AS_USER unless it is the app role',
  WAREHOUSE            VARCHAR NOT NULL,
  SCHEDULE             VARCHAR COMMENT 'Cron with time zone, e.g. 0 * * * * UTC; NULL for manual only',
  ENABLED              BOOLEAN DEFAULT TRUE NOT NULL,
  NEXT_RUN_MODE        VARCHAR COMMENT 'One-off full or reconcile for the next run',
  CREATED_AT           TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP(),
  UPDATED_AT           TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
);

-- One row per run; replaces the CLI's state/ folder.
CREATE TABLE IF NOT EXISTS {{APP}}.SNOWFORCE_RUNS (
  RUN_ID        VARCHAR NOT NULL PRIMARY KEY,
  JOB_NAME      VARCHAR NOT NULL,
  MODE          VARCHAR,
  STATUS        VARCHAR COMMENT 'RUNNING, SUCCEEDED, or FAILED',
  STARTED_AT    TIMESTAMP_LTZ,
  FINISHED_AT   TIMESTAMP_LTZ,
  RUN_USER      VARCHAR,
  RUN_ROLE      VARCHAR,
  WATERMARK     VARCHAR,
  SOQL          VARCHAR,
  ROWS_EXPORTED NUMBER,
  ROWS_INSERTED NUMBER,
  ROWS_UPDATED  NUMBER,
  ROWS_DELETED  NUMBER,
  MESSAGE       VARCHAR
);

-- Captured Salesforce describe output; replaces the CLI's schema/ folder.
CREATE TABLE IF NOT EXISTS {{APP}}.SNOWFORCE_FIELDS (
  CONNECTION_NAME VARCHAR NOT NULL,
  SOBJECT         VARCHAR NOT NULL,
  POSITION        NUMBER,
  FIELD_NAME      VARCHAR NOT NULL,
  LABEL           VARCHAR,
  SF_TYPE         VARCHAR,
  LENGTH          NUMBER,
  PRECISION       NUMBER,
  SCALE           NUMBER,
  NILLABLE        BOOLEAN,
  CALCULATED      BOOLEAN,
  CUSTOM          BOOLEAN,
  REFERENCE_TO    VARCHAR,
  SNOWFLAKE_TYPE  VARCHAR,
  EXPORTABLE      BOOLEAN,
  CAPTURED_AT     TIMESTAMP_LTZ
);

-- Procedure code, and Bulk API exports waiting to be loaded
CREATE STAGE IF NOT EXISTS {{APP}}.SNOWFORCE_CODE ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE');
CREATE STAGE IF NOT EXISTS {{APP}}.SNOWFORCE_EXPORTS ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE');

CREATE OR REPLACE FILE FORMAT {{APP}}.SNOWFORCE_CSV
  TYPE = 'CSV'
  PARSE_HEADER = TRUE
  FIELD_OPTIONALLY_ENCLOSED_BY = '"'
  NULL_IF = ('', 'NULL')
  EMPTY_FIELD_AS_NULL = TRUE;

-- Every production org and sandbox. The admin's integration references this rule.
CREATE NETWORK RULE IF NOT EXISTS {{APP}}.SNOWFORCE_SALESFORCE_EGRESS
  MODE = EGRESS
  TYPE = HOST_PORT
  VALUE_LIST = ('login.salesforce.com', 'test.salesforce.com',
                '*.my.salesforce.com', '*.sandbox.my.salesforce.com');
