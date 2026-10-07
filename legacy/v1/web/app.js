import { BrowserController } from './browser.js';

const $ = (id) => document.getElementById(id);
const els = {
  messages: $('messages'), input: $('input'), composer: $('composer'), send: $('sendBtn'), stop: $('stopBtn'),
  dot: $('statusDot'), statusText: $('statusText'), overlay: $('overlay'), takeover: $('takeoverBtn'),
  view: $('view'), viewer: document.querySelector('.viewer'), live: $('liveBadge'), back: $('backBtn'), fwd: $('fwdBtn'), reload: $('reloadBtn'),
};

// ---------- conversation id (the server keeps the memory per thread) ----------
let threadId = crypto.randomUUID();

// ---------- chat log ----------
// Small, safe markdown renderer for the agent's answers (HTML is escaped first, so nothing from a page can inject markup).
const esc = (t) => t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
function inline(t) {
  const codes = [];
  t = t.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
  t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
       .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>')
       .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');
  return t.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[i]}</code>`);
}
function md(src) {
  const out = [];
  const lines = esc(src.replace(/\r/g, '')).split('\n');
  for (let i = 0; i < lines.length;) {
    const l = lines[i];
    if (/^```/.test(l)) {                                   // code block
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++; out.push(`<pre><code>${buf.join('\n')}</code></pre>`); continue;
    }
    if (/^\s*\|.*\|\s*$/.test(l) && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1] || '')) {   // table
      const cells = (r) => r.trim().replace(/^\||\|$/g, '').split('|').map((c) => inline(c.trim()));
      const head = cells(l); i += 2; const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(cells(lines[i++]));
      out.push(`<table><thead><tr>${head.map((c) => `<th>${c}</th>`).join('')}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table>`);
      continue;
    }
    const h = /^(#{1,3})\s+(.*)$/.exec(l);
    if (h) { out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); i++; continue; }
    const li = /^\s*([-*]|\d+\.)\s+/.exec(l);
    if (li) {                                               // list
      const tag = /\d/.test(li[1]) ? 'ol' : 'ul'; const items = [];
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) items.push(`<li>${inline(lines[i++].replace(/^\s*([-*]|\d+\.)\s+/, ''))}</li>`);
      out.push(`<${tag}>${items.join('')}</${tag}>`); continue;
    }
    if (!l.trim()) { i++; continue; }
    const para = [];                                        // paragraph
    while (i < lines.length && lines[i].trim() && !/^(```|#{1,3}\s|\s*([-*]|\d+\.)\s+|\s*\|.*\|\s*$)/.test(lines[i])) para.push(inline(lines[i++]));
    out.push(`<p>${para.join('<br>')}</p>`);
  }
  return out.join('');
}

function addMsg(kind, text) {
  const div = document.createElement('div');
  div.className = `msg ${kind}`;
  if (kind === 'assistant') div.innerHTML = md(text); else div.textContent = text;
  els.messages.appendChild(div);
  els.messages.scrollTop = els.messages.scrollHeight;
  return div;
}

// ---------- status / takeover ----------
let state = 'idle';
const LABELS = { idle: 'Ready', running: 'Agent is working…', thinking: 'Thinking…', paused: 'You are in control (agent paused)', error: 'Browser not reachable' };
function setStatus(s) {
  state = s;
  els.dot.className = `dot ${s}`;
  els.statusText.textContent = LABELS[s] || s;
  const busy = s === 'running' || s === 'thinking' || s === 'paused';
  els.send.hidden = busy;
  els.stop.hidden = !busy;
  els.overlay.hidden = !(s === 'running' || s === 'thinking');
  els.takeover.hidden = !busy;
  els.takeover.classList.toggle('active', s === 'paused');
  els.takeover.textContent = s === 'paused' ? 'Hand back to agent' : 'Take over';
  const mine = s === 'paused' || s === 'idle', busyAgent = s === 'running' || s === 'thinking';
  els.viewer.classList.toggle('is-mine', mine);
  els.viewer.classList.toggle('is-busy', busyAgent);
  if (typeof tabstrip !== 'undefined') { tabsKey = ''; tabstrip.classList.toggle('locked', !(s === 'paused' || s === 'idle')); }
  for (const b of [els.back, els.fwd, els.reload]) b.disabled = !(s === 'paused' || s === 'idle');
  els.live.classList.toggle('on', s !== 'error');
  if (s === 'paused') els.view.focus();
}

const browser = new BrowserController({ onLog: (t) => addMsg('system tech', t) });

// ---------- agent (runs on the server; we only stream its events) ----------
const api = (path) => fetch(`/agent/${path}`, { method: 'POST' }).catch(() => {});
let abortRun = null;

// ---------- website approvals (like ChatGPT: Always ask / Auto approve / Always allow, plus per-site rules) ----------
function loadPolicy() {
  try {
    const p = JSON.parse(localStorage.getItem('sitePolicy') || '{}');
    return {
      mode: ['ask', 'auto', 'allow'].includes(p.mode) ? p.mode : 'ask',
      sites: (Array.isArray(p.sites) ? p.sites : []).filter((r) => r && r.pattern && ['allow', 'ask', 'block'].includes(r.rule)),
    };
  } catch { return { mode: 'ask', sites: [] }; }
}
let policy = loadPolicy();
const savePolicy = () => { try { localStorage.setItem('sitePolicy', JSON.stringify(policy)); } catch {} };

function addApproval(ev) {
  const div = document.createElement('div');
  div.className = 'msg approval';
  const q = document.createElement('div'); q.className = 'q'; q.textContent = `Allow the agent to open ${ev.host}?`;
  const u = document.createElement('div'); u.className = 'u'; u.textContent = ev.url;
  const row = document.createElement('div'); row.className = 'btns';
  const decide = async (decision, label) => {
    row.remove(); u.remove(); q.className = 'done'; q.textContent = `${label}: ${ev.host}`; div.classList.add('answered');
    if (decision === 'site' && !policy.sites.some((r) => r.pattern === ev.host)) { policy.sites.push({ pattern: ev.host, rule: 'allow' }); savePolicy(); }
    await fetch('/agent/approve', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: ev.id, decision }) }).catch(() => {});
  };
  for (const [decision, label, done, cls] of [['once', 'Allow for this task', 'Allowed', 'primary'], ['site', 'Always allow this site', 'Always allowed', ''], ['deny', 'Deny', 'Denied', '']]) {
    const b = document.createElement('button'); b.type = 'button'; b.textContent = label; if (cls) b.className = cls;
    b.onclick = () => decide(decision, done); row.append(b);
  }
  div.append(q, u, row);
  els.messages.append(div);
  els.messages.scrollTop = els.messages.scrollHeight;
}

// Friendly progress lines for the chat. Raw tool calls and tool errors go to the hidden "Details" log.
function friendly(text) {
  const name = text.split('(')[0];
  const first = text.slice(name.length + 1).split(',')[0].trim().replace(/\)$/, '');
  if (name === 'browser_navigate') {
    let host = first; try { host = new URL(first.replace(/…$/, '')).hostname.replace(/^www\./, ''); } catch {}
    return `Opening ${host || 'a page'}`;
  }
  if (/^browser_(snapshot|find|evaluate|wait_for|console_messages|network_requests?)$/.test(name)) return 'Reading the page';
  if (/^browser_(click|hover|drag|mouse_.*)$/.test(name)) return 'Clicking';
  if (/^browser_(type|fill_form|press_key|select_option)$/.test(name)) return 'Filling in the page';
  if (name === 'browser_tabs') return 'Switching tabs';
  if (name === 'browser_navigate_back') return 'Going back';
  if (name === 'look') return 'Looking at the page';
  if (name === 'task') return 'Working in the browser';
  if (name === 'write_todos') return 'Planning the steps';
  if (/^(write_file|read_file|edit_file|ls|glob|grep)$/.test(name)) return 'Taking notes';
  return 'Working…';
}

let lastStep = '';
function onEvent(ev) {
  if (ev.type === 'action') {
    addMsg('action tech', `${ev.agent === 'browser' ? 'browser › ' : ''}${ev.text}`);
    const step = friendly(ev.text);
    if (step !== lastStep) { lastStep = step; addMsg('step', step); }
  } else if (ev.type === 'assistant') { lastStep = ''; addMsg('assistant', ev.text); }
  else if (ev.type === 'approval') addApproval(ev);
  else if (ev.type === 'thinking') addMsg('thinking tech', ev.text);
  else if (ev.type === 'error') {
    console.debug('agent error:', ev.text);
    if (/^(browser_\w+|look): /.test(ev.text)) addMsg('error tech', ev.text); // a tool hiccup; the agent retries
    else addMsg('error', ev.text.split('\n')[0].slice(0, 200));
  } else if (ev.type === 'stopped') addMsg('system', 'Stopped.');
}

async function runAgent(text) {
  setStatus('running');
  lastStep = '';
  abortRun = new AbortController();
  try {
    const res = await fetch('/agent/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, thread_id: threadId, policy }), signal: abortRun.signal,
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `Agent service error ${res.status}`);
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        if (chunk.startsWith('data: ')) { try { onEvent(JSON.parse(chunk.slice(6))); } catch {} }
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') addMsg('error', e.message);
  } finally {
    abortRun = null;
    saveCookies();
    document.querySelectorAll('.msg.approval .btns button').forEach((b) => { b.disabled = true; });
    setStatus('idle');
  }
}

els.takeover.onclick = async () => {
  if (state === 'paused') { saveCookies(); await api('resume'); setStatus('running'); addMsg('system', 'Control handed back to the agent.'); }
  else { await api('pause'); setStatus('paused'); addMsg('system', 'You have control. The agent pauses before its next action.'); }
};
els.stop.onclick = async () => { await api('stop'); abortRun?.abort(); };
$('newChatBtn').onclick = async () => {
  if (abortRun) { await api('stop'); abortRun.abort(); }
  threadId = crypto.randomUUID();
  els.messages.innerHTML = '';
  addMsg('system', 'New conversation started.');
};

// ---------- settings dialog ----------
const MODE_HELP = {
  ask: 'The agent asks before it opens any website.',
  auto: 'Normal https sites open automatically. Unusual ones (plain http, IP addresses, internal hosts) ask first.',
  allow: 'The agent opens websites without asking. Internal hosts still ask.',
};
const RULE_LABEL = { allow: 'Always allow', ask: 'Always ask', block: 'Block' };
function renderSettings() {
  $('approvalMode').value = policy.mode;
  $('modeHelp').textContent = MODE_HELP[policy.mode];
  const list = $('siteList');
  list.textContent = '';
  if (!policy.sites.length) { list.innerHTML = '<div class="empty">No site rules. The default above applies to every site.</div>'; return; }
  policy.sites.forEach((r, i) => {
    const row = document.createElement('div'); row.className = 'row';
    const label = document.createElement('span'); label.className = 'grow'; label.textContent = r.pattern;
    const sel = document.createElement('select');
    for (const k of Object.keys(RULE_LABEL)) { const o = document.createElement('option'); o.value = k; o.textContent = RULE_LABEL[k]; sel.append(o); }
    sel.value = r.rule; sel.onchange = () => { r.rule = sel.value; savePolicy(); };
    const del = document.createElement('button'); del.type = 'button'; del.textContent = 'Remove';
    del.onclick = () => { policy.sites.splice(i, 1); savePolicy(); renderSettings(); };
    row.append(label, sel, del); list.append(row);
  });
}
$('settingsBtn').onclick = () => { renderSettings(); $('addSite').hidden = true; $('settings').showModal(); };
$('approvalMode').onchange = (e) => { policy.mode = e.target.value; savePolicy(); renderSettings(); };
$('addSiteBtn').onclick = () => { $('addSite').hidden = false; $('siteInput').value = ''; $('siteInput').focus(); };
$('siteCancel').onclick = () => { $('addSite').hidden = true; };
const addSite = () => {
  const pattern = $('siteInput').value.trim().replace(/^https?:\/\//i, '').replace(/\/.*$/, '').toLowerCase();
  if (!pattern) return;
  const rule = $('siteRule').value;
  const existing = policy.sites.find((r) => r.pattern === pattern);
  if (existing) existing.rule = rule; else policy.sites.push({ pattern, rule });
  savePolicy(); $('addSite').hidden = true; renderSettings();
};
$('siteSave').onclick = addSite;
$('siteInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); addSite(); } });

// ---------- cookies (like ChatGPT Work: see which sites are saved, clear any time) ----------
const cookieDlg = $('cookies');
// Clearing goes through the server so the saved copy is cleared too (otherwise a restore would bring them back).
async function clearCookies(domain) {
  const res = await fetch('/agent/cookies/clear', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(domain ? { domain } : {}) }).catch(() => null);
  if (!res || !res.ok) await browser.clearCookies(domain);   // fall back to the browser only
}
const saveCookies = () => fetch('/agent/cookies/save', { method: 'POST' }).catch(() => {});
const ago = (t) => { const d = Math.max(0, Math.round(Date.now() / 1000 - t)); return d < 60 ? `${d}s ago` : d < 3600 ? `${Math.round(d / 60)} min ago` : `${Math.round(d / 3600)} h ago`; };
async function cookieStatus() {
  const el = $('cookieStatus');
  try {
    const st = await (await fetch('/agent/cookies/status')).json();
    el.textContent = st.saved_at ? `Saved to disk: ${st.count} cookies. Up to date as of ${ago(st.checked_at || st.saved_at)}.` : 'Nothing saved yet.';
    if (st.error) el.textContent += ` (${st.error})`;
  } catch { el.textContent = 'Cookie saving is unavailable (agent service not reachable).'; }
}
async function renderCookies() {
  cookieStatus();
  const list = $('cookieList');
  list.textContent = '';
  let cookies = [];
  try { cookies = await browser.cookies(); } catch (e) { list.innerHTML = '<div class="empty">Browser not reachable</div>'; return; }
  const byDomain = new Map();
  for (const c of cookies) byDomain.set(c.domain, (byDomain.get(c.domain) || 0) + 1);
  if (!byDomain.size) { list.innerHTML = '<div class="empty">No cookies saved</div>'; return; }
  for (const [domain, n] of [...byDomain].sort()) {
    const row = document.createElement('div'); row.className = 'row';
    const label = document.createElement('span'); label.textContent = `${domain} (${n})`;
    const btn = document.createElement('button'); btn.type = 'button'; btn.textContent = 'Remove';
    btn.onclick = async () => { await clearCookies(domain); renderCookies(); };
    row.append(label, btn); list.append(row);
  }
}
$('detailsBtn').onclick = () => {
  const on = document.body.classList.toggle('show-tech');
  $('detailsBtn').textContent = on ? 'Hide details' : 'Details';
  els.messages.scrollTop = els.messages.scrollHeight;
};
$('cookiesBtn').onclick = () => { cookieDlg.showModal(); renderCookies(); };
$('clearCookies').onclick = async () => { await clearCookies(); renderCookies(); addMsg('system', 'All saved cookies cleared.'); };

// ---------- live view: CDP screencast drawn on a canvas; input is forwarded only while paused ----------
const ctx = els.view.getContext('2d');
let drawing = false, pending = null;
function drawFrame(f) {
  pending = f;
  if (drawing) return;
  drawing = true;
  (async () => {
    while (pending) {
      const { data, width, height } = pending; pending = null;
      try {
        const bmp = await createImageBitmap(await (await fetch('data:image/jpeg;base64,' + data)).blob());
        if (els.view.width !== width || els.view.height !== height) { els.view.width = width; els.view.height = height; }
        ctx.drawImage(bmp, 0, 0, width, height);
        bmp.close();
      } catch {}
    }
    drawing = false;
  })();
}
const startView = () => browser.setLiveView(drawFrame).catch(() => {});
$('reloadViewBtn').onclick = startView;

// You can drive the browser whenever the agent isn't acting (idle or paused).
const canControl = () => state === 'paused' || state === 'idle';
const toPage = (e) => {
  const r = els.view.getBoundingClientRect();
  const scale = Math.min(r.width / els.view.width, r.height / els.view.height) || 1;
  const x = (e.clientX - r.left - (r.width - els.view.width * scale) / 2) / scale;
  const y = (e.clientY - r.top - (r.height - els.view.height * scale) / 2) / scale;
  return { x: Math.round(x), y: Math.round(y) };
};
const mods = (e) => (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0);
const BTN = ['left', 'middle', 'right'];
let held = 0;
const send = (p) => p.catch(() => {});
els.view.addEventListener('mousedown', (e) => {
  if (!canControl()) return; e.preventDefault(); els.view.focus();
  const { x, y } = toPage(e); held = 1 << e.button;
  send(browser.mouseEvent('mousePressed', x, y, { button: BTN[e.button] || 'left', clickCount: e.detail || 1, modifiers: mods(e) }));
});
els.view.addEventListener('mouseup', (e) => {
  if (!canControl()) return; e.preventDefault();
  const { x, y } = toPage(e); held = 0;
  send(browser.mouseEvent('mouseReleased', x, y, { button: BTN[e.button] || 'left', clickCount: e.detail || 1, modifiers: mods(e) }));
});
els.view.addEventListener('mousemove', (e) => {
  if (!canControl()) return;
  const { x, y } = toPage(e);
  send(browser.mouseEvent('mouseMoved', x, y, { buttons: held, modifiers: mods(e), ...(held ? { button: 'left' } : {}) }));
});
els.view.addEventListener('wheel', (e) => {
  if (!canControl()) return; e.preventDefault();
  const { x, y } = toPage(e);
  send(browser.mouseEvent('mouseWheel', x, y, { deltaX: e.deltaX, deltaY: e.deltaY, modifiers: mods(e) }));
}, { passive: false });
els.view.addEventListener('contextmenu', (e) => { if (canControl()) e.preventDefault(); });
for (const [dom, type] of [['keydown', 'keyDown'], ['keyup', 'keyUp']]) {
  els.view.addEventListener(dom, (e) => {
    if (!canControl()) return;
    // Let Cmd/Ctrl+V reach the paste handler below instead of being sent as a raw key.
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'v') return;
    e.preventDefault();
    send(browser.rawKey(type, { key: e.key, code: e.code, keyCode: e.keyCode, modifiers: mods(e) }));
  });
}
els.view.addEventListener('paste', (e) => {
  if (!canControl()) return; e.preventDefault();
  const t = e.clipboardData?.getData('text'); if (t) send(browser.insertText(t));
});

// ---------- send ----------
els.composer.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = els.input.value.trim();
  if (!text || abortRun) return;
  els.input.value = ''; els.input.style.height = 'auto';
  addMsg('user', text);
  await runAgent(text);
});
els.input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); els.composer.requestSubmit(); }
});

// ---------- tab strip + address bar (you can switch, open and close tabs yourself, like in ChatGPT's browser) ----------
const tabstrip = $('tabstrip'), urlInput = $('urlInput');
let tabsKey = '';
// Chrome's tab list re-orders itself (newest or most recently used first), which made the clicked tab jump
// to the first slot. Keep our own stable order: tabs stay where they were first seen.
let tabOrder = [];
function renderTabs(tabs) {
  tabOrder = tabOrder.filter((id) => tabs.some((t) => t.id === id));
  for (const t of tabs) if (!tabOrder.includes(t.id)) tabOrder.push(t.id);
  tabs = [...tabs].sort((a, b) => tabOrder.indexOf(a.id) - tabOrder.indexOf(b.id));
  const active = tabs.find((t) => t.id === browser.targetId) || tabs[0];
  const key = JSON.stringify([tabs.map((t) => [t.id, t.title]), active?.id, canControl()]);
  if (active && document.activeElement !== urlInput) urlInput.value = active.url === 'about:blank' ? '' : active.url;
  if (key === tabsKey) return;
  tabsKey = key;
  tabstrip.classList.toggle('locked', !canControl());
  tabstrip.textContent = '';
  for (const t of tabs) {
    const el = document.createElement('div');
    const isActive = t.id === active?.id;
    el.className = 'tab' + (isActive ? ' active' : '');
    el.setAttribute('role', 'tab'); el.setAttribute('aria-selected', String(isActive));
    el.title = t.url;
    const blank = !t.url || t.url === 'about:blank';
    const label = document.createElement('span'); label.className = 't'; label.textContent = blank ? 'New tab' : (t.title || t.url);
    const x = document.createElement('span'); x.className = 'x'; x.textContent = '×'; x.title = 'Close tab';
    el.append(label, x);
    el.onclick = async (e) => {
      if (!canControl()) return;
      try { if (e.target === x) await browser.closeTab(t.id); else await browser.switchTab(t.id); } catch (err) { addMsg('error', err.message); }
      poll();
    };
    tabstrip.append(el);
  }
  const add = document.createElement('div'); add.className = 'tab add'; add.textContent = '+'; add.title = 'New tab';
  add.onclick = async () => { if (canControl()) { try { await browser.newTab('about:blank'); } catch {} poll(); } };
  tabstrip.append(add);
}
const nav = (fn) => async () => { if (!canControl()) return; try { await fn(); } catch (err) { addMsg('error', err.message); } poll(); };
els.back.onclick = nav(() => browser.goBack());
els.fwd.onclick = nav(() => browser.goForward());
els.reload.onclick = nav(() => browser.reload());
els.input.addEventListener('input', () => { els.input.style.height = 'auto'; els.input.style.height = Math.min(els.input.scrollHeight, 160) + 'px'; });
$('urlbar').addEventListener('submit', async (e) => {
  e.preventDefault();
  const q = urlInput.value.trim();
  if (!q || !canControl()) return;
  const isUrl = /^[a-z]+:\/\//i.test(q) || /^[\w-]+(\.[\w-]+)+(\/.*)?$/.test(q);
  urlInput.blur();
  try { await browser.navigate(isUrl ? q : 'https://www.google.com/search?q=' + encodeURIComponent(q)); } catch (err) { addMsg('error', err.message); }
  poll();
});

// ---------- browser health + current tab label ----------
async function poll() {
  try {
    let tabs = await browser.listTabs();
    // The tab we were attached to is gone (closed by you or by the page): follow another one.
    if (tabs.length && !tabs.some((t) => t.id === browser.targetId)) { await browser.switchTab(tabs[0].id); tabs = await browser.listTabs(); }
    renderTabs(tabs);
    await browser.followNewTabs();
    if (state === 'error') { setStatus('idle'); startView(); }
  } catch {
    if (!abortRun) setStatus('error');
  }
}

// ---------- session lifecycle (only when served as a managed session: http://s-<id>.localhost:<port>) ----------
// The manager pauses the browser when you go idle and saves + releases it after you leave, so we tell it we are here.
const managed = location.hostname.match(/^s-([0-9a-f]+)\.localhost$/);
if (managed) {
  const mgr = `http://localhost:${location.port}/api/sessions/${managed[1]}`;
  const ended = () => {
    if (document.getElementById('sessionEnded')) return;
    const d = document.createElement('div'); d.id = 'sessionEnded';
    d.style.cssText = 'position:fixed;inset:0;display:grid;place-items:center;background:rgba(0,0,0,.6);z-index:50;color:#fff;text-align:center;padding:24px';
    d.innerHTML = '<div><h2 style="margin:0 0 8px">Session ended</h2><p style="margin:0 0 14px">It was closed after inactivity. Your logins and tabs are saved.</p><a style="color:#fff;font-weight:600" href="http://localhost:' + location.port + '/">Open it again</a></div>';
    document.body.append(d);
  };
  const beat = async () => { try { const r = await fetch(mgr + '/heartbeat', { method: 'POST' }); if (r.status === 404) ended(); } catch {} };
  beat();
  setInterval(() => { if (!document.hidden) beat(); }, 15000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) beat(); });
  window.addEventListener('pagehide', () => navigator.sendBeacon(mgr + '/bye'));
}

setStatus('idle');
startView();
poll();
setInterval(poll, 1500);
fetch('/agent/health').then((r) => r.json()).then((h) => {
  if (!h.openai_key_set) addMsg('system', 'Set OPENAI_API_KEY in .env and restart the deepagent service.');
  else if (!h.browser_connected) addMsg('error', `Agent can't reach the browser: ${h.error}`);
}).catch(() => addMsg('error', 'Agent service is not reachable at /agent/. Is the deepagent container running?'));
