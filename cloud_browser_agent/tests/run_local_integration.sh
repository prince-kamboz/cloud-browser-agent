#!/bin/bash
# Local Chromium + fake signed gateway + the real agent connection code, all in Docker. Nothing touches AWS.
set -u
cd "$(dirname "$0")/.."                       # cloud_browser_agent/
CHROMIUM_IMAGE=${CHROMIUM_IMAGE:-cba-chromium}   # any image with chromium; this one is already built locally
docker rm -f cba-test-chromium >/dev/null 2>&1
docker run -d --name cba-test-chromium --shm-size 256m --entrypoint chromium "$CHROMIUM_IMAGE" \
  --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage --remote-debugging-port=9222 \
  --remote-debugging-address=127.0.0.1 --remote-allow-origins='*' about:blank >/dev/null
sleep 4
docker run --rm --network container:cba-test-chromium \
  -v "$PWD/agent":/app/cloud_browser_agent/agent -v "$PWD/provider":/app/cloud_browser_agent/provider -v "$PWD/tests":/app/cloud_browser_agent/tests \
  cba-agent python -m cloud_browser_agent.tests.${TEST_MODULE:-integration_local}
RC=$?
docker rm -f cba-test-chromium >/dev/null 2>&1
exit $RC
