-- Glacier app objects. deploy.sh fills in the {{...}} settings from app.env.
-- Tables are created only if missing, so re-running this keeps existing config.

USE ROLE {{APP_ROLE}};
USE WAREHOUSE {{APP_WAREHOUSE}};

-- One row per source system account, e.g. a Salesforce org. SETTINGS holds the
-- connector's own settings; for Salesforce, MY_DOMAIN_URL, CLIENT_ID, USERNAME,
-- and API_VERSION. The secret is a GENERIC_STRING secret with the credential:
-- for Salesforce, the PEM private key whose certificate is uploaded to the
-- External Client App.
CREATE TABLE IF NOT EXISTS {{APP}}.GLACIER_CONNECTIONS (
  CONNECTION_NAME VARCHAR NOT NULL PRIMARY KEY,
  CONNECTOR       VARCHAR NOT NULL COMMENT 'salesforce',
  SETTINGS        OBJECT NOT NULL COMMENT 'The connector''s settings',
  SECRET_NAME     VARCHAR NOT NULL COMMENT 'Fully qualified GENERIC_STRING secret with the credential',
  DESCRIPTION     VARCHAR,
  CREATED_AT      TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP(),
  UPDATED_AT      TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
);

-- One row per sync job. Each job gets a task, GLACIER_JOB_<JOB_NAME>.
CREATE TABLE IF NOT EXISTS {{APP}}.GLACIER_JOBS (
  JOB_NAME             VARCHAR NOT NULL PRIMARY KEY,
  CONNECTION_NAME      VARCHAR NOT NULL,
  SOURCE_OBJECT        VARCHAR NOT NULL COMMENT 'The object to sync, e.g. a Salesforce object API name',
  FIELDS               ARRAY COMMENT 'Field names; NULL for every exportable field',
  TARGET_DATABASE      VARCHAR NOT NULL,
  TARGET_SCHEMA        VARCHAR NOT NULL,
  TARGET_TABLE         VARCHAR COMMENT 'NULL for the object name in the connector''s convention (Subscription__c -> SUBSCRIPTION_C); reserved words like ORDER are quoted',
  DELETE_MODE          VARCHAR DEFAULT 'flag' NOT NULL COMMENT 'off, flag, or remove',
  RECONCILE_AFTER_DAYS NUMBER DEFAULT 14 NOT NULL,
  TRACK_HISTORY        BOOLEAN DEFAULT FALSE NOT NULL COMMENT 'Keep type 2 history in <TARGET_TABLE>__HISTORY',
  RUN_AS_USER          VARCHAR COMMENT 'NULL to run as the system service in the app role',
  RUN_AS_ROLE          VARCHAR COMMENT 'Needs RUN_AS_USER unless it is the app role',
  WAREHOUSE            VARCHAR NOT NULL,
  SCHEDULE             VARCHAR COMMENT 'Cron with time zone, e.g. 0 * * * * UTC; NULL for manual only',
  ENABLED              BOOLEAN DEFAULT TRUE NOT NULL,
  NEXT_RUN_MODE        VARCHAR COMMENT 'One-off full or reconcile for the next run',
  CREATED_AT           TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP(),
  UPDATED_AT           TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
);

-- One row per run.
CREATE TABLE IF NOT EXISTS {{APP}}.GLACIER_RUNS (
  RUN_ID        VARCHAR NOT NULL PRIMARY KEY,
  JOB_NAME      VARCHAR NOT NULL,
  MODE          VARCHAR,
  STATUS        VARCHAR COMMENT 'RUNNING, SUCCEEDED, or FAILED',
  STARTED_AT    TIMESTAMP_LTZ,
  FINISHED_AT   TIMESTAMP_LTZ,
  RUN_USER      VARCHAR,
  RUN_ROLE      VARCHAR,
  WATERMARK     VARCHAR,
  SOURCE_QUERY  VARCHAR,
  ROWS_EXPORTED NUMBER,
  ROWS_INSERTED NUMBER,
  ROWS_UPDATED  NUMBER,
  ROWS_DELETED  NUMBER,
  MESSAGE       VARCHAR
);

-- Each object's captured fields (a Salesforce describe).
CREATE TABLE IF NOT EXISTS {{APP}}.GLACIER_FIELDS (
  CONNECTION_NAME VARCHAR NOT NULL,
  SOURCE_OBJECT   VARCHAR NOT NULL,
  POSITION        NUMBER,
  FIELD_NAME      VARCHAR NOT NULL,
  LABEL           VARCHAR,
  SOURCE_TYPE     VARCHAR,
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

-- Procedure code, and exports waiting to be loaded
CREATE STAGE IF NOT EXISTS {{APP}}.GLACIER_CODE ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE');
CREATE STAGE IF NOT EXISTS {{APP}}.GLACIER_EXPORTS ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE');

CREATE OR REPLACE FILE FORMAT {{APP}}.GLACIER_CSV
  TYPE = 'CSV'
  PARSE_HEADER = TRUE
  FIELD_OPTIONALLY_ENCLOSED_BY = '"'
  NULL_IF = ('', 'NULL')
  EMPTY_FIELD_AS_NULL = TRUE;

-- One network rule per connector; each admin-created integration references its
-- connector's rule. Salesforce: every production org and sandbox.
CREATE NETWORK RULE IF NOT EXISTS {{APP}}.GLACIER_SALESFORCE_EGRESS
  MODE = EGRESS
  TYPE = HOST_PORT
  VALUE_LIST = ('login.salesforce.com', 'test.salesforce.com',
                '*.my.salesforce.com', '*.sandbox.my.salesforce.com');
