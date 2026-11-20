-- The Glacier Streamlit app. deploy.sh first uploads app/streamlit/ (with a
-- copy of glacier_core.py) to GLACIER_CODE/streamlit/; CREATE ... FROM
-- copies those files into the app. It runs with its owner's rights, as
-- {{APP_ROLE}}, in {{APP}}.

CREATE OR REPLACE STREAMLIT {{APP}}.GLACIER_APP
  FROM '@{{APP}}.GLACIER_CODE/streamlit'
  MAIN_FILE = 'streamlit_app.py'
  -- The warehouse runtime installs environment.yml from Snowflake's Anaconda
  -- channel. The container runtime would need an external access integration
  -- for PyPI.
  RUNTIME_NAME = 'SYSTEM$WAREHOUSE_RUNTIME'
  QUERY_WAREHOUSE = {{APP_WAREHOUSE}}
  TITLE = 'Glacier'
  COMMENT = 'Glacier: Salesforce connections and sync jobs';
