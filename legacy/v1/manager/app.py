"""Session manager (local lifecycle prototype).

One user = one browser session, started on demand and released when the user goes away:

  open     -> take the user's lock, restore their saved state, start Chromium + agent, hand back a URL
  idle     -> (SUSPEND_AFTER s without a heartbeat) pause both containers: instant resume, like a microVM suspend
  release  -> (RELEASE_AFTER s suspended, or the app was closed) save state, quit Chromium cleanly, remove everything
  reopen   -> state restored into a fresh session: cookies, localStorage, IndexedDB, open tabs

State lives behind StateStore. Today that is an encrypted local folder; on AWS the same interface is S3 (+KMS).
"""
import asyncio
import gzip
import io
import json
import os
import re
import secrets
import tarfile
import time
import types
import urllib.request
from contextlib import asynccontextmanager

import docker
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

BROWSER_IMAGE = os.getenv("BROWSER_IMAGE", "cba-legacy-browser")
AGENT_IMAGE = os.getenv("AGENT_IMAGE", "cba-legacy-deepagent")
HOST_WEB_DIR = os.getenv("HOST_WEB_DIR", "")            # absolute path of ./web on the Docker host
NETWORK = os.getenv("NETWORK", "lifecycle-net")
PUBLIC_PORT = os.getenv("PUBLIC_PORT", "8090")
SUSPEND_AFTER = int(os.getenv("SUSPEND_AFTER", "60"))    # seconds without a heartbeat -> pause
RELEASE_AFTER = int(os.getenv("RELEASE_AFTER", "180"))   # seconds paused -> save and release
BYE_GRACE = int(os.getenv("BYE_GRACE", "10"))            # seconds after the app was closed -> save and release
STATE_DIR = os.getenv("STATE_DIR", "/state")
PASSTHROUGH = ["OPENAI_API_KEY", "MAIN_MODEL", "VISION_MODEL"]

# What we keep per user. Caches are left out on purpose: they are big and rebuildable.
SLIM_PATHS = [
    "cookie-vault.json", "clean-exit",
    "profile/Local State",
    "profile/Default/Preferences", "profile/Default/Secure Preferences",
    "profile/Default/Network", "profile/Default/Cookies",
    "profile/Default/Local Storage", "profile/Default/IndexedDB",
    "profile/Default/Session Storage", "profile/Default/Sessions",
]

dc = docker.from_env(version="auto")
S = {}                       # sid -> session
USER_LOCKS = {}              # user -> asyncio.Lock  (one browser per user)
ST = types.SimpleNamespace(reaper=None)


# ---------------------------------------------------------------- state store (swap for S3 on AWS)
class LocalStore:
    """Encrypted blob per user in a folder. The S3 version keeps the same four methods."""
    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)
        key = os.getenv("STATE_KEY")
        keyfile = os.path.join(root, ".key")
        if not key:
            if os.path.exists(keyfile):
                key = open(keyfile).read().strip()
            else:  # POC convenience only: on AWS the key comes from KMS, never from next to the data
                key = Fernet.generate_key().decode()
                fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(key)
        self.f = Fernet(key.encode())

    def _p(self, user, ext): return os.path.join(self.root, f"{user}.{ext}")

    def get(self, user):
        try:
            with open(self._p(user, "state"), "rb") as f:
                return self.f.decrypt(f.read())
        except FileNotFoundError:
            return None

    def put(self, user, blob: bytes, meta: dict):
        with open(self._p(user, "state"), "wb") as f:
            f.write(self.f.encrypt(blob))
        meta = {**meta, "user": user, "size": len(blob), "saved_at": time.time()}
        with open(self._p(user, "json"), "w") as f:
            json.dump(meta, f)

    def meta(self, user):
        try:
            return json.load(open(self._p(user, "json")))
        except (FileNotFoundError, ValueError):
            return {}

    def delete(self, user):
        for ext in ("state", "json"):
            try: os.remove(self._p(user, ext))
            except FileNotFoundError: pass

    def list(self):
        out = []
        for fn in sorted(os.listdir(self.root)):
            if fn.endswith(".json"):
                try: out.append(json.load(open(os.path.join(self.root, fn))))
                except ValueError: pass
        return out


store = LocalStore(STATE_DIR)


# ---------------------------------------------------------------- docker helpers (blocking: run in threads)
def http_json(url, method="GET", timeout=5):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return json.loads(body) if body else {}


def names(sid): return f"lc-browser-{sid}", f"lc-agent-{sid}", f"lc-prof-{sid}"


def container_ip(c):
    c.reload()
    return c.attrs["NetworkSettings"]["Networks"][NETWORK]["IPAddress"]


def restore_into_volume(vol, blob):
    helper = dc.containers.create(BROWSER_IMAGE, entrypoint="true", volumes={vol: {"bind": "/data", "mode": "rw"}})
    try:
        helper.put_archive("/data", gzip.decompress(blob))
    finally:
        helper.remove(force=True)


def slim_archive(vol) -> bytes:
    """Tar+gzip the slim paths. The helper writes a file and we fetch it with get_archive: binary-safe
    (reading the container's stdout through the SDK decodes it as text and corrupts the gzip)."""
    script = 'cd /data && for p in "$@"; do [ -e "$p" ] && printf "%s\\0" "$p"; done | tar czf /tmp/slim.tgz --null -T -'
    c = dc.containers.create(BROWSER_IMAGE, entrypoint=["sh", "-c"], command=[script, "sh", *SLIM_PATHS],
                             volumes={vol: {"bind": "/data", "mode": "ro"}})
    try:
        c.start()
        if c.wait()["StatusCode"] != 0:
            raise RuntimeError("could not archive the profile")
        stream, _ = c.get_archive("/tmp/slim.tgz")
        with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as t:
            blob = t.extractfile("slim.tgz").read()
        gzip.decompress(blob)          # refuse to store anything that is not a valid archive
        return blob
    finally:
        c.remove(force=True)


def wait_http(url, ok, timeout, host=None):
    end = time.time() + timeout
    while time.time() < end:
        try:
            req = urllib.request.Request(url, headers={"Host": host} if host else {})
            with urllib.request.urlopen(req, timeout=3) as r:
                if ok(r.read()):
                    return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def start_containers(sid, user, blob):
    t0 = time.time()
    bname, aname, vol = names(sid)
    dc.volumes.create(vol)
    restored = False
    if blob:
        restore_into_volume(vol, blob)
        restored = True
    t_restore = time.time()
    host = f"s-{sid}.localhost"
    labels = {
        "lifecycle.managed": "1", "lifecycle.sid": sid, "lifecycle.user": user,
        "traefik.enable": "true",
        f"traefik.http.routers.{bname}.rule": f"Host(`{host}`)",
        f"traefik.http.routers.{bname}.entrypoints": "web",
        f"traefik.http.services.{bname}.loadbalancer.server.port": "8080",
        "traefik.docker.network": NETWORK,
    }
    volumes = {vol: {"bind": "/data", "mode": "rw"}}
    if HOST_WEB_DIR:
        volumes[HOST_WEB_DIR] = {"bind": "/srv/web", "mode": "ro"}
    b = dc.containers.run(BROWSER_IMAGE, name=bname, detach=True, labels=labels, network=NETWORK, volumes=volumes,
                          shm_size="256m", mem_limit="1g", nano_cpus=int(1.5e9),
                          environment={"START_URL": "https://www.google.com"})
    ip = container_ip(b)
    if not wait_http(f"http://{ip}:8080/cdp/json/version", lambda _b: True, 60):
        raise RuntimeError("browser did not start")
    t_browser = time.time()
    env = {k: os.environ[k] for k in PASSTHROUGH if os.getenv(k)}
    dc.containers.run(AGENT_IMAGE, name=aname, detach=True, labels={"lifecycle.managed": "1", "lifecycle.sid": sid, "lifecycle.user": user},
                      network_mode=f"container:{b.id}", volumes={vol: {"bind": "/data", "mode": "rw"}},
                      mem_limit="1g", environment=env)
    ok = wait_http(f"http://{ip}:8080/agent/health", lambda body: b'"browser_connected":true' in body.replace(b" ", b""), 90)
    t_agent = time.time()
    # the route is registered by Traefik a moment after the container starts: wait until it answers
    wait_http(f"http://traefik/cdp/json/version", lambda _b: True, 15, host=host)
    t_route = time.time()
    return {"ip": ip, "restored": restored, "agent_ok": ok,
            "ms": {"restore": int((t_restore - t0) * 1000), "browser": int((t_browser - t_restore) * 1000),
                   "agent": int((t_agent - t_browser) * 1000), "route": int((t_route - t_agent) * 1000),
                   "total": int((t_route - t0) * 1000)}}


def fetch_tabs(sid):
    try:
        ip = container_ip(dc.containers.get(names(sid)[0]))
        return [{"url": t["url"], "title": t.get("title", "")} for t in http_json(f"http://{ip}:8080/cdp/json/list", timeout=3)
                if t.get("type") == "page"]
    except Exception:
        return []


def restore_tabs(ip, tabs):
    """Open saved tabs that Chromium's own session restore did not bring back (it can't after a hard kill)."""
    want = [t["url"] for t in tabs if t["url"].startswith("http")]
    if not want:
        return 0
    time.sleep(2)  # let Chromium finish restoring its own tabs first
    have = [t["url"].rstrip("/") for t in http_json(f"http://{ip}:8080/cdp/json/list") if t.get("type") == "page"]
    opened = 0
    for u in want:
        if u.rstrip("/") not in have:
            http_json(f"http://{ip}:8080/cdp/json/new?{u}", "PUT")
            opened += 1
    return opened


def pause_containers(sid):
    for n in names(sid)[:2]:
        try: dc.containers.get(n).pause()
        except Exception: pass


def unpause_containers(sid):
    for n in names(sid)[:2]:
        try: dc.containers.get(n).unpause()
        except Exception: pass


def save_and_remove(sid, user, last_tabs=()):
    bname, aname, vol = names(sid)
    unpause_containers(sid)
    b = dc.containers.get(bname)
    ip = container_ip(b)
    tabs = []
    try:
        http_json(f"http://{ip}:8080/agent/cookies/save", "POST", 10)
    except Exception:
        pass
    try:
        tabs = [{"url": t["url"], "title": t.get("title", "")} for t in http_json(f"http://{ip}:8080/cdp/json/list")
                if t.get("type") == "page"]
    except Exception:
        pass
    tabs = tabs or list(last_tabs)
    try: dc.containers.get(aname).stop(timeout=10)
    except Exception: pass
    b.stop(timeout=40)               # start.sh asks Chromium to quit properly, so cookies and storage are flushed
    b.reload()
    clean = b.attrs["State"]["ExitCode"] == 0
    blob = slim_archive(vol)
    for n in (aname, bname):
        try: dc.containers.get(n).remove(force=True)
        except Exception: pass
    try: dc.volumes.get(vol).remove(force=True)
    except Exception: pass
    return blob, {"tabs": tabs, "clean_exit": clean}


# ---------------------------------------------------------------- sessions
def info(s):
    now = time.time()
    return {"sid": s.sid, "user": s.user, "state": s.state, "url": f"http://s-{s.sid}.localhost:{PUBLIC_PORT}/",
            "idle_s": int(now - s.last_seen), "age_s": int(now - s.created), "restored": s.restored, "ms": s.ms,
            "suspended_for_s": int(now - s.suspended_at) if s.suspended_at else None}


async def open_session(user):
    lock = USER_LOCKS.setdefault(user, asyncio.Lock())
    async with lock:                                   # one browser per user: two would log each other out
        for s in S.values():
            if s.user == user:
                if s.state == "suspended":
                    await asyncio.to_thread(unpause_containers, s.sid)
                    s.state, s.suspended_at = "running", None
                s.last_seen, s.bye_at = time.time(), None
                return s
        sid = secrets.token_hex(4)
        s = types.SimpleNamespace(sid=sid, user=user, state="starting", created=time.time(), last_seen=time.time(),
                                  suspended_at=None, bye_at=None, restored=False, ms={}, tabs=[])
        S[sid] = s
        try:
            blob = await asyncio.to_thread(store.get, user)
            res = await asyncio.to_thread(start_containers, sid, user, blob)
        except Exception:
            S.pop(sid, None)
            await asyncio.to_thread(cleanup_leftovers, sid)
            raise
        if blob:
            try:
                await asyncio.to_thread(restore_tabs, res["ip"], (await asyncio.to_thread(store.meta, user)).get("tabs", []))
            except Exception as e:
                print("tab restore failed:", repr(e), flush=True)
        s.restored, s.ms, s.state, s.last_seen = res["restored"], res["ms"], "running", time.time()
        return s


def cleanup_leftovers(sid):
    for n in names(sid)[:2]:
        try: dc.containers.get(n).remove(force=True)
        except Exception: pass
    try: dc.volumes.get(names(sid)[2]).remove(force=True)
    except Exception: pass


async def release_session(s):
    lock = USER_LOCKS.setdefault(s.user, asyncio.Lock())
    async with lock:
        if s.sid not in S or s.state == "saving":
            return
        s.state = "saving"
        try:
            blob, meta = await asyncio.to_thread(save_and_remove, s.sid, s.user, s.tabs)
            await asyncio.to_thread(store.put, s.user, blob, meta)
        finally:
            S.pop(s.sid, None)


def agent_busy(s):
    try:
        ip = container_ip(dc.containers.get(names(s.sid)[0]))
        h = http_json(f"http://{ip}:8080/agent/health", timeout=3)
        return bool(h.get("busy"))
    except Exception:
        return False


async def reaper():
    while True:
        await asyncio.sleep(5)
        now = time.time()
        for s in list(S.values()):
            try:
                if s.state == "running":
                    s.tabs = await asyncio.to_thread(fetch_tabs, s.sid) or s.tabs
                    closing = s.bye_at and now - s.bye_at >= BYE_GRACE and s.last_seen <= s.bye_at
                    idle = now - s.last_seen >= SUSPEND_AFTER
                    if (closing or idle) and not await asyncio.to_thread(agent_busy, s):
                        if closing:
                            await release_session(s)
                        else:
                            await asyncio.to_thread(pause_containers, s.sid)
                            s.state, s.suspended_at = "suspended", now
                elif s.state == "suspended" and now - s.suspended_at >= RELEASE_AFTER:
                    await release_session(s)
            except Exception as e:
                print("reaper error:", sid_of(s), repr(e), flush=True)


def sid_of(s): return getattr(s, "sid", "?")


def reconcile():
    """After a manager restart, pick up the sessions that are still running."""
    for c in dc.containers.list(filters={"label": "lifecycle.managed=1", "name": "lc-browser-"}):
        sid, user = c.labels["lifecycle.sid"], c.labels["lifecycle.user"]
        paused = c.status == "paused"
        S[sid] = types.SimpleNamespace(sid=sid, user=user, state="suspended" if paused else "running", created=time.time(),
                                       last_seen=time.time(), suspended_at=time.time() if paused else None, bye_at=None,
                                       restored=False, ms={}, tabs=[])


@asynccontextmanager
async def lifespan(_app):
    await asyncio.to_thread(reconcile)
    ST.reaper = asyncio.create_task(reaper())
    yield
    ST.reaper.cancel()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origin_regex=r"http://(s-[0-9a-f]+\.)?localhost(:\d+)?",
                   allow_methods=["GET", "POST"], allow_headers=["*"])


def clean_user(raw):
    u = re.sub(r"[^a-z0-9_-]", "", (raw or "").lower())[:32]
    return u or None


@app.get("/")
async def dashboard():
    return FileResponse("/app/static/index.html")


@app.get("/api/config")
async def config():
    return {"suspend_after": SUSPEND_AFTER, "release_after": RELEASE_AFTER, "bye_grace": BYE_GRACE, "port": PUBLIC_PORT}


@app.get("/api/sessions")
async def sessions():
    return {"sessions": [info(s) for s in S.values()]}


@app.get("/api/state")
async def states():
    return {"states": await asyncio.to_thread(store.list)}


@app.post("/api/sessions")
async def open_(req: Request):
    user = clean_user((await req.json()).get("user"))
    if not user:
        return JSONResponse({"error": "user name required (letters, digits, - and _)"}, status_code=400)
    try:
        s = await open_session(user)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return info(s)


@app.post("/api/sessions/{sid}/heartbeat")
async def heartbeat(sid: str):
    s = S.get(sid)
    if not s or s.state in ("saving",):
        return JSONResponse({"error": "ended"}, status_code=404)
    if s.state == "suspended":
        await asyncio.to_thread(unpause_containers, sid)
        s.state, s.suspended_at = "running", None
    s.last_seen, s.bye_at = time.time(), None
    return {"state": s.state}


@app.post("/api/sessions/{sid}/bye")
async def bye(sid: str):
    s = S.get(sid)
    if s:
        s.bye_at = time.time()
    return {"ok": True}


@app.post("/api/sessions/{sid}/release")
async def release(sid: str):
    s = S.get(sid)
    if not s:
        return JSONResponse({"error": "no such session"}, status_code=404)
    await release_session(s)
    return {"ok": True}


@app.delete("/api/state/{user}")
async def delete_state(user: str):
    u = clean_user(user)
    if any(s.user == u for s in S.values()):
        return JSONResponse({"error": "user has a live session; release it first"}, status_code=409)
    await asyncio.to_thread(store.delete, u)
    return {"ok": True}
