#!/usr/bin/env bash
# Deploy the Snowforce engine to Snowflake: tables, stages, code, and procedures.
#
# Usage:
#   app/deploy.sh             Create or update the app (one Snowflake session)
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
}

case "${1:-deploy}" in
  admin-sql) render sql/20_admin_integration.sql ;;
  render)    deploy_sql ;;
  deploy)
    sql="$(mktemp)"
    trap 'rm -f "$sql"' EXIT
    deploy_sql > "$sql"
    echo "==> Deploying Snowforce to $app as $APP_ROLE via connection '$SNOW_CONNECTION'..."
    snow sql -c "$SNOW_CONNECTION" -f "$sql"
    echo "==> Done. See app/sql/examples.sql to add a connection and a job."
    ;;
  *) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
