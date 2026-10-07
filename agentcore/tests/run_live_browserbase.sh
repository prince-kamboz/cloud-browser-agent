#!/bin/bash
# LIVE check against your Browserbase account (uses a little real browser time).
# Needs BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID in the repo-root .env. Runs in Docker; keys are never printed.
set -e
cd "$(dirname "$0")/../.."
docker run --rm -v "$PWD":/w -w /w --env-file .env -e POST_RELEASE_WAIT=5 python:3.12-slim sh -c \
  "pip -q install websockets boto3 fastapi >/dev/null 2>&1 && python agentcore/tests/live_browserbase.py"
