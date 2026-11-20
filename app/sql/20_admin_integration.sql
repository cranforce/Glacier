-- One-time setup for a Snowflake admin (ACCOUNTADMIN, or a role with
-- CREATE INTEGRATION), once per connector. Run after the first deploy.sh,
-- which creates the connector's network rule.
-- Print a filled-in copy with:  app/deploy.sh admin-sql integration <connector>

USE ROLE ACCOUNTADMIN;

-- Lets the connector's procedures reach its hosts. ALLOWED_AUTHENTICATION_SECRETS
-- = ALL means adding a connection needs no admin; only {{APP_ROLE}} can use it.
CREATE EXTERNAL ACCESS INTEGRATION IF NOT EXISTS GLACIER_{{CONNECTOR}}_EAI
  ALLOWED_NETWORK_RULES = ({{APP}}.GLACIER_{{CONNECTOR}}_EGRESS)
  ALLOWED_AUTHENTICATION_SECRETS = ALL
  ENABLED = TRUE
  COMMENT = 'Glacier: {{CONNECTOR}} connector';

GRANT USAGE ON INTEGRATION GLACIER_{{CONNECTOR}}_EAI TO ROLE {{APP_ROLE}};

-- For each user a job runs as (EXECUTE AS USER). The task owner role must be
-- able to impersonate the user, and the user must hold the task owner role.
-- GRANT IMPERSONATE ON USER <run_as_user> TO ROLE {{APP_ROLE}};
-- GRANT ROLE {{APP_ROLE}} TO USER <run_as_user>;
-- GRANT ROLE <job_role> TO USER <run_as_user>;   -- only if the job switches roles
