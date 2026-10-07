import { md } from './md.js';

const $ = (id) => document.getElementById(id);
const els = {
  messages: $('messages'), input: $('input'), composer: $('composer'), send: $('sendBtn'), stop: $('stopBtn'),
  dot: $('statusDot'), statusText: $('statusText'), viewer: $('viewer'), live: $('live'), empty: $('empty'),
  overlay: $('overlay'), ended: $('ended'), banner: $('privateBanner'), badge: $('liveBadge'),
  user: $('user'), open: $('openBtn'), close: $('closeBtn'), takeover: $('takeoverBtn'), priv: $('privateBtn'), forget: $('forgetBtn'),
};

let cfg = { agent_url: '' };
let session = null;            // { user, viewer_url, ... } while a browser is open
let state = 'none';            // none | ready | running | paused
let privateOn = false;
let abortRun = null, heartbeat = null, viewerRetries = 0;
let threadId = crypto.randomUUID();

const cleanUser = (v) => (v || '').trim().toLowerCase().replace(/[^a-z0-9_-]/g, '').slice(0, 32);
try { els.user.value = localStorage.getItem('v2user') || els.user.value; } catch {}

// ---------------------------------------------------------------- talking to the two services
const agent = (path) => `${cfg.agent_url || `${location.protocol}//${location.hostname}:8200`}${path}`;
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || body.error || `Request failed (${res.status})`);
  return body;
}
const post = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }).catch(() => null);

// ---------------------------------------------------------------- chat log
function addMsg(kind, text) {
  const div = document.createElement('div');
  div.className = `msg ${kind}`;
  if (kind === 'assistant') div.innerHTML = md(text); else div.textContent = text;
  els.messages.appendChild(div);
  els.messages.scrollTop = els.messages.scrollHeight;
  return div;
}
function friendly(text) {                    // plain progress lines; raw tool calls go behind "Details"
  const name = text.split('(')[0];
  const first = text.slice(name.length + 1).split(',')[0].trim().replace(/\)$/, '');
  if (name === 'browser_navigate') { let host = first; try { host = new URL(first.replace(/…$/, '')).hostname.replace(/^www\./, ''); } catch {} return `Opening ${host || 'a page'}`; }
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
  else if (ev.type === 'thinking') addMsg('thinking tech', ev.text);
  else if (ev.type === 'error') {
    if (/^(browser_\w+|look): /.test(ev.text)) addMsg('error tech', ev.text); else addMsg('error', ev.text.split('\n')[0].slice(0, 220));
  } else if (ev.type === 'agent_tab') followAgentTab(ev);
  else if (ev.type === 'stopped') addMsg('system', 'Stopped.');
}
$('detailsBtn').onclick = () => {
  const on = document.body.classList.toggle('show-tech');
  $('detailsBtn').textContent = on ? 'Hide details' : 'Details';
};
$('newChatBtn').onclick = async () => {
  if (abortRun) { await post(agent('/stop'), { user: session.user }); abortRun.abort(); }
  threadId = crypto.randomUUID(); els.messages.innerHTML = ''; addMsg('system', 'New conversation started.');
};

// ---------------------------------------------------------------- state -> screen
const LABELS = { none: 'No browser open', ready: 'Browser ready', running: 'Agent is working…', paused: 'You are in control (agent paused)' };
let mode = 'none';
function setMode(m) {
  mode = m;
  document.body.classList.remove('mode-none', 'mode-card', 'mode-open', 'mode-wide');
  document.body.classList.add(`mode-${m}`);
  $('expandBtn').title = m === 'wide' ? 'Back to chat + browser' : 'Expand';
  sizeCard();
}
function sizeCard() {                              // the card shows the real 1280x800 page, scaled down to fit
  const w = els.live.parentElement.clientWidth;
  if (w) document.documentElement.style.setProperty('--card-scale', String(w / 1280));
}
new ResizeObserver(sizeCard).observe($('viewer'));
$('cardHit').onclick = () => setMode('open');
$('collapseBtn').onclick = () => setMode('card');
$('expandBtn').onclick = () => setMode(mode === 'wide' ? 'open' : 'wide');

function setState(s) {
  state = s;
  if (s === 'none') setMode('none'); else if (mode === 'none') setMode('card');
  const open = s !== 'none', busy = s === 'running';
  els.dot.className = `dot ${s === 'none' ? '' : s === 'ready' ? 'idle' : s}`;
  els.statusText.textContent = LABELS[s];
  els.open.hidden = open; els.user.disabled = open; els.close.hidden = !open;
  els.takeover.hidden = !(busy || s === 'paused');
  els.takeover.classList.toggle('active', s === 'paused'); els.takeover.textContent = s === 'paused' ? 'Hand back to agent' : 'Take over';
  els.priv.hidden = !open || busy;
  els.send.hidden = busy; els.stop.hidden = !busy;
  els.overlay.hidden = !busy;
  els.viewer.classList.toggle('is-mine', s === 'ready' || s === 'paused');
  els.viewer.classList.toggle('is-busy', busy);
  els.empty.hidden = open;
}

// ---------------------------------------------------------------- browser session
async function openBrowser() {
  const user = cleanUser(els.user.value);
  if (!user) { addMsg('error', 'Enter a name for this browser (letters, digits, - or _).'); return false; }
  els.open.disabled = true; els.statusText.textContent = 'Starting browser…';
  try {
    const r = await api('/api/sessions', { method: 'POST', body: JSON.stringify({ user }) });
    session = { user, ...r };
    try { localStorage.setItem('v2user', user); } catch {}
    els.ended.hidden = true; els.live.src = r.viewer_url; viewerRetries = 0;
    clearInterval(heartbeat); heartbeat = setInterval(beat, 15000);
    setState('ready');
    addMsg('system tech', `Browser ${r.reused ? 'reused' : 'started'}${r.restored ? ' with saved logins' : ''}.`);
    if (r.restored) addMsg('system', 'Browser opened with your saved logins.');
    if (r.profile_warning) addMsg('error', r.profile_warning);
    return true;
  } catch (e) {
    addMsg('error', e.message); setState('none'); return false;
  } finally { els.open.disabled = false; }
}
async function beat() {
  if (!session) return;
  const r = await fetch(`/api/sessions/${session.user}/heartbeat`, { method: 'POST' }).catch(() => null);
  if (r && r.status === 404) endedByServer();
}
function endedByServer() {
  clearInterval(heartbeat); session = null; privateOn = false; els.banner.hidden = true; els.live.src = 'about:blank';
  els.badge.classList.remove('on'); setState('none'); els.ended.hidden = false;
}
async function closeBrowser() {
  if (!session) return;
  const user = session.user;
  if (abortRun) { await post(agent('/stop'), { user }); abortRun.abort(); }
  els.close.disabled = true; els.statusText.textContent = 'Saving logins and closing…';
  try { await api(`/api/sessions/${user}/release`, { method: 'POST' }); } catch (e) { addMsg('error', e.message); }
  await post(agent('/disconnect'), { user });
  clearInterval(heartbeat); session = null; privateOn = false; els.banner.hidden = true; els.priv.classList.remove('on');
  els.live.src = 'about:blank'; els.badge.classList.remove('on'); setState('none'); els.close.disabled = false;
  addMsg('system', 'Browser closed. Logins saved.');
}
els.open.onclick = openBrowser;
$('reopenBtn').onclick = () => { els.ended.hidden = true; openBrowser(); };
els.close.onclick = closeBrowser;
els.forget.onclick = async () => {
  const user = cleanUser(els.user.value);
  if (!user || !confirm(`Delete saved logins for "${user}"? They will have to sign in again.`)) return;
  if (session && session.user === user) await closeBrowser();
  try { await api(`/api/users/${user}/logins`, { method: 'DELETE' }); addMsg('system', `Saved logins for "${user}" deleted.`); } catch (e) { addMsg('error', e.message); }
};

// ---------------------------------------------------------------- live view hand-off (iframe <-> this page)
window.addEventListener('message', async (e) => {
  const d = e.data; if (!d || d.source !== 'live-view' || e.source !== els.live.contentWindow) return;
  if (d.type === 'connected') { els.badge.classList.add('on'); viewerRetries = 0; return; }
  els.badge.classList.remove('on');
  if (session && viewerRetries++ < 3) {                         // signed live-view links are short-lived: ask for a fresh one
    await new Promise((r) => setTimeout(r, 1500));
    try { const r = await api(`/api/sessions/${session.user}/live-view`); els.live.src = r.viewer_url; } catch { /* session may be gone: the heartbeat will say so */ }
  }
});

// ---------------------------------------------------------------- take over / private input
async function setPrivate(on) {
  if (!session) return;
  await api(`/api/sessions/${session.user}/secret-entry`, { method: 'POST', body: JSON.stringify({ on }) });
  privateOn = on; els.priv.classList.toggle('on', on); els.banner.hidden = !on;
}
els.priv.onclick = async () => { try { await setPrivate(!privateOn); } catch (e) { addMsg('error', e.message); } };
els.takeover.onclick = async () => {
  if (!session) return;
  if (state === 'paused') { await post(agent('/resume'), { user: session.user }); setState('running'); addMsg('system', 'Control handed back to the agent.'); }
  else { await post(agent('/pause'), { user: session.user }); setState('paused'); addMsg('system', 'You have control. The agent pauses before its next action.'); }
};
els.stop.onclick = async () => { if (session) await post(agent('/stop'), { user: session.user }); abortRun?.abort(); };

// ---------------------------------------------------------------- running a task
async function runAgent(text) {
  if (!session && !(await openBrowser())) return;
  if (privateOn) await setPrivate(false).catch(() => {});          // the agent needs to see the page again
  setState('running'); lastStep = ''; abortRun = new AbortController();
  try {
    const res = await fetch(agent('/chat'), { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user: session.user, message: text, thread_id: threadId }), signal: abortRun.signal });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `Agent service error ${res.status}`);
    const reader = res.body.getReader(), dec = new TextDecoder(); let buf = '';
    for (;;) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true }); let i;
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        if (chunk.startsWith('data: ')) { try { onEvent(JSON.parse(chunk.slice(6))); } catch {} }
      }
    }
  } catch (e) {
    if (e.name === 'AbortError') { /* stopped by the user */ }
    else if (e instanceof TypeError) addMsg('error', 'Lost the connection to the agent, so the task was stopped. You can send it again.');
    else addMsg('error', e.message);
  } finally {
    abortRun = null;
    if (session) { setState('ready'); await post(agent('/resume'), { user: session.user }); }   // never leave the agent paused with no task
  }
}
els.composer.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = els.input.value.trim();
  if (!text || abortRun) return;
  els.input.value = ''; els.input.style.height = 'auto';
  addMsg('user', text); await runAgent(text);
});
els.input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); els.composer.requestSubmit(); } });
els.input.addEventListener('input', () => { els.input.style.height = 'auto'; els.input.style.height = Math.min(els.input.scrollHeight, 160) + 'px'; });

// ---------------------------------------------------------------- start
setState('none');
els.live.src = 'about:blank';
api('/api/config').then((c) => { cfg = c; }).catch(() => addMsg('error', 'The control plane is not reachable.'));
fetch(agent('/health')).then((r) => r.json()).then((h) => { if (!h.key_set) addMsg('system', 'The agent service has no OPENAI_API_KEY, so chat will not work.'); }).catch(() => {});

// ---------------------------------------------------------------- tab strip (for backends whose live view shows one page)
const tabsUi = { known: new Set(), active: null, sig: '', sid: null, busy: false, list: [], follow: null };
const norm = (u) => (u || '').replace(/#.*$/, '').replace(/\/$/, '');
const findTab = (ev) => tabsUi.list.find((t) => norm(t.url) === norm(ev.url)) || tabsUi.list.find((t) => ev.title && t.title === ev.title);
function followAgentTab(ev) {                      // the tab the agent is working in is always the one on screen
  const t = findTab(ev);
  if (t) selectTab(t); else tabsUi.follow = ev;     // not listed yet: the next poll applies it
}
function selectTab(t) {
  if (tabsUi.active === t.id) return;
  tabsUi.active = t.id; tabsUi.sig = ''; els.live.src = t.view_url; renderTabs($('tabstrip'), tabsUi.list);
}
async function pollTabs() {
  const row = $('tabrow'), strip = $('tabstrip');
  if (!session) { row.hidden = true; tabsUi.known.clear(); tabsUi.active = null; tabsUi.sig = ''; tabsUi.sid = null; return; }
  if (tabsUi.busy) return;
  tabsUi.busy = true;
  try {
    const r = await api(`/api/sessions/${session.user}/tabs`);
    if (!r.supported) { row.hidden = true; return; }
    if (tabsUi.sid !== session.session_id) { tabsUi.sid = session.session_id; tabsUi.known.clear(); tabsUi.active = null; }
    const tabs = r.tabs, ids = tabs.map((t) => t.id);
    tabsUi.list = tabs;
    const fresh = tabs.filter((t) => !tabsUi.known.has(t.id));
    let want = tabsUi.active;
    if (tabsUi.known.size && fresh.length) want = fresh[fresh.length - 1].id;      // a page opened (agent or link): follow it
    if (tabsUi.follow) { const f = findTab(tabsUi.follow); if (f) { want = f.id; tabsUi.follow = null; } }
    if (!ids.includes(want)) want = ids[0] || null;
    tabs.forEach((t) => tabsUi.known.add(t.id));
    const changed = want !== tabsUi.active;
    tabsUi.active = want;
    const sig = JSON.stringify([want, tabs.map((t) => [t.id, t.title])]);
    if (sig !== tabsUi.sig) { tabsUi.sig = sig; renderTabs(strip, tabs); }
    row.hidden = false;
    if (changed && want) els.live.src = tabs.find((t) => t.id === want).view_url;
  } catch { /* transient: next poll retries */ } finally { tabsUi.busy = false; }
}
function renderTabs(strip, tabs) {
  strip.textContent = '';
  for (const t of tabs) {
    const d = document.createElement('div'); d.className = 'tab' + (t.id === tabsUi.active ? ' active' : ''); d.title = t.url;
    const title = document.createElement('span'); title.className = 't'; title.textContent = t.title || 'New tab';
    const x = document.createElement('span'); x.className = 'x'; x.textContent = '×'; x.title = 'Close tab';
    d.append(title, x);
    d.onclick = (e) => {
      if (e.target === x) { api(`/api/sessions/${session.user}/tabs/${encodeURIComponent(t.id)}`, { method: 'DELETE' }).then(pollTabs).catch((er) => addMsg('error', er.message)); return; }
      selectTab(t);
    };
    strip.append(d);
  }
  const add = document.createElement('div'); add.className = 'tab add'; add.textContent = '+'; add.title = 'New tab';
  add.onclick = () => api(`/api/sessions/${session.user}/tabs`, { method: 'POST', body: JSON.stringify({ url: 'about:blank' }) }).then(pollTabs).catch((er) => addMsg('error', er.message));
  strip.append(add);
}
// poll fast only while the agent is working; slowly when idle; not at all while this page is hidden
(function loop() {
  if (!document.hidden) pollTabs();
  setTimeout(loop, abortRun ? 2000 : 8000);
})();
document.addEventListener('visibilitychange', () => { if (!document.hidden) pollTabs(); });
