#!/usr/bin/env sh
set -eu

: "${DATA_DIR:=/opt/miru/data}"
: "${LOG_DIR:=/opt/miru/logs}"
: "${PORT:=5001}"
: "${SERVER_PORT:=$PORT}"
: "${MIRU_HEADLESS:=1}"
: "${FLASK_DEBUG:=false}"
: "${TZ:=Asia/Shanghai}"
: "${TIMEZONE:=$TZ}"

export DATA_DIR LOG_DIR PORT SERVER_PORT MIRU_HEADLESS FLASK_DEBUG TZ TIMEZONE

mkdir -p "$DATA_DIR" "$LOG_DIR" "$DATA_DIR/_admin"

python scripts/docker_init.py

exec "$@"
