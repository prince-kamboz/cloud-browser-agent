# Provider: Browser Use Cloud (`PROVIDER=browseruse`)

Browser Use's hosted **browsers** (managed Chromium over CDP). Only the browser product is used, not Browser Use's hosted agents
or its open-source agent library: our own Deep Agents code stays the agent.

| | |
|---|---|
| Saved logins | a Browser Use **profile**, attached when the browser starts; its state is kept when the session stops |
| Agent connection | `cdpUrl` (an `https://` base) resolved to its `wss://` address through `/json/version`; no headers |
| Live view | Browser Use's hosted page (`live.browser-use.com`) in an iframe. It draws its own tabs and address bar |
| Tabs | the live view shows them itself; our tab strip is not used for this provider |
| Hide the agent while typing secrets | not available; the agent's pause gate stops it |
| Price | about $0.02 per browser hour (billed to the minute, unused time refunded on stop), proxy traffic extra; no subscription |
| Status | **Verified live** (2026-10-07): cookies and localStorage survive release and reopen; benchmarked against Browserbase and Docker |

## Set up

1. Create an account at [cloud.browser-use.com](https://cloud.browser-use.com) and create an API key (it starts with `bu_`).
2. Put it in the repo-root `.env` (git-ignored):
   ```
   BROWSER_USE_API_KEY=bu_...
   # BROWSER_USE_PROXY_COUNTRY=us     optional; empty = no managed proxy
   ```
3. `./run.sh browseruse`, then open http://localhost:8100.

Never paste the key in chat or commit it. Treat the live-view URL as a credential too: anyone holding it can control the browser.

## Behaviour worth knowing

- **Stop with the API, not by disconnecting.** The docs say a dropped CDP connection does not stop a browser or its billing; the
  provider calls `PATCH /browsers/{id}` with `{"action":"stop"}` and waits for the session to finish before reporting done.
- **The profile must exist before the session**, so the control plane creates it on a user's first open (`profile_at_start`) and
  reuses it. `PROFILE_STRATEGY=overwrite` is the default for this provider.
- **No managed proxy by default.** The API's own default is a US residential proxy. We turn it off so the comparison with other
  providers is like for like and no proxy traffic is billed; set `BROWSER_USE_PROXY_COUNTRY` to turn it on.
- **Concurrency:** 10 sessions at the free tier, more by lifetime spend (50 at $200, 250 at $1,000, ...).
- **Max session length:** 240 minutes.

## How it compared (see [benchmarks](../benchmarks.md))

About 2.6 s slower than Browserbase to a usable browser (7.8 s vs 5.2 s) and to reopen; the same command speed once running; page
loads and screenshots about even. **Google search was blocked on every run without a proxy** (not tested with one).

## Known issue: live-view responsiveness

In testing, the live view took roughly 4 to 8 seconds to show a change after a navigation, even though the browser itself
navigated in under half a second. The address bar, scrolling and page clicks all worked, just slowly. The agent keeps working
at full speed, so this only affects a person watching or taking over. Showing this provider through our own screencast viewer
(as the Docker provider does) is the likely fix and is not built yet.

## Check it works

```bash
cloud_browser_agent/tests/run_live.sh browseruse
```
Opens a session, sets a cookie and localStorage, releases, reopens, and confirms both came back. The key is read from `.env` and never printed.
