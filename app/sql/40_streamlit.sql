-- The Snowforce Streamlit app. deploy.sh first uploads app/streamlit/ (with a
-- copy of snowforce_core.py) to SNOWFORCE_CODE/streamlit/; CREATE ... FROM
-- copies those files into the app. It runs with its owner's rights, as
-- {{APP_ROLE}}, in {{APP}}.

CREATE OR REPLACE STREAMLIT {{APP}}.SNOWFORCE_APP
  FROM '@{{APP}}.SNOWFORCE_CODE/streamlit'
  MAIN_FILE = 'streamlit_app.py'
  -- The warehouse runtime installs environment.yml from Snowflake's Anaconda
  -- channel. The container runtime would need an external access integration
  -- for PyPI.
  RUNTIME_NAME = 'SYSTEM$WAREHOUSE_RUNTIME'
  QUERY_WAREHOUSE = {{APP_WAREHOUSE}}
  TITLE = 'Snowforce'
  COMMENT = 'Snowforce: Salesforce connections and sync jobs';
