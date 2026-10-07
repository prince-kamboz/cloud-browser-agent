# v2 agent service: Deep Agents + Playwright MCP talking to a REMOTE browser (AgentCore) over a signed CDP WebSocket.
# Playwright MCP needs no browser here: it only drives the remote one.
FROM node:22-slim

RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @playwright/mcp@latest

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY agent/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app
COPY __init__.py /app/agentcore/__init__.py
COPY provider /app/agentcore/provider
COPY agent /app/agentcore/agent
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
EXPOSE 8200
CMD ["uvicorn", "agentcore.agent.app:app", "--host", "0.0.0.0", "--port", "8200"]
