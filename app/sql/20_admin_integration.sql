-- One-time setup for a Snowflake admin (ACCOUNTADMIN, or a role with
-- CREATE INTEGRATION). Run after the first deploy.sh, which creates the network rule.
-- Print a filled-in copy with:  app/deploy.sh admin-sql integration

USE ROLE ACCOUNTADMIN;

-- Lets the Glacier procedures reach Salesforce. ALLOWED_AUTHENTICATION_SECRETS
-- = ALL means adding a connection needs no admin; only {{APP_ROLE}} can use it.
CREATE EXTERNAL ACCESS INTEGRATION IF NOT EXISTS {{APP_INTEGRATION}}
  ALLOWED_NETWORK_RULES = ({{APP}}.GLACIER_SALESFORCE_EGRESS)
  ALLOWED_AUTHENTICATION_SECRETS = ALL
  ENABLED = TRUE
  COMMENT = 'Glacier: Salesforce Bulk API access';

GRANT USAGE ON INTEGRATION {{APP_INTEGRATION}} TO ROLE {{APP_ROLE}};

-- For each user a job runs as (EXECUTE AS USER). The task owner role must be
-- able to impersonate the user, and the user must hold the task owner role.
-- GRANT IMPERSONATE ON USER <run_as_user> TO ROLE {{APP_ROLE}};
-- GRANT ROLE {{APP_ROLE}} TO USER <run_as_user>;
-- GRANT ROLE <job_role> TO USER <run_as_user>;   -- only if the job switches roles
