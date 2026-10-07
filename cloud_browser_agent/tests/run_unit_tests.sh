#!/bin/bash
# Fast unit tests, all in Docker (nothing is installed on the host, nothing touches AWS or Browserbase).
set -e
cd "$(dirname "$0")/../.."                    # repo root
docker run --rm -v "$PWD":/w -w /w python:3.12-slim sh -c \
  "pip -q install boto3 fastapi httpx websockets langchain-core langchain-mcp-adapters docker >/dev/null 2>&1 \
   && python -m unittest discover -s cloud_browser_agent/tests -p 'test_*.py' -v 2>&1 | tail -${TAIL:-8}"
