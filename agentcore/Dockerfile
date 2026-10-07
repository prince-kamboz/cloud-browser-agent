# Control plane + the v2 UI. The Amazon DCV web client is fetched from AWS's own npm package at BUILD time and is never
# committed to this repo: its EULA is a limited, non-transferable, non-sublicensable, internal-use licence.
FROM node:22-slim AS dcv
RUN cd /tmp && npm pack bedrock-agentcore --silent >/dev/null && tar xzf bedrock-agentcore-*.tgz \
 && mkdir -p /sdk && cp -r package/dist/src/tools/browser/live-view/nice-dcv-web-client-sdk/dcvjs-umd /sdk/

FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" boto3 pydantic
WORKDIR /app
COPY __init__.py /app/agentcore/__init__.py
COPY provider /app/agentcore/provider
COPY control /app/agentcore/control
COPY ui /app/agentcore/ui
COPY --from=dcv /sdk /app/dcv-sdk
ENV PYTHONUNBUFFERED=1 STATE_DB=/data/state.db
VOLUME /data
EXPOSE 8100
CMD ["uvicorn", "agentcore.control.app:app", "--host", "0.0.0.0", "--port", "8100"]
