# Cloud Browser Agent

A chat agent that drives a **real browser in the cloud (or in a local container)** while you watch it live, take over when you
need to (logins, CAPTCHAs), and come back later to find your logins still saved.

You choose where the browser runs, with one setting:

| `PROVIDER` | Browser | Needs | Best for |
|---|---|---|---|
| `docker` | headless Chromium in a local container | Docker, an OpenAI key | development, demos, keeping browsing on your own machine |
| `browserbase` | [Browserbase](https://www.browserbase.com) hosted browser | a Browserbase account | production-style cloud browsers without running infrastructure |
| `browseruse` | [Browser Use Cloud](https://browser-use.com) hosted browser | a Browser Use API key | the cheapest hosted option (about $0.02 per browser hour) |
| `agentcore` | [Amazon Bedrock AgentCore Browser](https://aws.amazon.com/bedrock/agentcore/) | an AWS account with quota | AWS-native deployments (built and tested against fakes; not yet run live) |

```
 You (chat + live browser)  ──►  Control plane  ──►  Browser provider (docker | browserbase | agentcore)
                                       ▲                         ▲
                                 Agent service (Deep Agents + OpenAI) ── Playwright MCP ──┘
```

## What you get

- **Chat first.** The conversation is the main view. The browser appears as a small live card on the right; click it to open a
  large panel beside the chat, expand it to full window, or minimize it again.
- **Watch and take over.** See the real page, click and type in it, press **Take over** to pause the agent, **Hand back** to
  resume. **Private input** is for typing passwords yourself.
- **Real tabs.** A tab strip you can use (open, switch, close); the tab the agent is working in is always the active one.
- **Saved logins per user.** Close the browser and open it again: cookies, localStorage and open tabs are back.
- **One browser per user**, released automatically after 2 minutes without a heartbeat (logins saved first).
- **A capable agent.** Deep Agents with a browser subagent over Playwright MCP, accessibility snapshots first and a vision
  tool only when the page text is not enough. OpenAI models for both.

## Quick start

1. Install Docker Desktop.
2. Copy the settings file and add your OpenAI key:
   ```bash
   cp .env.example .env        # then edit .env: OPENAI_API_KEY=...
   ```
3. Start with the browser you want:
   ```bash
   ./run.sh docker             # a local Chromium, no other account needed
   ./run.sh browserbase        # needs BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID in .env
   ./run.sh browseruse         # needs BROWSER_USE_API_KEY in .env
   ./run.sh agentcore          # needs deploy/aws/.env.aws (see docs/providers/agentcore.md)
   ```
4. Open **http://localhost:8100** (use `localhost`, not `0.0.0.0`, which some VPNs and firewalls block).
5. Click **Open browser**, click the card on the right to open the panel, and ask something, e.g.
   *"Find the top 3 Python repos trending on GitHub today."*

Switch backends any time by stopping (`./run.sh stop`) and starting with another provider. Saved logins belong to the backend
that created them, so each provider keeps its own.

## Configuration

Set in `.env` (see `.env.example`); every value has a default except the keys.

| Variable | Default | Meaning |
|---|---|---|
| `OPENAI_API_KEY` | | required for the agent |
| `MAIN_MODEL`, `VISION_MODEL` | `gpt-6.1-sol` | models for the agent and for `look()` |
| `PROVIDER` | set by `./run.sh` | `docker`, `browserbase`, `browseruse`, `agentcore`, `fake` |
| `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID` | | Browserbase credentials |
| `BROWSER_USE_API_KEY` | | Browser Use Cloud key (starts with `bu_`) |
| `IDLE_AFTER_S` | `120` | seconds without a heartbeat before a user's browser is saved and released |
| `DOCKER_MAX_BROWSERS` | `5` | concurrent local browsers (`docker` provider) |
| `INTERNAL_TOKEN` | dev default | protects the endpoint that hands the agent its browser address; set your own off a laptop |

Provider-specific settings are in each provider guide.

## Which provider?

Measured on 5 runs each (details and caveats in [docs/benchmarks.md](docs/benchmarks.md)):

| | docker | browserbase | browseruse |
|---|---|---|---|
| Time to a usable browser | 0.7 s | 5.2 s | 7.8 s |
| Command speed once running | instant (local) | about the same | about the same |
| Google search not blocked | no | **yes** | no (without a proxy) |
| Cost | your own compute | from $20/mo, then $0.10 to $0.12 per hour | about $0.02 per hour, no subscription |

Use **docker** to develop, **browserbase** when sites resist automation, **browseruse** when cost matters most. Prices come from
the providers' pages and change; check them before deciding.

## Documentation

- [Architecture](docs/architecture.md): components, the provider interface, how a session lives and dies, security notes.
- Provider guides: [Docker Chromium](docs/providers/docker.md), [Browserbase](docs/providers/browserbase.md),
  [Browser Use](docs/providers/browseruse.md), [AgentCore](docs/providers/agentcore.md) (AWS setup, IAM policy, quotas).
- [Benchmarks](docs/benchmarks.md): the same test run on each provider.
- [Testing](docs/testing.md): unit tests, live checks, and manual scenarios with ready-made prompts.
- [Legacy v1](legacy/v1/README.md): the original single-container proof of concept, kept frozen.

## Project layout

```
cloud_browser_agent/      the Python package
  control/                control plane: sessions, SQLite state, HTTP API, live-view bridge
  agent/                  agent service: Deep Agents, Playwright MCP, pause gate, vision tool
  provider/               one file per backend (docker_chromium, browserbase, browseruse, agentcore) + test fakes
  ui/                     the web UI (vanilla JS, no build step) and the live-view pages
  tests/                  unit tests, live checks, local integration scripts
  dev/, bench/            a no-account dev stack; the provider benchmark and the AgentCore Step 0 benchmark
browsers/chromium/        the browser image the docker provider starts per user
deploy/aws/               AgentCore IAM policy and credentials template
docs/                     architecture, provider guides, testing
legacy/v1/                the original proof of concept (frozen)
docker-compose.yml        control + agent (+ browser image build for docker)
run.sh                    start or stop the system with the provider of your choice
```

## Testing

```bash
cloud_browser_agent/tests/run_unit_tests.sh         # 70 tests, a few seconds, no accounts
cloud_browser_agent/tests/run_live.sh docker         # real open -> cookie -> release -> reopen check
cloud_browser_agent/bench/run_bench.sh --runs 5 --providers browserbase browseruse docker   # compare providers
```
More in [docs/testing.md](docs/testing.md).

## Status

| | State |
|---|---|
| Docker provider | verified end to end (UI, agent, two tabs, saved logins) |
| Browserbase provider | verified live (lifecycle, saved logins, tabs, agent task) |
| Browser Use provider | verified live (lifecycle, saved logins, benchmark); live view is slow to update and shows its own tab bar |
| AgentCore provider | built and unit-tested; a live run was blocked by the test account's zero quotas |
| Known gaps | a saved profile that fails to load is replaced rather than retried; "Take over" resumes if you send a message; no website approvals or cookie vault (they exist in `legacy/v1`) |
| User authentication | not built |

## Security

This is a proof of concept. The OpenAI and provider keys stay on the server and out of git (`.env` is ignored). There is **no
user login**: the API and live view are bound to `127.0.0.1`, so anyone who can reach them can drive the browser. The Docker
provider needs the Docker socket, which is root on the host: use it on your own machine, not a shared server. Add
authentication and drop the socket (or isolate it) before exposing this anywhere.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Browser says it cannot connect to `0.0.0.0:8100` | use http://localhost:8100 |
| `port is already allocated` on 8100 or 8200 | another copy is running: `./run.sh stop` |
| "Browser quota reached" (Browserbase) | the free plan allows one browser at a time; close the other one |
| `image 'cba-chromium' not found` | start with `./run.sh docker`, which builds it |
| Live view is black for a few seconds | normal while it connects |
| Browser Use live view reacts 4 to 8 seconds late | known; it is Browser Use's viewer, not the browser (see [its guide](docs/providers/browseruse.md)) |
| "the agent is already working on a task" | a task is still running for that user: press **Stop** in the chat |
| Chat says it lost the connection to the agent | the agent service is not running: `docker compose ps` |
