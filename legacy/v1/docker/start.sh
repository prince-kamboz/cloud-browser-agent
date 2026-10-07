#!/usr/bin/env bash
# Starts: headless Chromium (with CDP) -> nginx. No X server, VNC or noVNC:
# the live view is a CDP screencast drawn by the web UI.
set -u

W="${SCREEN_WIDTH:-1280}"
H="${SCREEN_HEIGHT:-800}"
CHROME="${CHROME_BIN:-chromium}"
PROFILE="${PROFILE_DIR:-/data/profile}"
NGINX_CONF="${NGINX_CONF:-}"

mkdir -p "$PROFILE"
# Chromium refuses to start if a previous container left a lock behind
rm -f "$PROFILE"/Singleton* 2>/dev/null

# 1. Headless Chromium, restarted automatically if it exits.
# On shutdown we ask it to quit (SIGTERM) and wait, so it flushes cookies and site data to the profile volume.
PIDFILE=/tmp/chromium.pid
# Open the start page only on the very first run. After that Chromium restores the previous tabs itself,
# and passing a URL as well would add one more tab on every restart.
START_ARGS=()
[ -z "$(ls -A "$PROFILE/Default/Sessions" 2>/dev/null)" ] && START_ARGS=("${START_URL:-https://www.google.com}")
rm -f "$PIDFILE" /tmp/stopping
(
  while [ ! -e /tmp/stopping ]; do
    # Tell the agent service which launch this is and whether the previous exit was clean (it only is when
    # shutdown() below saw Chromium quit by itself). After an unclean exit (kill, crash, power loss) Chromium
    # comes back with data up to 30 s old, so the agent restores its newer cookie copy over it.
    if [ -e "$PROFILE/../clean-exit" ]; then CLEAN=1; rm -f "$PROFILE/../clean-exit"; else CLEAN=0; fi
    echo "$(date +%s%N) $CLEAN" > "$PROFILE/../browser-start"
    "$CHROME" \
      --headless=new \
      --no-sandbox \
      --no-first-run \
      --no-default-browser-check \
      --disable-dev-shm-usage \
      --disable-gpu \
      --disable-extensions \
      --disable-background-networking \
      --disable-component-update \
      --disable-features=Translate,MediaRouter \
      --mute-audio \
      --force-device-scale-factor=1 \
      --window-size="${W},${H}" \
      --remote-debugging-port=9222 \
      --remote-debugging-address=127.0.0.1 \
      --remote-allow-origins='*' \
      --password-store=basic \
      --user-data-dir="$PROFILE" \
      "${START_ARGS[@]}" >/tmp/chromium.log 2>&1 &
    echo $! > "$PIDFILE"
    wait $!
    [ -e /tmp/stopping ] && break
    echo "[start.sh] chromium exited, restarting in 1s" >&2
    rm -f "$PROFILE"/Singleton* 2>/dev/null
    sleep 1
  done
) &

# Chromium only flushes its cookie database and site data on an orderly quit. SIGTERM takes a fast exit path that
# skips the flush (tested: even a persistent cookie set seconds earlier was lost), so we ask it to quit through
# CDP (Browser.close) with a bare-bones WebSocket client, since this image has no other tools for that.
graceful_close() {
  local path payload len
  path=$(curl -s -m 3 http://127.0.0.1:9222/json/version | sed -n 's/.*"webSocketDebuggerUrl": *"ws:\/\/[^\/]*\(\/[^"]*\)".*/\1/p')
  [ -n "$path" ] || return 1
  exec 3<>/dev/tcp/127.0.0.1/9222 || return 1
  printf 'GET %s HTTP/1.1\r\nHost: localhost:9222\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n' "$path" >&3
  sleep 0.5
  payload='{"id":1,"method":"Browser.close"}'
  len=${#payload}
  # one masked text frame (mask key 00000000, so the payload goes out unchanged)
  { printf '\x81'; printf "\\x$(printf '%02x' $((0x80 | len)))"; printf '\x00\x00\x00\x00'; printf '%s' "$payload"; } >&3
  sleep 0.5
  exec 3>&- 3<&-
}

shutdown() {
  echo "[start.sh] stopping: asking Chromium to quit so it flushes cookies and storage" >&2
  touch /tmp/stopping
  graceful_close || echo "[start.sh] could not reach CDP, falling back to SIGTERM" >&2
  if [ -s "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    for i in $(seq 1 100); do kill -0 "$PID" 2>/dev/null || break; sleep 0.2; done   # up to 20s
    kill -0 "$PID" 2>/dev/null || touch "$PROFILE/../clean-exit"   # it quit by itself: data was flushed
    kill -0 "$PID" 2>/dev/null && { echo "[start.sh] Chromium did not quit in time, sending SIGTERM" >&2; kill -TERM "$PID" 2>/dev/null; sleep 2; }
  fi
  nginx -s quit 2>/dev/null
  exit 0
}
trap shutdown TERM INT

# 2. nginx: the only public port
if [ -n "$NGINX_CONF" ]; then
  nginx -c "$NGINX_CONF" -g 'daemon off;' &
else
  nginx -g 'daemon off;' &
fi

# `wait` returns early when a trapped signal arrives, so shutdown() runs promptly.
wait -n
echo "[start.sh] a core process exited; stopping container" >&2
exit 1
