#!/bin/bash
# Real Deep Agents run (OpenAI) -> agent service -> control-plane stub -> signed fake gateway -> local Chromium.
# Uses OPENAI_API_KEY from the repo's .env. Costs a few cents. Nothing touches AWS.
set -u
cd "$(dirname "$0")/.."
ENVFILE=${OPENAI_ENV_FILE:-../.env}
[ -f "$ENVFILE" ] || { echo "need $ENVFILE with OPENAI_API_KEY"; exit 2; }
CHROMIUM_IMAGE=${CHROMIUM_IMAGE:-chatgpt-browser-browser-agent}
docker rm -f v2-chromium >/dev/null 2>&1
docker run -d --name v2-chromium --shm-size 256m --entrypoint chromium "$CHROMIUM_IMAGE" \
  --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage --remote-debugging-port=9222 \
  --remote-debugging-address=127.0.0.1 --remote-allow-origins='*' about:blank >/dev/null
sleep 4
docker run --rm --network container:v2-chromium --env-file "$ENVFILE" \
  -e SOURCE=control -e CONTROL_URL=http://127.0.0.1:8100 -e INTERNAL_TOKEN=e2e-token \
  -v "$PWD/agent":/app/cloud_browser_agent/agent -v "$PWD/provider":/app/cloud_browser_agent/provider -v "$PWD/tests":/app/cloud_browser_agent/tests \
  v2-agent bash -c '
    python -m cloud_browser_agent.tests.serve_local_stack > /tmp/stack.log 2>&1 &
    uvicorn cloud_browser_agent.agent.app:app --host 127.0.0.1 --port 8200 --log-level warning > /tmp/agent.log 2>&1 &
    for i in $(seq 1 30); do curl -s localhost:8200/health >/dev/null 2>&1 && curl -s localhost:8100/ >/dev/null 2>&1; [ $? -le 22 ] && break; sleep 1; done
    sleep 2
    python -m cloud_browser_agent.tests.e2e_client "$0"
    echo "--- agent service log (errors only):"; grep -i "error\|traceback" /tmp/agent.log | head -5 || true' "${1:-Open http://127.0.0.1:8099/ and tell me the heading, the plan name and the label of the button on the page.}"
RC=$?
docker rm -f v2-chromium >/dev/null 2>&1
exit $RC
