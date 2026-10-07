#!/bin/bash
# Start the whole system with the browser backend of your choice, then open http://localhost:8100
#   ./run.sh docker        a real Chromium in a local container (no account; needs only OPENAI_API_KEY in .env)
#   ./run.sh browserbase   Browserbase (BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID in .env)
#   ./run.sh browseruse    Browser Use Cloud browsers (BROWSER_USE_API_KEY in .env)
#   ./run.sh agentcore     Amazon Bedrock AgentCore Browser (keys in deploy/aws/.env.aws)
#   ./run.sh fake          simulated browsers: UI and lifecycle only, no agent browsing
#   ./run.sh stop          stop everything
set -e
cd "$(dirname "$0")"
case "${1:-}" in
  docker)               PROFILES=(--profile docker) ;;
  browserbase|browseruse|agentcore|fake) PROFILES=() ;;
  stop)                 docker compose --profile docker down; exit 0 ;;
  *)                    sed -n 2,8p "$0"; exit 2 ;;
esac
export PROVIDER="$1"
docker compose "${PROFILES[@]}" up --build -d
echo "up with PROVIDER=$PROVIDER  ->  http://localhost:8100   (logs: docker compose logs -f control)"
