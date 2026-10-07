# v2: browser agent on Amazon Bedrock AgentCore Browser

Branch `cloud-browser-agent`. v1 (self-hosted Chromium in Docker) is on `main`; nothing here depends on it.

## How it works

```
 User's browser ──(1) open ──► Control plane ──(2) StartBrowserSession(profile) ──► AgentCore Browser
   (chat + live view)            sessions, locks,                                    one isolated Chromium
        ▲                        profile pointer,                                    per user session
        │                        idle reaper                                             │
        │ (3) live view URL            │ (4) signed CDP endpoint (server side only)      │
        │  signed, 5 min, DCV          ▼                                                 │
        └──────────── video ◄──────────────────────────────────────────────────────────┤
                                  Agent service (Deep Agents + Playwright MCP) ──CDP───┘
                                  OpenAI main + vision models                    (WebSocket, SigV4)
```

1. **Open.** The user starts a browser chat. The control plane takes that user's lock (one browser per user), looks up
   their saved profile, and calls `StartBrowserSession` with it. AgentCore boots an isolated Chromium in its own microVM
   with the saved cookies and local storage already loaded.
2. **Watch and take over.** The control plane returns a *live-view URL*: a SigV4-presigned link, valid 5 minutes, refreshed
   by the UI. The video stream (Amazon DCV) flows from AWS straight to the user's browser. The user sees the real Chrome
   window (tabs, address bar) and can click and type in it.
3. **The agent works.** The agent service asks the control plane for a *signed CDP endpoint* (credentials, so server-side
   only) and connects Playwright MCP to it. Everything else is as in v1: snapshots first, `look()` vision on demand,
   approvals before opening sites, Deep Agents orchestration.
4. **Secrets.** When the user needs to type a password, the control plane switches the agent's automation stream **off**
   (`UpdateBrowserStream`), so the agent literally cannot see the page, and back on afterwards. Idle release is paused meanwhile.
5. **Keep it alive.** The UI sends a heartbeat. The control plane autosaves the profile every 60 s (AgentCore only saves
   when asked) and calls `StopBrowserSession` after 120 s without a heartbeat, saving first.
6. **Come back.** Next open starts a new session from the saved profile: logins are back. If AWS ended the session by itself
   (its time limit), only changes since the last autosave are lost.

### Saving logins: two strategies (an open question)
The console says profiles "are immutable and cannot be updated once they are created", while the API docs say a second save
overwrites. We do not know which is true for the data. `PROFILE_STRATEGY=rotate` (default) works either way: each save
creates a new profile, then the pointer moves and the old one is deleted. `overwrite` is simpler if saves overwrite.
`bench/step0_bench.py` answers the question; the tests cover both behaviours.

## What is built and tested (37 tests, all pass, no AWS needed)
- `provider/agentcore.py`: the real AgentCore calls and SigV4 signing for the WebSocket and live view. Request shapes and
  error mapping are tested against fake clients. **Not yet run against a live account.**
- `provider/fake.py`: simulates AgentCore, including write-once profiles and a zero profile quota (this account today).
- `control/core.py`: lifecycle logic. `control/store.py`: SQLite. `control/app.py`: HTTP API. `Dockerfile` + `docker-compose.yml`: runnable control plane.
- `bench/step0_bench.py`: the live benchmark, ready for when quotas allow.

## Agent connection (built, tested against a local Chromium)
`agent/` is the v2 agent service: the same Deep Agents setup as v1 (coordinator + browser subagent + `look()` vision tool),
but the browser is remote. Per user it keeps one Playwright MCP process connected to that user's browser:
- The signed address comes from the control plane (`ControlPlaneSource`) or a fixed one (`StaticSource`, for tests).
- Playwright MCP gets the signed headers through a 0600 config file (`browser.cdpHeaders`), never the command line, and
  the file is deleted as soon as MCP has started. Handshake headers (`Upgrade`, `Sec-WebSocket-*`, `Host`) are not passed on.
- AWS signatures are valid for about five minutes, so the connection is made right before use, checked immediately
  (a bad address or signature fails in `start()` with the real reason), and restarted with a fresh signature whenever the
  browser session changes.
- `gate.py` is the "Take over" pause: a paused tool call waits, then is *not executed* and the model is told to look again.
- Dangerous tools (close page, resize, arbitrary code, file upload) are removed; screenshots only reach the model as text via `look()`.

## Tests (all in Docker, nothing touches AWS)
| Command | What it proves |
|---|---|
| `docker run ... python -m unittest discover -s cloud_browser_agent/tests -t .` | 43 fast tests: lifecycle, SQLite, signing, request shapes, config files |
| `cloud_browser_agent/tests/run_local_integration.sh` | the real connection code through a fake AgentCore gateway to a local Chromium: signature checked independently, stale and bad signatures refused, tools load, page read, pause gate, restart on a new session, no signed headers left on disk |
| `TEST_MODULE=stress_restart cloud_browser_agent/tests/run_local_integration.sh` | 12 consecutive restarts |
| `cloud_browser_agent/tests/run_local_e2e.sh "<task>"` | a real Deep Agents run (OpenAI key from `../.env`, a few cents) through the whole path, including the vision tool |

The fake gateway found a real bug: botocore signs with its own clock, so `X-Amz-Date` has to be read back from the
signed request (computing it beforehand caused intermittent signature mismatches). Fixed, with a regression test.

## The v2 UI and live view (built, tested locally)
`ui/` is a vanilla-JS page (same look as v1) served by the control plane. Open a browser, chat with the agent, watch the
live view, take over, type secrets privately, close the browser (logins saved).
- **Live view = an iframe.** The control plane returns a `viewer_url`. For AgentCore it is `/viewer/dcv.html#url=<signed link>`,
  a small page (`ui/viewer/dcv.js`) that drives AWS's DCV Web Client the way AWS's own React component does: authenticate with
  the presigned URL, connect, scale the remote viewport to fit, report `connected` / `disconnected` to the page. The page
  asks for a fresh signed link and reloads the iframe if the stream drops (links last 5 minutes).
- **DCV files are never committed.** Their licence is limited, non-transferable, internal-use. The `Dockerfile` fetches them
  from AWS's `bedrock-agentcore` npm package at build time into `/app/dcv-sdk`; `cloud_browser_agent/.gitignore` excludes `dcv-sdk/`.
- **Take over** pauses the agent (`/pause`), **Private input** switches the agent's automation stream off
  (`/secret-entry`) and shows a banner; sending a task turns it back off. While the agent works, an overlay blocks the
  live view; heartbeats keep the session alive; a "Browser closed" overlay appears if the server ended it.

### Run it locally with no AWS: `cloud_browser_agent/dev/run_dev_stack.sh`, then open http://localhost:8100
A real headless Chromium, the signed fake gateway, a dev live viewer (screencast to a canvas, with mouse and keyboard),
the control plane + UI, and the agent service (OpenAI key from `../.env`). `DEV_VIEWER=dcv` swaps in the stand-in DCV
script to exercise the DCV viewer page. Stop with `dev/stop_dev_stack.sh`.

**Verified here:** open session, live view streaming real pixels, agent task from the chat (friendly steps, markdown
answer), clicking and typing in the live view (including text insertion), private input, take over mid-task (agent
paused server-side), reuse of a live session after a page reload.
**Not verified:** the real DCV stream (needs a live AgentCore session; our DCV page follows the documented API and the
code in AWS's component, but has only run against the stand-in), and hand back through to the end of a task (an
environment hiccup interrupted that run).

### A bug this found
Idle and autosave timing used the wall clock. The dev VM was suspended, the clock jumped 15 minutes, and every user looked
idle for 15 minutes and was released. Durations now use a monotonic clock (wall time only for stored timestamps), with a
regression test.

## Not built yet
1. Website approvals and the cookie backup in the v2 agent service (v1 has them; not ported yet).
2. Website approvals cards in the UI (the agent side is not ported yet).
3. Our own backup of cookies and local storage in SQLite or a file (independent of profiles, covers the 100-profile and
   write-once risks). Provider-agnostic.
4. User authentication, when this moves into the FastAPI app.

## Deploying
Scope: only the AgentCore **Browser tool** is used (sessions, live view, profiles). Not AgentCore Runtime, Memory or Gateway.
The agent stays our own Deep Agents code. State is **SQLite**. No DynamoDB, ECS, Secrets Manager or Cognito.

**Now (POC): everything on your machine, only the browsers run in AWS.**
`docker compose up` in `cloud_browser_agent/` runs the control plane (FastAPI + SQLite file in a Docker volume). The agent service
(Deep Agents + Playwright MCP, your existing Docker app) connects to the browser through the signed address the control
plane hands out. The IAM user's keys sit in `cloud_browser_agent/.env.aws` (git-ignored).

**Later: inside your FastAPI app.** `control/` has no framework lock-in beyond `app.py`: mount its routes into your app and
keep the same SQLite file. Move the OpenAI key and the user login wherever that app already keeps them.

**SQLite holds:** `users` (saved-profile pointer, save status) and `active` (live sessions, so a restart can re-adopt or
clean up sessions instead of leaving them running and billing).

## AWS access needed
Only the `bedrock-agentcore` browser actions in `iam-policy.json`, 12 in all: start/get/stop/list sessions, the automation and
live-view streams, the stream toggle, and create/get/delete/list/save profiles. No S3, no Bedrock models, no IAM, no STS.
