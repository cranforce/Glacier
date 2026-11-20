#!/usr/bin/env bash
# Run the Glacier app on your machine against the deployed engine, as the
# app role from app.env (the deployed app runs as that role too).
#
# Needs Python 3.11 with the app's packages:
#   python3.11 -m pip install streamlit==1.52.2 snowflake-snowpark-python
#
# Usage: app/streamlit/run_local.sh [streamlit options]

set -euo pipefail
cd "$(dirname "$0")"
source ../app.env

export GLACIER_CONNECTION="$SNOW_CONNECTION"
export GLACIER_APP="$APP_DB.$APP_SCHEMA"
export GLACIER_ROLE="$APP_ROLE"
export GLACIER_WAREHOUSE="$APP_WAREHOUSE"
exec "${PYTHON:-python3.11}" -m streamlit run streamlit_app.py "$@"
