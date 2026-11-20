#!/usr/bin/env bash
# Deploy Snowforce to Snowflake: tables, stages, code, procedures, and the Streamlit app.
#
# Usage:
#   app/deploy.sh             Create or update the app (one Snowflake session)
#   app/deploy.sh streamlit   Update only the Streamlit app
#   app/deploy.sh admin-sql   Print the one-time SQL for a Snowflake admin
#   app/deploy.sh render      Print the deploy SQL without running it
#
# Settings are in app/app.env.

set -euo pipefail
cd "$(dirname "$0")"
source ./app.env

app="$APP_DB.$APP_SCHEMA"
imports="$(for f in src/*.py; do printf "'@%s.SNOWFORCE_CODE/%s', " "$app" "$(basename "$f")"; done)"
imports="(${imports%, })"

render() {
  sed -e "s|{{APP}}|$app|g" \
      -e "s|{{APP_ROLE}}|$APP_ROLE|g" \
      -e "s|{{APP_WAREHOUSE}}|$APP_WAREHOUSE|g" \
      -e "s|{{APP_INTEGRATION}}|$APP_INTEGRATION|g" \
      -e "s|{{IMPORTS}}|$imports|g" "$@"
}

deploy_sql() {
  render sql/10_objects.sql
  echo
  echo "-- Procedure code"
  for f in src/*.py; do
    echo "PUT 'file://$PWD/$f' @$app.SNOWFORCE_CODE AUTO_COMPRESS = FALSE OVERWRITE = TRUE;"
  done
  echo
  render sql/30_procedures.sql
  echo
  echo "CALL $app.SNOWFORCE_REFRESH_CONNECTIONS();"
  echo
  streamlit_sql
}

streamlit_sql() {
  echo "-- Streamlit app"
  echo "USE ROLE $APP_ROLE;"
  echo "USE WAREHOUSE $APP_WAREHOUSE;"
  for f in streamlit/*.py streamlit/pyproject.toml src/snowforce_core.py; do
    echo "PUT 'file://$PWD/$f' @$app.SNOWFORCE_CODE/streamlit AUTO_COMPRESS = FALSE OVERWRITE = TRUE;"
  done
  for f in streamlit/pages/*.py; do
    echo "PUT 'file://$PWD/$f' @$app.SNOWFORCE_CODE/streamlit/pages AUTO_COMPRESS = FALSE OVERWRITE = TRUE;"
  done
  render sql/40_streamlit.sql
}

run_sql() {
  sql="$(mktemp)"
  trap 'rm -f "$sql"' EXIT
  "$@" > "$sql"
  snow sql -c "$SNOW_CONNECTION" -f "$sql"
}

case "${1:-deploy}" in
  admin-sql) render sql/20_admin_integration.sql ;;
  render)    deploy_sql ;;
  deploy)
    echo "==> Deploying Snowforce to $app as $APP_ROLE via connection '$SNOW_CONNECTION'..."
    run_sql deploy_sql
    echo "==> Done. Open the SNOWFORCE_APP Streamlit app in Snowsight to add a connection and a job."
    ;;
  streamlit)
    echo "==> Deploying the Streamlit app to $app as $APP_ROLE via connection '$SNOW_CONNECTION'..."
    run_sql streamlit_sql
    ;;
  *) sed -n '2,11p' "$(basename "$0")" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
