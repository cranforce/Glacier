#!/usr/bin/env bash
# Deploy Glacier to Snowflake: tables, stages, code, procedures, and the Streamlit app.
#
# Usage:
#   app/deploy.sh                        Create or update the app (one Snowflake session)
#   app/deploy.sh streamlit              Update only the Streamlit app
#   app/deploy.sh admin-sql setup        One-time admin SQL, before the first deploy
#   app/deploy.sh admin-sql integration <connector>
#                                        One-time admin SQL per connector, after the first deploy
#   app/deploy.sh render                 Print the deploy SQL without running it
#
# Settings are in app/app.env.

set -euo pipefail
cd "$(dirname "$0")"
source ./app.env

app="$APP_DB.$APP_SCHEMA"
imports="$(for f in src/*.py; do printf "'@%s.GLACIER_CODE/%s', " "$app" "$(basename "$f")"; done)"
imports="(${imports%, })"

render() {
  sed -e "s|{{APP}}|$app|g" \
      -e "s|{{APP_DB}}|$APP_DB|g" \
      -e "s|{{APP_ROLE}}|$APP_ROLE|g" \
      -e "s|{{APP_WAREHOUSE}}|$APP_WAREHOUSE|g" \
      -e "s|{{IMPORTS}}|$imports|g" "$@"
}

deploy_sql() {
  render sql/10_objects.sql
  echo
  echo "-- Procedure code"
  for f in src/*.py; do
    echo "PUT 'file://$PWD/$f' @$app.GLACIER_CODE AUTO_COMPRESS = FALSE OVERWRITE = TRUE;"
  done
  echo
  render sql/30_procedures.sql
  echo
  echo "CALL $app.GLACIER_REFRESH_CONNECTIONS();"
  echo
  streamlit_sql
}

streamlit_sql() {
  echo "-- Streamlit app"
  echo "USE ROLE $APP_ROLE;"
  echo "USE WAREHOUSE $APP_WAREHOUSE;"
  for f in streamlit/*.py streamlit/environment.yml src/*.py; do
    echo "PUT 'file://$PWD/$f' @$app.GLACIER_CODE/streamlit AUTO_COMPRESS = FALSE OVERWRITE = TRUE;"
  done
  for f in streamlit/pages/*.py; do
    echo "PUT 'file://$PWD/$f' @$app.GLACIER_CODE/streamlit/pages AUTO_COMPRESS = FALSE OVERWRITE = TRUE;"
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
  admin-sql)
    case "${2:-}" in
      setup)       render sql/00_admin_setup.sql ;;
      integration)
        connector="$(echo "${3:-}" | tr '[:upper:]' '[:lower:]')"
        if [[ -z "$connector" || ! -f "src/connector_$connector.py" ]]; then
          echo "Usage: $0 admin-sql integration <connector>, e.g. salesforce" >&2; exit 1
        fi
        render sql/20_admin_integration.sql |
          sed -e "s|{{CONNECTOR}}|$(echo "$connector" | tr '[:lower:]' '[:upper:]')|g" ;;
      *) echo "Usage: $0 admin-sql setup|integration <connector>" >&2; exit 1 ;;
    esac ;;
  render)    deploy_sql ;;
  deploy)
    echo "==> Deploying Glacier to $app as $APP_ROLE via connection '$SNOW_CONNECTION'..."
    run_sql deploy_sql
    echo "==> Done. Open the GLACIER_APP Streamlit app in Snowsight to add a connection and a job."
    ;;
  streamlit)
    echo "==> Deploying the Streamlit app to $app as $APP_ROLE via connection '$SNOW_CONNECTION'..."
    run_sql streamlit_sql
    ;;
  *) sed -n '2,13p' "$(basename "$0")" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
