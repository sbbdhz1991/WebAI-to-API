#!/usr/bin/env bash
# Boot the Xvfb + fluxbox + x11vnc + websockify stack, then exec Chromium
# in the foreground. Chromium's exit code becomes the container's exit
# code, so any crash will be visible to docker / a restart policy.
#
# All env vars below can be overridden via docker-compose if needed.

set -euo pipefail

DISPLAY_NUM="${DISPLAY_NUM:-99}"
SCREEN_GEOMETRY="${SCREEN_GEOMETRY:-1366x768x24}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
VNC_PORT="${VNC_PORT:-5900}"
# Debian-packaged chromium has a downstream patch that ignores
# --remote-debugging-address and force-binds CDP to 127.0.0.1, so anything
# outside the container (the webai sidecar) cannot reach it directly. We
# work around this by letting chromium bind to its forced loopback port
# (CDP_INTERNAL_PORT) and running a socat forwarder that listens on
# 0.0.0.0:CDP_PORT and tunnels to 127.0.0.1:CDP_INTERNAL_PORT.
CDP_PORT="${CDP_PORT:-9222}"
CDP_INTERNAL_PORT="${CDP_INTERNAL_PORT:-9333}"
USER_DATA_DIR="${USER_DATA_DIR:-/data/chrome-profile}"
START_URL="${START_URL:-https://gemini.google.com/app}"
# Optional upstream proxy for ALL of Chromium's traffic (login + DBSC
# rotation). Routing the Google session through a clean/residential exit IP
# is the single biggest lever against Google revoking a session it sees
# originating from a datacenter IP. Use a STABLE exit IP in the account's
# usual region; a rotating-IP proxy makes things worse, not better.
#
# Two ways to set it (all empty = direct, current behaviour, unchanged):
#
#   PROXY_SERVER   — a proxy that needs NO auth (IP-whitelisted). Chromium
#                    points straight at it. Format:
#                    "http://host:port" or "socks5://host:port".
#
#   PROXY_UPSTREAM — a proxy that needs user:pass. Headless Chromium can't
#                    answer a proxy auth dialog, so we run a local tinyproxy
#                    that listens with NO auth on 127.0.0.1 and chains to
#                    this authenticated upstream; Chromium then points at the
#                    local bridge. Format includes credentials, e.g.
#                    "http://user:pass@host:port" or
#                    "socks5://user:pass@host:port".
#                    Takes precedence over PROXY_SERVER when both are set.
PROXY_SERVER="${PROXY_SERVER:-}"
PROXY_UPSTREAM="${PROXY_UPSTREAM:-}"
PROXY_BRIDGE_PORT="${PROXY_BRIDGE_PORT:-18080}"

# First-launch privilege gate: when we boot as root (always, in this image),
# we own the bind-mounted profile dir to the chrome user — the host dir
# may have been created by docker as root and chmod 755, which would
# block chromium from writing SingletonLock etc. After fix-up, drop privs
# to chrome and re-exec the same script.
if [ "$(id -u)" = "0" ]; then
    mkdir -p "${USER_DATA_DIR}"
    chown -R chrome:chrome "${USER_DATA_DIR}" /home/chrome /tmp
    echo "[entrypoint] dropping privileges to user 'chrome'"
    exec gosu chrome:chrome "$0" "$@"
fi

# ===== From here on, we are running as the chrome user (uid 1000) =====

export DISPLAY=":${DISPLAY_NUM}"
export HOME="/home/chrome"

mkdir -p "${USER_DATA_DIR}"

# Clear stale singleton locks. Chromium writes these on launch to detect a
# second instance using the same profile and removes them on clean shutdown.
# Container kills, OOMs, or any unclean exit leaves them behind — pointing
# at the dead container's hostname/PID — and the next start refuses to
# run because it thinks "another Chromium on host 3a8c1def17de is using
# this profile". Safe to delete unconditionally at boot: we're the only
# container that ever touches this profile dir.
rm -f "${USER_DATA_DIR}/SingletonLock" \
      "${USER_DATA_DIR}/SingletonSocket" \
      "${USER_DATA_DIR}/SingletonCookie"

PIDS=()
cleanup() {
  echo "[entrypoint] cleaning up background processes..."
  for pid in "${PIDS[@]:-}"; do
    if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT INT TERM

# 1. Virtual framebuffer — no real GPU/display available in a container.
echo "[entrypoint] starting Xvfb on ${DISPLAY} (${SCREEN_GEOMETRY})"
Xvfb "${DISPLAY}" -screen 0 "${SCREEN_GEOMETRY}" -nolisten tcp \
    > /tmp/xvfb.log 2>&1 &
PIDS+=($!)

# Give Xvfb a moment to bind the display socket before children attach.
for _ in $(seq 1 20); do
  if [ -e "/tmp/.X${DISPLAY_NUM}-lock" ]; then
    break
  fi
  sleep 0.1
done

# 2. Minimal window manager so dialogs / popups behave normally.
echo "[entrypoint] starting fluxbox"
fluxbox > /tmp/fluxbox.log 2>&1 &
PIDS+=($!)

# 3. VNC server attached to the Xvfb display.
# Auth: if VNC_PASSWORD is set we drop a hashed password file and pass
# -rfbauth; otherwise run -nopw and shout into the log. The VNC protocol
# has an 8-char password limit (anything longer is silently truncated),
# so this is a "stop drive-by scans" measure — not a replacement for
# putting noVNC behind a reverse proxy with real auth.
VNC_AUTH_ARGS="-nopw"
if [ -n "${VNC_PASSWORD:-}" ]; then
  VNC_PW_FILE="/home/chrome/.vncpasswd"
  x11vnc -storepasswd "${VNC_PASSWORD}" "${VNC_PW_FILE}" >/dev/null
  chmod 600 "${VNC_PW_FILE}"
  VNC_AUTH_ARGS="-rfbauth ${VNC_PW_FILE}"
  if [ "${#VNC_PASSWORD}" -gt 8 ]; then
    echo "[entrypoint] WARNING: VNC_PASSWORD longer than 8 chars; protocol truncates."
  fi
  echo "[entrypoint] x11vnc password auth ENABLED"
else
  echo "[entrypoint] WARNING: VNC_PASSWORD not set — noVNC is OPEN to anyone"
  echo "[entrypoint]          who can reach port ${NOVNC_PORT}. Set VNC_PASSWORD"
  echo "[entrypoint]          in .env or put noVNC behind a reverse proxy."
fi

echo "[entrypoint] starting x11vnc on :${VNC_PORT}"
x11vnc -display "${DISPLAY}" ${VNC_AUTH_ARGS} -forever -shared -quiet \
       -rfbport "${VNC_PORT}" -bg -o /tmp/x11vnc.log

# 4. WebSocket-to-VNC bridge + serves the noVNC HTML/JS bundle.
echo "[entrypoint] starting websockify + noVNC on :${NOVNC_PORT}"
websockify --web=/usr/share/novnc/ "${NOVNC_PORT}" "localhost:${VNC_PORT}" \
       > /tmp/websockify.log 2>&1 &
PIDS+=($!)

# 4b. CDP forwarder. Debian chromium pins remote-debugging to 127.0.0.1
# regardless of --remote-debugging-address; this socat exposes it to the
# docker network so the webai sidecar can connect.
echo "[entrypoint] starting socat ${CDP_PORT} -> 127.0.0.1:${CDP_INTERNAL_PORT}"
socat TCP-LISTEN:"${CDP_PORT}",bind=0.0.0.0,fork,reuseaddr \
      TCP:127.0.0.1:"${CDP_INTERNAL_PORT}" \
      > /tmp/socat.log 2>&1 &
PIDS+=($!)

# 4c. Authenticated-proxy bridge. When PROXY_UPSTREAM carries credentials,
# run a local tinyproxy that listens with NO auth on 127.0.0.1 and chains
# to the authenticated upstream, so headless Chromium (which can't answer a
# proxy auth dialog) can still use a user:pass proxy. EFFECTIVE_PROXY ends
# up pointing Chromium at either this bridge, the no-auth PROXY_SERVER, or
# nothing (direct).
EFFECTIVE_PROXY="${PROXY_SERVER}"
if [ -n "${PROXY_UPSTREAM}" ]; then
    up_scheme="${PROXY_UPSTREAM%%://*}"
    up_rest="${PROXY_UPSTREAM#*://}"
    up_creds=""
    up_hostport="${up_rest}"
    case "${up_rest}" in
      *@*) up_creds="${up_rest%@*}"; up_hostport="${up_rest##*@}" ;;
    esac
    case "${up_scheme}" in
      http|https) ty_scheme="http" ;;
      socks5|socks5h) ty_scheme="socks5" ;;
      socks4) ty_scheme="socks4" ;;
      *) echo "[entrypoint] ERROR: unsupported PROXY_UPSTREAM scheme '${up_scheme}'" >&2; exit 1 ;;
    esac
    cat > /tmp/tinyproxy.conf <<EOF
Port ${PROXY_BRIDGE_PORT}
Listen 127.0.0.1
Timeout 600
Allow 127.0.0.1
LogLevel Info
Logfile "/tmp/tinyproxy.log"
PidFile "/tmp/tinyproxy.pid"
upstream ${ty_scheme} ${up_creds:+${up_creds}@}${up_hostport}
EOF
    echo "[entrypoint] starting proxy bridge: 127.0.0.1:${PROXY_BRIDGE_PORT} -> ${up_scheme}://${up_creds:+***@}${up_hostport}"
    tinyproxy -d -c /tmp/tinyproxy.conf > /tmp/tinyproxy.boot.log 2>&1 &
    PIDS+=($!)
    EFFECTIVE_PROXY="http://127.0.0.1:${PROXY_BRIDGE_PORT}"
fi

# 5. Chromium — foreground; container lives or dies with it.
# Notes:
#   --remote-debugging-address=0.0.0.0   exposes CDP to docker network only
#                                        (port 9222 is NOT published in compose)
#   --disable-dev-shm-usage              avoids OOM on small /dev/shm
#   --no-sandbox                         most managed Docker hosts (1panel,
#                                        Tencent Cloud, etc.) block the
#                                        clone(CLONE_NEWUSER) syscall via
#                                        seccomp, so Chrome's namespace
#                                        sandbox can't initialize and the
#                                        renderer dies on startup. Disabling
#                                        Chrome's internal sandbox is the
#                                        least-invasive workaround: we
#                                        still have the docker container's
#                                        own seccomp/uid isolation around us.
echo "[entrypoint] launching Chromium (CDP loopback :${CDP_INTERNAL_PORT}, profile=${USER_DATA_DIR})"
# --remote-allow-origins=* required since Chrome 111: even when CDP is
# reachable, the WebSocket Origin check rejects clients with unfamiliar
# (or absent) Origin headers unless we explicitly allow any origin.
CHROME_ARGS=(
    --no-sandbox
    --no-first-run
    --no-default-browser-check
    --disable-gpu
    --disable-dev-shm-usage
    --disable-features=Translate,InfiniteSessionRestore
    --password-store=basic
    --use-mock-keychain
    --remote-debugging-port="${CDP_INTERNAL_PORT}"
    --remote-allow-origins=*
    --user-data-dir="${USER_DATA_DIR}"
    --window-size=1280,720
    --start-maximized
)
if [ -n "${EFFECTIVE_PROXY}" ]; then
    echo "[entrypoint] routing Chromium through proxy: ${EFFECTIVE_PROXY}"
    CHROME_ARGS+=( --proxy-server="${EFFECTIVE_PROXY}" )
fi
exec chromium "${CHROME_ARGS[@]}" "${START_URL}"
