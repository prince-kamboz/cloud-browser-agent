# One headless Chromium per user session, used by PROVIDER=docker.
# Only nginx (:8080) is exposed, and only /cdp/ is proxied: Chromium accepts DevTools connections for Host "localhost" only.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive SCREEN_WIDTH=1280 SCREEN_HEIGHT=800 CHROME_BIN=chromium

RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium nginx fonts-liberation fonts-noto-color-emoji fonts-noto-cjk ca-certificates curl procps \
    && rm -rf /var/lib/apt/lists/* && rm -f /etc/nginx/sites-enabled/default

# "Continue where you left off": makes Chromium keep session cookies (logins with no expiry) and reopen the previous tabs.
COPY chromium-policy.json /etc/chromium/policies/managed/persist.json
COPY nginx.conf /etc/nginx/conf.d/cdp.conf
COPY start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh

EXPOSE 8080
CMD ["/usr/local/bin/start.sh"]
