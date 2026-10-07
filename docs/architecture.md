# Architecture

Cloud Browser Agent lets a chat agent drive a real browser that the user can watch and take over, with saved logins per
user. The browser can come from four places (providers) behind one interface.

```
 User's browser (chat + live view)
        │ open / heartbeat / tabs / take over                         live view (iframe)
        ▼                                                                    ▲
 ┌───────────────────────────┐  start / stop / profile   ┌─────────────────────────────┐
 │ Control plane (FastAPI)   │ ────────────────────────► │ Browser provider            │
 │ sessions, per-user lock,  │                           │  docker      local container │
 │ autosave, idle reaper,    │ ◄──── connection ──────── │  browserbase hosted browser │
 │  browseruse  hosted browser │
 │ SQLite state              │                           │  agentcore   AWS browser    │
 └──────────▲────────────────┘                           └──────────────▲──────────────┘
            │ connection (token-protected)                              │ CDP (WebSocket)
 ┌──────────┴────────────────┐                                          │
 │ Agent service             │ ─────── Playwright MCP ──────────────────┘
 │ Deep Agents + OpenAI      │
 └───────────────────────────┘
```

Three services run in Docker Compose: **control** (port 8100, also serves the UI), **agent** (8200), and, for the Docker
provider, one browser container per user session.

## The provider interface

`cloud_browser_agent/provider/base.py` is everything the control plane needs from a browser backend:

| Method | Meaning |
|---|---|
| `start_session(user, profile_id, timeout, viewport)` / `wait_ready` / `session_status` / `stop_session` | the browser's life |
| `automation_connection(session)` | `(wss URL, headers)` for the agent's CDP connection, fetched fresh each time |
| `live_view_url(session)` | what the UI shows (a page for iframe-style backends, a signed URL for DCV) |
| `set_automation_enabled(session, bool)` | hide the agent's stream while the user types secrets (AgentCore only; no-op elsewhere) |
| `create_profile` / `save_profile` / `delete_profile` | saved logins |
| `tabs` / `open_tab` / `close_tab` | optional: lets the UI draw the tab strip when the live view shows one page |
| `profile_at_start` (flag) | `True` if the profile must exist when the session starts and is written when it ends |

| | docker | browserbase | browseruse | agentcore |
|---|---|---|---|---|
| Saved logins | Docker volume | Browserbase context | Browser Use profile | AgentCore profile |
| `profile_at_start` | yes | yes | yes | no (saved from a running session) |
| Agent connection | `ws://container:8080/cdp/...` | `connectUrl` | `cdpUrl` resolved to `wss://` | SigV4-signed headers (about 5 min) |
| Live view | screencast via control-plane bridge | Browserbase page | Browser Use page (own tabs and address bar) | Amazon DCV stream |
| Tab strip | from Chromium `/json` | from `/debug` | inside the live view | from the DCV view itself |
| Hide agent from secrets | pause gate only | pause gate only (recording off) | pause gate only | stream switch (`UpdateBrowserStream`) |

Test-only providers: `fake` (simulated, includes write-once profiles and a zero quota) and `testgw` (a real local Chromium
behind a signed fake gateway, used by the dev stack and the signing tests).

## Control plane (`control/`)

- **`core.py` (`SessionService`)**: `open`, `heartbeat`, `save`, `release`, `reap`, `secret_entry`, `forget`, `recover`.
  One browser per user (a per-user lock, so two tabs cannot log each other out). The UI sends a heartbeat every 15 s; after
  `IDLE_AFTER_S` (120) without one the user's browser is saved and released. It autosaves every 60 s where saving mid-session
  is meaningful. Durations use a **monotonic clock** (a suspended VM once made every user look idle for 15 minutes).
- **`store.py`**: SQLite with two tables, `users` (profile pointer, save status) and `active` (live sessions, so a restart can
  **re-adopt** sessions that are still running instead of leaving them running and billing).
- **`app.py`**: the HTTP API (`/api/sessions`, heartbeat, release, tabs, secret-entry, `/connection` for the agent, config),
  the static UI, and the screencast bridge. `PROVIDER` picks the backend.
- **`live_bridge.py`**: for the Docker provider, relays a CDP screencast to the viewer and mouse/keyboard back.
- Profile strategies: `overwrite` (one profile per user, saved in place; the default except for AgentCore) and `rotate` (a new
  profile per save, then delete the old one; used for AgentCore while it is unknown whether saves overwrite).

## Agent service (`agent/`)

- **Deep Agents** coordinator with a browser subagent, OpenAI for both the main and the vision model.
- **Playwright MCP** gives the subagent its browser tools. One persistent MCP process per user is connected to that user's
  remote browser; the address comes from the control plane (`ControlPlaneSource`). Connection headers go into a `0600` temp
  config file, never a command line, and the file is deleted once MCP has started.
- **Accessibility snapshots first**; a `look()` tool asks the vision model about a screenshot only when text is not enough, and
  returns text (screenshots never go to the main model).
- **`gate.py`** is the "Take over" pause: a paused tool call waits, is then *not executed*, and the model is told to look again.
- Dangerous tools (close page, resize, arbitrary code, file upload) are removed.
- **`events.py`** turns agent steps into UI events, including `agent_tab`, parsed from Playwright MCP's "(current)" tab marker.

## UI (`ui/`)

Vanilla JS, no build step. The chat is the main view. When a browser is open it appears as a small live **card** on the right;
clicking it opens the large browser panel beside the chat (expand = full window, X = back to the card). The panel has the tab
strip, **Take over**, and **Private input**. Frame colour shows who is in control (green = you, blue = the agent). The tab the
agent is working in is the active one; the tab list is polled every 2 s while the agent works, every 8 s when idle, and not at
all while the page is hidden.

## Security notes

- The OpenAI key and provider keys stay on the server side (`.env`, git-ignored). `/connection` returns credentials, so it
  requires `INTERNAL_TOKEN`.
- The API and live bridge have **no user authentication yet** and are bound to `127.0.0.1`. Add auth before exposing them.
- The Docker provider mounts the Docker socket (root on the host): laptop use only.
- Website approvals and the cookie vault from v1 are **not ported** to this version yet.

## Problems found while building, and what fixed them

- botocore signs with its own clock, so `X-Amz-Date` must be read back from the signed request (intermittent AgentCore 403s).
- Chromium flushes cookies only on an orderly quit; `SIGTERM` loses them. Fixed with a `Browser.close` shutdown.
- A wall-clock jump after VM suspend released every user. Fixed with a monotonic clock.
- A Browserbase session ends when the last CDP client disconnects unless `keepAlive` is on.
- Browser Use browsers keep running (and billing) after a dropped connection; they must be stopped through the API.
- A Browserbase context attached after the session starts saves nothing; it must exist first.
- A profile created for a session that then failed to start leaked; now removed.

## Not built yet

1. Website approvals and the cookie backup/vault in this version (v1 has them).
2. User authentication.
3. Our own provider-independent backup of cookies/localStorage.
   (Also: when a saved profile fails to load on reopen, the control plane starts a fresh one and drops the old pointer; it should retry and keep the old profile.)
4. A live test of AgentCore (the test AWS account's quotas were zero) and of the real DCV stream.
