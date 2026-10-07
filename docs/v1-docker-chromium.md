# v1: Docker Chromium + Deep Agents (original POC)

> This is the original v1 write-up. It lives on `main`; the newer versions are described in the [root README](../README.md).
> Commands below are run from the repo root on the `main` branch.

A chat agent in your browser that controls a **real Chromium running in Docker** and shows it live inside the page, like ChatGPT agent.
There's no custom backend: the whole agent is plain HTML/JS. Docker only runs off-the-shelf parts (headless Chromium, nginx).

```
Your browser (http://localhost:8080)
 ├─ Chat UI + agent loop (web/*.js)
 │    ├─ calls OpenAI Responses API directly (computer tool + browser function tools)
 │    └─ controls Chromium via Chrome DevTools Protocol (WebSocket /cdp/)
 └─ Live view canvas (CDP screencast over the same /cdp/ socket)  ← you watch and can take over

Docker container
 headless Chromium (CDP :9222) → nginx :8080   (no X server, VNC or noVNC)
```

## Run it

1. Install Docker Desktop.
2. In this folder:
   ```bash
   docker compose up --build
   ```
3. Open **http://localhost:8080**, click **Settings**, paste your OpenAI API key and save.
4. Ask something, e.g. *"Go to news.ycombinator.com and tell me the top 5 stories."*

UI files in `web/` are mounted into the container, so edit them and refresh the page. No rebuild needed.

## What the agent can do

| Capability | How |
|---|---|
| Vision | Screenshot after every step (computer tool) plus a screenshot with each of your messages |
| Mouse | click, double-click, right/middle click, move, drag, scroll |
| Keyboard | type text, key combos (Ctrl+A, Enter, Tab, arrows, F-keys…) |
| Navigation | `navigate`, `go_back`, `go_forward`, `reload` |
| Reading | `get_page_text` returns the page's text and links for accurate reading |
| Tabs | `list_tabs`, `switch_tab`, `new_tab`, `close_tab`, and it follows tabs opened by links automatically |
| JavaScript | `run_javascript` (can be switched off in Settings) |
| Dialogs | `alert`/`confirm` popups are auto-accepted so they don't block the agent |
| Memory | Multi-turn chat through `previous_response_id` (**New chat** resets it) |
| Human in the loop | **Take over** pauses the agent and lets you use the browser (logins, CAPTCHAs); **Hand back** resumes. **Stop** ends the run |
| Safety | Prompt tells it to confirm purchases/sends and never type passwords; legacy `pending_safety_checks` trigger a confirm dialog; step limit |
| Persistent logins | Browser profile is stored in a Docker volume |

## Settings

- **Model**: default `gpt-6.1-sol`. Any model that supports the `computer` tool works. Check OpenAI's [computer use guide](https://developers.openai.com/api/docs/guides/tools-computer-use) for the current list.
- **Computer tool**: `computer` (current API), or `computer_use_preview` for the older `computer-use-preview` model. This is picked automatically when that model is chosen.
- **Max steps**, **reasoning effort**, **allow JavaScript**.

## Files

- `docker/Dockerfile`, `docker/start.sh`: headless Chromium + nginx
- `docker/nginx.conf`: serves the UI and proxies `/cdp/` (Chrome only accepts `Host: localhost`, so nginx rewrites it)
- `web/agent.js`: agent loop (Responses API, computer actions, function tools)
- `web/browser.js`: CDP browser control (mouse, keyboard, screenshots, tabs, page text, live-view screencast)
- `web/cdp.js`: tiny CDP WebSocket client
- `web/app.js`, `web/index.html`, `web/style.css`: chat UI, live-view canvas, takeover (mouse/keyboard/paste forwarded over CDP)

## Important limitations (POC)

- **The API key lives in the browser.** That's fine on your own machine, but never host this publicly. For production, move the OpenAI calls to a small backend.
- Port 8080 gives full control of the browser, so it's bound to `127.0.0.1` only.
- Screenshots show only the page area (no address bar); the agent uses `navigate` for URLs.
- Headless mode has no browser chrome: no address bar, native context menus, or file/print dialogs in the live view.
- Pressing **Stop** in the middle of a tool call resets the conversation memory, because the API chain can't be continued.
- For many concurrent users you'd run one container per session (or use Browserbase/Steel), plus a backend.

## Troubleshooting

- **Live view is black or frozen**: click **Reconnect view**. Frames are only sent when the page changes, so a static page looks idle.
- **"Browser not reachable"**: run `docker compose logs`. Chromium restarts automatically if its window is closed.
- **OpenAI error about the model/tool**: change the model in Settings, or switch the tool type.
- **Apple Silicon**: works natively; Debian's Chromium supports arm64.

## Deep Agent mode (POC): website approvals

Settings → **Website approvals** works like ChatGPT's Cloud computer page:

- **Always ask**: every site the agent wants to open needs your OK (an approval card appears in the chat).
- **Auto approve**: normal https sites open; unusual ones (plain http, IP addresses, internal hosts) ask.
- **Always allow**: no questions (internal hosts still ask).
- **Website permissions**: per-site overrides (`example.com`, `*.example.com`): Always allow / Always ask / Block.
- Always blocked: `localhost`, loopback and link-local addresses (cloud metadata), and non-web schemes (`file:`, `javascript:`, `data:`, `chrome:`).
- Approval cards offer *Allow for this task*, *Always allow this site* (saved as a rule) and *Deny*.

Rules live in your browser (localStorage) and are sent with each message. They are enforced on the agent's own navigations
(`browser_navigate`, new tab URLs). Links the agent clicks and redirects are **not** checked yet, so this is a guardrail for the POC, not a security boundary.

## Lifecycle prototype: a browser per user, on demand

`docker compose -f docker-compose.lifecycle.yml up --build -d`, then open http://localhost:8090.

- **Open** starts a browser for a user (about 1.7 s cold, measured) and restores what they left: cookies, localStorage, IndexedDB, open tabs.
- **Idle** (no heartbeat for 60 s): both containers are paused, like a microVM suspend. A heartbeat wakes them in well under a second.
- **Release** (paused for 180 s, or the app was closed for 10 s): the browser quits cleanly, a slim profile (about 15-30 KB, no caches) is saved encrypted, and every container and volume is removed.
- One browser per user (a lock), so two sessions can't log each other out.
- Unclean exits (kill -9, crash): the cookie vault's copy wins and the last tab list the manager saw is reopened.
- State goes through `LocalStore` (an encrypted folder). The same four methods (`get`, `put`, `delete`, `list`) map to S3 + KMS on AWS.

Settings: `SUSPEND_AFTER`, `RELEASE_AFTER`, `BYE_GRACE` in `.env`. The manager mounts the Docker socket, which is root on the host: POC only.
