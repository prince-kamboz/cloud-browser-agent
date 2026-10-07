#!/bin/bash
# Regenerate the README screenshots (docs/images/*.png) from the running stack.
# Needs:  ./run.sh docker   (stack up)   and OPENAI_API_KEY in .env (a task costs a few cents).
set -e
cd "$(dirname "$0")/../.."
docker rm -f cba-shot >/dev/null 2>&1 || true
docker run -d --name cba-shot --network cba-net --shm-size 512m -e SCREEN_WIDTH=1440 -e SCREEN_HEIGHT=900 cba-chromium >/dev/null
# The capture browser lives inside Docker, so point the UI at the agent container and let the agent accept that origin.
AGENT_URL=http://cloud-browser-agent-agent-1:8200 CORS_ORIGINS=http://cloud-browser-agent-control-1:8100 docker compose up -d --no-build >/dev/null 2>&1
trap 'docker rm -f cba-shot >/dev/null 2>&1; docker compose up -d --no-build >/dev/null 2>&1' EXIT   # restore the normal addresses
sleep 4
mkdir -p docs/images
docker run --rm --network cba-net -v "$PWD":/w -w /w python:3.12-slim sh -c \
  "pip -q install websockets >/dev/null 2>&1 && python cloud_browser_agent/dev/take_screenshots.py"
