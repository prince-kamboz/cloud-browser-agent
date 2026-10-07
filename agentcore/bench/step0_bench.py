"""Step 0: answer the open AgentCore Browser questions against a real account. Costs cents; cleans up after itself.

  docker run --rm --env-file agentcore/.env.aws -v "$PWD":/w -w /w python:3.12-slim \
     bash -c "pip install -q boto3 playwright && python -m agentcore.bench.step0_bench --region us-east-1"

(Playwright only drives a remote browser here, so no browser download is needed.)  Prints a report and writes bench_report.json.
Questions answered:
  1. startup: API return, READY, first CDP command, egress IP (does it change per session?), stop
  2. profiles: does a second save overwrite? what survives: persistent cookie, session cookie, localStorage,
     sessionStorage, IndexedDB; how long does a save and a profile-start take
  3. does a client reconnect to the same live session after disconnecting?
  4. N sessions at once: time to READY, any throttling
"""
import argparse
import json
import statistics
import sys
import threading
import time

from playwright.sync_api import sync_playwright

from agentcore.provider.agentcore import AgentCoreProvider
from agentcore.provider.base import ProviderError

SET_JS = """async (v) => {
  document.cookie = 'pers=' + v + '; path=/; max-age=86400';
  document.cookie = 'sess=' + v + '; path=/';
  localStorage.setItem('ls', v); sessionStorage.setItem('ss', v);
  await new Promise((res, rej) => { const r = indexedDB.open('bench', 1);
    r.onupgradeneeded = () => r.result.createObjectStore('kv');
    r.onsuccess = () => { const t = r.result.transaction('kv', 'readwrite'); t.objectStore('kv').put(v, 'k'); t.oncomplete = res; t.onerror = rej; };
    r.onerror = rej; });
}"""
GET_JS = """async () => {
  const c = Object.fromEntries(document.cookie.split('; ').filter(Boolean).map(x => x.split('=')));
  const idb = await new Promise((res) => { const r = indexedDB.open('bench', 1);
    r.onupgradeneeded = () => { r.transaction.abort(); res(null); };
    r.onsuccess = () => { const g = r.result.transaction('kv').objectStore('kv').get('k'); g.onsuccess = () => res(g.result || null); g.onerror = () => res(null); };
    r.onerror = () => res(null); });
  return { pers: c.pers || null, sess: c.sess || null, ls: localStorage.getItem('ls'), ss: sessionStorage.getItem('ss'), idb };
}"""


def ms(t0): return int((time.time() - t0) * 1000)


def stats(xs): return {"n": len(xs), "min": min(xs), "median": int(statistics.median(xs)), "max": max(xs)} if xs else {}


class Remote:
    """A started session plus a Playwright connection to it."""
    def __init__(self, prov, pw, profile=None, viewport=(1280, 800), timeout=600):
        self.p, self.pw = prov, pw
        self.t = {}
        t0 = time.time(); self.s = prov.start_session("bench", profile, timeout, viewport); self.t["api_ms"] = ms(t0)
        prov.wait_ready(self.s); self.t["ready_ms"] = ms(t0)
        self.connect(); self.t["first_cmd_ms"] = ms(t0)

    def connect(self):
        ws, headers = self.p.automation_connection(self.s)
        self.browser = self.pw.chromium.connect_over_cdp(ws, headers=headers)
        self.ctx = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.goto("about:blank")

    def disconnect(self):
        try: self.browser.close()
        except Exception: pass

    def stop(self):
        self.disconnect()
        try: self.p.stop_session(self.s)
        except ProviderError as e: print("  stop failed:", e)


def t_startup(prov, pw, n):
    rows, ips = [], []
    for i in range(n):
        r = Remote(prov, pw)
        try:
            r.page.goto("https://api.ipify.org", timeout=30000)
            ips.append(r.page.inner_text("body").strip())
        except Exception as e:
            ips.append(f"error: {e}")
        t0 = time.time(); r.stop(); r.t["stop_ms"] = ms(t0)
        rows.append(r.t); print(f"  startup {i+1}/{n}: {r.t}")
    return {"runs": rows, "api_ms": stats([r["api_ms"] for r in rows]), "ready_ms": stats([r["ready_ms"] for r in rows]),
            "first_cmd_ms": stats([r["first_cmd_ms"] for r in rows]), "egress_ips": ips, "distinct_ips": len(set(ips))}


def t_profiles(prov, pw):
    out, pid = {}, None
    try:
        pid = prov.create_profile("bench_profile")
        out["created"] = True
    except ProviderError as e:
        return {"created": False, "error": str(e), "note": "profile creation is blocked on this account"}
    try:
        a = Remote(prov, pw); a.page.goto("https://example.com"); a.page.evaluate(SET_JS, "A1")
        t0 = time.time(); prov.save_profile(a.s, pid); out["first_save_ms"] = ms(t0); a.stop()
        b = Remote(prov, pw, profile=pid); out["start_with_profile"] = b.t
        b.page.goto("https://example.com"); out["after_first_save"] = b.page.evaluate(GET_JS)
        b.page.evaluate(SET_JS, "B1")
        try:
            t0 = time.time(); prov.save_profile(b.s, pid); out["second_save_ms"] = ms(t0); out["second_save"] = "accepted"
        except ProviderError as e:
            out["second_save"] = f"refused: {e}"
        b.stop()
        c = Remote(prov, pw, profile=pid); c.page.goto("https://example.com")
        out["after_second_save"] = c.page.evaluate(GET_JS)
        v = out["after_second_save"]["pers"]
        out["verdict"] = ("OVERWRITE: a second save replaces the data" if v == "B1" else
                          "WRITE-ONCE: the first save stays, use the rotate strategy" if v == "A1" else f"unexpected: {v}")
        c.stop()
    finally:
        try: prov.delete_profile(pid)
        except ProviderError as e: out["cleanup_error"] = str(e)
    return out


def t_reconnect(prov, pw):
    r = Remote(prov, pw)
    try:
        r.page.goto("https://example.com"); r.page.evaluate("window.__marker = 'still-here'")
        r.disconnect(); time.sleep(2)
        try:
            r.connect()
            marker = r.page.evaluate("window.__marker")
            return {"reconnect_works": True, "page_state_kept": marker == "still-here"}
        except Exception as e:
            return {"reconnect_works": False, "error": str(e)[:300]}
    finally:
        r.stop()


def t_concurrent(prov, m):
    results, sessions, lock = [], [], threading.Lock()
    def one():
        t0 = time.time()
        try:
            s = prov.start_session("bench", None, 300); prov.wait_ready(s)
            with lock: sessions.append(s); results.append({"ready_ms": ms(t0)})
        except ProviderError as e:
            with lock: results.append({"error": str(e)[:200]})
    ts = [threading.Thread(target=one) for _ in range(m)]
    [t.start() for t in ts]; [t.join() for t in ts]
    for s in sessions:
        try: prov.stop_session(s)
        except ProviderError: pass
    ok = [r["ready_ms"] for r in results if "ready_ms" in r]
    return {"requested": m, "ready": len(ok), "ready_ms": stats(ok), "errors": [r["error"] for r in results if "error" in r][:3]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="us-east-1"); ap.add_argument("--startup-runs", type=int, default=5)
    ap.add_argument("--concurrent", type=int, default=10)
    a = ap.parse_args()
    prov, report = AgentCoreProvider(a.region), {"region": a.region}
    with sync_playwright() as pw:
        for name, fn in (("startup", lambda: t_startup(prov, pw, a.startup_runs)), ("profiles", lambda: t_profiles(prov, pw)),
                         ("reconnect", lambda: t_reconnect(prov, pw)), ("concurrent", lambda: t_concurrent(prov, a.concurrent))):
            print(f"== {name}")
            try:
                report[name] = fn()
            except Exception as e:                          # one failing test must not hide the others
                report[name] = {"failed": f"{type(e).__name__}: {e}"[:400]}
            print(json.dumps(report[name], indent=2))
    json.dump(report, open("bench_report.json", "w"), indent=2)
    print("\nwrote bench_report.json")


if __name__ == "__main__":
    sys.exit(main())
