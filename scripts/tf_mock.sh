#!/usr/bin/env bash
# tf_mock.sh — start/stop the moto mock-AWS endpoint used for OFFLINE terraform
# plan (todo 24).
#
# When offline_plan=true the env roots (infra/terraform/envs/*) point the AWS
# provider's custom endpoints at http://localhost:${MOTO_PORT:-5000} and skip
# credential validation. moto then answers the data sources a plan actually
# reads — STS GetCallerIdentity (aws_caller_identity / aws_iam_session_context)
# and EC2 (aws_subnet etc.) — while aws_partition / aws_region resolve locally.
# The three EKS offline guards in the env roots avoid the data sources moto
# cannot serve (SSM /aws/ AMI parameter, EKS addon versions, OIDC tls_certificate).
#
# Usage:
#   scripts/tf_mock.sh start     # idempotent; starts moto_server if not already up
#   scripts/tf_mock.sh stop      # kills moto_server and verifies the port is free
#   scripts/tf_mock.sh status    # exit 0 when up, 1 when down
#   scripts/tf_mock.sh restart   # stop + start
#
# Env overrides: MOTO_PORT (default 5000).
# Logs: /tmp/tf_mock_moto.<port>.log   PID: /tmp/tf_mock_moto.<port>.pid
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${MOTO_PORT:-5000}"
PIDFILE="/tmp/tf_mock_moto.${PORT}.pid"
LOGFILE="/tmp/tf_mock_moto.${PORT}.log"
URL="http://localhost:${PORT}"

is_up() {
  curl -sf -o /dev/null --max-time 2 "${URL}/moto-api/"
}

start() {
  if is_up; then
    echo "tf_mock: moto already listening on ${URL} — nothing to do."
    return 0
  fi

  echo "tf_mock: starting moto_server on ${URL} (log: ${LOGFILE})"
  # moto[s3,server] is a root dev dependency; `uv run` resolves it from the repo
  # venv (moto.server ships with the `server` extra — there is no `moto server`
  # subcommand).
  (
    cd "${REPO_ROOT}"
    nohup uv run python -m moto.server -H 127.0.0.1 -p "${PORT}" >>"${LOGFILE}" 2>&1 &
    echo $! >"${PIDFILE}"
  )

  for _ in $(seq 1 40); do
    if is_up; then
      echo "tf_mock: moto is UP on ${URL} (pid $(cat "${PIDFILE}"))."
      return 0
    fi
    sleep 0.5
  done

  echo "tf_mock: ERROR — moto did not become ready on ${URL}; last log lines:" >&2
  tail -n 20 "${LOGFILE}" >&2 || true
  exit 1
}

stop() {
  if [[ -f "${PIDFILE}" ]]; then
    local pid
    pid="$(cat "${PIDFILE}")"
    if kill "${pid}" 2>/dev/null; then
      echo "tf_mock: sent TERM to moto launcher (pid ${pid})."
    fi
    rm -f "${PIDFILE}"
  fi

  # The launcher (uv) may have spawned python as a child — mop up anything still
  # serving moto on this port. ([.] keeps the regex from matching this script's
  # own command line.)
  pkill -f "moto[.]server -H 127.0.0.1 -p ${PORT}" 2>/dev/null || true

  # Give the port a moment to release, then verify it is actually free.
  for _ in $(seq 1 10); do
    if ! is_up; then
      echo "tf_mock: moto is DOWN on ${URL}."
      return 0
    fi
    sleep 0.5
  done

  echo "tf_mock: WARNING — something still answers on ${URL}." >&2
  exit 1
}

status() {
  if is_up; then
    echo "tf_mock: moto is UP on ${URL}."
  else
    echo "tf_mock: moto is DOWN on ${URL}."
    exit 1
  fi
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  restart)
    stop || true
    start
    ;;
  *)
    echo "usage: $(basename "$0") {start|stop|status|restart}" >&2
    exit 2
    ;;
esac
