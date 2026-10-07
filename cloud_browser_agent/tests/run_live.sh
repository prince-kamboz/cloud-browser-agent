#!/bin/bash
# LIVE lifecycle check against a real backend, in Docker:  ./run_live.sh docker | browserbase | browseruse
#   docker       needs the cba-chromium image (./run.sh docker builds it) and the cba-net network
#   browserbase  needs BROWSERBASE_* in the repo-root .env (uses a little real browser time; keys are never printed)
set -e
cd "$(dirname "$0")/../.."
KIND=${1:?usage: run_live.sh docker|browserbase|browseruse}
EXTRA=()
[ "$KIND" = docker ] && EXTRA=(--network cba-net -v /var/run/docker.sock:/var/run/docker.sock)
docker run --rm -v "$PWD":/w -w /w --env-file .env -e PROVIDER="$KIND" -e POST_RELEASE_WAIT=${POST_RELEASE_WAIT:-3} "${EXTRA[@]}" python:3.12-slim sh -c \
  "pip -q install websockets boto3 fastapi docker >/dev/null 2>&1 && python cloud_browser_agent/tests/live_lifecycle.py"
