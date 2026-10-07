#!/bin/bash
# The whole v2 stack on your machine with NO AWS: local Chromium (real pixels) -> signed fake gateway -> agent service,
# control plane + UI, live viewer. The agent uses OpenAI (key from ../.env). Open http://localhost:8100
#   DEV_VIEWER=dcv ./dev/run_dev_stack.sh     to exercise the DCV viewer page against the stand-in DCV script instead
set -u
cd "$(dirname "$0")/.."
ENVFILE=${OPENAI_ENV_FILE:-../.env}
[ -f "$ENVFILE" ] || { echo "need $ENVFILE with OPENAI_API_KEY"; exit 2; }
CHROMIUM_IMAGE=${CHROMIUM_IMAGE:-chatgpt-browser-browser-agent}
VIEWER=${DEV_VIEWER:-iframe}
./dev/stop_dev_stack.sh >/dev/null 2>&1
docker run -d --name v2-dev-chromium --shm-size 256m -p 127.0.0.1:8100:8100 -p 127.0.0.1:8200:8200 -p 127.0.0.1:8300:8300 \
  --entrypoint chromium "$CHROMIUM_IMAGE" --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage \
  --window-size=1280,800 --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --remote-allow-origins='*' about:blank >/dev/null
sleep 3
docker run -d --name v2-dev-stack --network container:v2-dev-chromium --env-file "$ENVFILE" \
  -e PROVIDER=local -e DEV_VIEWER="$VIEWER" -e STATE_DB=/tmp/state.db -e INTERNAL_TOKEN=dev-token -e AGENT_URL=http://localhost:8200 \
  -e SOURCE=control -e CONTROL_URL=http://127.0.0.1:8100 -e IDLE_AFTER_S=${IDLE_AFTER_S:-600} -e DCV_SDK_DIR=/app/cloud_browser_agent/dev/dcv_stub \
  -v "$PWD":/app/cloud_browser_agent v2-agent bash -c '
    python -m cloud_browser_agent.dev.stack_services > /tmp/services.log 2>&1 &
    uvicorn cloud_browser_agent.agent.app:app --host 0.0.0.0 --port 8200 --log-level warning > /tmp/agent.log 2>&1 &
    exec uvicorn cloud_browser_agent.control.app:app --host 0.0.0.0 --port 8100 --log-level info' >/dev/null
for i in $(seq 1 30); do curl -s -o /dev/null localhost:8100/api/config && break; sleep 1; done
echo "up: http://localhost:8100   (viewer: $VIEWER)   stop with ./dev/stop_dev_stack.sh"
curl -s localhost:8100/api/config; echo
