#!/usr/bin/env bash
# Headless Chromium (CDP on 127.0.0.1:9222) behind nginx (:8080/cdp/). The profile lives in /data/profile (a volume).
#
# Chromium only flushes cookies and site data to disk on an orderly quit. SIGTERM takes a fast exit path that skips the
# flush (tested: even a persistent cookie set seconds earlier was lost), so on shutdown we ask it to quit through CDP
# (Browser.close) with a bare-bones WebSocket client written in bash, then wait for it.
set -u

W="${SCREEN_WIDTH:-1280}"; H="${SCREEN_HEIGHT:-800}"; CHROME="${CHROME_BIN:-chromium}"
PROFILE="${PROFILE_DIR:-/data/profile}"
mkdir -p "$PROFILE"
rm -f "$PROFILE"/Singleton* 2>/dev/null          # a previous container may have left a lock behind

PIDFILE=/tmp/chromium.pid
# Open a start page only on the very first run; afterwards Chromium restores the previous tabs itself.
START_ARGS=()
[ -z "$(ls -A "$PROFILE/Default/Sessions" 2>/dev/null)" ] && START_ARGS=("${START_URL:-about:blank}")
rm -f "$PIDFILE" /tmp/stopping
(
  while [ ! -e /tmp/stopping ]; do
    "$CHROME" --headless=new --no-sandbox --no-first-run --no-default-browser-check --disable-dev-shm-usage \
      --disable-gpu --disable-extensions --disable-background-networking --disable-component-update \
      --disable-features=Translate,MediaRouter --mute-audio --force-device-scale-factor=1 \
      --window-size="${W},${H}" --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 \
      --remote-allow-origins='*' --password-store=basic --user-data-dir="$PROFILE" \
      "${START_ARGS[@]}" >/tmp/chromium.log 2>&1 &
    echo $! > "$PIDFILE"
    wait $!
    [ -e /tmp/stopping ] && break
    echo "[start.sh] chromium exited, restarting in 1s" >&2
    rm -f "$PROFILE"/Singleton* 2>/dev/null
    sleep 1
  done
) &

graceful_close() {
  local path payload len
  path=$(curl -s -m 3 http://127.0.0.1:9222/json/version | sed -n 's/.*"webSocketDebuggerUrl": *"ws:\/\/[^\/]*\(\/[^"]*\)".*/\1/p')
  [ -n "$path" ] || return 1
  exec 3<>/dev/tcp/127.0.0.1/9222 || return 1
  printf 'GET %s HTTP/1.1\r\nHost: localhost:9222\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n' "$path" >&3
  sleep 0.5
  payload='{"id":1,"method":"Browser.close"}'
  len=${#payload}
  { printf '\x81'; printf "\\x$(printf '%02x' $((0x80 | len)))"; printf '\x00\x00\x00\x00'; printf '%s' "$payload"; } >&3   # one masked text frame
  sleep 0.5
  exec 3>&- 3<&-
}

shutdown() {
  echo "[start.sh] stopping: asking Chromium to quit so it flushes cookies and storage" >&2
  touch /tmp/stopping
  graceful_close || echo "[start.sh] could not reach CDP, falling back to SIGTERM" >&2
  if [ -s "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    for i in $(seq 1 100); do kill -0 "$PID" 2>/dev/null || break; sleep 0.2; done      # up to 20 s
    kill -0 "$PID" 2>/dev/null && { echo "[start.sh] Chromium did not quit in time, sending SIGTERM" >&2; kill -TERM "$PID" 2>/dev/null; sleep 2; }
  fi
  nginx -s quit 2>/dev/null
  exit 0
}
trap shutdown TERM INT

nginx -g 'daemon off;' &
wait $!
