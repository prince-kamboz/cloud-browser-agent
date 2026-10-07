#!/bin/bash
# Local Chromium + fake signed gateway + the real agent connection code, all in Docker. Nothing touches AWS.
set -u
cd "$(dirname "$0")/.."                       # cloud_browser_agent/
CHROMIUM_IMAGE=${CHROMIUM_IMAGE:-chatgpt-browser-browser-agent}   # any image with chromium; this one is already built locally
docker rm -f v2-chromium >/dev/null 2>&1
docker run -d --name v2-chromium --shm-size 256m --entrypoint chromium "$CHROMIUM_IMAGE" \
  --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage --remote-debugging-port=9222 \
  --remote-debugging-address=127.0.0.1 --remote-allow-origins='*' about:blank >/dev/null
sleep 4
docker run --rm --network container:v2-chromium \
  -v "$PWD/agent":/app/cloud_browser_agent/agent -v "$PWD/provider":/app/cloud_browser_agent/provider -v "$PWD/tests":/app/cloud_browser_agent/tests \
  v2-agent python -m cloud_browser_agent.tests.${TEST_MODULE:-integration_local}
RC=$?
docker rm -f v2-chromium >/dev/null 2>&1
exit $RC
