-- The Snowforce Streamlit app. deploy.sh first uploads app/streamlit/ (with a
-- copy of snowforce_core.py) to SNOWFORCE_CODE/streamlit/; CREATE ... FROM
-- copies those files into the app. It runs with its owner's rights, as
-- {{APP_ROLE}}, in {{APP}}.

CREATE OR REPLACE STREAMLIT {{APP}}.SNOWFORCE_APP
  FROM '@{{APP}}.SNOWFORCE_CODE/streamlit'
  MAIN_FILE = 'streamlit_app.py'
  -- The container runtime installs the packages in pyproject.toml
  RUNTIME_NAME = 'SYSTEM$ST_CONTAINER_RUNTIME_PY3_11'
  COMPUTE_POOL = SYSTEM_COMPUTE_POOL_CPU
  QUERY_WAREHOUSE = {{APP_WAREHOUSE}}
  TITLE = 'Snowforce'
  COMMENT = 'Snowforce: Salesforce connections and sync jobs';
