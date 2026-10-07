#!/bin/bash
docker rm -f v2-dev-stack v2-dev-chromium >/dev/null 2>&1 && echo "dev stack stopped" || echo "nothing running"
