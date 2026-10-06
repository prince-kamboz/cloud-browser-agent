FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    SCREEN_WIDTH=1280 \
    SCREEN_HEIGHT=800 \
    CHROME_BIN=chromium

RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium \
      nginx \
      fonts-liberation fonts-noto-color-emoji fonts-noto-cjk \
      ca-certificates curl procps \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/nginx/sites-enabled/default

# Managed policy: "continue where you left off". It is also what makes Chromium keep session cookies
# (logins without an expiry) and reopen the previous tabs after a restart.
COPY chromium-policy.json /etc/chromium/policies/managed/persist.json
COPY nginx.conf /etc/nginx/conf.d/agent.conf
COPY start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh

# Only nginx is exposed. It serves the UI and the CDP endpoint (the live view is a CDP screencast).
EXPOSE 8080

CMD ["/usr/local/bin/start.sh"]
