-- Glacier procedures that don't read connection secrets. The ones that do, one
-- set per connector (GLACIER_<CONNECTOR>_EXTRACT and so on), are created by
-- GLACIER_REFRESH_CONNECTIONS, because their secret list changes with the
-- connections. deploy.sh uploads the code and fills in {{...}}.

USE ROLE {{APP_ROLE}};
USE WAREHOUSE {{APP_WAREHOUSE}};

-- Runs one job as the caller: the task's role, or yours when you CALL it.
-- MODE: auto (the job's NEXT_RUN_MODE, else sync), sync, full, or reconcile.
CREATE OR REPLACE PROCEDURE {{APP}}.GLACIER_RUN_JOB(
    JOB_NAME VARCHAR, MODE VARCHAR DEFAULT 'auto', APP_SCHEMA VARCHAR DEFAULT '{{APP}}')
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.11'
  PACKAGES = ('snowflake-snowpark-python', 'requests')
  IMPORTS = {{IMPORTS}}
  HANDLER = 'glacier_procs.run_job'
  EXECUTE AS CALLER;

-- Creates or replaces a job's task from its GLACIER_JOBS row.
CREATE OR REPLACE PROCEDURE {{APP}}.GLACIER_SAVE_JOB_TASK(
    JOB_NAME VARCHAR, APP_SCHEMA VARCHAR DEFAULT '{{APP}}')
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.11'
  PACKAGES = ('snowflake-snowpark-python', 'requests')
  IMPORTS = {{IMPORTS}}
  HANDLER = 'glacier_procs.save_job_task'
  EXECUTE AS OWNER;

-- Re-creates each connector's procedures with its connections' secrets, for
-- every connector whose integration (GLACIER_<CONNECTOR>_EAI) exists.
-- Call it after adding or removing a connection.
CREATE OR REPLACE PROCEDURE {{APP}}.GLACIER_REFRESH_CONNECTIONS(
    APP_SCHEMA VARCHAR DEFAULT '{{APP}}')
  RETURNS VARIANT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.11'
  PACKAGES = ('snowflake-snowpark-python', 'requests')
  IMPORTS = {{IMPORTS}}
  HANDLER = 'glacier_procs.refresh_connections'
  EXECUTE AS OWNER;
