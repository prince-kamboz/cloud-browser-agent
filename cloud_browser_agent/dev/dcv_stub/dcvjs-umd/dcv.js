// DEV STAND-IN for the Amazon DCV Web Client SDK. NOT the real thing: it follows the documented API shape
// (authenticate -> connect -> callbacks.firstFrame) so our viewer page can be exercised without AWS. It draws a test card.
window.dcv = {
  LogLevel: { INFO: 'INFO' }, setLogLevel() {},
  authenticate(url, cb) { setTimeout(() => cb.success(null, [{ sessionId: 'stub-session', authToken: 'stub-token' }]), 60); },
  connect(cfg) {
    const el = document.getElementById(cfg.divId);
    el.innerHTML = '<div style="width:100%;height:100%;display:grid;place-items:center;background:#1f2d4a;color:#fff;font:28px system-ui">STUB DCV STREAM<br><small style="font-size:16px">' + cfg.sessionId + '</small></div>';
    setTimeout(() => cfg.callbacks.firstFrame(), 80);
    return Promise.resolve({ requestDisplayLayout() {}, disconnect() { cfg.callbacks.disconnect({ message: 'client disconnect' }); } });
  },
};
