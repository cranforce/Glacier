-- One-time setup for a Snowflake admin (ACCOUNTADMIN, or roles with CREATE ROLE,
-- CREATE DATABASE, and CREATE WAREHOUSE), before the first deploy.sh.
-- Print a filled-in copy with:  app/deploy.sh admin-sql setup

USE ROLE ACCOUNTADMIN;

-- The role that owns the app schema, its objects, and the job tasks
CREATE ROLE IF NOT EXISTS {{APP_ROLE}} COMMENT = 'Glacier: owns the app and its job tasks';
GRANT ROLE {{APP_ROLE}} TO ROLE SYSADMIN;

CREATE WAREHOUSE IF NOT EXISTS {{APP_WAREHOUSE}}
  WAREHOUSE_SIZE = XSMALL
  AUTO_SUSPEND = 60
  AUTO_RESUME = TRUE
  INITIALLY_SUSPENDED = TRUE
  COMMENT = 'Glacier: deploys, the Streamlit app, and job runs';
GRANT USAGE, OPERATE ON WAREHOUSE {{APP_WAREHOUSE}} TO ROLE {{APP_ROLE}};

-- The app's own database and schema
CREATE DATABASE IF NOT EXISTS {{APP_DB}} COMMENT = 'Glacier: enterprise system sync';
CREATE SCHEMA IF NOT EXISTS {{APP}} COMMENT = 'Glacier app objects';
GRANT USAGE ON DATABASE {{APP_DB}} TO ROLE {{APP_ROLE}};
GRANT OWNERSHIP ON SCHEMA {{APP}} TO ROLE {{APP_ROLE}} COPY CURRENT GRANTS;

-- The app's Run now buttons start job tasks
GRANT EXECUTE TASK ON ACCOUNT TO ROLE {{APP_ROLE}};

-- Whoever deploys and administers the app
-- GRANT ROLE {{APP_ROLE}} TO USER <you>;

-- The default destination for synced Salesforce data. Each job can name
-- another; jobs without a run-as role load as {{APP_ROLE}}, which owns this one.
CREATE SCHEMA IF NOT EXISTS {{APP_DB}}.NPC_SALESFORCE_PROD_RAW COMMENT = 'Glacier: Salesforce production data';
GRANT OWNERSHIP ON SCHEMA {{APP_DB}}.NPC_SALESFORCE_PROD_RAW TO ROLE {{APP_ROLE}} COPY CURRENT GRANTS;

-- Other roles that load it, such as the local CLI's role or a job's run-as role:
-- GRANT USAGE ON DATABASE {{APP_DB}} TO ROLE <loader_role>;
-- GRANT USAGE, CREATE TABLE ON SCHEMA {{APP_DB}}.NPC_SALESFORCE_PROD_RAW TO ROLE <loader_role>;
