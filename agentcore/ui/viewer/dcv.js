// Drives the Amazon DCV Web Client SDK (fetched from AWS's npm package at build time; not part of this repo).
// Mirrors what AWS's own BrowserLiveView React component does: authenticate with the presigned URL, connect, then
// scale the remote viewport to fit. Contract with the parent page: postMessage {source:'live-view', type: connected|disconnected|error}.
(() => {
  const params = new URLSearchParams(location.hash.slice(1));
  const signedUrl = params.get('url');
  const W = Number(params.get('w')) || 1280, H = Number(params.get('h')) || 800;     // must match the session's viewport
  const msg = document.getElementById('msg'), box = document.getElementById('box'), display = document.getElementById('display');
  const say = (type, detail) => parent.postMessage({ source: 'live-view', type, detail }, '*');
  const fail = (text) => { msg.style.display = 'grid'; msg.textContent = text; say('error', text); };

  if (window.__noDcv || !window.dcv) return fail('The DCV client is not installed on this server (it is fetched from AWS\'s npm package when the image is built).');
  if (!signedUrl) return fail('No live view address was given.');

  const search = () => new URL(signedUrl).searchParams;                               // the signature travels as query parameters
  let conn = null;

  function fit() {                                                                    // same maths as AWS's calculateScale
    const w = box.clientWidth, h = box.clientHeight;
    if (w <= 0 || h <= 0) return;
    const scale = Math.min(w / W, h / H), offsetX = Math.max(0, (w - W * scale) / 2);
    Object.assign(display.style, { width: W + 'px', height: H + 'px', transform: `scale(${scale})`, left: offsetX + 'px' });
  }
  new ResizeObserver(fit).observe(box); fit();

  function connect(sessionId, authToken) {
    window.dcv.connect({
      url: signedUrl, sessionId, authToken, divId: 'display', baseUrl: '/dcv-sdk/dcvjs-umd',
      clipboardAutoSync: true, observers: { httpExtraSearchParams: search },
      callbacks: {
        firstFrame: () => { msg.style.display = 'none'; say('connected'); },
        disconnect: (reason) => { msg.style.display = 'grid'; msg.textContent = 'Live view disconnected'; say('disconnected', reason && reason.message); },
      },
    }).then((c) => {
      conn = c;
      try { c.requestDisplayLayout([{ name: 'Main Display', rect: { x: 0, y: 0, width: W, height: H }, primary: true }]); } catch (e) { /* not ready yet */ }
      fit();
    }).catch((e) => fail('Could not connect to the live view: ' + (e && e.message || e)));
  }

  try {
    window.dcv.setLogLevel(window.dcv.LogLevel.INFO);
    window.dcv.authenticate(signedUrl, {
      promptCredentials: () => {},
      error: (_a, e) => fail('Live view sign-in failed: ' + (e && e.message || 'unknown error')),
      success: (_a, result) => { const r = (result && result[0]) || {}; connect(r.sessionId || '', r.authToken || ''); },
      httpExtraSearchParams: search,
    });
  } catch (e) { fail('Live view error: ' + e.message); }
  window.addEventListener('pagehide', () => { try { conn && conn.disconnect(); } catch (e) { /* closing anyway */ } });
})();
