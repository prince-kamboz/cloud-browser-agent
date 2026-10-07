# Provider: Browserbase (`PROVIDER=browserbase`)

A hosted cloud browser. Nothing runs locally except the control plane and the agent.

| | |
|---|---|
| Saved logins | a Browserbase **context**, attached when the session starts and written when it ends |
| Agent connection | one `connectUrl` (a `wss://` address that already carries its credentials, no headers) |
| Live view | Browserbase's own page in an iframe; it shows one page at a time, so our UI draws the tab strip from `GET /sessions/{id}/debug` |
| Tabs | listed from `/debug`; opened and closed through a short browser-level CDP call |
| Hide the agent while typing secrets | **not available.** The agent's pause gate stops it, and session recording is turned off so typed secrets are not in a replay |
| Status | **Verified live** (2026-10-07): open about 3 s, release about 4 s, cookies and localStorage survive release and reopen |

## Set up

1. Create a Browserbase account and copy the **API key** and **Project ID** from the dashboard.
2. Put them in the repo-root `.env` (git-ignored; see `.env.example`):
   ```
   BROWSERBASE_API_KEY=bb_live_...
   BROWSERBASE_PROJECT_ID=...
   ```
3. `./run.sh browserbase`, then open http://localhost:8100.

Never paste the key in chat or commit it. The control plane hands the agent the `connectUrl`, which is a credential, only
through its token-protected `/connection` endpoint.

## Behaviour worth knowing

- **`keepAlive` is on.** Without it Browserbase ends the session as soon as the last CDP client disconnects; the agent
  reconnects often, and the user may be watching with no agent attached.
- **The context must exist before the session.** The control plane creates it on a user's first open
  (`profile_at_start = True`) and reuses it. A context is written when the session closes, so mid-session "save" is a no-op
  and the control plane uses `PROFILE_STRATEGY=overwrite` (the default for this provider).
- **Closing waits for the session to end** (up to 30 s) before reporting done, so an immediate reopen sees the saved state.
  Browserbase's docs advise waiting a few seconds before reusing a context.
- **Free plan: one browser at a time.** A second session fails with `402` and the UI shows "Browser quota reached". Close the
  first browser (or let it idle out) before opening another.
- **A context can only be used by one session at a time**, which the per-user lock already guarantees.

## Check it works (uses a little real browser time)

```bash
cloud_browser_agent/tests/run_live.sh browserbase
```
Opens a session, sets a cookie and localStorage, releases, reopens, and confirms both came back. Keys are read from `.env`
and never printed.
