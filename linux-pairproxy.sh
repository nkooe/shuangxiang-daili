#!/usr/bin/env bash
set -euo pipefail

APP_NAME="pairproxy"
APP_DIR="/opt/${APP_NAME}"
ENV_FILE="/etc/${APP_NAME}.env"
UNIT_FILE="/etc/systemd/system/${APP_NAME}.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_PY="${SCRIPT_DIR}/reverse_pair_proxy.py"

need_root() {
  if [ "${EUID}" -ne 0 ]; then
    echo "Run as root: sudo $0 $*" >&2
    exit 1
  fi
}

ensure_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "$1 not found. Install it first." >&2
    exit 1
  }
}

load_env() {
  if [ -f "${ENV_FILE}" ]; then
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
  fi

  PAIRPROXY_PROXY_BIND="${PAIRPROXY_PROXY_BIND:-0.0.0.0}"
  PAIRPROXY_PROXY_PORT="${PAIRPROXY_PROXY_PORT:-${PAIRPROXY_PORT:-27499}}"
  PAIRPROXY_AGENT_BIND="${PAIRPROXY_AGENT_BIND:-0.0.0.0}"
  PAIRPROXY_AGENT_PORT="${PAIRPROXY_AGENT_PORT:-27500}"
  PAIRPROXY_AGENT_TOKEN="${PAIRPROXY_AGENT_TOKEN:-${PAIRPROXY_TOKEN:-}}"
  PAIRPROXY_PROXY_USER="${PAIRPROXY_PROXY_USER:-}"
  PAIRPROXY_PROXY_PASS="${PAIRPROXY_PROXY_PASS:-}"
  PAIRPROXY_AGENT_WAIT_TIMEOUT="${PAIRPROXY_AGENT_WAIT_TIMEOUT:-15}"
  PAIRPROXY_AGENT_HANDSHAKE_TIMEOUT="${PAIRPROXY_AGENT_HANDSHAKE_TIMEOUT:-10}"
  PAIRPROXY_AGENT_PROBE_INTERVAL="${PAIRPROXY_AGENT_PROBE_INTERVAL:-30}"
  PAIRPROXY_AGENT_PROBE_TIMEOUT="${PAIRPROXY_AGENT_PROBE_TIMEOUT:-3}"
  PAIRPROXY_AGENT_PROBE_PER_CYCLE="${PAIRPROXY_AGENT_PROBE_PER_CYCLE:-8}"
  PAIRPROXY_AGENT_MAX_IDLE_LIFETIME="${PAIRPROXY_AGENT_MAX_IDLE_LIFETIME:-1800}"
  PAIRPROXY_AGENT_RECYCLE_PER_CYCLE="${PAIRPROXY_AGENT_RECYCLE_PER_CYCLE:-8}"
  PAIRPROXY_PUBLIC_HANDSHAKE_TIMEOUT="${PAIRPROXY_PUBLIC_HANDSHAKE_TIMEOUT:-15}"
  PAIRPROXY_MAX_IDLE_AGENTS="${PAIRPROXY_MAX_IDLE_AGENTS:-2048}"
  PAIRPROXY_CONNECT_TIMEOUT="${PAIRPROXY_CONNECT_TIMEOUT:-10}"
  PAIRPROXY_RELAY_IDLE_TIMEOUT="${PAIRPROXY_RELAY_IDLE_TIMEOUT:-300}"
  PAIRPROXY_KEEPALIVE_IDLE="${PAIRPROXY_KEEPALIVE_IDLE:-60}"
  PAIRPROXY_KEEPALIVE_INTERVAL="${PAIRPROXY_KEEPALIVE_INTERVAL:-15}"
  PAIRPROXY_KEEPALIVE_COUNT="${PAIRPROXY_KEEPALIVE_COUNT:-4}"
  PAIRPROXY_AGENT_TLS="${PAIRPROXY_AGENT_TLS:-1}"
  PAIRPROXY_CERT="${PAIRPROXY_CERT:-${APP_DIR}/server.crt}"
  PAIRPROXY_KEY="${PAIRPROXY_KEY:-${APP_DIR}/server.key}"
  PAIRPROXY_LOG_LEVEL="${PAIRPROXY_LOG_LEVEL:-INFO}"
}

validate_secret() {
  local name="$1"
  local value="$2"
  case "${value}" in
    ""|*[!A-Za-z0-9._-]*)
      echo "${name} must contain only letters, digits, dot, underscore or hyphen." >&2
      exit 1
      ;;
  esac
}

write_env() {
  if [ -z "${PAIRPROXY_AGENT_TOKEN}" ]; then
    PAIRPROXY_AGENT_TOKEN="$(openssl rand -hex 24)"
  fi
  if [ -z "${PAIRPROXY_PROXY_USER}" ]; then
    PAIRPROXY_PROXY_USER="pp_$(openssl rand -hex 5)"
  fi
  if [ -z "${PAIRPROXY_PROXY_PASS}" ]; then
    PAIRPROXY_PROXY_PASS="$(openssl rand -hex 20)"
  fi

  validate_secret "PAIRPROXY_AGENT_TOKEN" "${PAIRPROXY_AGENT_TOKEN}"
  validate_secret "PAIRPROXY_PROXY_USER" "${PAIRPROXY_PROXY_USER}"
  validate_secret "PAIRPROXY_PROXY_PASS" "${PAIRPROXY_PROXY_PASS}"

  cat > "${ENV_FILE}" <<EOF_ENV
PAIRPROXY_PROXY_BIND=${PAIRPROXY_PROXY_BIND}
PAIRPROXY_PROXY_PORT=${PAIRPROXY_PROXY_PORT}
PAIRPROXY_AGENT_BIND=${PAIRPROXY_AGENT_BIND}
PAIRPROXY_AGENT_PORT=${PAIRPROXY_AGENT_PORT}
PAIRPROXY_AGENT_TOKEN=${PAIRPROXY_AGENT_TOKEN}
PAIRPROXY_PROXY_USER=${PAIRPROXY_PROXY_USER}
PAIRPROXY_PROXY_PASS=${PAIRPROXY_PROXY_PASS}
PAIRPROXY_AGENT_WAIT_TIMEOUT=${PAIRPROXY_AGENT_WAIT_TIMEOUT}
PAIRPROXY_AGENT_HANDSHAKE_TIMEOUT=${PAIRPROXY_AGENT_HANDSHAKE_TIMEOUT}
PAIRPROXY_AGENT_PROBE_INTERVAL=${PAIRPROXY_AGENT_PROBE_INTERVAL}
PAIRPROXY_AGENT_PROBE_TIMEOUT=${PAIRPROXY_AGENT_PROBE_TIMEOUT}
PAIRPROXY_AGENT_PROBE_PER_CYCLE=${PAIRPROXY_AGENT_PROBE_PER_CYCLE}
PAIRPROXY_AGENT_MAX_IDLE_LIFETIME=${PAIRPROXY_AGENT_MAX_IDLE_LIFETIME}
PAIRPROXY_AGENT_RECYCLE_PER_CYCLE=${PAIRPROXY_AGENT_RECYCLE_PER_CYCLE}
PAIRPROXY_PUBLIC_HANDSHAKE_TIMEOUT=${PAIRPROXY_PUBLIC_HANDSHAKE_TIMEOUT}
PAIRPROXY_MAX_IDLE_AGENTS=${PAIRPROXY_MAX_IDLE_AGENTS}
PAIRPROXY_CONNECT_TIMEOUT=${PAIRPROXY_CONNECT_TIMEOUT}
PAIRPROXY_RELAY_IDLE_TIMEOUT=${PAIRPROXY_RELAY_IDLE_TIMEOUT}
PAIRPROXY_KEEPALIVE_IDLE=${PAIRPROXY_KEEPALIVE_IDLE}
PAIRPROXY_KEEPALIVE_INTERVAL=${PAIRPROXY_KEEPALIVE_INTERVAL}
PAIRPROXY_KEEPALIVE_COUNT=${PAIRPROXY_KEEPALIVE_COUNT}
PAIRPROXY_AGENT_TLS=${PAIRPROXY_AGENT_TLS}
PAIRPROXY_CERT=${PAIRPROXY_CERT}
PAIRPROXY_KEY=${PAIRPROXY_KEY}
PAIRPROXY_LOG_LEVEL=${PAIRPROXY_LOG_LEVEL}
EOF_ENV
  chmod 600 "${ENV_FILE}"
}

make_cert() {
  if [ "${PAIRPROXY_AGENT_TLS}" != "1" ]; then
    return
  fi
  if [ -f "${PAIRPROXY_CERT}" ] && [ -f "${PAIRPROXY_KEY}" ]; then
    return
  fi
  local cn
  cn="$(hostname -f 2>/dev/null || hostname || echo pairproxy)"
  openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
    -keyout "${PAIRPROXY_KEY}" \
    -out "${PAIRPROXY_CERT}" \
    -subj "/CN=${cn}" >/dev/null 2>&1
  chmod 600 "${PAIRPROXY_KEY}"
}

write_unit() {
  local tls_args=""
  if [ "${PAIRPROXY_AGENT_TLS}" = "1" ]; then
    tls_args='--agent-tls --cert ${PAIRPROXY_CERT} --key ${PAIRPROXY_KEY}'
  fi

  cat > "${UNIT_FILE}" <<EOF_UNIT
[Unit]
Description=PairProxy public SOCKS5 and HTTP broker backed by Windows agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=${ENV_FILE}
WorkingDirectory=${APP_DIR}
ExecStart=/usr/bin/env python3 ${APP_DIR}/reverse_pair_proxy.py broker --proxy-bind \${PAIRPROXY_PROXY_BIND} --proxy-port \${PAIRPROXY_PROXY_PORT} --proxy-user \${PAIRPROXY_PROXY_USER} --proxy-pass \${PAIRPROXY_PROXY_PASS} --agent-bind \${PAIRPROXY_AGENT_BIND} --agent-port \${PAIRPROXY_AGENT_PORT} --agent-token \${PAIRPROXY_AGENT_TOKEN} --agent-wait-timeout \${PAIRPROXY_AGENT_WAIT_TIMEOUT} --agent-handshake-timeout \${PAIRPROXY_AGENT_HANDSHAKE_TIMEOUT} --agent-probe-interval \${PAIRPROXY_AGENT_PROBE_INTERVAL} --agent-probe-timeout \${PAIRPROXY_AGENT_PROBE_TIMEOUT} --agent-probe-per-cycle \${PAIRPROXY_AGENT_PROBE_PER_CYCLE} --agent-max-idle-lifetime \${PAIRPROXY_AGENT_MAX_IDLE_LIFETIME} --agent-recycle-per-cycle \${PAIRPROXY_AGENT_RECYCLE_PER_CYCLE} --public-handshake-timeout \${PAIRPROXY_PUBLIC_HANDSHAKE_TIMEOUT} --max-idle-agents \${PAIRPROXY_MAX_IDLE_AGENTS} --connect-timeout \${PAIRPROXY_CONNECT_TIMEOUT} --relay-idle-timeout \${PAIRPROXY_RELAY_IDLE_TIMEOUT} --keepalive-idle \${PAIRPROXY_KEEPALIVE_IDLE} --keepalive-interval \${PAIRPROXY_KEEPALIVE_INTERVAL} --keepalive-count \${PAIRPROXY_KEEPALIVE_COUNT} ${tls_args} --log-level \${PAIRPROXY_LOG_LEVEL}
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF_UNIT
}

fingerprint() {
  load_env
  if [ ! -f "${PAIRPROXY_CERT}" ]; then
    echo "Certificate not found: ${PAIRPROXY_CERT}" >&2
    exit 1
  fi
  openssl x509 -in "${PAIRPROXY_CERT}" -outform DER | sha256sum | awk '{print $1}'
}

install_service() {
  need_root "$@"
  ensure_command python3
  ensure_command openssl
  ensure_command systemctl
  if [ ! -f "${SOURCE_PY}" ]; then
    echo "reverse_pair_proxy.py not found next to this script: ${SOURCE_PY}" >&2
    exit 1
  fi

  load_env
  systemctl disable --now "${APP_NAME}.service" >/dev/null 2>&1 || true
  mkdir -p "${APP_DIR}"
  cp "${SOURCE_PY}" "${APP_DIR}/reverse_pair_proxy.py"
  chmod 755 "${APP_DIR}/reverse_pair_proxy.py"
  rm -f "${APP_DIR}/pair_proxy.py"
  write_env
  make_cert
  write_unit
  systemctl daemon-reload
  systemctl enable --now "${APP_NAME}.service"

  echo "Installed ${APP_NAME} reverse broker."
  echo "Public proxy port: ${PAIRPROXY_PROXY_PORT}"
  echo "Agent control port: ${PAIRPROXY_AGENT_PORT}"
  echo "Proxy username: ${PAIRPROXY_PROXY_USER}"
  echo "Proxy password: ${PAIRPROXY_PROXY_PASS}"
  echo "Agent token: ${PAIRPROXY_AGENT_TOKEN}"
  if [ "${PAIRPROXY_AGENT_TLS}" = "1" ]; then
    echo "Agent TLS fingerprint: $(fingerprint)"
  fi
  echo "Check: systemctl status ${APP_NAME} --no-pager"
}

start_service() {
  need_root "$@"
  systemctl enable --now "${APP_NAME}.service"
}

stop_service() {
  need_root "$@"
  systemctl stop "${APP_NAME}.service" || true
}

status_service() {
  systemctl status "${APP_NAME}.service" --no-pager || true
}

uninstall_service() {
  need_root "$@"
  systemctl disable --now "${APP_NAME}.service" || true
  rm -f "${UNIT_FILE}"
  systemctl daemon-reload
  echo "Service removed. Data kept at ${APP_DIR}, env kept at ${ENV_FILE}."
}

case "${1:-}" in
  install) install_service "$@" ;;
  start) start_service "$@" ;;
  stop) stop_service "$@" ;;
  status) status_service ;;
  fingerprint) fingerprint ;;
  uninstall) uninstall_service "$@" ;;
  *)
    cat <<EOF_USAGE
Usage:
  sudo ./linux-pairproxy.sh install      install, enable and start reverse broker
  sudo ./linux-pairproxy.sh start        enable and start broker
  sudo ./linux-pairproxy.sh stop         stop broker
  ./linux-pairproxy.sh status             show broker status
  sudo ./linux-pairproxy.sh fingerprint  print agent TLS cert SHA256 fingerprint
  sudo ./linux-pairproxy.sh uninstall    remove service, keep files

Ports:
  27499 = public SOCKS5/HTTP proxy for third parties
  27500 = private Windows agent TLS control port
EOF_USAGE
    exit 1
    ;;
esac
