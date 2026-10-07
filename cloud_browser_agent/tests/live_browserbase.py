"""LIVE test against Browserbase (uses real quota, a few minutes of browser time).
Checks the whole lifecycle through SessionService: open -> set a login cookie -> release -> open again -> cookie is back.
Run in Docker:  see run_live_browserbase.sh   (keys come from the environment, never printed)."""
import asyncio, json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import websockets
from urllib.parse import urlparse
from cloud_browser_agent.control.core import SessionService
from cloud_browser_agent.control.store import SqliteStore
from cloud_browser_agent.provider.browserbase import BrowserbaseProvider

prov = BrowserbaseProvider(os.environ["BROWSERBASE_API_KEY"], os.environ["BROWSERBASE_PROJECT_ID"])
svc = SessionService(prov, SqliteStore(":memory:"), strategy="overwrite", session_timeout_s=300)
USER = "live_test_user"


async def cdp(url, fn):
    async with websockets.connect(url, max_size=None) as ws:
        n = 0
        async def call(method, params=None, sid=None):
            nonlocal n; n += 1
            m = {"id": n, "method": method, "params": params or {}}
            if sid: m["sessionId"] = sid
            await ws.send(json.dumps(m))
            while True:
                r = json.loads(await ws.recv())
                if r.get("id") == n:
                    if "error" in r: raise RuntimeError(r["error"])
                    return r["result"]
        pages = [x for x in (await call("Target.getTargets"))["targetInfos"] if x["type"] == "page"]
        print("pages:", [(x["url"][:40], x.get("browserContextId", "")[:6]) for x in pages])
        sid = (await call("Target.attachToTarget", {"targetId": pages[0]["targetId"], "flatten": True}))["sessionId"]
        await call("Page.enable", sid=sid)
        await call("Page.navigate", {"url": "https://example.com"}, sid=sid)
        await asyncio.sleep(3)
        return await fn(call, sid)


def run(fn):
    url, headers = svc.connection(USER)
    return asyncio.run(cdp(url, fn))


t0 = time.time()
a = svc.open(USER); print(f"open: {time.time()-t0:.1f}s restored={a['restored']}")
view = svc.live_view(USER); print("live view host:", urlparse(view).hostname)

async def setc(call, sid):
    await call("Network.enable", sid=sid)
    r = await call("Network.setCookie", {"name": "bb_login", "value": "persist-ok", "domain": "example.com",
                                          "path": "/", "secure": True, "expires": time.time() + 86400}, sid=sid)
    await call("Runtime.evaluate", {"expression": "localStorage.setItem('bb_ls','ls-ok'); document.cookie='bb_js=js-ok; max-age=86400; path=/'"}, sid=sid)
    return r
print("set cookie:", run(setc))
time.sleep(float(os.getenv("PRE_RELEASE_WAIT","0")))
t0 = time.time(); print("release:", svc.release(USER), f"{time.time()-t0:.1f}s")
print("record:", svc.store.get(USER))
time.sleep(float(os.getenv("POST_RELEASE_WAIT","3")))
t0 = time.time(); b = svc.open(USER); print(f"reopen: {time.time()-t0:.1f}s restored={b['restored']}")

async def getc(call, sid):
    ls = await call("Runtime.evaluate", {"expression": "localStorage.getItem('bb_ls')"}, sid=sid)
    print("localStorage:", ls["result"].get("value"))
    allc = (await call("Network.getAllCookies", sid=sid))["cookies"]
    print("all cookies:", [c["name"] for c in allc])
    return (await call("Network.getCookies", {"urls": ["https://example.com"]}, sid=sid))["cookies"]
cs = run(getc); print("cookies after reopen:", [(c["name"], c["value"]) for c in cs])
ok = any(c["name"] == "bb_login" and c["value"] == "persist-ok" for c in cs)
svc.release(USER)
prov.delete_profile(svc.store.get(USER)["profile_id"]) if svc.store.get(USER).get("profile_id") else None
print("RESULT:", "PASS" if ok else "FAIL"); sys.exit(0 if ok else 1)
