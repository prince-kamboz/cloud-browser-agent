# Testing

Everything runs in Docker; nothing is installed on your machine.

## 1. Unit tests (fast, no accounts)

```bash
cloud_browser_agent/tests/run_unit_tests.sh
```
63 tests: lifecycle logic, SQLite, request shapes and signing, the Docker and Browserbase providers against fakes, the agent's
config files and tab parsing. Add `TAIL=200` to see every test name.

## 2. Live lifecycle check against a real backend

```bash
./run.sh docker                                  # once, so the browser image and network exist
cloud_browser_agent/tests/run_live.sh docker
cloud_browser_agent/tests/run_live.sh browserbase   # uses a little real browser time; needs BROWSERBASE_* in .env
```
Opens a session, sets a cookie and a localStorage value, releases, reopens, and checks both came back. Expect `RESULT: PASS`.

## 3. Agent connection against a local Chromium (no accounts)

```bash
docker compose build agent                       # builds the cba-agent image these scripts use
docker compose --profile docker build chromium   # builds the cba-chromium image they borrow Chromium from
cloud_browser_agent/tests/run_local_integration.sh                          # connection through a fake signed gateway
TEST_MODULE=stress_restart cloud_browser_agent/tests/run_local_integration.sh   # 12 restarts in a row
cloud_browser_agent/tests/run_local_e2e.sh "Open example.com and tell me its title"   # real agent run, uses OPENAI_API_KEY
```
The integration test checks the SigV4 signature independently, refuses stale and bad signatures, loads the tools, reads a
page, exercises the pause gate, and checks no signed headers are left on disk.

## 4. Manual scenarios in the UI

Start with `./run.sh <provider>`, open http://localhost:8100, click **Open browser**, click the card to open the panel.
Run these with any provider (Browserbase's free plan allows one browser at a time).

**Simple prompts**
1. `Go to wikipedia.org, search for "Ada Lovelace" and tell me the year she was born.`
2. `Go to news.google.com and list the top 5 headlines.`
3. `Go to the-internet.herokuapp.com/login and tell me what the page asks the user to do. Don't log in.`

**Complex scenarios**

1. *Multi-tab research.* `Open Hacker News, GitHub Trending and Wikipedia's "Python (programming language)" page in three separate
   tabs. From Hacker News take the top story's title and score, from GitHub Trending the top repository's name and star count,
   from Wikipedia the year Python was first released. Then close the Wikipedia tab and give me one table with all three.`
   Expect three tabs, the highlighted tab following the agent, and the Wikipedia tab gone at the end.
2. *Looking at a page, and forms.* `Go to the-internet.herokuapp.com/challenging_dom. Look at the page and tell me the colour of the
   first button and how many rows the table has. Then go to the-internet.herokuapp.com/dropdown and choose "Option 2", and go to
   the-internet.herokuapp.com/checkboxes and tick the first checkbox. Describe what you see after each step.`
   Expect the vision tool to be used for the colour.
3. *Login that stays saved, with a pause for you.* `Go to the-internet.herokuapp.com/login and stop. Tell me you need me to log in and
   wait for me. After I log in myself, check you can see the secure area and tell me the message at the top. Then open a new tab
   with news.google.com and list the top 3 headlines.`
   When the agent stops, press **Take over**, log in with `tomsmith` / `SuperSecretPassword!`, hand back. Then **Close browser**,
   wait about 5 seconds, **Open browser**, and ask `Go to the-internet.herokuapp.com/secure and tell me if I'm still logged in.`
   Expect yes, with no login prompt.

**Checks on the UI changes**

| Check | Expect |
|---|---|
| Open a browser and leave the page alone for a minute | no `/tabs` or heartbeat lines in `docker compose logs control` |
| Run complex scenario 1 | the tab strip updates within about 2 s of each step |
| Switch to another window for 30 s and back | the strip refreshes on return; no polling while hidden |
| Click **+**, a tab, then **x** | a new tab opens, becomes active, then closes |
| Leave the page open for 3 minutes | the browser is still there (the heartbeat keeps it alive) |
| Click the card, then expand, then X | card -> panel -> full window -> card |
| **Forget logins**, then open again | the saved login is gone |

