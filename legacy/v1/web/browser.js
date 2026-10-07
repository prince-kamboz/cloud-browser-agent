// Controls the Chromium running in Docker through CDP (proxied by nginx at /cdp/).
import { CDP } from './cdp.js';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const MOD = { Alt: 1, Control: 2, Meta: 4, Shift: 8 };

// Map the key names the model uses ("CTRL", "ENTER", "a") to CDP key events.
function keyInfo(name) {
  const raw = String(name);
  const k = raw.toUpperCase().replace(/[\s_-]/g, '');
  const named = {
    ENTER: ['Enter', 'Enter', 13, '\r'], RETURN: ['Enter', 'Enter', 13, '\r'],
    TAB: ['Tab', 'Tab', 9], ESC: ['Escape', 'Escape', 27], ESCAPE: ['Escape', 'Escape', 27],
    BACKSPACE: ['Backspace', 'Backspace', 8], DELETE: ['Delete', 'Delete', 46], DEL: ['Delete', 'Delete', 46],
    SPACE: [' ', 'Space', 32, ' '], INSERT: ['Insert', 'Insert', 45],
    ARROWUP: ['ArrowUp', 'ArrowUp', 38], UP: ['ArrowUp', 'ArrowUp', 38],
    ARROWDOWN: ['ArrowDown', 'ArrowDown', 40], DOWN: ['ArrowDown', 'ArrowDown', 40],
    ARROWLEFT: ['ArrowLeft', 'ArrowLeft', 37], LEFT: ['ArrowLeft', 'ArrowLeft', 37],
    ARROWRIGHT: ['ArrowRight', 'ArrowRight', 39], RIGHT: ['ArrowRight', 'ArrowRight', 39],
    HOME: ['Home', 'Home', 36], END: ['End', 'End', 35],
    PAGEUP: ['PageUp', 'PageUp', 33], PAGEDOWN: ['PageDown', 'PageDown', 34],
    CTRL: ['Control', 'ControlLeft', 17], CONTROL: ['Control', 'ControlLeft', 17],
    SHIFT: ['Shift', 'ShiftLeft', 16], ALT: ['Alt', 'AltLeft', 18], OPTION: ['Alt', 'AltLeft', 18],
    META: ['Meta', 'MetaLeft', 91], CMD: ['Meta', 'MetaLeft', 91], COMMAND: ['Meta', 'MetaLeft', 91],
    SUPER: ['Meta', 'MetaLeft', 91], WIN: ['Meta', 'MetaLeft', 91],
  };
  if (named[k]) { const [key, code, keyCode, text] = named[k]; return { key, code, keyCode, text }; }
  const f = /^F(\d{1,2})$/.exec(k);
  if (f) return { key: 'F' + f[1], code: 'F' + f[1], keyCode: 111 + Number(f[1]) };
  if (raw.length === 1) {
    const ch = raw;
    const up = ch.toUpperCase();
    let code = '';
    if (/[A-Z]/.test(up)) code = 'Key' + up;
    else if (/[0-9]/.test(ch)) code = 'Digit' + ch;
    const keyCode = /[A-Z0-9]/.test(up) ? up.charCodeAt(0) : 0;
    return { key: ch, code, keyCode, text: ch };
  }
  return { key: raw, code: raw, keyCode: 0 };
}

export class BrowserController {
  constructor({ base = '/cdp', onLog = () => {} } = {}) {
    this.base = base;
    this.cdp = null;
    this.targetId = null;
    this.knownTabs = new Set();
    this.onLog = onLog;
    this.mouse = { x: 0, y: 0 };
    this.onFrame = null; // set by the live view: called with { data, width, height } per screencast frame
  }

  wsUrl(targetId) {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    return `${proto}://${location.host}${this.base}/devtools/page/${targetId}`;
  }

  async json(path, method = 'GET') {
    const res = await fetch(`${this.base}/json/${path}`, { method });
    if (!res.ok) throw new Error(`CDP HTTP ${res.status} for ${path}`);
    const txt = await res.text();
    try { return JSON.parse(txt); } catch { return txt; }
  }

  async listTabs() {
    const list = await this.json('list');
    return list.filter((t) => t.type === 'page').map((t) => ({ id: t.id, title: t.title, url: t.url }));
  }

  async attach(targetId) {
    if (this.cdp) { this.cdp.close(); this.cdp = null; }
    const cdp = new CDP(this.wsUrl(targetId));
    await cdp.connect();
    this.cdp = cdp;
    this.targetId = targetId;
    await cdp.send('Page.enable');
    await cdp.send('Runtime.enable');
    // Never let alert()/confirm() block the agent.
    cdp.on('Page.javascriptDialogOpening', async (p) => {
      this.onLog(`Page dialog (${p.type}): "${p.message}" - auto-accepted`);
      try { await cdp.send('Page.handleJavaScriptDialog', { accept: true, promptText: p.defaultPrompt || '' }); } catch {}
    });
    try { await this.json(`activate/${targetId}`); } catch {}
    if (this.onFrame) await this.startScreencast();
  }

  // ---------- live view (replaces VNC) ----------
  // Chromium pushes JPEG frames only when the page changes; each frame must be acked.
  async startScreencast() {
    const cdp = this.cdp;
    if (!cdp) return;
    if (!cdp.screencastBound) {
      cdp.screencastBound = true;
      cdp.on('Page.screencastFrame', (p) => {
        cdp.send('Page.screencastFrameAck', { sessionId: p.sessionId }).catch(() => {});
        if (cdp === this.cdp) this.onFrame?.({ data: p.data, width: p.metadata.deviceWidth, height: p.metadata.deviceHeight });
      });
    }
    await cdp.send('Page.startScreencast', { format: 'jpeg', quality: 60, everyNthFrame: 1 }).catch(() => {});
  }

  async setLiveView(onFrame) {
    this.onFrame = onFrame;
    await this.ensure(); // attach() starts the screencast; it is idempotent if already attached
    await this.startScreencast();
  }

  // Raw input forwarded from the live view while the user has taken over.
  async rawKey(type, e) {
    await this.ensure();
    const text = type === 'keyDown' && e.key.length === 1 && !(e.modifiers & (MOD.Control | MOD.Meta | MOD.Alt)) ? e.key : undefined;
    await this.cdp.send('Input.dispatchKeyEvent', {
      type: text ? 'keyDown' : type === 'keyDown' ? 'rawKeyDown' : 'keyUp', key: e.key, code: e.code,
      windowsVirtualKeyCode: e.keyCode, modifiers: e.modifiers, ...(text ? { text, unmodifiedText: text } : {}),
    });
  }

  // ---------- cookies (the persistent profile keeps logins; the user can inspect and clear them) ----------
  async cookies() { await this.ensure(); return ((await this.cdp.send('Network.getAllCookies')).cookies || []).filter((c) => c.name !== '__agent_vault_alive'); }
  async clearCookies(domain) {
    await this.ensure();
    if (!domain) return this.cdp.send('Network.clearBrowserCookies');
    for (const c of await this.cookies()) if (c.domain === domain) await this.cdp.send('Network.deleteCookies', { name: c.name, domain: c.domain, path: c.path });
  }

  async insertText(text) { await this.ensure(); await this.cdp.send('Input.insertText', { text }); }

  async ensure() {
    if (this.cdp && !this.cdp.closed) return;
    let tabs = await this.listTabs();
    if (!tabs.length) {
      await this.json('new?about:blank', 'PUT');
      await sleep(300);
      tabs = await this.listTabs();
    }
    tabs.forEach((t) => this.knownTabs.add(t.id));
    const keep = tabs.find((t) => t.id === this.targetId) || tabs[0];
    await this.attach(keep.id);
  }

  // If an action opened a new tab (target=_blank etc.), follow it.
  async followNewTabs() {
    const tabs = await this.listTabs();
    const fresh = tabs.filter((t) => !this.knownTabs.has(t.id));
    tabs.forEach((t) => this.knownTabs.add(t.id));
    if (fresh.length) {
      await this.attach(fresh[0].id);
      this.onLog(`Switched to new tab: ${fresh[0].url}`);
      return true;
    }
    return false;
  }

  async evaluate(expression, { awaitPromise = true, timeoutMs = 15000 } = {}) {
    await this.ensure();
    const r = await this.cdp.send('Runtime.evaluate', { expression, awaitPromise, returnByValue: true, userGesture: true }, timeoutMs);
    if (r.exceptionDetails) {
      const d = r.exceptionDetails;
      throw new Error(d.exception?.description || d.text || 'JavaScript error');
    }
    return r.result?.value;
  }

  async pageInfo() {
    try { return await this.evaluate('({url: location.href, title: document.title, w: innerWidth, h: innerHeight})', { timeoutMs: 5000 }); }
    catch { return { url: '', title: '', w: 0, h: 0 }; }
  }

  async settle(maxMs = 4000) {
    await sleep(400);
    const end = Date.now() + maxMs;
    while (Date.now() < end) {
      try {
        await this.followNewTabs();
        const s = await this.evaluate('document.readyState', { timeoutMs: 2000 });
        if (s === 'complete') break;
      } catch { /* page is navigating */ }
      await sleep(250);
    }
    await sleep(200);
  }

  async screenshot() {
    await this.ensure();
    const r = await this.cdp.send('Page.captureScreenshot', { format: 'png' }, 20000);
    return 'data:image/png;base64,' + r.data;
  }

  async viewport() {
    const info = await this.pageInfo();
    return { width: info.w || 1280, height: info.h || 720 };
  }

  // ---------- mouse ----------
  async mouseEvent(type, x, y, extra = {}) {
    await this.ensure();
    await this.cdp.send('Input.dispatchMouseEvent', { type, x, y, ...extra });
    this.mouse = { x, y };
  }

  async click(x, y, button = 'left', clickCount = 1) {
    if (button === 'back') return this.goBack();
    if (button === 'forward') return this.goForward();
    const b = button === 'wheel' || button === 'middle' ? 'middle' : button === 'right' ? 'right' : 'left';
    await this.mouseEvent('mouseMoved', x, y);
    for (let i = 1; i <= clickCount; i++) {
      await this.mouseEvent('mousePressed', x, y, { button: b, clickCount: i });
      await this.mouseEvent('mouseReleased', x, y, { button: b, clickCount: i });
    }
  }

  async move(x, y) { await this.mouseEvent('mouseMoved', x, y); }

  async scroll(x, y, dx = 0, dy = 0) {
    await this.mouseEvent('mouseMoved', x, y);
    await this.mouseEvent('mouseWheel', x, y, { deltaX: dx, deltaY: dy });
  }

  async drag(path) {
    if (!path?.length) return;
    const pts = path.map((p) => (Array.isArray(p) ? { x: p[0], y: p[1] } : p));
    await this.mouseEvent('mouseMoved', pts[0].x, pts[0].y);
    await this.mouseEvent('mousePressed', pts[0].x, pts[0].y, { button: 'left', clickCount: 1 });
    for (const p of pts.slice(1)) { await this.mouseEvent('mouseMoved', p.x, p.y, { button: 'left', buttons: 1 }); await sleep(30); }
    const last = pts[pts.length - 1];
    await this.mouseEvent('mouseReleased', last.x, last.y, { button: 'left', clickCount: 1 });
  }

  // ---------- keyboard ----------
  async type(text) {
    await this.ensure();
    const parts = String(text).split('\n');
    for (let i = 0; i < parts.length; i++) {
      if (parts[i]) await this.cdp.send('Input.insertText', { text: parts[i] });
      if (i < parts.length - 1) await this.keypress(['Enter']);
    }
  }

  async keypress(keys) {
    await this.ensure();
    const list = (Array.isArray(keys) ? keys : [keys]).flatMap((k) => (typeof k === 'string' && k.length > 1 && k.includes('+') ? k.split('+') : [k]));
    const infos = list.map(keyInfo);
    let modifiers = 0;
    const mods = infos.filter((i) => MOD[i.key]);
    const main = infos.filter((i) => !MOD[i.key]);
    for (const m of mods) {
      modifiers |= MOD[m.key];
      await this.cdp.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: m.key, code: m.code, windowsVirtualKeyCode: m.keyCode, modifiers });
    }
    for (const k of main) {
      const withText = k.text && !(modifiers & (MOD.Control | MOD.Meta | MOD.Alt));
      const text = withText ? (modifiers & MOD.Shift ? k.text.toUpperCase() : k.text) : undefined;
      await this.cdp.send('Input.dispatchKeyEvent', {
        type: withText ? 'keyDown' : 'rawKeyDown', key: k.key, code: k.code,
        windowsVirtualKeyCode: k.keyCode, modifiers, ...(text ? { text, unmodifiedText: text } : {}),
      });
      await this.cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: k.key, code: k.code, windowsVirtualKeyCode: k.keyCode, modifiers });
    }
    for (const m of mods.reverse()) {
      modifiers &= ~MOD[m.key];
      await this.cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: m.key, code: m.code, windowsVirtualKeyCode: m.keyCode, modifiers });
    }
  }

  // ---------- navigation & tabs ----------
  async navigate(url) {
    await this.ensure();
    if (!/^[a-z]+:/i.test(url)) url = 'https://' + url;
    const loaded = this.cdp.once('Page.loadEventFired', 15000);
    const r = await this.cdp.send('Page.navigate', { url });
    if (r.errorText) throw new Error(`Navigation failed: ${r.errorText}`);
    await loaded;
    await this.settle();
  }

  async goBack() { await this.evaluate('history.back()', { awaitPromise: false }); await this.settle(); }
  async goForward() { await this.evaluate('history.forward()', { awaitPromise: false }); await this.settle(); }
  async reload() { await this.ensure(); await this.cdp.send('Page.reload'); await this.settle(); }

  async newTab(url) {
    const t = await this.json(`new?${url || 'about:blank'}`, 'PUT');
    this.knownTabs.add(t.id);
    await this.attach(t.id);
    await this.settle();
    return t.id;
  }

  async switchTab(id) {
    const tabs = await this.listTabs();
    if (!tabs.find((t) => t.id === id)) throw new Error('No tab with id ' + id);
    await this.attach(id);
  }

  async closeTab(id) {
    await this.json(`close/${id}`);
    this.knownTabs.delete(id);
    if (id === this.targetId) { this.cdp?.close(); this.cdp = null; await sleep(300); await this.ensure(); }
  }

  async pageText(maxChars = 15000) {
    const v = await this.evaluate(`(() => {
      const t = (document.body ? document.body.innerText : '').replace(/\\n{3,}/g, '\\n\\n');
      const links = [...document.querySelectorAll('a[href]')].slice(0, 60)
        .map(a => (a.innerText || '').trim().slice(0, 80) + ' -> ' + a.href).filter(s => !s.startsWith(' ->'));
      return { url: location.href, title: document.title, text: t, links };
    })()`);
    const text = v.text.length > maxChars ? v.text.slice(0, maxChars) + '\n...[truncated]' : v.text;
    return `URL: ${v.url}\nTitle: ${v.title}\n\n${text}\n\nLinks:\n${v.links.join('\n')}`;
  }
}
