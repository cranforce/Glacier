#!/usr/bin/env bash
# Run the Snowforce app on your machine against the deployed engine, as the
# app role from app.env (the deployed app runs as that role too).
#
# Needs Python 3.11 with the app's packages:
#   python3.11 -m pip install streamlit==1.52.2 snowflake-snowpark-python
#
# Usage: app/streamlit/run_local.sh [streamlit options]

set -euo pipefail
cd "$(dirname "$0")"
source ../app.env

export SNOWFORCE_CONNECTION="$SNOW_CONNECTION"
export SNOWFORCE_APP="$APP_DB.$APP_SCHEMA"
export SNOWFORCE_ROLE="$APP_ROLE"
export SNOWFORCE_WAREHOUSE="$APP_WAREHOUSE"
exec "${PYTHON:-python3.11}" -m streamlit run streamlit_app.py "$@"
