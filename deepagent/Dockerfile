# POC: Deep Agents service. Python for the agent, Node only for the Playwright MCP server.
FROM node:22-slim

RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Playwright MCP drives an already-running Chromium over CDP, so no browser download is needed.
RUN npm install -g @playwright/mcp@latest

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app
COPY app.py /app/app.py
CMD ["uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8000"]
