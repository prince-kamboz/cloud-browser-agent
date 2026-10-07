# Provider benchmark

Run on 2026-10-07 with `cloud_browser_agent/bench/run_bench.sh --runs 5 --providers browserbase browseruse docker`.
Same test on each provider, round-robin, timed from the client (what the agent experiences). Median / worst of 5 runs, in ms.

| metric (ms, median / worst) | browserbase | browseruse | docker |
|---|---|---|---|
| time_to_usable | 5,191 / 5,525 | 7,841 / 8,398 | 679 / 743 |
| start_api | 1,065 / 1,090 | 1,532 / 1,556 | 154 / 218 |
| ready | 2,060 / 2,166 | 2,854 / 3,425 | 675 / 736 |
| first_cdp | 1,287 / 1,446 | 1,527 / 1,559 | 2 / 16 |
| rtt_p50 | 276 / 290 | 285 / 288 | 0 / 1 |
| rtt_p95 | 394 / 432 | 385 / 417 | 0 / 3 |
| nav_example.com | 428 / 466 | 472 / 616 | 297 / 561 |
| nav_wikipedia | 1,384 / 1,739 | 880 / 1,175 | 1,466 / 1,816 |
| nav_hacker_news | 536 / 568 | 688 / 910 | 1,392 / 1,524 |
| screenshot | 944 / 1,641 | 717 / 2,848 | 47 / 64 |
| live_view_url | 956 / 2,226 | 1,265 / 1,336 | 0 / 0 |
| stop | 1,869 / 1,925 | 2,514 / 2,676 | 1,196 / 1,248 |
| reopen_usable | 5,084 / 5,369 | 7,798 / 8,302 | 406 / 695 |
| runs ok | 5/5 | 5/5 | 5/5 |
| cookie persisted | 5/5 | 5/5 | 5/5 |
| google blocked | 0/5 | 5/5 | 5/5 |
| sannysoft failed checks (median) | 0 | 0 | 4 |
| navigator.webdriver | 0/5 | 0/5 | 0/5 |

## Reading it

- **Startup and reopen:** Browserbase is about 2.6 s faster to a usable browser (5.2 s vs 7.8 s), and the same on reopen.
  Browser Use is slower at every step: API return, ready, stop. Docker is under 1 s.
- **Once running, they are the same.** Command round trip is about 280 ms on both; that is mostly the network between the test
  machine and the cloud, not the service. Page loads and screenshots trade places (Browser Use faster on Wikipedia and median
  screenshots, Browserbase on Hacker News).
- **Reliability and saving:** 5/5 runs and 5/5 cookie persistence on all three.
- **Blocking:** Google search worked on Browserbase (0/5 blocked) and was blocked on Browser Use and Docker (5/5). Both cloud
  services pass the bot-detection page with 0 failed checks; Docker's headless Chromium fails 4.

## Caveats

- Five runs, one client machine and network: latency numbers depend on where the test runs. Treat differences under about
  300 ms as noise.
- Browser Use was run with **no managed proxy** (our provider default); its API default is a US residential proxy, which may
  change the Google result. Browserbase's free plan has no proxy either. Neither was tested with a proxy.
- "Google blocked" is a heuristic (a `/sorry/` URL or "unusual traffic" text), not a screenshot review.
- Free-plan Browserbase limits (one browser at a time, 15-minute sessions) applied during the run.
