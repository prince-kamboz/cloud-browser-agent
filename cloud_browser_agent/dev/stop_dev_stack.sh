#!/bin/bash
docker rm -f cba-dev-stack cba-dev-chromium >/dev/null 2>&1 && echo "dev stack stopped" || echo "nothing running"
