"""Compare browser providers on the same test, through the same provider interface.

For each round and each provider (round-robin, so time-of-day and network noise hit all of them alike):
  create a profile -> start a session -> wait ready -> connect over CDP -> 30 command round trips -> load 3 pages ->
  3 screenshots -> read the live-view URL -> try Google search and a bot-detection page -> set a cookie -> stop ->
  reopen with the profile (is the cookie back, how long) -> stop -> delete the profile.
Everything is timed from the client's side, which is what the agent experiences.

Run it in Docker with run_bench.sh. Keys are read from the environment and never printed. Real browser time is used.
"""
import argparse
import asyncio
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import websockets

PAGES = [("example.com", "https://example.com"),
         ("wikipedia", "https://en.wikipedia.org/wiki/Python_(programming_language)"),
         ("hacker_news", "https://news.ycombinator.com")]


def make(kind):
    if kind == "docker":
        from cloud_browser_agent.provider.docker_chromium import DockerChromiumProvider
        return DockerChromiumProvider(os.getenv("DOCKER_BROWSER_IMAGE", "cba-chromium"), os.getenv("DOCKER_NETWORK", "cba-net"))
    if kind == "browserbase":
        from cloud_browser_agent.provider.browserbase import BrowserbaseProvider
        return BrowserbaseProvider(os.environ["BROWSERBASE_API_KEY"], os.environ["BROWSERBASE_PROJECT_ID"])
    if kind == "browseruse":
        from cloud_browser_agent.provider.browseruse import BrowserUseProvider
        return BrowserUseProvider(os.environ["BROWSER_USE_API_KEY"])
    raise SystemExit(f"unknown provider {kind}")


class Cdp:
    def __init__(self, ws):
        self.ws, self.n, self.events = ws, 0, asyncio.Queue()
        self._pending, self._reader = {}, asyncio.create_task(self._read())

    async def _read(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if "id" in m and m["id"] in self._pending:
                self._pending.pop(m["id"]).set_result(m)
            else:
                self.events.put_nowait(m)

    async def call(self, method, params=None, sid=None, timeout=30):
        self.n += 1
        fut = asyncio.get_event_loop().create_future()
        self._pending[self.n] = fut
        msg = {"id": self.n, "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        await self.ws.send(json.dumps(msg))
        r = await asyncio.wait_for(fut, timeout)
        if "error" in r:
            raise RuntimeError(f"{method}: {r['error'].get('message')}")
        return r.get("result", {})

    async def close(self):
        self._reader.cancel()
        await self.ws.close()


async def attach(url):
    t0 = time.perf_counter()
    cdp = Cdp(await websockets.connect(url, max_size=None, open_timeout=30))
    await cdp.call("Browser.getVersion")
    first = (time.perf_counter() - t0) * 1000
    pages = [t for t in (await cdp.call("Target.getTargets"))["targetInfos"] if t["type"] == "page"]
    if pages:
        tid = pages[0]["targetId"]
    else:
        tid = (await cdp.call("Target.createTarget", {"url": "about:blank"}))["targetId"]
    sid = (await cdp.call("Target.attachToTarget", {"targetId": tid, "flatten": True}))["sessionId"]
    await cdp.call("Page.enable", sid=sid)
    return cdp, sid, first


async def navigate(cdp, sid, url, wait=30):
    while not cdp.events.empty():
        cdp.events.get_nowait()
    t0 = time.perf_counter()
    await cdp.call("Page.navigate", {"url": url}, sid=sid)
    end = time.time() + wait
    while time.time() < end:
        try:
            m = await asyncio.wait_for(cdp.events.get(), 1)
        except asyncio.TimeoutError:
            continue
        if m.get("method") == "Page.loadEventFired":
            return (time.perf_counter() - t0) * 1000
    return None


async def evaluate(cdp, sid, expr):
    r = await cdp.call("Runtime.evaluate", {"expression": expr, "returnByValue": True}, sid=sid)
    return r.get("result", {}).get("value")


def ms(t0):
    return (time.perf_counter() - t0) * 1000


async def one_run(kind, prov, out):
    prof = None
    try:
        t = time.perf_counter(); prof = prov.create_profile(f"bench_{int(time.time())}"); out["create_profile_ms"] = ms(t)
        t0 = time.perf_counter()
        s = prov.start_session("bench", prof, timeout_s=600); out["start_api_ms"] = ms(t0)
        prov.wait_ready(s); out["ready_ms"] = ms(t0)
        url, headers = prov.automation_connection(s)
        cdp, sid, out["first_cdp_ms"] = await attach(url)
        out["time_to_usable_ms"] = ms(t0)
        rtts = []
        for _ in range(30):
            t = time.perf_counter(); await cdp.call("Runtime.evaluate", {"expression": "1+1"}, sid=sid); rtts.append(ms(t))
        rtts.sort(); out["rtt_p50_ms"], out["rtt_p95_ms"] = rtts[14], rtts[28]
        for name, u in PAGES:
            out[f"nav_{name}_ms"] = await navigate(cdp, sid, u)
        shots = []
        for _ in range(3):
            t = time.perf_counter(); await cdp.call("Page.captureScreenshot", {"format": "jpeg", "quality": 60}, sid=sid); shots.append(ms(t))
        out["screenshot_ms"] = statistics.median(shots)
        t = time.perf_counter(); prov.live_view_url(s); out["live_view_url_ms"] = ms(t)
        await navigate(cdp, sid, "https://www.google.com/search?q=python+programming")
        out["google_blocked"] = "/sorry/" in (await evaluate(cdp, sid, "location.href") or "") or "unusual traffic" in (await evaluate(cdp, sid, "document.body.innerText.slice(0,2000)") or "").lower()
        await navigate(cdp, sid, "https://bot.sannysoft.com")
        await asyncio.sleep(2)
        out["sannysoft_failed_checks"] = await evaluate(cdp, sid, "document.querySelectorAll('td.failed').length")
        out["webdriver_flag"] = await evaluate(cdp, sid, "navigator.webdriver")
        await navigate(cdp, sid, "https://example.com")
        await cdp.call("Network.enable", sid=sid)
        await cdp.call("Network.setCookie", {"name": "bench", "value": "ok", "domain": "example.com", "path": "/", "secure": True, "expires": time.time() + 86400}, sid=sid)
        await cdp.close()
        t = time.perf_counter(); prov.stop_session(s); out["stop_ms"] = ms(t)
        time.sleep(float(os.getenv("POST_STOP_WAIT", "3")))
        t0 = time.perf_counter()
        s2 = prov.start_session("bench", prof, timeout_s=600); prov.wait_ready(s2); out["reopen_ready_ms"] = ms(t0)
        url, _ = prov.automation_connection(s2)
        cdp, sid, _ = await attach(url)
        out["reopen_usable_ms"] = ms(t0)
        await navigate(cdp, sid, "https://example.com")
        cookies = (await cdp.call("Network.getCookies", {"urls": ["https://example.com"]}, sid=sid))["cookies"]
        out["cookie_persisted"] = any(c["name"] == "bench" for c in cookies)
        await cdp.close()
        prov.stop_session(s2)
        out["ok"] = True
    except Exception as e:                      # one failed run is a data point (reliability), not a crash
        out["ok"], out["error"] = False, f"{type(e).__name__}: {str(e)[:160]}"
    finally:
        if prof:
            try:
                prov.delete_profile(prof)
            except Exception:
                pass


def pct(vals, p):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    return vals[min(len(vals) - 1, int(round(p * (len(vals) - 1))))]


def report(results, kinds):
    metrics = ["time_to_usable_ms", "start_api_ms", "ready_ms", "first_cdp_ms", "rtt_p50_ms", "rtt_p95_ms", "nav_example.com_ms",
               "nav_wikipedia_ms", "nav_hacker_news_ms", "screenshot_ms", "live_view_url_ms", "stop_ms", "reopen_usable_ms"]
    lines = ["| metric (ms, median / worst) | " + " | ".join(kinds) + " |", "|---|" + "---|" * len(kinds)]
    for m in metrics:
        row = [m.replace("_ms", "")]
        for k in kinds:
            v = [r.get(m) for r in results[k] if r.get("ok")]
            med = pct(v, 0.5)
            row.append("n/a" if med is None else f"{med:,.0f} / {pct(v, 1.0):,.0f}")
        lines.append("| " + " | ".join(row) + " |")
    for label, key in [("runs ok", None), ("cookie persisted", "cookie_persisted"), ("google blocked", "google_blocked"),
                       ("sannysoft failed checks (median)", "sannysoft_failed_checks"), ("navigator.webdriver", "webdriver_flag")]:
        row = [label]
        for k in kinds:
            rs = results[k]
            if key is None:
                row.append(f"{sum(1 for r in rs if r.get('ok'))}/{len(rs)}")
            elif key == "sannysoft_failed_checks":
                row.append(str(pct([r.get(key) for r in rs if r.get("ok")], 0.5)))
            else:
                row.append(f"{sum(1 for r in rs if r.get(key))}/{sum(1 for r in rs if r.get('ok'))}")
        lines.append("| " + " | ".join(row) + " |")
    errs = [(k, r["error"]) for k in kinds for r in results[k] if not r.get("ok")]
    return "\n".join(lines) + ("\n\nerrors:\n" + "\n".join(f"  {k}: {e}" for k, e in errs) if errs else "")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--providers", nargs="+", default=["browserbase", "browseruse"])
    ap.add_argument("--runs", type=int, default=3)
    a = ap.parse_args()
    provs = {k: make(k) for k in a.providers}
    results = {k: [] for k in a.providers}
    for i in range(a.runs):
        for k in a.providers:
            out = {}
            print(f"round {i + 1}/{a.runs}  {k} ...", flush=True)
            await one_run(k, provs[k], out)
            print(f"   ok={out.get('ok')} usable={out.get('time_to_usable_ms', 0):.0f}ms {out.get('error', '')}", flush=True)
            results[k].append(out)
    print("\n" + report(results, a.providers))
    path = os.getenv("BENCH_OUT", "/tmp/bench_results.json")
    json.dump(results, open(path, "w"), indent=1, default=str)
    print(f"\nraw results: {path}")

if __name__ == "__main__":
    asyncio.run(main())
