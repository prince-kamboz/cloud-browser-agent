#!/bin/bash
# Benchmark providers against each other, in Docker:  ./run_bench.sh [--runs 5] [--providers browserbase browseruse docker]
# Uses real browser time on each service; keys come from the repo-root .env and are never printed. Results: bench/results.json
set -e
cd "$(dirname "$0")/../.."
EXTRA=()
case " $* " in *" docker "*) EXTRA=(--network cba-net -v /var/run/docker.sock:/var/run/docker.sock) ;; esac
docker run --rm -v "$PWD":/w -w /w --env-file .env -e BENCH_OUT=/w/cloud_browser_agent/bench/results.json "${EXTRA[@]}" python:3.12-slim sh -c \
  "pip -q install websockets boto3 fastapi docker >/dev/null 2>&1 && python cloud_browser_agent/bench/bench_providers.py $*"
